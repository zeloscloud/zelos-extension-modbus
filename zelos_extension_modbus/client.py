"""Modbus connection and device classes with Zelos SDK integration.

A ModbusConnection is one link (TCP socket or serial port). Each ModbusDevice
(unit id + register map) on it shares that link and its request choke point.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import zelos_sdk
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from zelos_extension_modbus.blocks import ReadBlock, plan_blocks
from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MIN_RATE,
    MODBUS_MAX_BIT_READ_COUNT,
    MODBUS_MAX_READ_COUNT,
    ByteOrder,
    RegisterType,
    Transport,
    WriteMode,
    trace_layout,
)
from zelos_extension_modbus.register_map import Register, RegisterMap
from zelos_extension_modbus.serial_diag import diagnose_serial_port

logger = logging.getLogger(__name__)

# Run full serial diagnostics on the 1st connect failure, then every Nth
# consecutive failure. A success resets.
_DIAG_EVERY = 10

# Seconds between attempts to build a device's register map when the device
# answered but discovery failed (a timeout follows the demotion schedule).
MAP_RETRY_INTERVAL = 30.0

# Reconnect backoff: doubles per failed connect up to the cap; a completed poll resets it.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0

# Auto-demotion (Kepware): a demoted device is probed after this many seconds,
# doubling per failed probe up to demote_max_s.
DEMOTE_BACKOFF = 10.0

# Exception codes for an unreadable address in a block: illegal address, illegal
# value (some devices use 03 for a read past the end of a table).
ILLEGAL_ADDRESS = (0x02, 0x03)

# Gateway path unavailable / target failed to respond: the unit is absent, which
# counts as no response.
GATEWAY_ABSENT = (0x0A, 0x0B)

# Exception code names, for action errors.
EXCEPTION_NAMES = {
    0x01: "illegal function",
    0x02: "illegal data address",
    0x03: "illegal data value",
    0x04: "server device failure",
    0x05: "acknowledge",
    0x06: "server device busy",
    0x07: "negative acknowledge",
    0x08: "memory parity error",
    0x0A: "gateway path unavailable",
    0x0B: "gateway target failed to respond",
}

NO_RESPONSE = "no response from device"
# A write that got no answer may still have landed.
OUTCOME_UNKNOWN = " (outcome unknown, check the device)"

# A block the device refuses (illegal address) is retried this often (Kepware
# "Deactivate Tags on Illegal Address").
REFUSED_RETRY = 600.0

# Achieved-rate smoothing per read, and how long an overload (or its recovery)
# must hold before it is logged.
RATE_EWMA = 0.2
OVERLOAD_SUSTAIN = 30.0

READ_METHODS = {
    RegisterType.HOLDING: "read_holding_registers",
    RegisterType.INPUT: "read_input_registers",
    RegisterType.COIL: "read_coils",
    RegisterType.DISCRETE_INPUT: "read_discrete_inputs",
}


def _clamped(name: str, value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``, warning when it was out of range."""
    clamped = max(lo, min(hi, value))
    if clamped != value:
        logger.warning(f"{name} {value} out of range [{lo}, {hi}]; clamping to {clamped}")
    return clamped


@dataclass(eq=False)
class _Block:
    """One planned read at one rate: the scheduler's unit of work."""

    read: ReadBlock
    rate: float
    next_due: float = 0.0  # monotonic
    last_read: float | None = None  # previous read's start, for the achieved rate
    error: int | None = None  # exception code of the failing read, warned once

    @property
    def refused(self) -> bool:
        """The device answered illegal address: not polled, retried every REFUSED_RETRY."""
        return self.error in ILLEGAL_ADDRESS


@dataclass
class _Tier:
    """Requested vs achieved rate for one rate on a device."""

    rate: float
    interval: float | None = None  # smoothed read interval (s)
    overloaded: bool = False  # as last logged
    since: float | None = None  # when the measured state started to differ from it

    @property
    def overload_pct(self) -> float | None:
        """100 x mean lateness / rate: 100 = reads come at half the requested rate."""
        if self.interval is None:
            return None
        return max(0.0, 100 * (self.interval / self.rate - 1))


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
    "string": zelos_sdk.DataType.String,
}

# SunSpec's scale-factor range; anything wider is a bad read, not a value.
MAX_SCALE_EXPONENT = 10


def _swap_bytes(word: int) -> int:
    return ((word & 0xFF) << 8) | (word >> 8)


def _reorder_registers(registers: list[int], byte_order: str) -> list[int]:
    """Map wire words to/from big-endian (ABCD) words; its own inverse.

    For bytes A (most significant) .. D: big = AB CD, little = DC BA,
    big_swap = CD AB, little_swap = BA DC; 64-bit extends the same way.
    A single register is never reordered.
    """
    if len(registers) <= 1 or byte_order == ByteOrder.BIG:
        return list(registers)
    if byte_order == ByteOrder.BIG_SWAP:
        return registers[::-1]
    if byte_order == ByteOrder.LITTLE_SWAP:
        return [_swap_bytes(w) for w in registers]
    return [_swap_bytes(w) for w in registers[::-1]]  # little


# Pack/unpack format, per numeric datatype.
_FORMATS = {
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float32": "f",
    "uint64": "Q",
    "int64": "q",
    "float64": "d",
}


def is_scaled_int(datatype: str, scale: float) -> bool:
    """An integer register with a non-1 scale decodes to a float."""
    return datatype in _FORMATS and datatype not in ("float32", "float64") and scale != 1


