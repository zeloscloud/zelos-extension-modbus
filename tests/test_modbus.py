"""Tests for Zelos Modbus extension.

Tests core functionality:
- Register map parsing
- Value encoding/decoding
- Simulator physics logic
- Integration tests with demo server
"""

import asyncio
import json
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest

from zelos_extension_modbus.client import (
    ModbusClient,
    _reorder_registers,
    decode_value,
    encode_value,
)
from zelos_extension_modbus.demo.simulator import (
    PowerMeterSimulator,
    create_demo_context,
    float32_to_registers,
    uint32_to_registers,
)
from zelos_extension_modbus.register_map import Register, RegisterMap

# =============================================================================
# Register Map Tests
# =============================================================================


class TestRegister:
    """Test Register dataclass."""

    def test_defaults(self):
        """Minimal required fields use sensible defaults."""
        reg = Register(address=0, name="test")
        assert reg.type == "holding"
        assert reg.datatype == "uint16"
        assert reg.count == 1

    def test_count_by_datatype(self):
        """Register count matches datatype size."""
        assert Register(address=0, name="t", datatype="uint16").count == 1
        assert Register(address=0, name="t", datatype="float32").count == 2
        assert Register(address=0, name="t", datatype="float64").count == 4

    def test_invalid_type_raises(self):
        """Invalid register type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid register type"):
            Register(address=0, name="test", type="invalid")

    def test_invalid_datatype_raises(self):
        """Invalid datatype raises ValueError."""
        with pytest.raises(ValueError, match="Invalid datatype"):
            Register(address=0, name="test", datatype="invalid")

    def test_invalid_byte_order_raises(self):
        """Invalid byte_order raises ValueError."""
        with pytest.raises(ValueError, match="Invalid byte_order"):
            Register(address=0, name="test", byte_order="invalid")

    def test_byte_order_defaults_to_big(self):
        """Default byte order is big endian."""
        reg = Register(address=0, name="test")
        assert reg.byte_order == "big"

    def test_valid_byte_orders(self):
        """All valid byte orders are accepted."""
        for order in ["big", "little", "big_swap", "little_swap"]:
            reg = Register(address=0, name="test", byte_order=order)
            assert reg.byte_order == order

    def test_writable_defaults_true(self):
        """Holding registers and coils are writable by default."""
        assert Register(address=0, name="t", type="holding").writable is True
        assert Register(address=0, name="t", type="coil").writable is True

    def test_input_registers_not_writable(self):
        """Input registers and discrete inputs are read-only."""
        assert Register(address=0, name="t", type="input").writable is False
        assert Register(address=0, name="t", type="discrete_input").writable is False


class TestRegisterMap:
    """Test RegisterMap parsing."""

    def test_from_dict_creates_events(self):
        """Events are correctly parsed from dict."""
        data = {
            "events": {
                "voltage": [{"name": "L1", "address": 0}],
                "current": [{"name": "L1", "address": 6}],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert set(reg_map.event_names) == {"voltage", "current"}
        assert len(reg_map.registers) == 2

    def test_mixed_types_in_event(self):
        """Single event can contain different register types."""
        data = {
            "events": {
                "status": [
                    {"name": "temp", "address": 0, "type": "holding"},
                    {"name": "alarm", "address": 0, "type": "coil"},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        regs = reg_map.get_event("status")
        assert regs[0].type == "holding"
        assert regs[1].type == "coil"

    def test_from_file(self):
        """Register map loads from JSON file."""
        data = {"events": {"test": [{"name": "reg", "address": 0}]}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            reg_map = RegisterMap.from_file(f.name)
        assert len(reg_map.registers) == 1
        Path(f.name).unlink()

    def test_get_by_name(self):
        """Find register by name across events."""
        data = {
            "events": {
                "a": [{"name": "voltage", "address": 0}],
                "b": [{"name": "current", "address": 1}],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert reg_map.get_by_name("voltage").address == 0
        assert reg_map.get_by_name("current").address == 1
        assert reg_map.get_by_name("nonexistent") is None

    def test_writable_registers(self):
        """writable_registers excludes input and discrete_input types."""
        data = {
            "events": {
                "sensors": [
                    {"name": "temp", "address": 0, "type": "holding"},
                    {"name": "sensor", "address": 1, "type": "input"},
                ],
                "controls": [
                    {"name": "relay", "address": 0, "type": "coil"},
                    {"name": "status", "address": 0, "type": "discrete_input"},
                ],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        writable = reg_map.writable_registers
        assert len(writable) == 2
        assert {r.name for r in writable} == {"temp", "relay"}

    def test_byte_order_parsed_from_dict(self):
        """byte_order is correctly parsed from JSON."""
        data = {
            "events": {
                "test": [
                    {"name": "big_val", "address": 0, "byte_order": "big"},
                    {"name": "swapped", "address": 2, "byte_order": "big_swap"},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert reg_map.get_by_name("big_val").byte_order == "big"
        assert reg_map.get_by_name("swapped").byte_order == "big_swap"


# =============================================================================
# Value Encoding/Decoding Tests
# =============================================================================


class TestValueCodec:
    """Test value encoding and decoding."""

    @pytest.mark.parametrize(
        "datatype,raw,expected",
        [
            ("uint16", [1000], 1000),
            ("int16", [65535], -1),
            ("int16", [32768], -32768),
            ("bool", [1], True),
            ("bool", [0], False),
        ],
    )
    def test_decode_basic(self, datatype, raw, expected):
        """Basic decoding for single-register types."""
        assert decode_value(raw, datatype) == expected

    def test_decode_uint32(self):
        """32-bit values span two registers."""
        # 0x00010000 = 65536
        assert decode_value([0x0001, 0x0000], "uint32") == 65536

    def test_decode_float32(self):
        """IEEE 754 float32 decoding."""
        # 3.14 ≈ 0x4048F5C3
        result = decode_value([0x4048, 0xF5C3], "float32")
        assert abs(result - 3.14) < 0.01

    def test_decode_with_scale(self):
        """Scale factor is applied after decoding."""
        assert decode_value([1000], "uint16", scale=0.1) == 100

    @pytest.mark.parametrize(
        "datatype,value,expected",
        [
            ("uint16", 1000, [1000]),
            ("int16", -1, [65535]),
            ("bool", True, [1]),
            ("bool", False, [0]),
        ],
    )
    def test_encode_basic(self, datatype, value, expected):
        """Basic encoding for single-register types."""
        assert encode_value(value, datatype) == expected

    def test_encode_uint32(self):
        """32-bit values encode to two registers."""
        assert encode_value(65536, "uint32") == [0x0001, 0x0000]

    def test_encode_with_scale(self):
        """Scale factor is applied before encoding."""
        assert encode_value(100, "uint16", scale=0.1) == [1000]

    def test_roundtrip(self):
        """Encode then decode returns original value."""
        for value, datatype in [(1234, "uint16"), (-100, "int16"), (100000, "uint32")]:
            encoded = encode_value(value, datatype)
            decoded = decode_value(encoded, datatype)
            assert decoded == value


class TestByteOrder:
    """Test byte order handling for multi-register values."""

    def test_reorder_single_register_unchanged(self):
        """Single register values are unchanged by byte order."""
        regs = [0x1234]
        for order in ["big", "little", "big_swap", "little_swap"]:
            assert _reorder_registers(regs, order) == [0x1234]

    def test_reorder_big_endian(self):
        """Big endian keeps registers in original order."""
        regs = [0xABCD, 0xEF01]
        assert _reorder_registers(regs, "big") == [0xABCD, 0xEF01]

    def test_reorder_little_endian(self):
        """Little endian reverses register order."""
        regs = [0xABCD, 0xEF01]
        assert _reorder_registers(regs, "little") == [0xEF01, 0xABCD]

    def test_reorder_big_swap(self):
        """Big swap swaps word pairs."""
        regs = [0xABCD, 0xEF01]
        assert _reorder_registers(regs, "big_swap") == [0xEF01, 0xABCD]

    def test_reorder_little_swap_32bit(self):
        """Little swap on 32-bit: BA DC."""
        regs = [0xABCD, 0xEF01]
        assert _reorder_registers(regs, "little_swap") == [0xEF01, 0xABCD]

    def test_reorder_64bit_little(self):
        """Little endian on 64-bit reverses all four registers."""
        regs = [0x0001, 0x0002, 0x0003, 0x0004]
        assert _reorder_registers(regs, "little") == [0x0004, 0x0003, 0x0002, 0x0001]

    def test_decode_float32_big_swap(self):
        """Decode float32 with word-swapped byte order."""
        # 3.14 in big endian is [0x4048, 0xF5C3]
        # Word swapped would be [0xF5C3, 0x4048]
        result = decode_value([0xF5C3, 0x4048], "float32", byte_order="big_swap")
        assert abs(result - 3.14) < 0.01

    def test_encode_uint32_big_swap(self):
        """Encode uint32 with word-swapped byte order."""
        # 65536 (0x00010000) in big endian is [0x0001, 0x0000]
        # Word swapped would be [0x0000, 0x0001]
        result = encode_value(65536, "uint32", byte_order="big_swap")
        assert result == [0x0000, 0x0001]

    def test_roundtrip_all_byte_orders(self):
        """Encode then decode with same byte order returns original."""
        for order in ["big", "little", "big_swap", "little_swap"]:
            encoded = encode_value(123456, "uint32", byte_order=order)
            decoded = decode_value(encoded, "uint32", byte_order=order)
            assert decoded == 123456, f"Failed for byte_order={order}"


# =============================================================================
# Simulator Tests (no network)
# =============================================================================


class TestSimulatorHelpers:
    """Test simulator helper functions."""

    def test_float32_to_registers(self):
        """Float32 converts to two big-endian registers."""
        r1, r2 = float32_to_registers(3.14)
        # Reconstruct and verify
        packed = struct.pack(">HH", r1, r2)
        result = struct.unpack(">f", packed)[0]
        assert abs(result - 3.14) < 0.01

    def test_uint32_to_registers(self):
        """Uint32 converts to two big-endian registers."""
        r1, r2 = uint32_to_registers(65536)
        assert r1 == 0x0001
        assert r2 == 0x0000


class TestPowerMeterSimulator:
    """Test simulator physics logic."""

    def test_update_returns_all_fields(self):
        """Update returns complete value dictionary."""
        sim = PowerMeterSimulator()
        values = sim.update(dt=0.1)

        # Check all expected fields exist
        expected = {
            "voltage_l1",
            "voltage_l2",
            "voltage_l3",
            "current_l1",
            "current_l2",
            "current_l3",
            "power_total",
            "power_factor",
            "frequency",
            "energy_total",
            "temperature",
            "relay1",
            "relay2",
            "alarm",
        }
        assert set(values.keys()) == expected

    def test_voltage_near_nominal(self):
        """Voltage stays within ±5% of nominal."""
        sim = PowerMeterSimulator()
        values = sim.update(dt=0.1)

        for phase in ["voltage_l1", "voltage_l2", "voltage_l3"]:
            v = values[phase]
            assert 218 < v < 242  # 230V ±5%

    def test_frequency_near_nominal(self):
        """Frequency stays near 50Hz."""
        sim = PowerMeterSimulator()
        values = sim.update(dt=0.1)
        assert 49.9 < values["frequency"] < 50.1

    def test_energy_accumulates(self):
        """Energy increases over time."""
        sim = PowerMeterSimulator()
        sim.update(dt=1.0)
        e1 = sim.energy_total
        sim.update(dt=1.0)
        e2 = sim.energy_total
        assert e2 > e1

    def test_power_factor_in_range(self):
        """Power factor stays in valid range."""
        sim = PowerMeterSimulator()
        values = sim.update(dt=0.1)
        assert 0.7 < values["power_factor"] < 1.0


# =============================================================================
# Integration Tests with Demo Server
# =============================================================================


class DemoServer:
    """Helper to run demo server in background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 15020):
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server_task = None

    def start(self):
        """Start server in background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        time.sleep(0.5)

    def _run(self):
        """Run server event loop."""
        from pymodbus.server import StartAsyncTcpServer

        from zelos_extension_modbus.demo.simulator import SimulatorUpdater

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        context = create_demo_context()
        simulator = PowerMeterSimulator()
        updater = SimulatorUpdater(simulator, context, interval=0.05)
        updater.start()

        async def run_server():
            await StartAsyncTcpServer(context=context, address=(self.host, self.port))

        try:
            self._loop.run_until_complete(run_server())
        except Exception:
            pass
        finally:
            updater.stop()

    def stop(self):
        """Stop the server."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture(scope="module")
