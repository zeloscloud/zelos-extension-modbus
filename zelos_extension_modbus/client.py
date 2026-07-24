"""Core Modbus client wrapper with Zelos SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

import zelos_sdk
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from zelos_extension_modbus.blocks import ReadBlock, plan_blocks
from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MIN_POLL_INTERVAL,
    MODBUS_MAX_READ_COUNT,
    ByteOrder,
    RegisterType,
    Transport,
    WriteMode,
    sanitize_source_name,
)
from zelos_extension_modbus.register_map import Register, RegisterMap
from zelos_extension_modbus.serial_diag import diagnose_serial_port

logger = logging.getLogger(__name__)

# Run full serial diagnostics on the 1st connect failure, then every Nth
# consecutive failure (~30s at the 3s reconnect cadence). A success resets.
_DIAG_EVERY = 10


def _clamped(name: str, value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``, warning when it was out of range."""
    clamped = max(lo, min(hi, value))
    if clamped != value:
        logger.warning(f"{name} {value} out of range [{lo}, {hi}]; clamping to {clamped}")
    return clamped


@dataclass
class _PollItem:
    """A single register scheduled for polling at its own cadence."""

    event: str
    register: Register
    interval: float
    next_due: float = 0.0  # monotonic deadline; 0.0 = due immediately


# SDK data type mapping (module-level constant)
SDK_DATATYPE_MAP: dict[str, zelos_sdk.DataType] = {
    "bool": zelos_sdk.DataType.Boolean,
    "uint16": zelos_sdk.DataType.UInt16,
    "int16": zelos_sdk.DataType.Int16,
    "uint32": zelos_sdk.DataType.UInt32,
    "int32": zelos_sdk.DataType.Int32,
    "float32": zelos_sdk.DataType.Float32,
    "uint64": zelos_sdk.DataType.UInt64,
    "int64": zelos_sdk.DataType.Int64,
    "float64": zelos_sdk.DataType.Float64,
}


def _reorder_registers(registers: list[int], byte_order: str, for_decode: bool = True) -> list[int]:
    """Reorder registers based on byte order.

    Args:
        registers: List of 16-bit register values
        byte_order: One of 'big', 'little', 'big_swap', 'little_swap'
        for_decode: True if preparing for decode, False for encode

    Returns:
        Reordered register list
    """
    if len(registers) <= 1:
        return registers

    regs = list(registers)

    if byte_order == ByteOrder.BIG:
        # Standard Modbus: AB CD (no change)
        pass
    elif byte_order == ByteOrder.LITTLE:
        # Full little endian: DC BA (reverse all)
        regs = regs[::-1]
    elif byte_order == ByteOrder.BIG_SWAP:
        # Big endian with word swap: CD AB (swap pairs)
        if len(regs) == 2:
            regs = [regs[1], regs[0]]
        elif len(regs) == 4:
            regs = [regs[1], regs[0], regs[3], regs[2]]
    elif byte_order == ByteOrder.LITTLE_SWAP:
        # Little endian with word swap: BA DC
        if len(regs) == 2:
            regs = [regs[1], regs[0]]
        elif len(regs) == 4:
            regs = [regs[3], regs[2], regs[1], regs[0]]

    return regs