def decode_value(
    registers: list[int], datatype: str, scale: float = 1.0, byte_order: str = "big"
) -> float | int | bool | str:
    """Decode raw words to a typed value.

    Floats and scaled integers come back as float; an unscaled integer stays an
    exact int (a float multiply would round 64-bit values past 2**53).
    ``byte_order`` is ignored for strings (high byte first).
    """
    if datatype == "string":
        text = struct.pack(f">{len(registers)}H", *registers).decode("utf-8", errors="replace")
        return text.split("\x00", 1)[0].rstrip(" ")
    if datatype == "bool":
        return bool(registers[0])
    fmt = _FORMATS.get(datatype)
    if fmt is None:
        return registers[0]
    n = struct.calcsize(fmt) // 2
    regs = _reorder_registers(list(registers[:n]), byte_order)
    value = struct.unpack(f">{fmt}", struct.pack(f">{n}H", *regs))[0]
    if isinstance(value, float) or scale != 1:
        # A 1/n scale divides by n: 2305 / 10 is exact where 2305 * 0.1 is not.
        inverse = round(1 / scale) if 0 < abs(scale) < 1 else 0
        if inverse and abs(inverse * scale - 1) < 1e-12:
            return float(value / inverse)
        return float(value * scale)
    return value


def encode_value(
    value: float | int | bool, datatype: str, scale: float = 1.0, byte_order: str = "big"
) -> list[int]:
    """Encode a typed value to raw words; exact inverse of ``decode_value``.

    Integers are ``value / scale`` rounded to nearest; out of range raises ValueError.
    """
    if datatype == "bool":
        return [1 if value else 0]
    fmt = _FORMATS.get(datatype)
    if fmt is None:
        raise ValueError(f"{datatype} is not writable")
    scaled = value / scale if scale != 1 else value
    try:
        if fmt not in "fd" and not isinstance(scaled, int):
            scaled = math.floor(scaled + 0.5)  # halves round up, like JS Math.round
        raw = struct.pack(f">{fmt}", scaled)
    except (struct.error, OverflowError, ValueError) as e:
        raise ValueError(f"{value} is out of range for {datatype} (scale {scale})") from e
    n = len(raw) // 2
    return _reorder_registers(list(struct.unpack(f">{n}H", raw)), byte_order)


