"""Tests for the Zelos Modbus extension."""

import json
import tempfile
from pathlib import Path

import pytest

from zelos_extension_modbus.client import decode_value, encode_value
from zelos_extension_modbus.register_map import Register, RegisterMap


class TestRegisterMap:
    """Tests for RegisterMap class."""

    def test_register_defaults(self):
        """Test Register default values."""
        reg = Register(address=0, name="test")
        assert reg.type == "holding"
        assert reg.datatype == "uint16"
        assert reg.unit == ""
        assert reg.scale == 1.0
        assert reg.count == 1

    def test_register_count_for_datatypes(self):
        """Test register count calculation for different datatypes."""
        assert Register(address=0, name="t", datatype="uint16").count == 1
        assert Register(address=0, name="t", datatype="int16").count == 1
        assert Register(address=0, name="t", datatype="uint32").count == 2
        assert Register(address=0, name="t", datatype="float32").count == 2
        assert Register(address=0, name="t", datatype="uint64").count == 4
        assert Register(address=0, name="t", datatype="float64").count == 4

    def test_register_invalid_type(self):
        """Test that invalid register type raises error."""
        with pytest.raises(ValueError, match="Invalid register type"):
            Register(address=0, name="test", type="invalid")

    def test_register_invalid_datatype(self):
        """Test that invalid datatype raises error."""
        with pytest.raises(ValueError, match="Invalid datatype"):
            Register(address=0, name="test", datatype="invalid")

    def test_from_dict_minimal(self):
        """Test loading register map with minimal fields."""
        data = {
            "events": {
                "sensors": [
                    {"address": 0, "name": "voltage"},
                    {"address": 1, "name": "current"},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert len(reg_map.registers) == 2
        assert reg_map.event_names == ["sensors"]
        assert reg_map.get_event("sensors")[0].name == "voltage"
        assert reg_map.get_event("sensors")[1].name == "current"

    def test_from_dict_full(self):
        """Test loading register map with all fields."""
        data = {
            "name": "test_device",
            "description": "Test device description",
            "events": {
                "electrical": [
                    {
                        "address": 0,
                        "name": "voltage",
                        "type": "holding",
                        "datatype": "float32",
                        "unit": "V",
                        "scale": 0.1,
                        "description": "Input voltage",
                    }
                ]
            },
        }
        reg_map = RegisterMap.from_dict(data)
        assert reg_map.name == "test_device"
        assert reg_map.description == "Test device description"
        reg = reg_map.get_event("electrical")[0]
        assert reg.address == 0
        assert reg.name == "voltage"
        assert reg.type == "holding"
        assert reg.datatype == "float32"
        assert reg.unit == "V"
        assert reg.scale == 0.1
        assert reg.count == 2  # float32 uses 2 registers

    def test_from_dict_multiple_events(self):
        """Test loading register map with multiple user-defined events."""
        data = {
            "events": {
                "temperature": [
                    {"address": 0, "name": "pcb_temp", "type": "holding"},
                    {"address": 1, "name": "overtemp", "type": "coil"},
                ],
                "voltage/ac": [
                    {"address": 10, "name": "phsA", "type": "holding", "datatype": "float32"},
                ],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert set(reg_map.event_names) == {"temperature", "voltage/ac"}
        assert len(reg_map.get_event("temperature")) == 2
        assert len(reg_map.get_event("voltage/ac")) == 1
        # Registers can have different types within same event
        temp_regs = reg_map.get_event("temperature")
        assert temp_regs[0].type == "holding"
        assert temp_regs[1].type == "coil"

    def test_from_file(self):
        """Test loading register map from JSON file."""
        data = {
            "name": "file_test",
            "events": {
                "test_event": [{"address": 0, "name": "test_reg"}]
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            reg_map = RegisterMap.from_file(f.name)

        assert reg_map.name == "file_test"
        assert len(reg_map.registers) == 1
        Path(f.name).unlink()

    def test_from_file_not_found(self):
        """Test that loading non-existent file raises error."""
        with pytest.raises(FileNotFoundError):
            RegisterMap.from_file("/nonexistent/path.json")

    def test_registers_property(self):
        """Test flat registers property across events."""
        data = {
            "events": {
                "event1": [
                    {"address": 0, "name": "reg1"},
                    {"address": 1, "name": "reg2"},
                ],
                "event2": [
                    {"address": 2, "name": "reg3"},
                ],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert len(reg_map.registers) == 3

    def test_get_by_name(self):
        """Test finding register by name across events."""
        data = {
            "events": {
                "sensors": [{"address": 0, "name": "voltage"}],
                "relays": [{"address": 1, "name": "current"}],
            }
        }
        reg_map = RegisterMap.from_dict(data)

        reg = reg_map.get_by_name("voltage")
        assert reg is not None
        assert reg.address == 0

        reg = reg_map.get_by_name("current")
        assert reg is not None
        assert reg.address == 1

        reg = reg_map.get_by_name("nonexistent")
        assert reg is None

    def test_get_by_address(self):
        """Test finding register by address and type."""
        data = {
            "events": {
                "mixed": [
                    {"address": 0, "name": "hold0", "type": "holding"},
                    {"address": 0, "name": "input0", "type": "input"},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)

        reg = reg_map.get_by_address(0, "holding")
        assert reg is not None
        assert reg.name == "hold0"

        reg = reg_map.get_by_address(0, "input")
        assert reg is not None
        assert reg.name == "input0"

        reg = reg_map.get_by_address(99, "holding")
        assert reg is None


class TestValueEncoding:
    """Tests for value encoding/decoding functions."""

    def test_decode_uint16(self):
        """Test uint16 decoding."""
        assert decode_value([1000], "uint16") == 1000
        assert decode_value([1000], "uint16", scale=0.1) == 100

    def test_decode_int16(self):
        """Test int16 decoding (signed)."""
        assert decode_value([65535], "int16") == -1
        assert decode_value([32768], "int16") == -32768

    def test_decode_uint32(self):
        """Test uint32 decoding."""
        # 0x00010000 = 65536
        assert decode_value([0x0001, 0x0000], "uint32") == 65536

    def test_decode_float32(self):
        """Test float32 decoding."""
        # IEEE 754: 3.14 ≈ 0x4048F5C3
        result = decode_value([0x4048, 0xF5C3], "float32")
        assert abs(result - 3.14) < 0.001

    def test_decode_bool(self):
        """Test boolean decoding."""
        assert decode_value([1], "bool") is True
        assert decode_value([0], "bool") is False

    def test_encode_uint16(self):
        """Test uint16 encoding."""
        assert encode_value(1000, "uint16") == [1000]
        assert encode_value(100, "uint16", scale=0.1) == [1000]

    def test_encode_int16(self):
        """Test int16 encoding (signed)."""
        assert encode_value(-1, "int16") == [65535]

    def test_encode_uint32(self):
        """Test uint32 encoding."""
        assert encode_value(65536, "uint32") == [0x0001, 0x0000]

    def test_encode_bool(self):
        """Test boolean encoding."""
        assert encode_value(True, "bool") == [1]
        assert encode_value(False, "bool") == [0]

    def test_roundtrip(self):
        """Test encode/decode roundtrip for various types."""
        test_cases = [
            (1234, "uint16", 1.0),
            (-100, "int16", 1.0),
            (100000, "uint32", 1.0),
            (3.14159, "float32", 1.0),
            (True, "bool", 1.0),
        ]
        for value, datatype, scale in test_cases:
            encoded = encode_value(value, datatype, scale)
            decoded = decode_value(encoded, datatype, scale)
            if datatype == "float32":
                assert abs(decoded - value) < 0.001
            else:
                assert decoded == value