def demo_server():
    """Fixture that starts demo server for integration tests."""
    server = DemoServer(port=15020)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def register_map():
    """Load the demo power meter register map."""
    map_path = Path(__file__).parent.parent / "zelos_extension_modbus" / "demo" / "power_meter.json"
    return RegisterMap.from_file(str(map_path))


@pytest.fixture
def client(demo_server, register_map):
    """Create a connected ModbusClient."""
    client = ModbusClient(
        host=demo_server.host,
        port=demo_server.port,
        register_map=register_map,
    )

    async def connect():
        await client.connect()

    asyncio.get_event_loop().run_until_complete(connect())
    yield client

    async def disconnect():
        await client.disconnect()

    asyncio.get_event_loop().run_until_complete(disconnect())


class TestDemoServerIntegration:
    """Integration tests against the demo server."""

    def test_read_holding_register_float32(self, client):
        """Read float32 holding register (voltage)."""
        reg = client.register_map.get_by_name("L1")  # voltage L1
        assert reg is not None
        assert reg.type == "holding"
        assert reg.datatype == "float32"

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert 200 < value < 260  # Reasonable voltage range

    def test_read_holding_register_uint32(self, client):
        """Read uint32 holding register (energy)."""
        reg = client.register_map.get_by_name("energy")
        assert reg is not None
        assert reg.datatype == "uint32"

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert isinstance(value, int)
        assert value >= 0

    def test_read_holding_register_int16_with_scale(self, client):
        """Read int16 holding register with scale (temperature)."""
        reg = client.register_map.get_by_name("temperature")
        assert reg is not None
        assert reg.datatype == "int16"
        assert reg.scale == 0.1

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        # Temperature should be reasonable (raw value is scaled by 0.1)
        assert 0 < value < 100

    def test_read_input_register(self, client):
        """Read input register (firmware_version)."""
        reg = client.register_map.get_by_name("firmware_version")
        assert reg is not None
        assert reg.type == "input"
        assert reg.writable is False

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        # Firmware version 0x0102 = 258
        assert value == 0x0102

    def test_read_input_register_uint32(self, client):
        """Read uint32 input register (serial_number)."""
        reg = client.register_map.get_by_name("serial_number")
        assert reg is not None
        assert reg.type == "input"
        assert reg.datatype == "uint32"

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value == 12345678

    def test_read_coil(self, client):
        """Read coil register."""
        reg = client.register_map.get_by_name("relay1")
        assert reg is not None
        assert reg.type == "coil"
        assert reg.writable is True

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value in (True, False)

    def test_read_discrete_input(self, client):
        """Read discrete input register."""
        reg = client.register_map.get_by_name("grid_connected")
        assert reg is not None
        assert reg.type == "discrete_input"
        assert reg.writable is False

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        # Initial value is True (grid connected)
        assert value is True

    def test_read_swapped_float(self, client):
        """Read float32 with big_swap byte order."""
        reg = client.register_map.get_by_name("calibration_factor")
        assert reg is not None
        assert reg.datatype == "float32"
        assert reg.byte_order == "big_swap"

        async def read():
            return await client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        # Initial value is 1.0
        assert value is not None
        assert abs(value - 1.0) < 0.01

    def test_write_holding_register_uint16(self, client):
        """Write uint16 holding register."""
        reg = client.register_map.get_by_name("voltage_high_limit")
        assert reg is not None
        assert reg.type == "holding"
        assert reg.datatype == "uint16"
        assert reg.writable is True

        async def write_and_read():
            # Write new value
            success = await client.write_register_value(reg, 245)
            assert success is True

            # Read back
            value = await client.read_register_value(reg)
            return value

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert value == 245

    def test_write_holding_register_int32(self, client):
        """Write int32 holding register."""
        reg = client.register_map.get_by_name("power_limit")
        assert reg is not None
        assert reg.datatype == "int32"
        assert reg.writable is True

        async def write_and_read():
            # Write new value (negative to test signed)
            success = await client.write_register_value(reg, -10000)
            assert success is True

            # Read back
            value = await client.read_register_value(reg)
            return value

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert value == -10000

    def test_write_coil(self, client):
        """Write coil register."""
        reg = client.register_map.get_by_name("relay1")
        assert reg is not None
        assert reg.type == "coil"

        async def write_and_read():
            # Write True
            success = await client.write_register_value(reg, True)
            assert success is True
            value = await client.read_register_value(reg)
            assert value is True

            # Write False
            success = await client.write_register_value(reg, False)
            assert success is True
            value = await client.read_register_value(reg)
            assert value is False

        asyncio.get_event_loop().run_until_complete(write_and_read())

    def test_write_swapped_float(self, client):
        """Write float32 with big_swap byte order."""
        reg = client.register_map.get_by_name("offset_value")
        assert reg is not None
        assert reg.byte_order == "big_swap"

        async def write_and_read():
            # Write a specific value
            success = await client.write_register_value(reg, 3.14159)
            assert success is True

            # Read back
            value = await client.read_register_value(reg)
            return value

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert abs(value - 3.14159) < 0.001

    def test_write_input_register_fails(self, client):
        """Writing to input register should fail."""
        reg = client.register_map.get_by_name("firmware_version")
        assert reg is not None
        assert reg.type == "input"
        assert reg.writable is False

        async def try_write():
            return await client.write_register_value(reg, 999)

        success = asyncio.get_event_loop().run_until_complete(try_write())
        assert success is False

    def test_write_discrete_input_fails(self, client):
        """Writing to discrete input should fail."""
        reg = client.register_map.get_by_name("door_open")
        assert reg is not None
        assert reg.type == "discrete_input"
        assert reg.writable is False

        async def try_write():
            return await client.write_register_value(reg, True)

        success = asyncio.get_event_loop().run_until_complete(try_write())
        assert success is False

    def test_poll_all_events(self, client):
        """Poll all registers and verify event structure."""

        async def poll():
            return await client._poll_registers()

        results = asyncio.get_event_loop().run_until_complete(poll())

        # Should have all events from register map
        assert "voltage" in results
        assert "current" in results
        assert "power" in results
        assert "status" in results
        assert "inputs" in results
        assert "digital_inputs" in results
        assert "setpoints" in results
        assert "swapped_floats" in results

        # Voltage event should have L1, L2, L3
        assert "L1" in results["voltage"]
        assert "L2" in results["voltage"]
        assert "L3" in results["voltage"]

        # Check values are reasonable
        assert 200 < results["voltage"]["L1"] < 260