def encode_register(register: Register, value: float | int | bool) -> tuple[list[int], Any]:
    """(raw words, the value they decode to) for a write; ValueError if not exact.

    A value the datatype and scale cannot hold exactly is refused, naming the
    nearest writable value, rather than quietly rounded. float32 keeps its own
    precision (~7 digits).
    """
    if register.type == RegisterType.COIL:
        return [int(bool(value))], bool(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{value} is not a finite number")
    raw = encode_value(value, register.datatype, register.scale, register.byte_order)
    written = decode_value(raw, register.datatype, register.scale, register.byte_order)
    rel_tol = 1e-6 if register.datatype == "float32" else 1e-9
    if not math.isclose(written, value, rel_tol=rel_tol, abs_tol=1e-9 * abs(register.scale)):
        raise ValueError(
            f"{value} is not a whole {register.datatype} step (scale {register.scale:g}); "
            f"nearest writable value is {written}"
        )
    return raw, written


def decode_register(register: Register, raw: list[int] | list[bool]) -> Any:
    """Decode one register's raw slice; None when it holds an ``invalid`` value.

    A scale_ref is applied by the caller (see ``apply_scale_ref``): it needs
    another register's value.
    """
    if register.type in BIT_REGISTER_TYPES:
        return bool(raw[0])
    if register.invalid:
        words = _reorder_registers(list(raw), register.byte_order)
        if int.from_bytes(struct.pack(f">{len(words)}H", *words)) in register.invalid:
            return None
    return decode_value(raw, register.datatype, register.scale, register.byte_order)


def apply_scale_ref(value: Any, exponent: Any) -> float | None:
    """``value * 10**exponent``; None if either is None or the exponent is out of range."""
    if value is None or exponent is None or abs(exponent) > MAX_SCALE_EXPONENT:
        return None
    # Divide for negative exponents: 2305 / 10 is exact where 2305 * 0.1 is not.
    return float(value * 10**exponent if exponent >= 0 else value / 10**-exponent)


def decode_block(block: ReadBlock, raw: list[int] | list[bool]) -> list[tuple[Register, Any]]:
    """(register, value) per register of ``block``, sliced from ``raw`` at its offset.

    A register whose slice is short (truncated response) is warned about and
    skipped, not decoded from garbage.
    """
    results: list[tuple[Register, Any]] = []
    for reg in block.registers:
        offset = reg.address - block.address
        span = reg.address_span
        chunk = raw[offset : offset + span]
        if len(chunk) < span:
            logger.warning(
                f"Short block response for register '{reg.name}' at address "
                f"{reg.map_address}: expected {span}, got {len(chunk)}"
            )
            continue
        results.append((reg, decode_register(reg, chunk)))
    return results


def json_safe(value: Any) -> Any:
    """None for a non-finite float: the SDK's JSON conversion rejects NaN/inf outright."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class RequestFailed(Exception):
    """An action's request failed; the message is the reason shown to the user."""


def refused(code: int) -> str:
    """``device refused: exception 02 (illegal data address)``."""
    return f"device refused: exception {code:02X} ({EXCEPTION_NAMES.get(code, 'unknown code')})"


def _is_connection_error(error: Exception) -> bool:
    """Check if an exception indicates a connection problem."""
    if isinstance(error, (ConnectionError, ConnectionException, TimeoutError, OSError)):
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


class ModbusConnection:
    """One link (TCP socket or serial port) shared by every device on it.

    Owns the pymodbus client, connect/reconnect and the single request choke
    point, and runs one poll loop that schedules its devices' reads. Requests
    from all devices serialize here: RS485 is half-duplex, and many TCP
    gateways accept only a few connections.
    """

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
        timeout: float = 3.0,
        retries: int = 1,
        request_delay_ms: int = 0,
        connect_delay_ms: int = 0,
        name: str | None = None,
    ) -> None:
        """Initialize a connection.

        Args:
            transport: 'tcp' or 'rtu'
            host: TCP host address
            port: TCP port
            serial_port: Serial port for RTU
            baudrate: Serial baudrate for RTU
            parity: Serial parity ('N', 'E', 'O')
            stopbits: Number of stop bits (1 or 2)
            bytesize: Number of data bits (7 or 8)
            timeout: Request timeout in seconds
            retries: Extra attempts per request before it counts as failed
                (clamped to >= 0)
            request_delay_ms: Minimum gap between consecutive requests
                (clamped to >= 0)
            connect_delay_ms: Pause after each (re)connect before the first
                request (clamped to >= 0)
            name: Trace/action name; defaults to the sanitized endpoint
                (``10_0_0_5``, ``dev_ttyUSB0``)
        """
        self.transport = transport
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.timeout = timeout
        self.retries = int(_clamped("retries", retries, 0, float("inf")))
        self.request_delay_ms = int(_clamped("request_delay_ms", request_delay_ms, 0, float("inf")))
        self.connect_delay_ms = int(_clamped("connect_delay_ms", connect_delay_ms, 0, float("inf")))
        raw_name = host if transport == Transport.TCP else serial_port
        self.name = name or zelos_sdk.sanitize_name(raw_name, kind="source")

        # Devices register themselves here (ModbusDevice.__init__).
        self.devices: list[ModbusDevice] = []

        self._client: AsyncModbusTcpClient | AsyncModbusSerialClient | None = None
        self.connected = False
        self._running = False
        # Consecutive connect failures (throttles serial diagnostics); the last reason.
        self._connect_failures = 0
        self.connect_error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        # close_after_sweep: set by start(); parked = closed on purpose while idle.
        self._close_after_sweep = False
        self._parked = False

        # See ``lock``; also holds the request_delay_ms gap.
        self._lock: asyncio.Lock | None = None
        self._last_request_end = 0.0

    def _create_client(self) -> AsyncModbusTcpClient | AsyncModbusSerialClient:
        # reconnect_delay 0: we reconnect, under the lock, with connect_delay_ms.
        common = {"timeout": self.timeout, "retries": self.retries, "reconnect_delay": 0}
        if self.transport == Transport.TCP:
            return AsyncModbusTcpClient(host=self.host, port=self.port, **common)
        return AsyncModbusSerialClient(
            port=self.serial_port,
            baudrate=self.baudrate,
            parity=self.parity,
            stopbits=self.stopbits,
            bytesize=self.bytesize,
            **common,
        )

    @property
    def endpoint(self) -> str:
        """``host:port`` for TCP, ``serial_port@baudrate`` for RTU."""
        if self.transport == Transport.TCP:
            return f"{self.host}:{self.port}"
        return f"{self.serial_port}@{self.baudrate}"

    @property
    def _connection_str(self) -> str:
        """``[name] endpoint`` for logs."""
        return f"[{self.name}] {self.endpoint}"

    async def connect(self) -> bool:
        """Connect, then hold connect_delay_ms; ``connected`` is set after it.

        A failure runs throttled serial diagnostics (RTU) so a re-enumerated or
        permission-blocked adapter surfaces instead of a silent retry loop.
        """
        exc: Exception | None = None
        self.connected = False
        up = False
        try:
            self._client = self._create_client()
            await self._client.connect()
            up = bool(self._client.connected)
        except Exception as e:
            exc = e

        if up:
            self._connect_failures = 0
            # A close_after_sweep reopen is routine, not news.
            log = logger.debug if self._parked else logger.info
            self._parked = False
            log(f"Connected to Modbus {self.transport}://{self._connection_str}")
            if self.connect_delay_ms:
                await asyncio.sleep(self.connect_delay_ms / 1000)
            self.connected = True
            return True

        # pymodbus tends to swallow the underlying errno; keep it when we have it.
        # run_async logs it on its backoff cadence.
        self._connect_failures += 1
        self.connect_error = f": {exc!r}" if exc is not None else ""
        await self._run_serial_diagnostics()
        return False

    async def _run_serial_diagnostics(self) -> None:
        """Emit serial-port diagnostics for RTU, throttled to 1st + every Nth failure."""
        # Fail closed: only RTU has a serial port to probe; any other transport skips.
        if self.transport != Transport.RTU:
            return
        if (self._connect_failures - 1) % _DIAG_EVERY != 0:
            return
        try:
            # Probes can block (filesystem, subprocess): keep them off the event loop.
            findings = await asyncio.to_thread(diagnose_serial_port, self.serial_port)
        except Exception as e:
            logger.warning(f"serial diagnosis unavailable: {e!r}")
            return
        for finding in findings:
            logger.warning(f"serial diagnosis: {finding}")

    async def disconnect(self) -> None:
        """Disconnect from the link."""
        if self._client:
            self._client.close()
            self.connected = False
            logger.info(f"Disconnected from {self._connection_str}")

    @property
    def lock(self) -> asyncio.Lock:
        """Serializes every request, connect and park on the link.

        Created on first use, inside the running loop.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure_connected(self) -> bool:
        """Connected, reconnecting if needed."""
        async with self.lock:
            return await self._ensure_connected()

    async def _ensure_connected(self) -> bool:
        """``ensure_connected`` with the lock held."""
        if self.connected and self._client and self._client.connected:
            return True

        # Connection lost or not established - try to reconnect
        self.connected = False
        if self._client:
            with contextlib.suppress(Exception):
                self._client.close()

        quiet = self._parked or self._connect_failures
        (logger.debug if quiet else logger.info)(f"Connecting to {self._connection_str}...")
        return await self.connect()

    async def _park(self) -> None:
        """close_after_sweep: release the link while idle; the next request reopens it."""
        async with self.lock:
            if self._client:
                self._client.close()
            self.connected = False
            self._parked = True

    async def request(self, method: str, unit_id: int, single: bool = False, **kwargs: Any) -> Any:
        """Issue one pymodbus request; the single choke point for all device I/O.

        Requests from every device (poll or action) run one at a time, and each
        starts at least request_delay_ms after the previous one ended. A down
        link is reopened here, under the lock and with connect_delay_ms.
        ``single``: one attempt, no retries (a demoted device's probe).

        Raises:
            ConnectionException: the link cannot be (re)opened.
            ModbusIOException: no response.
        """
        async with self.lock:
            wait = self._last_request_end + self.request_delay_ms / 1000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if not await self._ensure_connected():
                raise ConnectionException(f"cannot connect to {self.endpoint}")
            self._client.ctx.retries = 0 if single else self.retries
            try:
                return await getattr(self._client, method)(device_id=unit_id, **kwargs)
            except ModbusIOException as e:
                # pymodbus reports a cancelled request as ModbusIOException;
                # shutdown needs the cancel to propagate.
                if isinstance(e.__cause__, asyncio.CancelledError):
                    raise asyncio.CancelledError from e
                raise
            finally:
                self._last_request_end = time.monotonic()

    def start(self, prefix: str, source: zelos_sdk.TraceSource | None = None) -> None:
        """Declare every device's trace events.

        Args:
            prefix: Trace prefix (see ``trace_layout``); empty = cleared.
            source: The shared source named ``prefix``. Required with a prefix;
                cleared, the connection creates its own source.
        """
        self._running = True
        for device in self.devices:
            source_name, event_prefix = trace_layout(prefix, self.name, device.name)
            source = source or zelos_sdk.TraceSource(source_name)
            device.init_trace(source, event_prefix)
        opted = [d.close_after_sweep for d in self.devices]
        self._close_after_sweep = bool(opted) and all(opted)
        if any(opted) and not all(opted):
            logger.warning(
                f"{self._connection_str}: close_after_sweep ignored; only some of its "
                "devices set it, and they share the link"
            )
        logger.info(f"{self._connection_str} started with {len(self.devices)} device(s)")

    def stop(self) -> None:
        """Stop the poll loop."""
        self._running = False
        logger.info(f"{self._connection_str} stopped")

    def _batch(self, now: float) -> list[tuple[ModbusDevice, _Block | None]]:
        """This tick's work: (device, block), or (device, None) for a map build.

        Every due block at the connection's fastest rate, plus at most ONE due
        slower block (OpenEMS LOW round robin), most overdue first (lateness /
        rate). Slower blocks thus spread one per tick instead of bursting and
        stalling the fast points, and a first sweep staggers them for good (a
        block is due again one rate after it was read). A demoted device gets
        one probe block when its retry is due, and nothing else.
        """
        work: list[tuple[ModbusDevice, _Block | None]] = []
        live: list[tuple[ModbusDevice, _Block]] = []
        probes: list[tuple[ModbusDevice, _Block]] = []
        for dev in self.devices:
            if now < dev._retry_at:
                continue
            if dev.map_pending:
                work.append((dev, None))
            elif dev.demoted:
                probes += [(dev, b) for b in dev._schedule(now)[:1]]
            else:
                live += [(dev, b) for b in dev._schedule(now)]
        if not live:
            return work + probes

        def overdue(item: tuple[ModbusDevice, _Block]) -> tuple[float, float]:
            block = item[1]
            return ((block.next_due - now) / block.rate, block.rate)

        fastest = min(b.rate for _, b in live)
        due = sorted(((d, b) for d, b in live if b.next_due <= now), key=overdue)
        fast = [item for item in due if item[1].rate == fastest]
        slow = [item for item in due if item[1].rate != fastest][:1]
        return work + probes + sorted(fast + slow, key=overdue)

    async def _poll(self, work: list[tuple[ModbusDevice, _Block | None]]) -> bool:
        """Run one tick's work, then log each device's values; False if the link was lost."""
        polled: dict[int, ModbusDevice] = {}
        held = True
        for dev, block in work:
            now = time.monotonic()
            if now < dev._retry_at:
                continue  # demoted earlier this tick
            try:
                if block is None:
                    await dev.load_map(now)
                else:
                    polled[id(dev)] = dev
                    await dev._read_block(block, now)
            except Exception as e:
                dev.failed_reads += 1
                logger.error(f"[{dev.path}] Poll error: {e}")
                if _is_connection_error(e):
                    self.connected = False
                    logger.warning(f"{self._connection_str}: connection lost")
                    held = False
                    break
        for dev in polled.values():
            dev._log_values(dev._flush())
            dev._poll_count += 1
        return held

    async def run_async(self) -> None:
        """Poll every device over this link, reconnecting as needed.

        A failed connect waits RECONNECT_INITIAL, doubling to RECONNECT_MAX; it
        is logged first and then only when the wait grows. Only a poll that
        keeps the link resets the backoff (a link that accepts and drops would
        otherwise spin).
        """
        self._loop = asyncio.get_running_loop()
        backoff, logged = RECONNECT_INITIAL, 0.0
        try:
            while self._running:
                now = time.monotonic()
                work = self._batch(now)
                if not work and self._close_after_sweep:
                    if self.connected:
                        await self._park()
                # A down link takes every device on it down; reconnect once.
                elif not await self.ensure_connected():
                    for dev in self.devices:
                        dev._missed(time.monotonic())
                    if backoff != logged:
                        logger.warning(
                            f"Cannot connect to {self._connection_str}{self.connect_error}; "
                            f"retrying in {backoff:g}s"
                        )
                        logged = backoff
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue
                if work:
                    if await self._poll(work):
                        backoff, logged = RECONNECT_INITIAL, 0.0
                    continue
                wake = min((d._next_due() for d in self.devices), default=math.inf)
                await self._sleep(wake - now)
        finally:
            await self.disconnect()

    async def _sleep(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking at least once a second to see a stop.

        sleep(<=0) is a bare yield, so every loop iteration awaits once.
        """
        end = time.monotonic() + seconds
        while True:
            await asyncio.sleep(max(0.0, min(end - time.monotonic(), 1.0)))
            if not self._running or time.monotonic() >= end:
                return


class ModbusDevice:
    """One unit id on a connection: its register map, poll schedule and trace events."""

    def __init__(
        self,
        connection: ModbusConnection,
        unit_id: int = 1,
        register_map: RegisterMap | None = None,
        rate: float = 1.0,
        min_rate: float = 0.0,
        write_mode: str = WriteMode.AUTO,
        block_reads: bool = True,
        max_block_size: int = MODBUS_MAX_READ_COUNT,
        max_bit_block_size: int = MODBUS_MAX_BIT_READ_COUNT,
        max_read_gap: int = 0,
        close_after_sweep: bool = False,
        demote_after: int = 3,
        demote_max_s: float = 300.0,
        name: str | None = None,
        map_loader: Callable[[ModbusDevice], Awaitable[RegisterMap]] | None = None,
    ) -> None:
        """Initialize a device and attach it to ``connection``.

        Args:
            connection: The link this device is reached over
            unit_id: Modbus slave/unit ID
            register_map: Optional register map for named access
            rate: Poll rate in seconds for registers without their own
                (0 = not polled; else >= MIN_RATE)
            min_rate: Floor on every register's rate (the device cannot be
                polled faster)
            write_mode: 'auto' (FC 6 for single, FC 16 for multi) or
                        'fc16' (always FC 16 for all writes)
            block_reads: Coalesce contiguous registers into range reads
            max_block_size: Maximum addresses per register read (clamped to 1-125)
            max_bit_block_size: Maximum addresses per coil/discrete-input read
                (clamped to 1-2000)
            max_read_gap: Maximum uncovered addresses to bridge within a block
                (clamped to >= 0; 0 = strictly contiguous)
            close_after_sweep: Ask the connection to close whenever polling
                goes idle (honored only when every device on it asks)
            demote_after: Consecutive timeouts before the device is skipped
                and only probed (clamped to >= 1)
            demote_max_s: Longest wait between probes of a demoted device
            name: Trace/action name; defaults to ``unit<unit_id>``
            map_loader: Builds the register map over the link once connected
                (SunSpec discovery), in place of ``register_map``. Retried
                until it succeeds; the device's events are declared then.
        """
        self.connection = connection
        connection.devices.append(self)
        self.unit_id = unit_id
        self.name = name or f"unit{unit_id}"
        self.register_map = register_map
        self.write_mode = write_mode

        # Backstop for out-of-range knobs: the map `device` block and direct
        # construction bypass the config schema.
        self.rate = _clamped("rate", rate, 0, float("inf"))
        if 0 < self.rate < MIN_RATE:
            self.rate = _clamped("rate", rate, MIN_RATE, float("inf"))
        self.min_rate = _clamped("min_rate", min_rate, 0, float("inf"))
        self.block_reads = block_reads
        self.max_block_size = int(
            _clamped("max_block_size", max_block_size, 1, MODBUS_MAX_READ_COUNT)
        )
        self.max_bit_block_size = int(
            _clamped("max_bit_block_size", max_bit_block_size, 1, MODBUS_MAX_BIT_READ_COUNT)
        )
        self.max_read_gap = int(_clamped("max_read_gap", max_read_gap, 0, float("inf")))
        self.close_after_sweep = close_after_sweep
        self.demote_after = int(_clamped("demote_after", demote_after, 1, float("inf")))
        self.demote_max_s = _clamped("demote_max_s", demote_max_s, DEMOTE_BACKOFF, float("inf"))

        self._poll_count = 0
        # Kepware-style counters over poll reads; a timeout is a failed read.
        self.successful_reads = 0
        self.failed_reads = 0
        # Why the device is not polling (map discovery failed), else None.
        self.last_error: str | None = None

        self._map_loader = map_loader
        self._trace_target: tuple[zelos_sdk.TraceSource, str] | None = None

        # Schedule: None = not planned yet. Tiers keyed by rate.
        self._blocks: list[_Block] | None = None
        self._tiers: dict[float, _Tier] = {}
        self._event_of: dict[int, str] = {}  # id(register) -> event, polled registers only
        # This tick's decoded values, and the last value of every register read
        # (scale_ref exponents come from here when read in another block).
        self._pending: dict[int, tuple[Register, Any]] = {}
        self._decoded: dict[int, Any] = {}

        # Demotion: consecutive timeouts; backoff > 0 while demoted. retry_at
        # gates both probes and map builds.
        self._timeouts = 0
        self._backoff = 0.0
        self._retry_at = 0.0

        # Trace events by map event name, and where they live ("Modbus/conn/unit1").
        self._events: dict[str, Any] = {}
        self.trace_path: str | None = None

        # Last decoded value per qualified register path ("event/name") ->
        # (value, wall-clock ms). Fed by the poll sweep and by on-demand named
        # reads so get_snapshot can answer without any device I/O.
        self._last_values: dict[str, tuple[Any, int]] = {}

    @property
    def path(self) -> str:
        """``<connection>/<device>``: the registry key actions select by."""
        return f"{self.connection.name}/{self.name}"

    @property
    def connected(self) -> bool:
        """Whether this device's link is up."""
        return self.connection.connected

    @property
    def address_base(self) -> int:
        """Numbering of user-facing addresses: the map's, 1 without a map."""
        return self.register_map.address_base if self.register_map else 1

    @property
    def map_pending(self) -> bool:
        """Whether the register map is still to be built by the map loader."""
        return self._map_loader is not None

    @property
    def demoted(self) -> bool:
        """Skipped after demote_after consecutive timeouts; only probed until it answers."""
        return self._backoff > 0

    def rate_of(self, register: Register) -> float:
        """Requested poll rate (s): the register's own, else the device's; 0 = not polled.

        min_rate floors it.
        """
        rate = self.rate if register.rate is None else register.rate
        return max(rate, self.min_rate) if rate else 0.0

    @property
    def polled_events(self) -> dict[str, list[Register]]:
        """Events mapped to their polled registers; events with none are omitted."""
        result: dict[str, list[Register]] = {}
        for event_name, regs in (self.register_map.events if self.register_map else {}).items():
            polled = [r for r in regs if self.rate_of(r)]
            if polled:
                result[event_name] = polled
        return result

    def rate_status(self) -> dict[str, Any]:
        """Requested vs achieved rate and demotion, for get_status and friends.

        The top-level rates are the worst tier's (highest overload; the fastest
        tier until one is measured); ``tiers`` has them all. Achieved rates are
        null until measured, again after a demotion or while the link is down.
        """
        tiers = [
            {
                "requested_rate": t.rate,
                "achieved_rate": None if t.interval is None else round(t.interval, 3),
                "overload_pct": None if t.interval is None else round(t.overload_pct, 1),
                "blocks": sum(b.rate == t.rate for b in self._blocks or []),
            }
            for t in sorted(self._tiers.values(), key=lambda t: t.rate)
        ]
        measured = [t for t in tiers if t["overload_pct"] is not None]
        head = max(measured, key=lambda t: t["overload_pct"], default=tiers[0] if tiers else {})
        now = time.monotonic()
        retry = max(0.0, self._retry_at - now) if self.demoted else None
        return {
            "requested_rate": head.get("requested_rate"),
            "achieved_rate": head.get("achieved_rate"),
            "overload_pct": head.get("overload_pct"),
            "demoted": self.demoted,
            "retry_in_s": None if retry is None else round(retry, 1),
            "tiers": tiers,
            "refused": [
                {
                    "range": self._range(b.read),
                    "code": b.error,
                    "retry_in_s": round(max(0.0, b.next_due - now), 1),
                }
                for b in self._blocks or []
                if b.refused
            ],
        }

    async def load_map(self, now: float | None = None) -> None:
        """Build the register map over the link, then declare its events.

        A failure is the device's error: logged, kept in ``last_error`` and
        retried, while the connection keeps serving its other devices. A
        timeout counts toward demotion; any other failure retries after
        MAP_RETRY_INTERVAL.
        """
        now = time.monotonic() if now is None else now
        try:
            self.register_map = await self._map_loader(self)
        except ConnectionException:
            raise  # the link is down, not the device
        except ModbusIOException as e:
            self.last_error = f"register map discovery: no response ({e})"
            self._timed_out(now, self.last_error)
            return
        except Exception as e:
            self._responded(now)
            self._retry_at = now + MAP_RETRY_INTERVAL
            self.last_error = f"register map discovery failed: {e!r}"
            logger.error(f"[{self.path}] {self.last_error}; retrying in {MAP_RETRY_INTERVAL:.0f}s")
            return
        self._responded(now)
        self._map_loader = None
        self._blocks = None  # plan the new map on the next tick
        self.last_error = None
        logger.info(
            f"[{self.path}] Built register map '{self.register_map.name}': "
            f"{len(self.register_map.events)} events, {len(self.register_map.registers)} registers"
        )
        if self._trace_target:
            self.init_trace(*self._trace_target)

    def init_trace(self, source: zelos_sdk.TraceSource, event_prefix: str) -> None:
        """Declare this device's events on ``source`` under ``event_prefix``.

        With a map still pending, only remembers where; ``load_map`` declares them.
        """
        self.trace_path = f"{source.name}/{event_prefix}"
        self._trace_target = (source, event_prefix)
        if self.map_pending:
            return

        if not self.register_map or not self.register_map.events:
            # No register map - create a generic raw event
            source.add_event(
                f"{event_prefix}/raw",
                [
                    zelos_sdk.TraceEventFieldMetadata("address", zelos_sdk.DataType.UInt16),
                    zelos_sdk.TraceEventFieldMetadata("value", zelos_sdk.DataType.Int32),
                ],
            )
            return

        # Create events from user-defined event names. polled_events already
        # drops unpolled registers and all-unpolled events (no dead leaves in
        # the signal tree). Field names are the register's precomputed
        # trace-safe name; from_dict guarantees they don't collide per event.
        for event_name, regs in self.polled_events.items():
            fields = [
                zelos_sdk.TraceEventFieldMetadata(
                    reg.field_name,
                    # Bits log booleans; a scale or scale_ref makes an integer fractional.
                    zelos_sdk.DataType.Boolean
                    if reg.type in BIT_REGISTER_TYPES
                    else zelos_sdk.DataType.Float64
                    if reg.ref or is_scaled_int(reg.datatype, reg.scale)
                    else SDK_DATATYPE_MAP.get(reg.datatype, zelos_sdk.DataType.Int32),
                    reg.unit,
                )
                for reg in regs
            ]
            path = f"{event_prefix}/{event_name}"
            self._events[event_name] = source.add_event(path, fields)
            for reg in regs:
                if reg.values:
                    source.add_value_table(path, reg.field_name, reg.values)

    @property
    def last_values(self) -> dict[str, tuple[Any, int]]:
        """Copy of the last decoded value per qualified register path.

        Maps ``"event/name"`` (the path the named read/write actions accept) to
        ``(value, wall_clock_ms)``. Registers never polled or read are absent.
        """
        return dict(self._last_values)

    def record_value(self, register: Register, value: Any, event: str | None = None) -> None:
        """Cache a decoded value under its qualified ``event/name`` path.

        Args:
            register: Register the value was decoded for.
            value: Decoded (scaled) value.
            event: Owning event name. The poll sweep already knows it; on-demand
                reads pass None and let the register map resolve it.
        """
        if event is None:
            event = self._event_for(register)
            if event is None:
                return
        self._last_values[f"{event}/{register.name}"] = (value, int(time.time() * 1000))

    def _event_for(self, register: Register) -> str | None:
        """Event owning ``register`` in this device's map (identity match)."""
        if not self.register_map:
            return None
        for event_name, regs in self.register_map.events.items():
            if any(reg is register for reg in regs):
                return event_name
        return None

    async def _fetch(self, reg_type: str, address: int, count: int) -> list[int] | list[bool] | int:
        """One poll or discovery read: the values, or the exception code the device answered with.

        A demoted device's read is a probe: one attempt, no retries.

        Raises:
            ModbusIOException: no response, or a gateway answering that the unit is absent.
            ConnectionException: the link is down.
        """
        result = await self.connection.request(
            READ_METHODS[reg_type], self.unit_id, self.demoted, address=address, count=count
        )
        if result.isError():
            code = getattr(result, "exception_code", -1)
            if code in GATEWAY_ABSENT:
                raise ModbusIOException(
                    f"gateway: no response from the unit (exception {code:02X})"
                )
            return code
        if reg_type in BIT_REGISTER_TYPES:
            return list(result.bits[:count])
        return list(result.registers)

    async def _action_request(self, method: str, address: int, write: bool, **kwargs: Any) -> Any:
        """One action request at wire ``address``: the response, or RequestFailed with the reason.

        Retried per ``retries`` like any request: FC 5/6/15/16 write absolute
        values, so a repeated write is idempotent.
        """
        try:
            result = await self.connection.request(method, self.unit_id, address=address, **kwargs)
        except ConnectionException:
            reason = f"cannot connect to {self.connection.endpoint}"
        except ModbusIOException:
            reason = NO_RESPONSE + (OUTCOME_UNKNOWN if write else "")
        except ModbusException as e:
            reason = f"modbus error: {e}"
        else:
            if not result.isError():
                return result
            code = getattr(result, "exception_code", -1)
            # 0B: the gateway forwarded the request and the unit never answered.
            reason = refused(code) + (OUTCOME_UNKNOWN if write and code == 0x0B else "")
        logger.warning(f"[{self.path}] {method} at {address + self.address_base}: {reason}")
        raise RequestFailed(reason)

    async def _read_range(self, reg_type: str, address: int, count: int) -> list[int] | list[bool]:
        """Typed read for actions (wire ``address``).

        Raises:
            RequestFailed: with the reason.
        """
        result = await self._action_request(READ_METHODS[reg_type], address, False, count=count)
        if reg_type in BIT_REGISTER_TYPES:
            return list(result.bits[:count])
        return list(result.registers)

    async def _write(self, method: str, address: int, **kwargs: Any) -> bool:
        """One write at wire ``address``; True, or RequestFailed with the reason."""
        await self._action_request(method, address, True, **kwargs)
        return True

    async def write_register(self, address: int, value: int) -> bool:
        """FC 6 at wire ``address``."""
        return await self._write("write_register", address, value=value)

    async def write_registers(self, address: int, values: list[int]) -> bool:
        """FC 16 from wire ``address``."""
        return await self._write("write_registers", address, values=values)

    async def write_coil(self, address: int, value: bool) -> bool:
        """FC 5 at wire ``address``."""
        return await self._write("write_coil", address, value=value)

    async def read_register_value(self, register: Register) -> Any:
        """Read and decode ``register``; None for an ``invalid`` value.

        Raises:
            RequestFailed: with the reason.
        """
        raw = await self._read_range(register.type, register.address, register.address_span)
        value = decode_register(register, raw)
        if register.ref:
            value = apply_scale_ref(value, await self.read_register_value(register.ref))
        return value

    async def write_register_value(self, register: Register, value: float | int | bool) -> bool:
        """Encode and write ``value`` to ``register``; True on success, False if not writable.

        Raises:
            ValueError: the register cannot hold ``value`` exactly (see encode_register).
            RequestFailed: with the reason.
        """
        if not register.writable:
            logger.warning(
                f"[{self.path}] Register '{register.name}' is not writable (type: {register.type})"
            )
            return False

        raw, _ = encode_register(register, value)
        if register.type == RegisterType.COIL:
            return await self.write_coil(register.address, bool(raw[0]))
        if len(raw) == 1 and self.write_mode != WriteMode.FC16:
            return await self.write_register(register.address, raw[0])
        return await self.write_registers(register.address, raw)

    # --- Poll schedule --------------------------------------------------------

    def _schedule(self, now: float) -> list[_Block]:
        """This device's blocks, planned on first use."""
        if self._blocks is None:
            self._plan(now)
        return self._blocks

    def _plan(self, now: float) -> None:
        """Build the read blocks: one plan per rate (blocks never mix rates).

        A scale_ref register is read at the fastest rate of the registers it
        scales. Block size is static (Kepware): a refused block is retried,
        never split.
        """
        rates: dict[int, tuple[Register, float]] = {}
        self._event_of = {}
        for event, regs in self.polled_events.items():
            for reg in regs:
                self._event_of[id(reg)] = event
                rates[id(reg)] = (reg, self.rate_of(reg))
        for reg, rate in list(rates.values()):
            if reg.ref:
                ref_rate = rates.get(id(reg.ref), (None, rate))[1]
                rates[id(reg.ref)] = (reg.ref, min(ref_rate, rate))

        tiers: dict[float, list[Register]] = {}
        for reg, rate in rates.values():
            tiers.setdefault(rate, []).append(reg)
        self._blocks = []
        for rate, regs in sorted(tiers.items()):
            if self.block_reads:
                plan = plan_blocks(
                    regs, self.max_block_size, self.max_read_gap, self.max_bit_block_size
                )
            else:
                plan = [ReadBlock(r.type, r.address, r.address_span, (r,)) for r in regs]
            self._blocks += [_Block(read, rate, now) for read in plan]
        self._tiers = {rate: self._tiers.get(rate) or _Tier(rate) for rate in tiers}

    def _next_due(self) -> float:
        """Monotonic time this device next needs the link (inf: never)."""
        if self.demoted or self.map_pending:
            return self._retry_at
        if self._blocks is None:
            return 0.0
        return min((b.next_due for b in self._blocks), default=math.inf)

    async def _read_block(self, block: _Block, now: float) -> None:
        """Read one block into this tick's values.

        No response (or a gateway's unit-absent answer) counts toward demotion;
        an exception answer is warned once per block (``_failed``). The block
        is next due one rate from now (no backlog after a stall). A failed read
        logs none of its fields.
        """
        if block.last_read is not None and not self.demoted:  # probe gaps are not a rate
            self._sample(block.rate, now - block.last_read, now)
        block.last_read = now
        block.next_due = now + block.rate
        read = block.read
        try:
            raw = await self._fetch(read.type, read.address, read.count)
        except ModbusIOException as e:
            self.failed_reads += 1
            self._timed_out(now, f"No response for {self._range(read)}: {e}")
            return
        self._responded(now)
        if isinstance(raw, list):
            self.successful_reads += 1
            self._pending.update((id(r), (r, v)) for r, v in decode_block(read, raw))
            if block.error is not None:
                block.error = None
                logger.info(f"[{self.path}] {self._range(read)} reads again")
            return
        self.failed_reads += 1
        self._failed(block, now, raw)

    def _range(self, read: ReadBlock) -> str:
        """``holding 40001-40010``: a block's addresses in the map's base."""
        first = read.address + self.address_base
        return f"{read.type} {first}-{first + read.count - 1}"

    def _failed(self, block: _Block, now: float, code: int) -> None:
        """An exception answer for ``block``: warned once per code.

        Illegal address deactivates the block (Kepware): retried every
        REFUSED_RETRY. Block size is static, so one bad address silences its
        whole block; ``verify`` reads register by register to find it. Other
        codes keep the block polled.
        """
        if code in ILLEGAL_ADDRESS:
            block.next_due = now + REFUSED_RETRY
            block.last_read = None  # the retry gap is not a rate
        if code == block.error:
            return
        block.error = code
        if block.refused:
            logger.warning(
                f"[{self.path}] Device refuses {self._range(block.read)} (exception {code:02X}, "
                f"illegal address): not polled, retried every {REFUSED_RETRY / 60:.0f} min. "
                "Run verify to find the bad registers, then fix the map or max_block_size."
            )
        else:
            logger.warning(
                f"[{self.path}] {self._range(block.read)} fails with exception {code:02X}; "
                "still polled, warned once"
            )

    def _timed_out(self, now: float, what: str) -> None:
        """Count a request with no response; demote after demote_after in a row.

        A demoted device is skipped until retry_at, then probed with one
        single-attempt request; each failed probe doubles the wait up to demote_max_s. Polling
        a dead device would otherwise stall every device on the link for
        timeout x (1 + retries) per block.
        """
        if self.demoted:
            self._backoff = min(self._backoff * 2, self.demote_max_s)
            self._retry_at = now + self._backoff
            logger.debug(f"[{self.path}] Probe failed; next in {self._backoff:.0f}s")
            return
        self._timeouts += 1
        logger.warning(f"[{self.path}] {what}")
        if self._timeouts < self.demote_after:
            return
        self._backoff = DEMOTE_BACKOFF
        self._retry_at = now + self._backoff
        logger.warning(
            f"[{self.path}] Demoted after {self._timeouts} requests in a row with no response "
            f"(timeout or gateway exception 0A/0B): skipped, probed once after "
            f"{DEMOTE_BACKOFF:.0f}s, backing off to {self.demote_max_s:.0f}s"
        )
        # A gap while demoted is not an achieved rate.
        for block in self._blocks or []:
            block.last_read = None
        self._tiers = {rate: _Tier(rate) for rate in self._tiers}

    def _responded(self, now: float) -> None:
        """The device answered: clear the timeout count; a demoted device resumes now."""
        if self.demoted:
            logger.info(f"[{self.path}] Answering again; polling resumed")
            for block in self._blocks or []:
                if not block.refused:  # keeps its REFUSED_RETRY backoff
                    block.next_due = min(block.next_due, now)
        self._timeouts = 0
        self._backoff = 0.0
        self._retry_at = 0.0

    def _missed(self, now: float) -> None:
        """The link is down: each read that fell due is a failed read; no rate is achieved.

        A demoted device is only probed, so it misses nothing.
        """
        for block in self._blocks or []:
            if block.next_due <= now and not self.demoted:
                missed = int((now - block.next_due) // block.rate) + 1
                self.failed_reads += missed
                block.next_due += missed * block.rate
            block.last_read = None
        for tier in self._tiers.values():
            tier.interval = tier.since = None

    def _sample(self, rate: float, interval: float, now: float) -> None:
        """Fold one read interval into its tier; log a sustained overload once, then recovery."""
        tier = self._tiers.get(rate)
        if tier is None:
            return  # a block from before a re-plan dropped its tier
        if tier.interval is None:
            tier.interval = interval
        else:
            tier.interval += RATE_EWMA * (interval - tier.interval)
        over = tier.overload_pct > 100
        if over == tier.overloaded:
            tier.since = None
            return
        if tier.since is None:
            tier.since = now
        if now - tier.since < OVERLOAD_SUSTAIN:
            return
        tier.overloaded, tier.since = over, None
        if over:
            logger.warning(
                f"[{self.path}] {rate:g}s registers are read every {tier.interval:.2f}s "
                f"({tier.overload_pct:.0f}% overloaded): the link cannot keep up; "
                "slow some registers down (rate) or poll fewer"
            )
        else:
            logger.info(f"[{self.path}] {rate:g}s registers back on rate ({tier.interval:.2f}s)")

    def _flush(self) -> dict[str, dict[str, Any]]:
        """This tick's values by event, scale refs applied; caches them and clears the tick.

        A scale_ref exponent comes from this tick when its block was read,
        else from its last read (null until it has been read once).
        """
        pending, self._pending = self._pending, {}
        self._decoded.update((key, value) for key, (_, value) in pending.items())
        results: dict[str, dict[str, Any]] = {}
        for key, (reg, value) in pending.items():
            event = self._event_of.get(key)
            if event is None:
                continue  # an unpolled exponent, read only for its registers
            if reg.ref:
                value = apply_scale_ref(value, self._decoded.get(id(reg.ref)))
            results.setdefault(event, {})[reg.field_name] = value
            # Snapshot cache is keyed by the qualified register path, not the
            # sanitized trace field name, so actions address it the same way
            # read_named_register / write_named_register do.
            self.record_value(reg, value, event)
        return results

    def _log_values(self, values: dict[str, dict[str, Any]]) -> None:
        """Log ``{event: {field: value}}`` to this device's trace events."""
        for event_name, event_values in values.items():
            event = self._events.get(event_name)
            if event and event_values:
                event.log(**event_values)
