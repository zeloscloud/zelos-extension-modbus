"""Tests for Zelos Modbus extension.

Tests core functionality:
- Register map parsing
- Value encoding/decoding
- Simulator physics logic
- Integration tests with demo server
"""

import asyncio
import contextlib
import json
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from zelos_extension_modbus import actions, registry
from zelos_extension_modbus.blocks import ReadBlock, plan_blocks
from zelos_extension_modbus.client import (
    ModbusClient,
    _reorder_registers,
    decode_block,
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

    def test_name_defaults_to_r_address(self):
        """Omitting name defaults it to r<address>."""
        assert Register(address=42).name == "r42"
        assert Register(address=7, name="").name == "r7"

    def test_poll_interval_defaults_none(self):
        """poll_interval defaults to None (use interface interval)."""
        assert Register(address=0, name="t").poll_interval is None

    def test_poll_interval_positive_ok(self):
        """A positive poll_interval is accepted."""
        assert Register(address=0, name="t", poll_interval=5.0).poll_interval == 5.0

    def test_poll_interval_zero_disables(self):
        """A poll_interval of 0 is accepted and disables polling."""
        reg = Register(address=0, name="t", poll_interval=0)
        assert reg.poll_interval == 0
        assert reg.polled is False

    def test_polled_property(self):
        """polled is True unless poll_interval is exactly 0."""
        assert Register(address=0, name="t").polled is True  # None -> interface rate
        assert Register(address=0, name="t", poll_interval=5.0).polled is True
        assert Register(address=0, name="t", poll_interval=0).polled is False

    def test_poll_interval_negative_raises(self):
        """A negative poll_interval raises ValueError."""
        with pytest.raises(ValueError, match="poll_interval must be >= 0"):
            Register(address=0, name="t", poll_interval=-1.0)
        with pytest.raises(ValueError, match="poll_interval must be >= 0"):
            Register(address=0, name="t", poll_interval=-0.5)


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

    def test_from_dict_name_optional(self):
        """A register with no name defaults to r<address>."""
        data = {"events": {"e": [{"address": 5}, {"address": 6}]}}
        reg_map = RegisterMap.from_dict(data)
        names = [r.name for r in reg_map.get_event("e")]
        assert names == ["r5", "r6"]

    def test_poll_interval_parsed_from_dict(self):
        """poll_interval is parsed from JSON when present."""
        data = {
            "events": {
                "e": [
                    {"name": "fast", "address": 0},
                    {"name": "slow", "address": 1, "poll_interval": 5.0},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert reg_map.get_by_name("fast").poll_interval is None
        assert reg_map.get_by_name("slow").poll_interval == 5.0

    def test_poll_interval_zero_from_dict_disables(self):
        """poll_interval: 0 in JSON parses as disabled."""
        data = {"events": {"e": [{"name": "x", "address": 0, "poll_interval": 0}]}}
        reg = RegisterMap.from_dict(data).get_by_name("x")
        assert reg.poll_interval == 0
        assert reg.polled is False

    def test_poll_interval_null_from_dict_disables(self):
        """Explicit poll_interval: null in JSON is translated to 0 (disabled)."""
        data = {"events": {"e": [{"name": "x", "address": 0, "poll_interval": None}]}}
        reg = RegisterMap.from_dict(data).get_by_name("x")
        assert reg.poll_interval == 0.0
        assert reg.polled is False

    def test_poll_interval_absent_defaults_none(self):
        """An absent poll_interval key keeps the interface-default rate (None)."""
        data = {"events": {"e": [{"name": "x", "address": 0}]}}
        reg = RegisterMap.from_dict(data).get_by_name("x")
        assert reg.poll_interval is None
        assert reg.polled is True

    def test_poll_interval_negative_from_dict_raises(self):
        """A negative poll_interval in JSON raises ValueError at load."""
        for bad in (-1, -0.5):
            with pytest.raises(ValueError, match="poll_interval must be >= 0"):
                RegisterMap.from_dict(
                    {"events": {"e": [{"name": "x", "address": 0, "poll_interval": bad}]}}
                )

    def test_duplicate_field_names_raise(self):
        """Raw duplicate field names within an event raise ValueError."""
        data = {"events": {"e": [{"name": "v", "address": 0}, {"name": "v", "address": 1}]}}
        with pytest.raises(ValueError, match="Duplicate field name 'v' in event 'e'"):
            RegisterMap.from_dict(data)

    def test_sanitized_field_name_collision_raises(self):
        """Names that collide after sanitization (a.b vs a_b) raise ValueError."""
        data = {"events": {"e": [{"name": "a.b", "address": 0}, {"name": "a_b", "address": 1}]}}
        with pytest.raises(ValueError, match="Duplicate field name 'a_b' in event 'e'"):
            RegisterMap.from_dict(data)

    def test_distinct_events_same_field_name_ok(self):
        """The same field name in distinct events loads fine."""
        data = {
            "events": {
                "a": [{"name": "value", "address": 0}],
                "b": [{"name": "value", "address": 1}],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert len(reg_map.registers) == 2


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
# Block Planner Tests (pure)
# =============================================================================


def _block_tuples(blocks):
    """Summarize blocks as (type, address, count) for easy assertion."""
    return [(b.type, b.address, b.count) for b in blocks]


class TestBlockPlanner:
    """Test the pure block-read planner."""

    def test_contiguous_coalescing(self):
        """Adjacent same-type registers merge into one block."""
        regs = [
            Register(address=0, name="a", datatype="float32"),
            Register(address=2, name="b", datatype="float32"),
            Register(address=4, name="c", datatype="float32"),
        ]
        assert _block_tuples(plan_blocks(regs)) == [("holding", 0, 6)]

    def test_span_never_split_float32(self):
        """A float32 (2 registers) is always kept whole within its block."""
        regs = [Register(address=0, name="a", datatype="float32")]
        blocks = plan_blocks(regs)
        assert _block_tuples(blocks) == [("holding", 0, 2)]

    def test_oversized_span_single_block(self):
        """A span wider than max_block_size yields one oversized block, not a failure."""
        regs = [Register(address=0, name="a", datatype="float32")]
        blocks = plan_blocks(regs, max_block_size=1)
        assert _block_tuples(blocks) == [("holding", 0, 2)]

    def test_max_block_size_splitting(self):
        """Blocks split when they would exceed max_block_size."""
        regs = [Register(address=a, name=f"r{a}") for a in range(4)]
        blocks = plan_blocks(regs, max_block_size=2)
        assert _block_tuples(blocks) == [("holding", 0, 2), ("holding", 2, 2)]

    def test_gap_zero_splits(self):
        """With max_read_gap=0 a one-address hole starts a new block."""
        regs = [Register(address=0, name="a"), Register(address=2, name="b")]
        blocks = plan_blocks(regs, max_read_gap=0)
        assert _block_tuples(blocks) == [("holding", 0, 1), ("holding", 2, 1)]

    def test_gap_bridged(self):
        """A gap within max_read_gap is bridged into one block."""
        regs = [Register(address=0, name="a"), Register(address=2, name="b")]
        blocks = plan_blocks(regs, max_read_gap=1)
        assert _block_tuples(blocks) == [("holding", 0, 3)]

    def test_duplicate_addresses_share_block(self):
        """Duplicate/overlapping addresses merge into a single block."""
        regs = [Register(address=5, name="a"), Register(address=5, name="b")]
        blocks = plan_blocks(regs)
        assert _block_tuples(blocks) == [("holding", 5, 1)]
        assert len(blocks[0].registers) == 2

    def test_type_separation(self):
        """Different register types never share a block."""
        regs = [
            Register(address=0, name="h", type="holding"),
            Register(address=0, name="c", type="coil"),
            Register(address=0, name="i", type="input"),
            Register(address=0, name="d", type="discrete_input"),
        ]
        blocks = plan_blocks(regs)
        # Deterministic, sorted by type string then address.
        assert _block_tuples(blocks) == [
            ("coil", 0, 1),
            ("discrete_input", 0, 1),
            ("holding", 0, 1),
            ("input", 0, 1),
        ]

    def test_coils_coalesce(self):
        """Consecutive coils merge (each bit spans one address)."""
        regs = [Register(address=a, name=f"c{a}", type="coil") for a in range(3)]
        blocks = plan_blocks(regs)
        assert _block_tuples(blocks) == [("coil", 0, 3)]

    def test_demo_map_coalescing(self):
        """The bundled demo map coalesces into the expected transactions."""
        map_path = (
            Path(__file__).parent.parent / "zelos_extension_modbus" / "demo" / "power_meter.json"
        )
        reg_map = RegisterMap.from_file(str(map_path))
        blocks = plan_blocks(reg_map.registers)
        by_type = {}
        for b in blocks:
            by_type.setdefault(b.type, []).append((b.address, b.count))
        assert by_type["holding"] == [(0, 21), (100, 6), (110, 4)]
        assert by_type["input"] == [(0, 5)]
        assert by_type["coil"] == [(0, 3)]
        assert by_type["discrete_input"] == [(0, 3)]


class TestDecodeBlock:
    """Test decoding a raw block response back into per-register values."""

    def test_offsets(self):
        """Registers are sliced at their offset from the block start."""
        regs = (
            Register(address=0, name="a", datatype="float32"),
            Register(address=2, name="b", datatype="float32"),
        )
        block = ReadBlock(type="holding", address=0, count=4, registers=regs)
        # 3.14 ≈ [0x4048, 0xF5C3]; 1.0 = [0x3F80, 0x0000]
        raw = [0x4048, 0xF5C3, 0x3F80, 0x0000]
        decoded = decode_block(block, raw)
        assert abs(decoded[id(regs[0])] - 3.14) < 0.01
        assert abs(decoded[id(regs[1])] - 1.0) < 0.01

    def test_duplicate_address_decode(self):
        """Two registers at the same address decode independently from one slice."""
        r1 = Register(address=0, name="a", datatype="uint16")
        r2 = Register(address=0, name="b", datatype="uint16", scale=0.1)
        block = ReadBlock(type="holding", address=0, count=1, registers=(r1, r2))
        decoded = decode_block(block, [1000])
        assert decoded[id(r1)] == 1000
        assert decoded[id(r2)] == 100

    def test_short_response_skips_register(self):
        """A register whose slice is truncated is skipped, not decoded from garbage."""
        reg = Register(address=0, name="a", datatype="float32")
        block = ReadBlock(type="holding", address=0, count=2, registers=(reg,))
        decoded = decode_block(block, [0x4048])  # only one register, need two
        assert id(reg) not in decoded

    def test_bit_block_returns_bools(self):
        """Coil/discrete blocks return raw booleans."""
        regs = (
            Register(address=0, name="a", type="coil"),
            Register(address=1, name="b", type="coil"),
        )
        block = ReadBlock(type="coil", address=0, count=2, registers=regs)
        decoded = decode_block(block, [True, False])
        assert decoded[id(regs[0])] is True
        assert decoded[id(regs[1])] is False


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
        self._server = None

    def start(self):
        """Start server in background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        time.sleep(0.5)

    def _run(self):
        """Run server event loop."""
        from pymodbus.server import ModbusTcpServer

        from zelos_extension_modbus.demo.simulator import SimulatorUpdater

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        context = create_demo_context()
        simulator = PowerMeterSimulator()
        self._updater = SimulatorUpdater(simulator, context, interval=0.05)
        self._updater.start()

        async def run_server():
            self._server = ModbusTcpServer(context, address=(self.host, self.port))
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(run_server())
        except Exception:
            pass
        finally:
            self._updater.stop()

    def stop(self):
        """Stop the server and release the port."""
        if self._server and self._loop and not self._loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self._server.shutdown(), self._loop)
            with contextlib.suppress(Exception):
                future.result(timeout=3)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)
        if self._loop and not self._loop.is_closed():
            self._loop.close()


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
        """Write coil register at an address the simulator doesn't overwrite."""
        # Use coil address 3 (simulator only writes 0,1,2) to avoid race conditions
        from zelos_extension_modbus.register_map import Register

        reg = Register(address=3, name="test_coil", type="coil", datatype="bool")

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
# Block Reads Integration Tests
# =============================================================================


def _spy_reads(client):
    """Wrap a client's typed reads to record (address, count) per read kind."""
    calls = {"holding": [], "input": [], "coil": [], "discrete": []}
    originals = {
        "holding": client.read_holding_registers,
        "input": client.read_input_registers,
        "coil": client.read_coils,
        "discrete": client.read_discrete_inputs,
    }

    def make(kind, fn):
        async def wrapper(address, count=1):
            calls[kind].append((address, count))
            return await fn(address, count)

        return wrapper

    client.read_holding_registers = make("holding", originals["holding"])
    client.read_input_registers = make("input", originals["input"])
    client.read_coils = make("coil", originals["coil"])
    client.read_discrete_inputs = make("discrete", originals["discrete"])
    return calls


class TestBlockReadsIntegration:
    """Block-read polling against the demo server."""

    def test_demo_map_coalesces_transactions(self, client):
        """The demo map polls in the expected coalesced transactions."""
        calls = _spy_reads(client)
        results = asyncio.get_event_loop().run_until_complete(client._poll_registers())

        assert sorted(calls["holding"]) == [(0, 21), (100, 6), (110, 4)]
        assert calls["input"] == [(0, 5)]
        assert calls["coil"] == [(0, 3)]
        assert calls["discrete"] == [(0, 3)]
        # Results are still keyed by event/field.
        assert 200 < results["voltage"]["L1"] < 260

    def test_block_reads_false_one_call_per_register(self, demo_server, register_map):
        """With block_reads disabled there is one transaction per register."""
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=register_map,
            block_reads=False,
        )
        calls = _spy_reads(client)

        async def run():
            await client.connect()
            res = await client._poll_registers()
            await client.disconnect()
            return res

        results = asyncio.get_event_loop().run_until_complete(run())

        holding_regs = [r for r in register_map.registers if r.type == "holding"]
        assert len(calls["holding"]) == len(holding_regs)
        # Same result shape as block mode.
        assert 200 < results["voltage"]["L1"] < 260

    def test_failed_block_skips_only_its_registers(self, demo_server):
        """A block whose address is beyond the datastore skips only its registers."""
        data = {
            "name": "failskip",
            "events": {
                "good": [{"name": "v", "address": 0, "datatype": "float32"}],
                "bad": [{"name": "x", "address": 5000, "datatype": "uint16"}],
            },
        }
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=RegisterMap.from_dict(data),
        )

        async def run():
            await client.connect()
            res = await client._poll_registers()
            await client.disconnect()
            return res

        results = asyncio.get_event_loop().run_until_complete(run())
        assert "v" in results.get("good", {})
        assert "bad" not in results

    def test_max_block_size_limits_counts(self, demo_server, register_map):
        """max_block_size caps every transaction's count while keeping full coverage."""
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=register_map,
            max_block_size=8,
        )
        calls = _spy_reads(client)

        async def run():
            await client.connect()
            res = await client._poll_registers()
            await client.disconnect()
            return res

        results = asyncio.get_event_loop().run_until_complete(run())

        all_counts = [count for kind in calls.values() for (_, count) in kind]
        assert all_counts  # something was read
        assert all(c <= 8 for c in all_counts)
        # Full coverage: every event still produced values.
        for event in register_map.event_names:
            assert event in results


# =============================================================================
# Poll Scheduler Tests (fake clock)
# =============================================================================


class TestPollScheduler:
    """Per-register poll scheduling with a fake clock."""

    def _make_client(self):
        """Client with a fast (interface-rate) and a slow (5s) register, no network."""
        data = {
            "name": "sched",
            "events": {
                "fast": [{"name": "f", "address": 0}],
                "slow": [{"name": "s", "address": 1, "poll_interval": 5.0}],
            },
        }
        client = ModbusClient(
            register_map=RegisterMap.from_dict(data),
            poll_interval=1.0,
            block_reads=False,
        )
        polled: list[str] = []

        async def fake_read(reg):
            polled.append(reg.name)
            return 123

        client.read_register_value = fake_read
        return client, polled

    def test_due_set_membership(self):
        """Due sets follow each register's own cadence."""
        client, polled = self._make_client()
        run = asyncio.get_event_loop().run_until_complete

        run(client._poll_registers(now=0.0))
        assert set(polled) == {"f", "s"}
        polled.clear()

        run(client._poll_registers(now=1.0))
        assert polled == ["f"]  # slow not due until 5.0
        polled.clear()

        run(client._poll_registers(now=5.0))
        assert set(polled) == {"f", "s"}

    def test_no_backlog_after_stall(self):
        """A long stall polls each register once, rescheduling from now."""
        client, polled = self._make_client()
        run = asyncio.get_event_loop().run_until_complete

        run(client._poll_registers(now=0.0))  # f -> 1.0, s -> 5.0
        polled.clear()

        run(client._poll_registers(now=12.0))
        assert sorted(polled) == ["f", "s"]  # each due once, no backlog
        due = {it.register.name: it.next_due for it in client._poll_items}
        assert due["f"] == 13.0
        assert due["s"] == 17.0

    def test_seconds_until_next_due(self):
        """The sleep hint tracks the earliest-due item."""
        client, _ = self._make_client()
        client._build_poll_items()
        assert client._seconds_until_next_due(now=0.0) == 0.0

        asyncio.get_event_loop().run_until_complete(client._poll_registers(now=0.0))
        assert client._seconds_until_next_due(now=0.0) == 1.0
        assert client._seconds_until_next_due(now=0.5) == 0.5

    def test_no_map_falls_back_to_poll_interval(self):
        """Without poll items the sleep hint is the interface interval."""
        client = ModbusClient(poll_interval=2.5)
        assert client._seconds_until_next_due() == 2.5

    def test_poll_interval_negative_rejected(self):
        """A negative per-register poll_interval is rejected at load."""
        with pytest.raises(ValueError, match="poll_interval must be >= 0"):
            RegisterMap.from_dict(
                {"events": {"e": [{"name": "x", "address": 0, "poll_interval": -1}]}}
            )

    def test_build_poll_items_excludes_disabled(self):
        """A disabled register (poll_interval == 0) produces no poll item."""
        data = {
            "name": "sched",
            "events": {
                "e": [
                    {"name": "on", "address": 0},
                    {"name": "off", "address": 1, "poll_interval": 0},
                ]
            },
        }
        client = ModbusClient(register_map=RegisterMap.from_dict(data), poll_interval=1.0)
        client._build_poll_items()
        assert [it.register.name for it in client._poll_items] == ["on"]

    def test_disabled_register_no_interface_fallback(self):
        """Regression: poll_interval 0 must NOT fall back to the interface rate.

        The old ``reg.poll_interval or self.poll_interval`` silently turned 0
        into the interface interval and scheduled the register anyway.
        """
        data = {"events": {"e": [{"name": "off", "address": 0, "poll_interval": 0}]}}
        client = ModbusClient(register_map=RegisterMap.from_dict(data), poll_interval=1.0)
        client._build_poll_items()
        # No item at all — not one scheduled at the 1.0s interface rate.
        assert client._poll_items == []

    def test_disabled_register_never_polls(self):
        """A disabled register never polls, even after many clock cycles."""
        data = {
            "name": "sched",
            "events": {
                "on": [{"name": "f", "address": 0}],
                "off": [{"name": "d", "address": 1, "poll_interval": 0}],
            },
        }
        client = ModbusClient(
            register_map=RegisterMap.from_dict(data),
            poll_interval=1.0,
            block_reads=False,
        )
        polled: list[str] = []

        async def fake_read(reg):
            polled.append(reg.name)
            return 123

        client.read_register_value = fake_read
        run = asyncio.get_event_loop().run_until_complete

        for now in range(0, 100, 1):  # 100 cycles across the clock
            run(client._poll_registers(now=float(now)))

        assert "d" not in polled  # disabled register never polled
        assert "f" in polled

    def test_real_clock_fast_polls_more_than_slow(self):
        """Smoke test with the real clock: the fast register polls more often."""
        data = {
            "name": "smoke",
            "events": {
                "fast": [{"name": "f", "address": 0}],
                "slow": [{"name": "s", "address": 1, "poll_interval": 0.5}],
            },
        }
        client = ModbusClient(
            register_map=RegisterMap.from_dict(data),
            poll_interval=0.05,
            block_reads=False,
        )
        counts = {"f": 0, "s": 0}

        async def fake_read(reg):
            counts[reg.name] += 1
            return 1

        client.read_register_value = fake_read

        async def run():
            client._build_poll_items()
            start = time.monotonic()
            while time.monotonic() - start < 1.0:
                await client._poll_registers()
                await asyncio.sleep(0.02)

        asyncio.get_event_loop().run_until_complete(run())
        assert counts["f"] > counts["s"]
        assert counts["s"] >= 1


# =============================================================================
# Field Sanitization Tests
# =============================================================================


class TestFieldSanitization:
    """Trace-safe field naming."""

    def test_field_name_sanitizes_reserved_chars(self):
        """Dots and slashes/spaces collapse to underscores."""
        client = ModbusClient()
        assert client._field_name(Register(address=1, name="pcb.temp")) == "pcb_temp"
        assert client._field_name(Register(address=2, name="amps/phase a")) == "amps_phase_a"

    def test_field_name_all_junk_falls_back(self):
        """A name with nothing usable falls back to r<address>."""
        client = ModbusClient()
        assert client._field_name(Register(address=7, name="...")) == "r7"

    def test_dotted_name_roundtrips(self, demo_server):
        """A dotted register name flows through init + poll as a sanitized field."""
        data = {
            "name": "dotted_rt",
            "events": {
                "temps": [
                    {"name": "pcb.temp", "address": 20, "datatype": "int16", "scale": 0.1},
                ]
            },
        }
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=RegisterMap.from_dict(data),
        )
        client._init_trace_source()  # schema uses the sanitized field name

        async def run():
            await client.connect()
            res = await client._poll_registers()
            await client.disconnect()
            return res

        results = asyncio.get_event_loop().run_until_complete(run())
        assert "pcb_temp" in results.get("temps", {})


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


# =============================================================================
# Trace Source Init Tests
# =============================================================================


class TestInitTraceSource:
    """Tests for _init_trace_source."""

    def test_init_trace_source_no_register_map(self):
        """Without a register map, a generic 'raw' event is created."""
        client = ModbusClient()
        client._init_trace_source()
        assert client._source is not None

    def test_init_trace_source_with_register_map(self):
        """With a register map, events match the map."""
        data = {
            "name": "device",
            "events": {
                "sensors": [
                    {"name": "temp", "address": 0, "datatype": "uint16"},
                ],
            },
        }
        reg_map = RegisterMap.from_dict(data)
        client = ModbusClient(register_map=reg_map)
        client._init_trace_source()
        assert client._source is not None

    def test_disabled_field_absent_from_event(self):
        """A disabled register is not advertised as a field on its event."""
        data = {
            "name": "d",
            "events": {
                "e": [
                    {"name": "on", "address": 0, "datatype": "uint16"},
                    {"name": "off", "address": 1, "datatype": "uint16", "poll_interval": 0},
                ]
            },
        }
        client = ModbusClient(register_map=RegisterMap.from_dict(data))
        client._init_trace_source()
        event = getattr(client._source, "e", None)
        assert event is not None
        assert set(event.fields) == {"on"}

    def test_all_disabled_event_not_added(self):
        """An event whose registers are all disabled is never added to the source."""
        data = {
            "name": "d",
            "events": {
                "e": [
                    {"name": "off1", "address": 0, "poll_interval": 0},
                    {"name": "off2", "address": 1, "poll_interval": 0},
                ]
            },
        }
        client = ModbusClient(register_map=RegisterMap.from_dict(data))
        client._init_trace_source()
        assert getattr(client._source, "e", None) is None

    def test_mixed_event_keeps_only_polled_fields(self):
        """A mixed event advertises only its polled registers as fields."""
        data = {
            "name": "d",
            "events": {
                "e": [
                    {"name": "a", "address": 0, "datatype": "uint16"},
                    {"name": "b", "address": 1, "datatype": "uint16", "poll_interval": 0},
                    {"name": "c", "address": 2, "datatype": "uint16", "poll_interval": 5.0},
                ]
            },
        }
        client = ModbusClient(register_map=RegisterMap.from_dict(data))
        client._init_trace_source()
        event = getattr(client._source, "e", None)
        assert event is not None
        assert set(event.fields) == {"a", "c"}


# =============================================================================
# Disabled Register Actions Tests
# =============================================================================


class TestDisabledRegisterActions:
    """A disabled register stays fully usable for reads/writes and actions."""

    def test_disabled_register_read_write_still_works(self, demo_server):
        """Read and write on a disabled register work against the demo server."""
        data = {
            "name": "disabled_rw",
            "events": {
                "cfg": [
                    {"name": "limit", "address": 100, "datatype": "uint16", "poll_interval": 0},
                ]
            },
        }
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=RegisterMap.from_dict(data),
        )
        reg = client.register_map.get_by_name("limit")
        assert reg.polled is False  # disabled, but still read/writable

        async def run():
            await client.connect()
            ok = await client.write_register_value(reg, 231)
            value = await client.read_register_value(reg)
            await client.disconnect()
            return ok, value

        ok, value = asyncio.get_event_loop().run_until_complete(run())
        assert ok is True
        assert value == 231

    def test_disabled_register_listed_and_resolvable(self):
        """A disabled register stays in the map for list/named actions."""
        data = {
            "name": "disabled_list",
            "events": {
                "cfg": [
                    {"name": "limit", "address": 100, "datatype": "uint16", "poll_interval": 0},
                ]
            },
        }
        client = ModbusClient(register_map=RegisterMap.from_dict(data), interface_name="dis")
        registry.register("dis", client)
        try:
            result = actions.list_registers(interface="dis")
            names = [r["name"] for r in result["registers"]]
            assert "limit" in names
            # Named actions still resolve the disabled register.
            error, reg = actions._resolve_register(client, "cfg/limit")
            assert error is None
            assert reg.name == "limit"
        finally:
            registry.clear()


# =============================================================================
# Write Mode Tests
# =============================================================================


class TestWriteMode:
    """Tests for write_mode=fc16."""

    def test_fc16_mode_uses_write_registers(self, demo_server, register_map):
        """With write_mode='fc16', even single-register writes use FC 16."""
        client = ModbusClient(
            host=demo_server.host,
            port=demo_server.port,
            register_map=register_map,
            write_mode="fc16",
        )

        async def run():
            await client.connect()
            reg = register_map.get_by_name("voltage_high_limit")
            # This is a uint16 (single register) but fc16 mode should still work
            success = await client.write_register_value(reg, 240)
            assert success is True
            value = await client.read_register_value(reg)
            assert value == 240
            await client.disconnect()

        asyncio.get_event_loop().run_until_complete(run())


# =============================================================================
# TCP Reconnection Integration Tests
# =============================================================================


class TestTcpReconnection:
    """Test that the polling loop recovers when a TCP server goes offline and comes back."""

    def test_tcp_reconnect_after_server_restart(self, register_map):
        """Server goes offline, reads fail, server comes back, reads succeed again."""
        port = 15021  # dedicated port to avoid collision with module-scoped demo_server

        # Start server
        server = DemoServer(port=port)
        server.start()

        client = ModbusClient(
            host="127.0.0.1",
            port=port,
            register_map=register_map,
            timeout=1.0,
        )

        async def run():
            # Phase 1: connect and read successfully
            await client.connect()
            assert client._connected
            reg = register_map.get_by_name("voltage_high_limit")
            val = await client.read_register_value(reg)
            assert val is not None

            # Phase 2: kill server — next read should fail
            server.stop()
            await asyncio.sleep(0.5)

            val = await client.read_register_value(reg)
            assert val is None  # read fails

            # Phase 3: mark disconnected (as _run_async would) then restart
            client._connected = False
            await asyncio.sleep(1)  # allow socket cleanup after server.stop()

            server2 = DemoServer(port=port)
            server2.start()

            # Phase 4: reconnect succeeds and reads work again
            connected = await client._ensure_connected()
            assert connected
            assert client._connected

            val = await client.read_register_value(reg)
            assert val is not None

            await client.disconnect()
            server2.stop()

        asyncio.get_event_loop().run_until_complete(run())

    def test_tcp_poll_loop_survives_server_restart(self, register_map):
        """The _run_async polling loop reconnects automatically after server outage."""
        port = 15022
        server = DemoServer(port=port)
        server.start()

        client = ModbusClient(
            host="127.0.0.1",
            port=port,
            register_map=register_map,
            timeout=1.0,
            poll_interval=0.5,
        )

        poll_results: list[dict] = []
        original_poll = client._poll_registers

        async def tracking_poll():
            result = await original_poll()
            poll_results.append(result)
            return result

        client._poll_registers = tracking_poll

        async def run():
            loop = asyncio.get_event_loop()
            client._loop = loop
            client._running = True

            # Start polling in background
            poll_task = asyncio.create_task(client._run_async())

            # Wait for a few successful polls
            for _ in range(40):
                if len(poll_results) >= 2:
                    break
                await asyncio.sleep(0.1)
            assert len(poll_results) >= 2, "Should have polled at least twice"

            good_count = len(poll_results)

            # Kill server
            server.stop()
            await asyncio.sleep(4.0)  # let reconnect attempts happen + TIME_WAIT clear

            # Restart server
            server2 = DemoServer(port=port)
            server2.start()

            # Wait for recovery — new successful polls should appear
            for _ in range(60):
                if len(poll_results) > good_count:
                    break
                await asyncio.sleep(0.1)

            assert len(poll_results) > good_count, (
                "Should have resumed polling after server restart"
            )
            assert client._connected

            # Shutdown
            client._running = False
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
            server2.stop()

        asyncio.get_event_loop().run_until_complete(run())

    def test_tcp_connect_to_nonexistent_server(self, register_map):
        """Connecting to a server that doesn't exist returns False, doesn't hang."""
        client = ModbusClient(
            host="127.0.0.1",
            port=19999,  # nothing listening here
            register_map=register_map,
            timeout=1.0,
        )

        async def run():
            result = await client.connect()
            assert result is False or not client._connected
            await client.disconnect()

        asyncio.get_event_loop().run_until_complete(run())


# =============================================================================
# RTU Serial Integration Tests (virtual serial ports via socat)
# =============================================================================

requires_socat = pytest.mark.skipif(
    shutil.which("socat") is None,
    reason="socat not installed (brew install socat)",
)


class RtuDemoServer:
    """Helper to run Modbus RTU server on a virtual serial port."""

    def __init__(self, serial_port: str):
        self.serial_port = serial_port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(2.0)  # serial startup is slower than TCP

    def _run(self):
        from pymodbus.server import StartAsyncSerialServer

        from zelos_extension_modbus.demo.simulator import SimulatorUpdater

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        context = create_demo_context()
        simulator = PowerMeterSimulator()
        updater = SimulatorUpdater(simulator, context, interval=0.05)
        updater.start()

        async def run_server():
            await StartAsyncSerialServer(
                context=context,
                port=self.serial_port,
                baudrate=9600,
                timeout=1,
            )

        try:
            self._loop.run_until_complete(run_server())
        except Exception:
            pass
        finally:
            updater.stop()

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture(scope="module")
def serial_ports(tmp_path_factory):
    """Create a virtual serial port pair using socat.

    Yields (server_port, client_port) paths.
    """
    if shutil.which("socat") is None:
        pytest.skip("socat not installed")

    tmpdir = tmp_path_factory.mktemp("socat")
    server_link = str(tmpdir / "server")
    client_link = str(tmpdir / "client")

    proc = subprocess.Popen(
        [
            "socat",
            f"PTY,raw,echo=0,link={server_link}",
            f"PTY,raw,echo=0,link={client_link}",
        ],
        stderr=subprocess.PIPE,
    )

    # Wait for symlinks to appear
    for link in (server_link, client_link):
        for _ in range(40):
            if Path(link).exists():
                break
            time.sleep(0.05)
        else:
            proc.terminate()
            pytest.fail(f"socat PTY link {link} did not appear")

    yield server_link, client_link

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def rtu_demo_server(serial_ports):
    """Start RTU demo server on one end of the virtual serial pair."""
    server_port, _ = serial_ports
    server = RtuDemoServer(serial_port=server_port)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def rtu_client(serial_ports, rtu_demo_server, register_map):
    """Create a connected ModbusClient in RTU mode on the other end."""
    _, client_port = serial_ports
    rtu = ModbusClient(
        transport="rtu",
        serial_port=client_port,
        baudrate=9600,
        unit_id=1,
        timeout=2.0,
        register_map=register_map,
    )

    async def connect():
        await rtu.connect()

    asyncio.get_event_loop().run_until_complete(connect())
    yield rtu

    async def disconnect():
        await rtu.disconnect()

    asyncio.get_event_loop().run_until_complete(disconnect())


@requires_socat
class TestRtuIntegration:
    """Integration tests over virtual serial RTU — mirrors TestDemoServerIntegration."""

    def test_rtu_read_holding_float32(self, rtu_client):
        """Read float32 holding register (voltage) over RTU."""
        reg = rtu_client.register_map.get_by_name("L1")
        assert reg is not None

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert 200 < value < 260

    def test_rtu_read_holding_uint32(self, rtu_client):
        """Read uint32 holding register (energy) over RTU."""
        reg = rtu_client.register_map.get_by_name("energy")
        assert reg is not None

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert isinstance(value, int)
        assert value >= 0

    def test_rtu_read_holding_int16_scaled(self, rtu_client):
        """Read int16 holding register with scale (temperature) over RTU."""
        reg = rtu_client.register_map.get_by_name("temperature")
        assert reg is not None

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert 0 < value < 100

    def test_rtu_read_input_register(self, rtu_client):
        """Read input register (firmware_version) over RTU."""
        reg = rtu_client.register_map.get_by_name("firmware_version")
        assert reg is not None
        assert reg.type == "input"

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value == 0x0102

    def test_rtu_read_input_uint32(self, rtu_client):
        """Read uint32 input register (serial_number) over RTU."""
        reg = rtu_client.register_map.get_by_name("serial_number")
        assert reg is not None

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value == 12345678

    def test_rtu_read_coil(self, rtu_client):
        """Read coil register over RTU."""
        reg = rtu_client.register_map.get_by_name("relay1")
        assert reg is not None
        assert reg.type == "coil"

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value in (True, False)

    def test_rtu_read_discrete_input(self, rtu_client):
        """Read discrete input over RTU."""
        reg = rtu_client.register_map.get_by_name("grid_connected")
        assert reg is not None
        assert reg.type == "discrete_input"

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is True

    def test_rtu_write_holding_and_readback(self, rtu_client):
        """Write uint16 holding register and read back over RTU."""
        reg = rtu_client.register_map.get_by_name("voltage_high_limit")
        assert reg is not None

        async def write_and_read():
            success = await rtu_client.write_register_value(reg, 242)
            assert success is True
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert value == 242

    def test_rtu_write_coil_and_readback(self, rtu_client):
        """Write coil and read back over RTU."""
        reg = rtu_client.register_map.get_by_name("relay1")
        assert reg is not None

        async def write_and_read():
            success = await rtu_client.write_register_value(reg, True)
            assert success is True
            val = await rtu_client.read_register_value(reg)
            assert val is True

            success = await rtu_client.write_register_value(reg, False)
            assert success is True
            val = await rtu_client.read_register_value(reg)
            assert val is False

        asyncio.get_event_loop().run_until_complete(write_and_read())

    def test_rtu_read_swapped_float(self, rtu_client):
        """Read float32 with big_swap byte order over RTU."""
        reg = rtu_client.register_map.get_by_name("calibration_factor")
        assert reg is not None
        assert reg.byte_order == "big_swap"

        async def read():
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(read())
        assert value is not None
        assert abs(value - 1.0) < 0.01

    def test_rtu_write_int32(self, rtu_client):
        """Write int32 holding register over RTU."""
        reg = rtu_client.register_map.get_by_name("power_limit")
        assert reg is not None

        async def write_and_read():
            success = await rtu_client.write_register_value(reg, -5000)
            assert success is True
            return await rtu_client.read_register_value(reg)

        value = asyncio.get_event_loop().run_until_complete(write_and_read())
        assert value == -5000

    def test_rtu_values_change_over_time(self, rtu_client):
        """Two consecutive polls return different simulator values."""
        reg = rtu_client.register_map.get_by_name("L1")
        assert reg is not None

        async def two_reads():
            v1 = await rtu_client.read_register_value(reg)
            await asyncio.sleep(0.3)
            v2 = await rtu_client.read_register_value(reg)
            return v1, v2

        v1, v2 = asyncio.get_event_loop().run_until_complete(two_reads())
        # Both should be valid voltages
        assert 200 < v1 < 260
        assert 200 < v2 < 260

    def test_rtu_poll_all_events(self, rtu_client):
        """Poll all registers via register map over RTU."""

        async def poll():
            return await rtu_client._poll_registers()

        results = asyncio.get_event_loop().run_until_complete(poll())

        assert "voltage" in results
        assert "current" in results
        assert "power" in results
        assert "status" in results
        assert "inputs" in results
        assert "digital_inputs" in results
        assert "setpoints" in results
        assert "swapped_floats" in results

        assert "L1" in results["voltage"]
        assert 200 < results["voltage"]["L1"] < 260

    def test_rtu_transport_is_rtu(self, rtu_client):
        """Client transport reflects RTU mode."""
        assert rtu_client._connected is True
        assert rtu_client.transport == "rtu"


# =============================================================================
# RTU Reconnection Tests
# =============================================================================


@requires_socat
class TestRtuReconnection:
    """Test RTU reconnection when serial link is interrupted."""

    def test_rtu_read_fails_when_server_stops(self, register_map):
        """Reads return None after RTU server stops."""
        tmpdir = tempfile.mkdtemp(prefix="socat_recon_")
        server_link = f"{tmpdir}/server"
        client_link = f"{tmpdir}/client"

        # Create PTY pair
        socat_proc = subprocess.Popen(
            [
                "socat",
                f"PTY,raw,echo=0,link={server_link}",
                f"PTY,raw,echo=0,link={client_link}",
            ],
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)

        # Start RTU server
        rtu_server = RtuDemoServer(serial_port=server_link)
        rtu_server.start()

        client = ModbusClient(
            transport="rtu",
            serial_port=client_link,
            baudrate=9600,
            unit_id=1,
            timeout=2.0,
            register_map=register_map,
        )

        async def run():
            await client.connect()
            assert client._connected

            reg = register_map.get_by_name("voltage_high_limit")

            # Phase 1: reads work
            val = await client.read_register_value(reg)
            assert val is not None

            # Phase 2: kill server — reads should fail
            rtu_server.stop()
            socat_proc.terminate()
            socat_proc.wait(timeout=5)
            await asyncio.sleep(0.5)

            val = await client.read_register_value(reg)
            # After socat dies, read should return None (not hang)
            assert val is None

            await client.disconnect()

        asyncio.get_event_loop().run_until_complete(run())

    def test_rtu_reconnect_after_link_restored(self, register_map):
        """Client reconnects when serial link is re-established."""
        tmpdir = tempfile.mkdtemp(prefix="socat_recon2_")
        server_link = f"{tmpdir}/server"
        client_link = f"{tmpdir}/client"

        # Create PTY pair
        socat_proc = subprocess.Popen(
            [
                "socat",
                f"PTY,raw,echo=0,link={server_link}",
                f"PTY,raw,echo=0,link={client_link}",
            ],
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)

        rtu_server = RtuDemoServer(serial_port=server_link)
        rtu_server.start()

        client = ModbusClient(
            transport="rtu",
            serial_port=client_link,
            baudrate=9600,
            unit_id=1,
            timeout=2.0,
            register_map=register_map,
        )

        async def run():
            await client.connect()
            reg = register_map.get_by_name("voltage_high_limit")

            # Phase 1: reads work
            val = await client.read_register_value(reg)
            assert val is not None

            # Phase 2: kill everything
            rtu_server.stop()
            socat_proc.terminate()
            socat_proc.wait(timeout=5)
            await asyncio.sleep(0.5)
            client._connected = False

            # Phase 3: re-establish link + server
            socat_proc2 = subprocess.Popen(
                [
                    "socat",
                    f"PTY,raw,echo=0,link={server_link}",
                    f"PTY,raw,echo=0,link={client_link}",
                ],
                stderr=subprocess.PIPE,
            )
            time.sleep(0.5)

            rtu_server2 = RtuDemoServer(serial_port=server_link)
            rtu_server2.start()

            # Phase 4: reconnect and read
            connected = await client._ensure_connected()
            assert connected

            val = await client.read_register_value(reg)
            assert val is not None

            await client.disconnect()
            rtu_server2.stop()
            socat_proc2.terminate()
            socat_proc2.wait(timeout=5)

        asyncio.get_event_loop().run_until_complete(run())

    def test_rtu_is_connection_error_detection(self):
        """Verify _is_connection_error catches serial-related exceptions."""
        client = ModbusClient(transport="rtu", serial_port="/dev/null", unit_id=1)

        # By exception type (isinstance check)
        assert client._is_connection_error(ConnectionError("anything"))
        assert client._is_connection_error(TimeoutError("read timed out"))
        assert client._is_connection_error(OSError("serial port not available"))

        # By error message string matching
        assert client._is_connection_error(Exception("device disconnected"))
        assert client._is_connection_error(Exception("connection refused"))
        assert client._is_connection_error(Exception("No response received after 3 retries"))

        # Non-connection errors
        assert not client._is_connection_error(ValueError("bad register value"))
        assert not client._is_connection_error(KeyError("missing_field"))


# =============================================================================
# Action-Specific Edge Cases
# =============================================================================


class TestActionEdgeCases:
    """Edge case tests for action functions."""

    def test_write_registers_action_invalid_values(self):
        """Write Registers action with non-integer values returns error."""
        # Register a dummy client so the interface lookup succeeds
        client = ModbusClient(interface_name="edge_test")
        registry.register("edge_test", client)
        try:
            result = actions.write_registers(interface="edge_test", address=0, values="abc,def")
            assert result["success"] is False
            assert "error" in result
        finally:
            registry.clear()


class TestActionsUnit:
    """Unit tests for SDK actions (no network)."""

    @pytest.fixture(autouse=True)
    def setup_actions(self):
        """Create client with register map and register in registry."""
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
        client = ModbusClient(register_map=reg_map, interface_name="test")
        registry.register("test", client)
        yield
        registry.clear()

    @pytest.fixture
    def no_map_actions(self):
        """Register a client with no register map."""
        client = ModbusClient(interface_name="no_map")
        registry.register("no_map", client)
        yield
        registry.clear()

    def test_get_status_returns_info(self):
        """Get Status action returns expected fields."""
        result = actions.get_status(interface="test")
        assert "connected" in result
        assert "transport" in result
        assert "unit_id" in result
        assert "poll_count" in result
        assert "registers" in result
        assert result["registers"] == 4
        # Block-read knobs are reported (defaults).
        assert result["block_reads"] is True
        assert result["max_block_size"] == 125
        assert result["max_read_gap"] == 0

    def test_list_registers_returns_all(self):
        """List Registers action returns all registers."""
        result = actions.list_registers(interface="test")
        assert result["count"] == 4
        names = [r["name"] for r in result["registers"]]
        assert "temp" in names
        assert "humidity" in names
        assert "relay" in names
        assert "setpoint" in names

    def test_list_writable_registers_filters(self):
        """List Writable Registers only returns writable ones."""
        result = actions.list_writable_registers(interface="test")
        # holding and coil are writable, input is not
        assert result["count"] == 3
        names = [r["name"] for r in result["registers"]]
        assert "temp" in names
        assert "relay" in names
        assert "setpoint" in names
        assert "humidity" not in names  # input register, not writable

    def test_list_registers_no_map(self, no_map_actions):
        """List Registers with no map returns empty."""
        result = actions.list_registers(interface="no_map")
        assert result["count"] == 0
        assert result["registers"] == []

    def test_list_writable_no_map(self, no_map_actions):
        """List Writable with no map returns empty."""
        result = actions.list_writable_registers(interface="no_map")
        assert result["count"] == 0

    def test_read_named_no_map(self, no_map_actions):
        """Read Named Register with no map returns error."""
        result = actions.read_named_register(interface="no_map", name="anything")
        assert result["success"] is False
        assert "error" in result

    def test_read_named_not_found(self):
        """Read Named Register with unknown name returns error."""
        result = actions.read_named_register(interface="test", name="sensors/nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_no_map(self, no_map_actions):
        """Write Named Register with no map returns error."""
        result = actions.write_named_register(interface="no_map", name="anything", value=100)
        assert result["success"] is False
        assert "error" in result

    def test_write_named_not_found(self):
        """Write Named Register with unknown name returns error."""
        result = actions.write_named_register(
            interface="test", name="sensors/nonexistent", value=100
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_write_named_not_writable(self):
        """Write Named Register to input register returns error."""
        result = actions.write_named_register(interface="test", name="sensors/humidity", value=100)
        assert result["success"] is False
        assert "not writable" in result["error"]

    def test_unknown_interface_returns_error(self):
        """Actions with unknown interface return error."""
        result = actions.get_status(interface="nonexistent")
        assert "not found" in result["error"]

    def test_multi_interface_registry(self):
        """Multiple interfaces registered and isolated."""
        data2 = {
            "name": "device2",
            "events": {"temps": [{"name": "t1", "address": 0, "type": "holding"}]},
        }
        client2 = ModbusClient(register_map=RegisterMap.from_dict(data2), interface_name="second")
        registry.register("second", client2)

        assert set(registry.all_interfaces()) == {"test", "second"}

        r1 = actions.list_registers(interface="test")
        r2 = actions.list_registers(interface="second")
        assert r1["count"] == 4
        assert r2["count"] == 1

    def test_interface_specific_dropdown(self):
        """Dropdown callables return per-interface registers."""
        names = registry.interface_registers("test")
        assert "sensors/temp" in names
        assert "controls/relay" in names

        writable = registry.interface_writable_registers("test")
        assert "sensors/humidity" not in writable
        assert "controls/setpoint" in writable


class TestActionsIntegration:
    """Integration tests for SDK actions with demo server.

    Tests actions that don't require network (list actions) and validates
    action response structure. Network read/write is tested in
    TestDemoServerIntegration.
    """

    @pytest.fixture(autouse=True)
    def bind_actions(self, client):
        """Register the integration test client in the registry."""
        registry.register("test", client)
        yield
        registry.clear()

    def test_list_registers_action(self, client):
        """List Registers action returns all demo registers."""
        result = actions.list_registers(interface="test")
        assert result["count"] > 0
        names = [r["name"] for r in result["registers"]]
        assert "L1" in names
        assert "relay1" in names
        assert "firmware_version" in names

    def test_list_writable_action(self, client):
        """List Writable action excludes input/discrete registers."""
        result = actions.list_writable_registers(interface="test")
        names = [r["name"] for r in result["registers"]]
        assert "voltage_high_limit" in names
        assert "relay1" in names
        assert "firmware_version" not in names
        assert "door_open" not in names

    def test_get_status_action(self, client):
        """Get Status action returns info."""
        result = actions.get_status(interface="test")
        assert result["connected"] is True
        assert result["transport"] == "tcp"
        assert result["registers"] > 0

    def test_write_named_readonly_fails(self, client):
        """Write Named to input register fails gracefully."""
        result = actions.write_named_register(
            interface="test", name="inputs/firmware_version", value=999
        )
        assert result["success"] is False
        assert "not writable" in result["error"]