# =============================================================================
# Action Tests
# =============================================================================


class TestReconnection:
    """Tests for connection error detection."""

    def test_is_connection_error_timeout(self):
        """Timeout errors are detected as connection errors."""
        client = ModbusClient()
        assert client._is_connection_error(Exception("Connection timeout")) is True
        assert client._is_connection_error(Exception("No response received")) is True

    def test_is_connection_error_refused(self):
        """Connection refused errors are detected."""
        client = ModbusClient()
        assert client._is_connection_error(Exception("Connection refused")) is True
        assert client._is_connection_error(Exception("connection reset by peer")) is True

    def test_is_connection_error_false_for_other(self):
        """Non-connection errors return False."""
        client = ModbusClient()
        assert client._is_connection_error(Exception("Invalid address")) is False
        assert client._is_connection_error(Exception("Value out of range")) is False
        assert client._is_connection_error(ValueError("bad value")) is False


class TestActionsUnit:
    """Unit tests for SDK actions (no network)."""

    @pytest.fixture
    def client_with_map(self):
        """Create client with register map but no connection."""
        data = {
            "name": "test_device",
            "events": {
                "sensors": [
                    {"name": "temp", "address": 0, "type": "holding", "datatype": "uint16"},
                    {"name": "humidity", "address": 1, "type": "input", "datatype": "uint16"},
                ],
                "controls": [
                    {"name": "relay", "address": 0, "type": "coil"},
                    {"name": "setpoint", "address": 10, "type": "holding", "datatype": "float32"},
                ],
            },
        }
        reg_map = RegisterMap.from_dict(data)
        return ModbusClient(register_map=reg_map)

    def test_get_status_returns_info(self, client_with_map):
        """Get Status action returns expected fields."""
        result = client_with_map.get_status()
        assert "connected" in result
        assert "transport" in result
        assert "unit_id" in result
        assert "poll_count" in result
        assert "registers" in result
        assert result["registers"] == 4

    def test_list_registers_returns_all(self, client_with_map):
        """List Registers action returns all registers."""
        result = client_with_map.list_registers()
        assert result["count"] == 4
        names = [r["name"] for r in result["registers"]]
        assert "temp" in names
        assert "humidity" in names
        assert "relay" in names
        assert "setpoint" in names

    def test_list_writable_registers_filters(self, client_with_map):
        """List Writable Registers only returns writable ones."""
        result = client_with_map.list_writable_registers()
        # holding and coil are writable, input is not
        assert result["count"] == 3
        names = [r["name"] for r in result["registers"]]
        assert "temp" in names
        assert "relay" in names
        assert "setpoint" in names
        assert "humidity" not in names  # input register, not writable

    def test_list_registers_no_map(self):
        """List Registers with no map returns empty."""
        client = ModbusClient()
        result = client.list_registers()
        assert result["count"] == 0
        assert result["registers"] == []

    def test_list_writable_no_map(self):
        """List Writable with no map returns empty."""
        client = ModbusClient()
        result = client.list_writable_registers()
        assert result["count"] == 0

    def test_read_named_no_map(self):
        """Read Named Register with no map returns error."""
        client = ModbusClient()
        result = client.read_named_register("anything")
        assert result["success"] is False
        assert "error" in result

    def test_read_named_not_found(self, client_with_map):
        """Read Named Register with unknown name returns error."""
        result = client_with_map.read_named_register("nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_no_map(self):
        """Write Named Register with no map returns error."""
        client = ModbusClient()
        result = client.write_named_register("anything", 100)
        assert result["success"] is False
        assert "error" in result

    def test_write_named_not_found(self, client_with_map):
        """Write Named Register with unknown name returns error."""
        result = client_with_map.write_named_register("nonexistent", 100)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_not_writable(self, client_with_map):
        """Write Named Register to input register returns error."""
        result = client_with_map.write_named_register("humidity", 100)
        assert result["success"] is False
        assert "not writable" in result["error"]


class TestActionsIntegration:
    """Integration tests for SDK actions with demo server.

    Tests actions that don't require network (list actions) and validates
    action response structure. Network read/write is tested in
    TestDemoServerIntegration.
    """

    def test_list_registers_action(self, client):
        """List Registers action returns all demo registers."""
        result = client.list_registers()
        assert result["count"] > 0
        names = [r["name"] for r in result["registers"]]
        assert "L1" in names
        assert "relay1" in names
        assert "firmware_version" in names

    def test_list_writable_action(self, client):
        """List Writable action excludes input/discrete registers."""
        result = client.list_writable_registers()
        names = [r["name"] for r in result["registers"]]
        assert "voltage_high_limit" in names
        assert "relay1" in names
        assert "firmware_version" not in names
        assert "door_open" not in names

    def test_get_status_action(self, client):
        """Get Status action returns info."""
        result = client.get_status()
        assert result["connected"] is True
        assert result["transport"] == "tcp"
        assert result["registers"] > 0

    def test_write_named_readonly_fails(self, client):
        """Write Named to input register fails gracefully."""
        result = client.write_named_register("firmware_version", 999)
        assert result["success"] is False
        assert "not writable" in result["error"]
