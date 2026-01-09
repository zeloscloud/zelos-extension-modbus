"""Minimal tests for Zelos Modbus extension.

Tests core functionality without network dependencies:
- Register map parsing
- Value encoding/decoding
- Simulator physics logic
"""

import json
import struct
import tempfile
from pathlib import Path

import pytest

from zelos_extension_modbus.client import _reorder_registers, decode_value, encode_value
from zelos_extension_modbus.demo.simulator import (
    PowerMeterSimulator,
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