def decode_value(
    registers: list[int], datatype: str, scale: float = 1.0, byte_order: str = "big"
) -> float | int | bool:
    """Decode raw register values to typed value.

    Args:
        registers: List of 16-bit register values
        datatype: Data type string
        scale: Scale factor to apply
        byte_order: Byte order ('big', 'little', 'big_swap', 'little_swap')

    Returns:
        Decoded and scaled value
    """
    # Reorder registers based on byte order before decoding
    regs = _reorder_registers(registers, byte_order, for_decode=True)

    if datatype == "bool":
        return bool(regs[0])
    elif datatype == "uint16":
        return int(regs[0] * scale)
    elif datatype == "int16":
        raw = struct.pack(">H", regs[0])
        value = struct.unpack(">h", raw)[0]
        return int(value * scale)
    elif datatype == "uint32":
        raw = struct.pack(">HH", regs[0], regs[1])
        value = struct.unpack(">I", raw)[0]
        return int(value * scale)
    elif datatype == "int32":
        raw = struct.pack(">HH", regs[0], regs[1])
        value = struct.unpack(">i", raw)[0]
        return int(value * scale)
    elif datatype == "float32":
        raw = struct.pack(">HH", regs[0], regs[1])
        value = struct.unpack(">f", raw)[0]
        return float(value * scale)
    elif datatype == "uint64":
        raw = struct.pack(">HHHH", *regs[:4])
        value = struct.unpack(">Q", raw)[0]
        return int(value * scale)
    elif datatype == "int64":
        raw = struct.pack(">HHHH", *regs[:4])
        value = struct.unpack(">q", raw)[0]
        return int(value * scale)
    elif datatype == "float64":
        raw = struct.pack(">HHHH", *regs[:4])
        value = struct.unpack(">d", raw)[0]
        return float(value * scale)
    else:
        return regs[0]


def encode_value(
    value: float | int | bool, datatype: str, scale: float = 1.0, byte_order: str = "big"
) -> list[int]:
    """Encode typed value to raw register values.

    Args:
        value: Value to encode
        datatype: Data type string
        scale: Scale factor (value will be divided by scale)
        byte_order: Byte order ('big', 'little', 'big_swap', 'little_swap')

    Returns:
        List of 16-bit register values
    """
    scaled = value / scale if scale != 0 else value

    if datatype == "bool":
        regs = [1 if value else 0]
    elif datatype == "uint16":
        regs = [int(scaled) & 0xFFFF]
    elif datatype == "int16":
        raw = struct.pack(">h", int(scaled))
        regs = [struct.unpack(">H", raw)[0]]
    elif datatype == "uint32":
        raw = struct.pack(">I", int(scaled))
        regs = list(struct.unpack(">HH", raw))
    elif datatype == "int32":
        raw = struct.pack(">i", int(scaled))
        regs = list(struct.unpack(">HH", raw))
    elif datatype == "float32":
        raw = struct.pack(">f", float(scaled))
        regs = list(struct.unpack(">HH", raw))
    elif datatype == "uint64":
        raw = struct.pack(">Q", int(scaled))
        regs = list(struct.unpack(">HHHH", raw))
    elif datatype == "int64":
        raw = struct.pack(">q", int(scaled))
        regs = list(struct.unpack(">HHHH", raw))
    elif datatype == "float64":
        raw = struct.pack(">d", float(scaled))
        regs = list(struct.unpack(">HHHH", raw))
    else:
        regs = [int(value) & 0xFFFF]

    # Reorder registers based on byte order for writing
    return _reorder_registers(regs, byte_order, for_decode=False)


def decode_block(
    block: ReadBlock, raw: list[int] | list[bool]
) -> list[tuple[Register, float | int | bool]]:
    """Decode a block's raw response into (register, value) pairs.

    Each register is sliced out of ``raw`` at its offset from the block start and
    decoded like a standalone read. Bit types return the raw boolean; word types
    go through ``decode_value``. Registers whose slice is short (truncated
    response) are warned about and skipped rather than decoded from garbage.

    Args:
        block: The planned block that produced ``raw``.
        raw: The register/bit values returned for the block's address range.

    Returns:
        List of (register, decoded value) in ``block.registers`` order (skipped
        registers absent).
    """
    results: list[tuple[Register, float | int | bool]] = []
    is_bits = block.type in BIT_REGISTER_TYPES
    for reg in block.registers:
        offset = reg.address - block.address
        span = reg.address_span
        chunk = raw[offset : offset + span]
        if len(chunk) < span:
            logger.warning(
                f"Short block response for register '{reg.name}' at address "
                f"{reg.address}: expected {span}, got {len(chunk)}"
            )
            continue
        if is_bits:
            results.append((reg, bool(chunk[0])))
        else:
            results.append((reg, decode_value(chunk, reg.datatype, reg.scale, reg.byte_order)))
    return results


