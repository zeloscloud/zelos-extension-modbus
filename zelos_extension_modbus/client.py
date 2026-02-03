"""Core Modbus client wrapper with Zelos SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Any

import zelos_sdk
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from zelos_extension_modbus.register_map import Register, RegisterMap

logger = logging.getLogger(__name__)


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

    if byte_order == "big":
        # Standard Modbus: AB CD (no change)
        pass
    elif byte_order == "little":
        # Full little endian: DC BA (reverse all)
        regs = regs[::-1]
    elif byte_order == "big_swap":
        # Big endian with word swap: CD AB (swap pairs)
        if len(regs) == 2:
            regs = [regs[1], regs[0]]
        elif len(regs) == 4:
            regs = [regs[1], regs[0], regs[3], regs[2]]
    elif byte_order == "little_swap":
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


class ModbusClient:
    """Modbus client with polling and Zelos SDK integration."""

    def __init__(
        self,
        transport: str = "tcp",
        host: str = "127.0.0.1",
        port: int = 502,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 9600,
        unit_id: int = 1,
        timeout: float = 3.0,
        register_map: RegisterMap | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        """Initialize Modbus client.

        Args:
            transport: 'tcp' or 'rtu'
            host: TCP host address
            port: TCP port
            serial_port: Serial port for RTU
            baudrate: Serial baudrate for RTU
            unit_id: Modbus slave/unit ID
            timeout: Request timeout in seconds
            register_map: Optional register map for named access
            poll_interval: Polling interval in seconds
        """
        self.transport = transport
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.unit_id = unit_id
        self.timeout = timeout
        self.register_map = register_map
        self.poll_interval = poll_interval

        self._client: AsyncModbusTcpClient | AsyncModbusSerialClient | None = None
        self._running = False
        self._connected = False
        self._poll_count = 0
        self._error_count = 0

        # Zelos SDK trace source
        self._source: zelos_sdk.TraceSourceCacheLast | None = None
        self._schema_emitted = False

    def _create_client(self) -> AsyncModbusTcpClient | AsyncModbusSerialClient:
        """Create the appropriate Modbus client."""
        if self.transport == "tcp":
            return AsyncModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
            )
        else:  # rtu
            return AsyncModbusSerialClient(
                port=self.serial_port,
                baudrate=self.baudrate,
                timeout=self.timeout,
            )

    def _init_trace_source(self) -> None:
        """Initialize Zelos trace source and define schema from register map."""
        source_name = self.register_map.name if self.register_map else "modbus"
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

        # Create events from user-defined event names
        for event_name, regs in self.register_map.events.items():
            if not regs:
                continue

            fields = []
            for reg in regs:
                dtype = self._get_sdk_datatype(reg.datatype)
                fields.append(zelos_sdk.TraceEventFieldMetadata(reg.name, dtype, reg.unit))

            self._source.add_event(event_name, fields)

    def _get_sdk_datatype(self, datatype: str) -> zelos_sdk.DataType:
        """Map register datatype to Zelos SDK DataType."""
        mapping = {
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
        return mapping.get(datatype, zelos_sdk.DataType.Int32)

    async def connect(self) -> bool:
        """Connect to Modbus device.

        Returns:
            True if connected successfully
        """
        try:
            self._client = self._create_client()
            await self._client.connect()
            self._connected = self._client.connected
            if self._connected:
                logger.info(f"Connected to Modbus {self.transport}://{self._connection_str}")
            else:
                logger.error(f"Failed to connect to {self._connection_str}")
            return self._connected
        except Exception as e:
            logger.error(f"Connection error: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from Modbus device."""
        if self._client:
            self._client.close()
            self._connected = False
            logger.info("Disconnected from Modbus device")

    @property
    def _connection_str(self) -> str:
        """Get connection string for logging."""
        if self.transport == "tcp":
            return f"{self.host}:{self.port}"
        return f"{self.serial_port}@{self.baudrate}"

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
        if register.type == "holding":
            raw = await self.read_holding_registers(register.address, register.count)
        elif register.type == "input":
            raw = await self.read_input_registers(register.address, register.count)
        elif register.type == "coil":
            result = await self.read_coils(register.address, 1)
            return result[0] if result else None
        elif register.type == "discrete_input":
            result = await self.read_discrete_inputs(register.address, 1)
            return result[0] if result else None
        else:
            return None

        if raw is None:
            return None

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

        if register.type == "coil":
            return await self.write_coil(register.address, bool(value))

        raw = encode_value(value, register.datatype, register.scale, register.byte_order)

        if len(raw) == 1:
            return await self.write_register(register.address, raw[0])
        else:
            return await self.write_registers(register.address, raw)

    async def _poll_registers(self) -> dict[str, dict[str, Any]]:
        """Poll all registers in the register map.

        Returns:
            Dictionary of {event_name: {field_name: value}}
        """
        if not self.register_map:
            return {}

        results: dict[str, dict[str, Any]] = {}

        for event_name, regs in self.register_map.events.items():
            event_results: dict[str, Any] = {}
            for reg in regs:
                value = await self.read_register_value(reg)
                if value is not None:
                    event_results[reg.name] = value

            if event_results:
                results[event_name] = event_results

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

                await asyncio.sleep(self.poll_interval)
        finally:
            await self.disconnect()

    def _is_connection_error(self, error: Exception) -> bool:
        """Check if an exception indicates a connection problem."""
        error_str = str(error).lower()
        connection_indicators = [
            "connection",
            "timeout",
            "refused",
            "reset",
            "broken pipe",
            "no response",
            "disconnected",
            "not connected",
        ]
        return any(ind in error_str for ind in connection_indicators)

    # SDK Action methods
    @zelos_sdk.action("Get Status", "Get connection and polling status")
    def get_status(self) -> dict[str, Any]:
        """Get current client status."""
        return {
            "connected": self._connected,
            "transport": self.transport,
            "connection": self._connection_str,
            "unit_id": self.unit_id,
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "poll_interval": self.poll_interval,
            "registers": len(self.register_map.registers) if self.register_map else 0,
        }

    @zelos_sdk.action("Read Register", "Read a single register by address")
    @zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
    @zelos_sdk.action.select(
        "reg_type",
        choices=["holding", "input", "coil", "discrete_input"],
        default="holding",
        title="Register Type",
    )
    @zelos_sdk.action.number("count", minimum=1, maximum=125, default=1, title="Count")
    def read_register_action(self, address: int, reg_type: str, count: int) -> dict[str, Any]:
        """Read register(s) by address."""

        async def _read() -> list | None:
            if not self._connected:
                await self.connect()
            if reg_type == "holding":
                return await self.read_holding_registers(int(address), int(count))
            elif reg_type == "input":
                return await self.read_input_registers(int(address), int(count))
            elif reg_type == "coil":
                return await self.read_coils(int(address), int(count))
            else:  # discrete_input
                return await self.read_discrete_inputs(int(address), int(count))

        result = asyncio.run(_read())
        return {
            "address": address,
            "type": reg_type,
            "count": count,
            "values": result,
            "success": result is not None,
        }

    @zelos_sdk.action("Write Register", "Write a value to a holding register")
    @zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
    @zelos_sdk.action.number("value", title="Value")
    def write_register_action(self, address: int, value: int) -> dict[str, Any]:
        """Write a single register."""

        async def _write() -> bool:
            if not self._connected:
                await self.connect()
            return await self.write_register(int(address), int(value))

        success = asyncio.run(_write())
        return {
            "address": address,
            "value": value,
            "success": success,
        }

    @zelos_sdk.action("Read Named Register", "Read a register by name from the map")
    @zelos_sdk.action.text("name", title="Register Name")
    def read_named_register(self, name: str) -> dict[str, Any]:
        """Read a register by name from the register map."""
        if not self.register_map:
            return {"error": "No register map loaded", "success": False}

        reg = self.register_map.get_by_name(name)
        if not reg:
            return {"error": f"Register '{name}' not found", "success": False}

        async def _read() -> Any:
            if not self._connected:
                await self.connect()
            return await self.read_register_value(reg)

        value = asyncio.run(_read())
        return {
            "name": name,
            "address": reg.address,
            "type": reg.type,
            "datatype": reg.datatype,
            "value": value,
            "unit": reg.unit,
            "success": value is not None,
        }

    @zelos_sdk.action("List Registers", "List all registers in the map")
    def list_registers(self) -> dict[str, Any]:
        """List all registers in the register map."""
        if not self.register_map:
            return {"registers": [], "count": 0}

        regs = [
            {
                "name": r.name,
                "address": r.address,
                "type": r.type,
                "datatype": r.datatype,
                "unit": r.unit,
                "writable": r.writable,
                "byte_order": r.byte_order,
            }
            for r in self.register_map.registers
        ]
        return {"registers": regs, "count": len(regs)}

    @zelos_sdk.action("Write Named Register", "Write a value to a register by name")
    @zelos_sdk.action.text("name", title="Register Name")
    @zelos_sdk.action.number("value", title="Value")
    def write_named_register(self, name: str, value: float) -> dict[str, Any]:
        """Write a value to a register by name from the register map."""
        if not self.register_map:
            return {"error": "No register map loaded", "success": False}

        reg = self.register_map.get_by_name(name)
        if not reg:
            return {"error": f"Register '{name}' not found", "success": False}

        if not reg.writable:
            return {
                "error": f"Register '{name}' is not writable (type: {reg.type})",
                "success": False,
            }

        async def _write() -> bool:
            if not self._connected:
                await self.connect()
            return await self.write_register_value(reg, value)

        success = asyncio.run(_write())
        return {
            "name": name,
            "address": reg.address,
            "type": reg.type,
            "datatype": reg.datatype,
            "value": value,
            "unit": reg.unit,
            "success": success,
        }

    @zelos_sdk.action("Write Coil", "Write a boolean value to a coil")
    @zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
    @zelos_sdk.action.select("value", choices=["ON", "OFF"], default="OFF", title="Value")
    def write_coil_action(self, address: int, value: str) -> dict[str, Any]:
        """Write a coil by address."""
        bool_value = value == "ON"

        async def _write() -> bool:
            if not self._connected:
                await self.connect()
            return await self.write_coil(int(address), bool_value)

        success = asyncio.run(_write())
        return {
            "address": address,
            "value": bool_value,
            "success": success,
        }

    @zelos_sdk.action("List Writable Registers", "List all writable registers")
    def list_writable_registers(self) -> dict[str, Any]:
        """List all writable registers in the register map."""
        if not self.register_map:
            return {"registers": [], "count": 0}

        regs = [
            {
                "name": r.name,
                "address": r.address,
                "type": r.type,
                "datatype": r.datatype,
                "unit": r.unit,
                "byte_order": r.byte_order,
            }
            for r in self.register_map.writable_registers
        ]
        return {"registers": regs, "count": len(regs)}