class ModbusClient:
    """Modbus client with polling and Zelos SDK integration."""

    def __init__(
        self,
        transport: str = Transport.TCP,
        host: str = "127.0.0.1",
        port: int = 502,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
        unit_id: int = 1,
        timeout: float = 3.0,
        register_map: RegisterMap | None = None,
        poll_interval: float = 1.0,
        write_mode: str = WriteMode.AUTO,
        block_reads: bool = True,
        max_block_size: int = 125,
        max_read_gap: int = 0,
        interface_name: str | None = None,
    ) -> None:
        """Initialize Modbus client.

        Args:
            transport: 'tcp' or 'rtu'
            host: TCP host address
            port: TCP port
            serial_port: Serial port for RTU
            baudrate: Serial baudrate for RTU
            parity: Serial parity ('N', 'E', 'O')
            stopbits: Number of stop bits (1 or 2)
            bytesize: Number of data bits (7 or 8)
            unit_id: Modbus slave/unit ID
            timeout: Request timeout in seconds
            register_map: Optional register map for named access
            poll_interval: Default polling interval in seconds (per-register
                poll_interval overrides this)
            write_mode: 'auto' (FC 6 for single, FC 16 for multi) or
                        'fc16' (always FC 16 for all writes)
            block_reads: Coalesce contiguous registers into range reads
            max_block_size: Maximum addresses per range read (clamped to 1-125)
            max_read_gap: Maximum uncovered addresses to bridge within a block
                (clamped to >= 0; 0 = strictly contiguous)
            interface_name: Optional name for this interface (used for trace source naming)
        """
        self.transport = transport
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.unit_id = unit_id
        self.timeout = timeout
        self.register_map = register_map
        self.write_mode = write_mode

        # The schema oneOf branch isn't validated by the SDK, so the constructor
        # is the backstop for out-of-range block-read and poll knobs.
        self.poll_interval = _clamped(
            "poll_interval", poll_interval, MIN_POLL_INTERVAL, float("inf")
        )
        self.block_reads = block_reads
        self.max_block_size = int(
            _clamped("max_block_size", max_block_size, 1, MODBUS_MAX_READ_COUNT)
        )
        self.max_read_gap = int(_clamped("max_read_gap", max_read_gap, 0, float("inf")))

        self.interface_name = interface_name

        self._client: AsyncModbusTcpClient | AsyncModbusSerialClient | None = None
        self._running = False
        self._connected = False
        self._poll_count = 0
        self._error_count = 0
        # Consecutive connect failures; drives throttled serial diagnostics.
        self._connect_failures = 0
        self._loop: asyncio.AbstractEventLoop | None = None

        # Per-register poll scheduler state. None means "not yet built"; an empty
        # list means "built, nothing to poll" (an all-disabled map) and must not
        # be rebuilt every cycle.
        self._poll_items: list[_PollItem] | None = None

        # Block plans keyed by the due-set's register identities; the map is
        # immutable after construction so a plan only depends on which registers
        # are due, never on their contents.
        self._plan_cache: dict[tuple[int, ...], list[ReadBlock]] = {}

        # Zelos SDK trace source
        self._source: zelos_sdk.TraceSourceCacheLast | None = None

    def _create_client(self) -> AsyncModbusTcpClient | AsyncModbusSerialClient:
        """Create the appropriate Modbus client."""
        if self.transport == Transport.TCP:
            return AsyncModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
            )
        else:  # rtu
            return AsyncModbusSerialClient(
                port=self.serial_port,
                baudrate=self.baudrate,
                parity=self.parity,
                stopbits=self.stopbits,
                bytesize=self.bytesize,
                timeout=self.timeout,
            )

    def _init_trace_source(self) -> None:
        """Initialize Zelos trace source and define schema from register map."""
        if self.interface_name:
            source_name = self.interface_name
        elif self.register_map:
            source_name = self.register_map.name
        else:
            source_name = "modbus"
        # Backstop: ensure the name is safe as a trace-source path segment
        # regardless of how the client was constructed (idempotent on names
        # already sanitized by the app runner).
        source_name = sanitize_source_name(source_name, "modbus")
        self._source = zelos_sdk.TraceSourceCacheLast(source_name)

        if not self.register_map or not self.register_map.events:
            # No register map - create a generic raw event
            self._source.add_event(
                "raw",
                [
                    zelos_sdk.TraceEventFieldMetadata("address", zelos_sdk.DataType.UInt16),
                    zelos_sdk.TraceEventFieldMetadata("value", zelos_sdk.DataType.Int32),
                ],
            )
            return

        # Create events from user-defined event names. polled_events already
        # drops disabled registers and all-disabled events (no dead leaves in
        # the signal tree). Field names are the register's precomputed
        # trace-safe name; from_dict guarantees they don't collide per event.
        for event_name, regs in self.register_map.polled_events.items():
            fields = [
                zelos_sdk.TraceEventFieldMetadata(
                    reg.field_name, self._get_sdk_datatype(reg.datatype), reg.unit
                )
                for reg in regs
            ]
            self._source.add_event(event_name, fields)

    def _get_sdk_datatype(self, datatype: str) -> zelos_sdk.DataType:
        """Map register datatype to Zelos SDK DataType."""
        return SDK_DATATYPE_MAP.get(datatype, zelos_sdk.DataType.Int32)

    async def connect(self) -> bool:
        """Connect to Modbus device.

        On failure (either a False ``connected`` flag or a raised exception),
        runs throttled serial diagnostics for RTU transports so a re-enumerated
        or permission-blocked adapter surfaces actionable findings instead of a
        silent retry loop. A successful connect resets the failure counter.

        Returns:
            True if connected successfully
        """
        exc: Exception | None = None
        try:
            self._client = self._create_client()
            await self._client.connect()
            self._connected = bool(self._client.connected)
        except Exception as e:
            exc = e
            self._connected = False

        if self._connected:
            self._connect_failures = 0
            logger.info(f"Connected to Modbus {self.transport}://{self._connection_str}")
            return True

        # pymodbus tends to swallow the underlying errno; include it when we have it.
        self._connect_failures += 1
        if exc is not None:
            logger.error(f"Failed to connect to {self._connection_str}: {exc!r}")
        else:
            logger.error(f"Failed to connect to {self._connection_str}")
        await self._run_serial_diagnostics()
        return False

    async def _run_serial_diagnostics(self) -> None:
        """Emit serial-port diagnostics for RTU, throttled to 1st + every Nth failure."""
        if self.transport == Transport.TCP:
            return
        if (self._connect_failures - 1) % _DIAG_EVERY != 0:
            return
        try:
            # Probes can block (filesystem, subprocess) — keep them off the event loop.
            findings = await asyncio.to_thread(diagnose_serial_port, self.serial_port)
        except Exception as e:
            logger.warning(f"serial diagnosis unavailable: {e!r}")
            return
        for finding in findings:
            logger.warning(f"serial diagnosis: {finding}")

    async def disconnect(self) -> None:
        """Disconnect from Modbus device."""
        if self._client:
            self._client.close()
            self._connected = False
            logger.info("Disconnected from Modbus device")

    @property
    def _connection_str(self) -> str:
        """Get connection string for logging."""
        prefix = f"[{self.interface_name}] " if self.interface_name else ""
        if self.transport == Transport.TCP:
            return f"{prefix}{self.host}:{self.port}"
        return f"{prefix}{self.serial_port}@{self.baudrate}"

    async def read_holding_registers(self, address: int, count: int = 1) -> list[int] | None:
        """Read holding registers.

        Args:
            address: Starting register address
            count: Number of registers to read

        Returns:
            List of register values or None on error
        """
        if not self._client or not self._connected:
            return None

        try:
            result = await self._client.read_holding_registers(
                address=address, count=count, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Read error at address {address}: {result}")
                return None
            return list(result.registers)
        except ModbusException as e:
            logger.error(f"Modbus exception reading {address}: {e}")
            return None

    async def read_input_registers(self, address: int, count: int = 1) -> list[int] | None:
        """Read input registers.

        Args:
            address: Starting register address
            count: Number of registers to read

        Returns:
            List of register values or None on error
        """
        if not self._client or not self._connected:
            return None

        try:
            result = await self._client.read_input_registers(
                address=address, count=count, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Read error at address {address}: {result}")
                return None
            return list(result.registers)
        except ModbusException as e:
            logger.error(f"Modbus exception reading {address}: {e}")
            return None

    async def read_coils(self, address: int, count: int = 1) -> list[bool] | None:
        """Read coils.

        Args:
            address: Starting coil address
            count: Number of coils to read

        Returns:
            List of coil values or None on error
        """
        if not self._client or not self._connected:
            return None

        try:
            result = await self._client.read_coils(
                address=address, count=count, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Read error at address {address}: {result}")
                return None
            return list(result.bits[:count])
        except ModbusException as e:
            logger.error(f"Modbus exception reading {address}: {e}")
            return None

    async def read_discrete_inputs(self, address: int, count: int = 1) -> list[bool] | None:
        """Read discrete inputs.

        Args:
            address: Starting input address
            count: Number of inputs to read

        Returns:
            List of input values or None on error
        """
        if not self._client or not self._connected:
            return None

        try:
            result = await self._client.read_discrete_inputs(
                address=address, count=count, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Read error at address {address}: {result}")
                return None
            return list(result.bits[:count])
        except ModbusException as e:
            logger.error(f"Modbus exception reading {address}: {e}")
            return None

    async def write_register(self, address: int, value: int) -> bool:
        """Write single holding register.

        Args:
            address: Register address
            value: Value to write

        Returns:
            True if successful
        """
        if not self._client or not self._connected:
            return False

        try:
            result = await self._client.write_register(
                address=address, value=value, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Write error at address {address}: {result}")
                return False
            return True
        except ModbusException as e:
            logger.error(f"Modbus exception writing {address}: {e}")
            return False

    async def write_registers(self, address: int, values: list[int]) -> bool:
        """Write multiple holding registers.

        Args:
            address: Starting register address
            values: Values to write

        Returns:
            True if successful
        """
        if not self._client or not self._connected:
            return False

        try:
            result = await self._client.write_registers(
                address=address, values=values, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Write error at address {address}: {result}")
                return False
            return True
        except ModbusException as e:
            logger.error(f"Modbus exception writing {address}: {e}")
            return False

    async def write_coil(self, address: int, value: bool) -> bool:
        """Write single coil.

        Args:
            address: Coil address
            value: Value to write

        Returns:
            True if successful
        """
        if not self._client or not self._connected:
            return False

        try:
            result = await self._client.write_coil(
                address=address, value=value, device_id=self.unit_id
            )
            if result.isError():
                logger.warning(f"Write error at address {address}: {result}")
                return False
            return True
        except ModbusException as e:
            logger.error(f"Modbus exception writing {address}: {e}")
            return False

    async def read_register_value(self, register: Register) -> float | int | bool | None:
        """Read and decode a register using its definition.

        Args:
            register: Register definition

        Returns:
            Decoded value or None on error
        """
        raw = await self._read_range(register.type, register.address, register.address_span)
        if not raw:
            return None
        if register.type in BIT_REGISTER_TYPES:
            return bool(raw[0])
        return decode_value(raw, register.datatype, register.scale, register.byte_order)

    async def write_register_value(self, register: Register, value: float | int | bool) -> bool:
        """Write a value to a register using its definition.

        Args:
            register: Register definition
            value: Value to write

        Returns:
            True if successful
        """
        if not register.writable:
            logger.warning(f"Register '{register.name}' is not writable (type: {register.type})")
            return False

        if register.type == RegisterType.COIL:
            return await self.write_coil(register.address, bool(value))

        raw = encode_value(value, register.datatype, register.scale, register.byte_order)

        if len(raw) == 1 and self.write_mode != WriteMode.FC16:
            return await self.write_register(register.address, raw[0])
        return await self.write_registers(register.address, raw)

    async def _read_range(
        self, reg_type: str, address: int, count: int
    ) -> list[int] | list[bool] | None:
        """Dispatch a range read to the typed read matching ``reg_type``.

        The single four-way dispatch to read_holding/input/coils/discrete_inputs.

        Returns:
            Raw register/bit values for the range, or None on error.
        """
        if reg_type == RegisterType.HOLDING:
            return await self.read_holding_registers(address, count)
        elif reg_type == RegisterType.INPUT:
            return await self.read_input_registers(address, count)
        elif reg_type == RegisterType.COIL:
            return await self.read_coils(address, count)
        elif reg_type == RegisterType.DISCRETE_INPUT:
            return await self.read_discrete_inputs(address, count)
        return None

    async def _read_block(self, block: ReadBlock) -> list[int] | list[bool] | None:
        """Read one planned block via the matching typed read."""
        return await self._read_range(block.type, block.address, block.count)

    def _build_poll_items(self) -> None:
        """Flatten the register map into per-register poll items.

        Disabled registers (poll_interval == 0) are excluded by polled_events and
        never polled. Each remaining register polls at its own poll_interval when
        set, otherwise at the interface interval. Items start due immediately
        (next_due == 0.0).
        """
        items: list[_PollItem] = []
        if self.register_map:
            for event_name, regs in self.register_map.polled_events.items():
                for reg in regs:
                    # ``is None`` (not truthiness): disabled registers are already
                    # filtered out by polled_events, so 0 never reaches here.
                    interval = (
                        self.poll_interval if reg.poll_interval is None else reg.poll_interval
                    )
                    items.append(_PollItem(event=event_name, register=reg, interval=interval))
        self._poll_items = items

    def _seconds_until_next_due(self, now: float | None = None) -> float:
        """Seconds until the earliest-due poll item (>= 0).

        Falls back to poll_interval when there are no items (raw/no-map mode).
        """
        if not self._poll_items:
            return self.poll_interval
        now = time.monotonic() if now is None else now
        return max(0.0, min(it.next_due - now for it in self._poll_items))

    def _plan_for(self, due: list[_PollItem]) -> list[ReadBlock]:
        """Block plan for a due set, memoized on the set's register identities."""
        key = tuple(id(it.register) for it in due)
        cached = self._plan_cache.get(key)
        if cached is not None:
            return cached
        plan = plan_blocks([it.register for it in due], self.max_block_size, self.max_read_gap)
        if len(self._plan_cache) > 64:  # safety valve against pathological due-set churn
            self._plan_cache.clear()
        self._plan_cache[key] = plan
        return plan

    async def _poll_registers(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Poll the registers that are due, honoring per-register poll rates.

        Args:
            now: Monotonic time to schedule against (defaults to the clock).

        Returns:
            Dictionary of {event_name: {field_name: value}}
        """
        if not self.register_map:
            return {}

        # None = not yet built; an all-disabled map builds to [] and is never re-walked.
        if self._poll_items is None:
            self._build_poll_items()
        now = time.monotonic() if now is None else now

        due = [it for it in self._poll_items if it.next_due <= now]
        if not due:
            return {}

        item_by_reg = {id(it.register): it for it in due}
        pairs: list[tuple[Register, Any]] = []

        if self.block_reads:
            for block in self._plan_for(due):
                raw = await self._read_block(block)
                if raw is None:
                    logger.warning(
                        f"Block read failed for {block.type} at address {block.address} "
                        f"(count {block.count}); skipping {len(block.registers)} register(s)"
                    )
                    continue
                pairs.extend(decode_block(block, raw))
        else:
            for it in due:
                pairs.append((it.register, await self.read_register_value(it.register)))

        results: dict[str, dict[str, Any]] = {}
        for reg, value in pairs:
            if value is None:
                continue
            event = item_by_reg[id(reg)].event
            results.setdefault(event, {})[reg.field_name] = value

        # Reschedule from now — no backlog after a stall.
        for it in due:
            it.next_due = now + it.interval

        return results

    async def _log_values(self, values: dict[str, dict[str, Any]]) -> None:
        """Log polled values to Zelos trace source.

        Args:
            values: Dictionary of {event_name: {field_name: value}}
        """
        if not self._source:
            return

        for event_name, event_values in values.items():
            if not event_values:
                continue

            event = getattr(self._source, event_name, None)
            if event:
                event.log(**event_values)

    def start(self) -> None:
        """Start the client (initialize trace source)."""
        self._running = True
        self._init_trace_source()
        # Poll items build lazily on the first _poll_registers cycle.
        logger.info("ModbusClient started")

    def stop(self) -> None:
        """Stop the client."""
        self._running = False
        logger.info("ModbusClient stopped")

    def run(self) -> None:
        """Run the polling loop (blocking)."""
        asyncio.run(self._run_async())

    async def _ensure_connected(self) -> bool:
        """Ensure connection is established, reconnecting if needed.

        Returns:
            True if connected
        """
        if self._connected and self._client and self._client.connected:
            return True

        # Connection lost or not established - try to reconnect
        self._connected = False
        if self._client:
            with contextlib.suppress(Exception):
                self._client.close()

        logger.info(f"Connecting to {self._connection_str}...")
        return await self.connect()

    async def _run_async(self) -> None:
        """Async polling loop with automatic reconnection."""
        self._loop = asyncio.get_running_loop()
        reconnect_interval = 3.0  # seconds between reconnect attempts

        try:
            while self._running:
                # Ensure we're connected
                if not await self._ensure_connected():
                    logger.warning(f"Connection failed, retrying in {reconnect_interval}s...")
                    await asyncio.sleep(reconnect_interval)
                    continue

                # Poll registers
                try:
                    values = await self._poll_registers()
                    await self._log_values(values)
                    self._poll_count += 1

                    if self._poll_count % 10 == 0:
                        logger.debug(f"Poll #{self._poll_count}: {values}")

                except Exception as e:
                    self._error_count += 1
                    logger.error(f"Poll error: {e}")

                    # Check if this looks like a connection error
                    if self._is_connection_error(e):
                        self._connected = False
                        logger.warning("Connection lost, will reconnect...")
                        continue  # Skip sleep, reconnect immediately

                # Sleep until the earliest-due item, waking at least once per
                # second so a shutdown (self._running flips false) is seen within
                # ~1s. sleep(<=0) is a bare yield, so every iteration awaits once
                # (no starvation even when there is no poll work).
                remaining = self._seconds_until_next_due()
                while True:
                    await asyncio.sleep(min(remaining, 1.0))
                    if not self._running:
                        break
                    remaining -= 1.0
                    if remaining <= 0:
                        break
        finally:
            await self.disconnect()

    def _is_connection_error(self, error: Exception) -> bool:
        """Check if an exception indicates a connection problem."""
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True
        error_str = str(error).lower()
        connection_indicators = [
            "connection",
            "timeout",
            "timed out",
            "refused",
            "reset",
            "broken pipe",
            "no response",
            "disconnected",
            "not connected",
        ]
        return any(ind in error_str for ind in connection_indicators)
