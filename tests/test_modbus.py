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
import logging
import math
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import zelos_sdk
from conftest import free_port, poll_once, wait_listening
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from zelos_extension_modbus import actions, registry
from zelos_extension_modbus.blocks import ReadBlock, plan_blocks
from zelos_extension_modbus.client import (
    REFUSED_RETRY,
    ModbusConnection,
    ModbusDevice,
    RequestFailed,
    _is_connection_error,
    _reorder_registers,
    coil_state,
    decode_block,
    decode_value,
    encode_register,
    encode_value,
)
from zelos_extension_modbus.constants import trace_layout
from zelos_extension_modbus.demo.simulator import (
    SCAN_TARGET_MAX_BLOCK,
    SCAN_TARGET_RANGES,
    PowerMeterSimulator,
    ScanTarget,
    create_demo_context,
    float32_to_registers,
    run_demo_server_sync,
    uint32_to_registers,
)
from zelos_extension_modbus.register_map import (
    Register,
    RegisterMap,
    load_configured_map,
    map_output_path,
    resolve_map_file,
)

_RATE_STATUS = (
    "requested_rate",
    "achieved_rate",
    "overload_pct",
    "demoted",
    "retry_in_s",
    "tiers",
    "refused",
)

_CONNECTION_KEYS = {
    "transport",
    "host",
    "port",
    "serial_port",
    "baudrate",
    "parity",
    "stopbits",
    "bytesize",
    "timeout",
    "retries",
    "request_delay_ms",
}


def _device(**kwargs) -> ModbusDevice:
    """A device on its own connection named "c"; kwargs split between the two."""
    link = {k: kwargs.pop(k) for k in list(kwargs) if k in _CONNECTION_KEYS}
    return ModbusDevice(ModbusConnection(name="c", **link), **kwargs)


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

    @pytest.mark.parametrize(
        ("field", "message"),
        [
            ("type", "Invalid register type"),
            ("datatype", "Invalid datatype"),
            ("byte_order", "Invalid byte_order"),
        ],
    )
    def test_invalid_field_raises(self, field, message):
        with pytest.raises(ValueError, match=message):
            Register(address=0, name="test", **{field: "invalid"})

    def test_byte_orders(self):
        """Default big; every ByteOrder is accepted."""
        assert Register(address=0, name="test").byte_order == "big"
        for order in ["big", "little", "big_swap", "little_swap"]:
            assert Register(address=0, name="test", byte_order=order).byte_order == order

    @pytest.mark.parametrize(
        ("type_", "writable"),
        [("holding", True), ("coil", True), ("input", False), ("discrete_input", False)],
    )
    def test_writable_by_type(self, type_, writable):
        """Read-only by default; writable: true opens holding and coil only."""
        assert Register(address=0, name="t", type=type_).writable is False
        assert Register(address=0, name="t", type=type_, writable=True).writable is writable

    def test_name_defaults_to_r_address(self):
        """Omitting name defaults it to r<address> in the map's base (default 1)."""
        assert Register(address=6, name="").name == "r7"  # wire 6
        assert Register(address=6, base=0).name == "r6"

    @pytest.mark.parametrize("rate", [-1.0, 0.005, "2", True, False])
    def test_bad_rate_raises(self, rate):
        """rate is 0 (not polled) or a number >= MIN_RATE; strings and bools are rejected."""
        with pytest.raises(ValueError, match="rate must be"):
            Register(address=0, name="t", rate=rate)


class TestRegisterMap:
    """Test RegisterMap parsing."""

    def test_from_dict_creates_events(self):
        """Events parse; the same field name in distinct events is fine."""
        data = {
            "events": {
                "voltage": [{"name": "L1", "address": 1}],
                "current": [{"name": "L1", "address": 7}],
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
                    {"name": "temp", "address": 1, "type": "holding"},
                    {"name": "alarm", "address": 1, "type": "coil"},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        regs = reg_map.get_event("status")
        assert regs[0].type == "holding"
        assert regs[1].type == "coil"

    def test_from_file(self):
        """Register map loads from JSON file."""
        data = {"events": {"test": [{"name": "reg", "address": 1}]}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            reg_map = RegisterMap.from_file(f.name)
        assert len(reg_map.registers) == 1
        Path(f.name).unlink()

    def test_resolve_map_file(self, tmp_path, monkeypatch):
        """Configured maps: ~ expands, relative is refused, a typo gets a close name,
        a value starting with `{` is the map itself."""
        monkeypatch.setenv("HOME", str(tmp_path))
        demo = Path(__file__).parents[1] / "zelos_extension_modbus/demo/power_meter.json"
        shutil.copy(demo, tmp_path / "meter.json")
        assert resolve_map_file("~/meter.json") == tmp_path / "meter.json"
        with pytest.raises(ValueError, match="absolute or start with ~"):
            resolve_map_file("maps/meter.json")
        with pytest.raises(FileNotFoundError, match=r"did you mean meter\.json"):
            resolve_map_file("~/meters.json")
        inline = load_configured_map(" \n" + demo.read_text())
        assert inline == load_configured_map("~/meter.json")
        with pytest.raises(ValueError, match=r"Invalid inline register map: .*line 1 column 12"):
            load_configured_map('{"events": ')
        missing = "Invalid inline register map: register 2 in event 'e' is missing 'address'"
        with pytest.raises(ValueError, match=missing):
            load_configured_map('{"events": {"e": [{"address": 1}, {"name": "v"}]}}')
        with pytest.raises(ValueError, match="absolute or start with ~"):
            load_configured_map("maps/meter.json")

    @pytest.mark.parametrize(
        ("path", "overwrite", "error"),
        [
            ("~/new.json", False, None),
            ("~/meter.json", True, None),
            ("~/meter.json", False, "exists; set overwrite"),
            ("maps/new.json", False, "absolute or start with ~"),
            ("~/missing/new.json", False, "does not exist"),
        ],
    )
    def test_map_output_path(self, tmp_path, monkeypatch, path, overwrite, error):
        """Save paths: ~ expands, relative is refused, the parent must exist, no silent clobber."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "meter.json").write_text("{}")
        if error:
            with pytest.raises(ValueError, match=error):
                map_output_path(path, overwrite)
        else:
            assert map_output_path(path, overwrite) == Path(path).expanduser()

    def test_get_by_name(self):
        """Find register by name across events."""
        data = {
            "events": {
                "a": [{"name": "voltage", "address": 1}],
                "b": [{"name": "current", "address": 2}],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert reg_map.get_by_name("voltage").address == 0
        assert reg_map.get_by_name("current").address == 1
        assert reg_map.get_by_name("nonexistent") is None

    def test_writable_registers(self):
        """writable_registers: marked holding and coil only, never input or discrete_input."""
        w = {"writable": True}
        data = {
            "events": {
                "sensors": [
                    {"name": "temp", "address": 1, "type": "holding", **w},
                    {"name": "sensor", "address": 2, "type": "input", **w},
                    {"name": "unmarked", "address": 3, "type": "holding"},
                ],
                "controls": [
                    {"name": "relay", "address": 1, "type": "coil", **w},
                    {"name": "status", "address": 1, "type": "discrete_input", **w},
                ],
            }
        }
        reg_map = RegisterMap.from_dict(data)
        writable = reg_map.writable_registers
        assert len(writable) == 2
        assert {r.name for r in writable} == {"temp", "relay"}

    def test_from_dict_name_optional(self):
        """A register with no name defaults to r<address>."""
        data = {"events": {"e": [{"address": 6}, {"address": 7}]}}
        reg_map = RegisterMap.from_dict(data)
        names = [r.name for r in reg_map.get_event("e")]
        assert names == ["r6", "r7"]  # the map's (1-based) address

    def test_rate_from_dict(self):
        """Absent rate inherits (None); a number is kept; JSON null means not polled (0)."""
        data = {
            "events": {
                "e": [
                    {"name": "a", "address": 1},
                    {"name": "b", "address": 2, "rate": 5.0},
                    {"name": "c", "address": 3, "rate": None},
                ]
            }
        }
        reg_map = RegisterMap.from_dict(data)
        assert [r.rate for r in reg_map.registers] == [None, 5.0, 0.0]

    @pytest.mark.parametrize(("a", "b", "field"), [("v", "v", "v"), ("a.b", "a_b", "a_b")])
    def test_duplicate_field_names_raise(self, a, b, field):
        """Names that collide raw or after sanitization (a.b vs a_b) fail the load."""
        data = {"events": {"e": [{"name": a, "address": 1}, {"name": b, "address": 2}]}}
        with pytest.raises(ValueError, match=f"Duplicate field name '{field}' in event 'e'"):
            RegisterMap.from_dict(data)

    @pytest.mark.parametrize(
        "device",
        [
            {"bogus": 1},
            {"max_block_size": 0},
            {"max_block_size": 126},
            {"max_block_size": True},
            {"max_read_gap": -1},
            {"write_mode": "fc6"},
            {"byte_order": "middle"},
            {"max_bit_block_size": 2001},
            {"min_rate": -1},
            {"close_after_sweep": 1},
            {"address_base": 2},
            [],
        ],
    )
    def test_device_block_invalid_raises(self, device):
        """An unknown key or bad value in the map "device" block fails the load."""
        with pytest.raises(ValueError):
            RegisterMap.from_dict({"device": device, "events": {}})

    def test_device_block_defaults(self):
        """device.byte_order defaults registers without their own; addresses are 1-based
        by default (map 1 = wire 0) and wire addresses with address_base 0."""
        device = {"byte_order": "big_swap", "max_block_size": 60}
        events = {
            "e": [
                {"name": "a", "address": 1},
                {"name": "b", "address": 3, "byte_order": "big"},
                {"name": "c", "type": "coil", "address": 1},
            ]
        }
        reg_map = RegisterMap.from_dict({"device": device, "events": events})
        assert reg_map.device == device
        assert [r.address for r in reg_map.registers] == [0, 2, 0]
        assert [r.map_address for r in reg_map.registers] == [1, 3, 1]
        assert reg_map.get_by_name("a").byte_order == "big_swap"
        assert reg_map.get_by_name("b").byte_order == "big"
        with pytest.raises(ValueError, match="address_base 1"):
            RegisterMap.from_dict({"device": device, "events": {"e": [{"address": 0}]}})
        wire = RegisterMap.from_dict(
            {"device": {"address_base": 0}, "events": {"e": [{"address": 0}]}}
        )
        assert (wire.registers[0].address, wire.registers[0].name) == (0, "r0")


# =============================================================================
# Value Encoding/Decoding Tests
# =============================================================================


class TestValueCodec:
    """Test value encoding and decoding."""

    @pytest.mark.parametrize(
        ("datatype", "raw", "scale", "expected"),
        [
            ("uint16", [1000], 1, 1000),
            ("int16", [65535], 1, -1),
            ("int16", [32768], 1, -32768),
            ("bool", [1], 1, True),
            ("bool", [0], 1, False),
            ("uint32", [0x0001, 0x0000], 1, 65536),
            ("float32", [0x4048, 0xF5C3], 1, pytest.approx(3.14, abs=0.01)),
            ("uint16", [1000], 0.1, 100),
            # Unscaled 64-bit stays exact past 2**53 (a float multiply would round).
            ("uint64", [0xFFFF] * 4, 1, 2**64 - 1),
            ("uint64", [0x0020, 0x0000, 0x0000, 0x0001], 1, 2**53 + 1),
            ("int64", [0x7FFF, 0xFFFF, 0xFFFF, 0xFFFF], 1, 2**63 - 1),
            ("int64", [0x8000, 0x0000, 0x0000, 0x0000], 1, -(2**63)),
            ("int64", [0xFFFF] * 4, 1, -1),
        ],
    )
    def test_decode(self, datatype, raw, scale, expected):
        assert decode_value(raw, datatype, scale=scale) == expected

    @pytest.mark.parametrize(
        ("datatype", "value", "scale", "expected"),
        [
            ("uint16", 1000, 1, [1000]),
            ("int16", -1, 1, [65535]),
            ("bool", True, 1, [1]),
            ("bool", False, 1, [0]),
            ("uint32", 65536, 1, [0x0001, 0x0000]),
            ("uint16", 100, 0.1, [1000]),
        ],
    )
    def test_encode(self, datatype, value, scale, expected):
        assert encode_value(value, datatype, scale=scale) == expected

    def test_encode_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            encode_value(6553.6, "uint16", scale=0.1)

    @pytest.mark.parametrize(
        ("datatype", "scale", "value", "nearest"),
        [
            ("uint16", 1.0, 2.5, "3"),
            ("int16", 1.0, -1.9, "-2"),
            ("uint16", 0.1, 230.57, "230.6"),
            # Exact in raw counts: a relative tolerance would accept these.
            ("uint32", 1.0, 1_000_000_000.4, "1000000000"),
            ("uint32", 0.1, 123456789.13, "123456789.1"),
        ],
    )
    def test_write_must_round_trip(self, datatype, scale, value, nearest):
        """A value the register cannot hold exactly is refused, naming the nearest one."""
        reg = Register(address=0, datatype=datatype, scale=scale)
        with pytest.raises(ValueError, match=f"nearest writable value is {nearest}$"):
            encode_register(reg, value)
        assert encode_register(reg, float(nearest)) == (
            encode_value(float(nearest), datatype, scale),
            float(nearest),
        )
        assert encode_register(Register(address=0, datatype="float32"), 0.1)[1] == pytest.approx(
            0.1
        )


class TestByteOrder:
    """Test byte order handling for multi-register values."""

    def test_reorder_single_register_unchanged(self):
        """Single register values are unchanged by byte order."""
        regs = [0x1234]
        for order in ["big", "little", "big_swap", "little_swap"]:
            assert _reorder_registers(regs, order) == [0x1234]

    @pytest.mark.parametrize(
        "datatype,value,scale,words",
        [
            (
                "float32",
                1.0,
                1,
                {
                    "big": [0x3F80, 0x0000],
                    "little": [0x0000, 0x803F],
                    "big_swap": [0x0000, 0x3F80],
                    "little_swap": [0x803F, 0x0000],
                },
            ),
            (
                "uint32",
                0x11223344,
                1,
                {
                    "big": [0x1122, 0x3344],
                    "little": [0x4433, 0x2211],
                    "big_swap": [0x3344, 0x1122],
                    "little_swap": [0x2211, 0x4433],
                },
            ),
            (
                "uint64",
                0x1122334455667788,
                1,
                {
                    "big": [0x1122, 0x3344, 0x5566, 0x7788],
                    "little": [0x8877, 0x6655, 0x4433, 0x2211],
                    "big_swap": [0x7788, 0x5566, 0x3344, 0x1122],
                    "little_swap": [0x2211, 0x4433, 0x6655, 0x8877],
                },
            ),
            # A scaled integer decodes to a float, and encodes rounded to nearest.
            ("int32", -123.4, 0.1, {"big": [0xFFFF, 0xFB2E], "little": [0x2EFB, 0xFFFF]}),
            ("uint16", 123.4, 0.1, {"big": [1234], "little": [1234]}),
        ],
    )
    def test_known_vectors(self, datatype, value, scale, words):
        """Standard orders (A = MSB): big ABCD, little DCBA, big_swap CDAB, little_swap BADC."""
        for order, raw in words.items():
            assert encode_value(value, datatype, scale, order) == raw
            decoded = decode_value(raw, datatype, scale, order)
            assert decoded == pytest.approx(value) and type(decoded) is type(value)


# =============================================================================
# Block Planner Tests (pure)
# =============================================================================


def _block_tuples(blocks):
    """Summarize blocks as (type, address, count) for easy assertion."""
    return [(b.type, b.address, b.count) for b in blocks]


class TestBlockPlanner:
    """Test the pure block-read planner."""

    @pytest.mark.parametrize(
        ("regs", "kwargs", "expected"),
        [
            pytest.param(
                [Register(address=a, name=f"r{a}", datatype="float32") for a in (0, 2, 4)],
                {},
                [("holding", 0, 6)],
                id="contiguous",
            ),
            pytest.param(
                [Register(address=0, name="a", datatype="float32")],
                {"max_block_size": 1},
                [("holding", 0, 2)],
                id="oversized-span-one-block",
            ),
            pytest.param(
                [Register(address=a, name=f"r{a}") for a in range(4)],
                {"max_block_size": 2},
                [("holding", 0, 2), ("holding", 2, 2)],
                id="max-block-size",
            ),
            pytest.param(
                [Register(address=0, name="a"), Register(address=2, name="b")],
                {"max_read_gap": 0},
                [("holding", 0, 1), ("holding", 2, 1)],
                id="gap-zero-splits",
            ),
            pytest.param(
                [Register(address=0, name="a"), Register(address=2, name="b")],
                {"max_read_gap": 1},
                [("holding", 0, 3)],
                id="gap-bridged",
            ),
            pytest.param(
                [Register(address=5, name="a"), Register(address=5, name="b")],
                {},
                [("holding", 5, 1)],
                id="duplicate-address",
            ),
            pytest.param(
                [Register(address=0, name=t, type=t) for t in ("holding", "coil", "input")]
                + [Register(address=0, name="d", type="discrete_input")],
                {},
                [("coil", 0, 1), ("discrete_input", 0, 1), ("holding", 0, 1), ("input", 0, 1)],
                id="types-separate-sorted",
            ),
            pytest.param(
                [Register(address=a, name=f"c{a}", type="coil") for a in range(3)],
                {},
                [("coil", 0, 3)],
                id="coils-coalesce",
            ),
        ],
    )
    def test_plan(self, regs, kwargs, expected):
        blocks = plan_blocks(regs, **kwargs)
        assert _block_tuples(blocks) == expected
        assert sum(len(b.registers) for b in blocks) == len(regs)

    def test_demo_map_coalescing(self, register_map):
        """The bundled demo map coalesces into the expected transactions."""
        blocks = plan_blocks(register_map.registers)
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
        decoded = {id(r): v for r, v in decode_block(block, raw)}
        assert abs(decoded[id(regs[0])] - 3.14) < 0.01
        assert abs(decoded[id(regs[1])] - 1.0) < 0.01

    def test_duplicate_address_decode(self):
        """Two registers at the same address decode independently from one slice."""
        r1 = Register(address=0, name="a", datatype="uint16")
        r2 = Register(address=0, name="b", datatype="uint16", scale=0.1)
        block = ReadBlock(type="holding", address=0, count=1, registers=(r1, r2))
        decoded = {id(r): v for r, v in decode_block(block, [1000])}
        assert decoded[id(r1)] == 1000
        assert decoded[id(r2)] == 100

    def test_short_response_skips_register(self):
        """A register whose slice is truncated is skipped, not decoded from garbage."""
        reg = Register(address=0, name="a", datatype="float32")
        block = ReadBlock(type="holding", address=0, count=2, registers=(reg,))
        decoded = {id(r): v for r, v in decode_block(block, [0x4048])}  # need two, got one
        assert id(reg) not in decoded

    def test_bit_block_returns_bools(self):
        """Coil/discrete blocks return raw booleans."""
        regs = (
            Register(address=0, name="a", type="coil"),
            Register(address=1, name="b", type="coil"),
        )
        block = ReadBlock(type="coil", address=0, count=2, registers=regs)
        decoded = {id(r): v for r, v in decode_block(block, [True, False])}
        assert decoded[id(regs[0])] is True
        assert decoded[id(regs[1])] is False


# =============================================================================
# Simulator Tests (no network)
# =============================================================================


class TestPowerMeterSimulator:
    """Simulator helpers and physics."""

    def test_register_helpers(self):
        """float32/uint32 split into two big-endian registers."""
        packed = struct.pack(">HH", *float32_to_registers(3.14))
        assert struct.unpack(">f", packed)[0] == pytest.approx(3.14, abs=0.01)
        assert uint32_to_registers(65536) == (0x0001, 0x0000)

    def test_update_values_in_range(self):
        """Every field is produced; voltage, frequency and power factor near nominal."""
        values = PowerMeterSimulator().update(dt=0.1)
        assert set(values) == {
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
        for phase in ["voltage_l1", "voltage_l2", "voltage_l3"]:
            assert 218 < values[phase] < 242  # 230V +-5%
        assert 49.9 < values["frequency"] < 50.1
        assert 0.7 < values["power_factor"] < 1.0

    def test_energy_accumulates(self):
        sim = PowerMeterSimulator()
        sim.update(dt=1.0)
        e1 = sim.energy_total
        sim.update(dt=1.0)
        assert sim.energy_total > e1


class TestDemoServerSyncWrapper:
    """run_demo_server_sync: the blocking CLI entry point for the simulator."""

    @pytest.fixture(autouse=True)
    def _keep_main_event_loop(self):
        """Put this thread's event loop back after the wrapper's asyncio.run().

        asyncio.run() unsets the current event loop on the way out, and the rest
        of the suite drives coroutines with get_event_loop().run_until_complete().
        """
        loop = asyncio.get_event_loop()
        yield
        asyncio.set_event_loop(loop)

    def test_bind_failure_names_the_endpoint(self):
        """A taken port fails with host:port and the likely cause, not a bare traceback."""
        occupied = socket.socket()
        # Port 0 = OS-assigned ephemeral, so this never collides with a fixture
        # port or the developer's own simulator on 5020.
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        try:
            with pytest.raises(RuntimeError) as exc:
                run_demo_server_sync(host="127.0.0.1", port=port)
        finally:
            occupied.close()

        message = str(exc.value)
        assert f"127.0.0.1:{port}" in message
        # pymodbus only says "Could not start listen, please check address."
        assert "simulator may already be running" in message

    def test_keyboard_interrupt_is_a_clean_stop(self, monkeypatch):
        """Ctrl-C exits normally instead of propagating out of the CLI."""

        async def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("zelos_extension_modbus.demo.simulator.StartAsyncTcpServer", _interrupt)
        run_demo_server_sync(host="127.0.0.1", port=0)  # returns, does not raise


# =============================================================================
# Integration Tests with Demo Server
# =============================================================================


class DemoServer:
    """Helper to run demo server in background thread."""

    def __init__(self, port: int | None = None, unit_ids=()):
        self.host = "127.0.0.1"
        self.port = port or free_port()
        self.unit_ids = unit_ids
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None

    def start(self):
        """Start server in background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        wait_listening(self.port)

    def _run(self):
        """Run server event loop."""
        from pymodbus.server import ModbusTcpServer

        from zelos_extension_modbus.demo.simulator import SimulatorUpdater

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        context = create_demo_context(self.unit_ids)
        self._updaters = [
            SimulatorUpdater(PowerMeterSimulator(), context, interval=0.05, unit_id=uid)
            for uid in self.unit_ids or [0]
        ]
        for updater in self._updaters:
            updater.start()

        async def run_server():
            self._server = ModbusTcpServer(context, address=(self.host, self.port))
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(run_server())
        except Exception:
            pass
        finally:
            for updater in self._updaters:
                updater.stop()

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
    server = DemoServer()
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
    """Create a connected device."""
    client = _device(
        host=demo_server.host,
        port=demo_server.port,
        register_map=register_map,
    )

    async def connect():
        await client.connection.connect()

    asyncio.get_event_loop().run_until_complete(connect())
    yield client

    async def disconnect():
        await client.connection.disconnect()

    asyncio.get_event_loop().run_until_complete(disconnect())


def _demo_client(demo_server, register_map, **kwargs):
    """A device pointed at the demo server with the given register map."""
    return _device(
        host=demo_server.host,
        port=demo_server.port,
        register_map=register_map,
        **kwargs,
    )


def _connected_poll(client):
    """Connect, run one poll cycle, disconnect; return the poll results."""

    async def run():
        await client.connection.connect()
        try:
            return await poll_once(client)
        finally:
            await client.connection.disconnect()

    return asyncio.get_event_loop().run_until_complete(run())


@pytest.fixture(params=["tcp", "rtu"])
def link_client(request, register_map):
    """A connected device on the demo meter, over TCP or RTU (socat; skipped without it)."""
    if request.param == "tcp":
        server = request.getfixturevalue("demo_server")
        dev = _device(host=server.host, port=server.port, register_map=register_map)
    else:
        _, client_port = request.getfixturevalue("serial_ports")
        request.getfixturevalue("rtu_demo_server")
        dev = _device(
            transport="rtu", serial_port=client_port, timeout=2.0, register_map=register_map
        )
    run = asyncio.get_event_loop().run_until_complete
    run(dev.connection.connect())
    yield dev
    run(dev.connection.disconnect())


class TestLinkIntegration:
    """Reads, writes and polls against the demo meter, over TCP and RTU."""

    @pytest.mark.parametrize(
        ("name", "check"),
        [
            ("L1", lambda v: 200 < v < 260),  # float32 holding
            ("energy", lambda v: isinstance(v, int) and v >= 0),  # uint32 holding
            ("temperature", lambda v: 0 < v < 100),  # int16, scale 0.1
            ("firmware_version", lambda v: v == 0x0102),  # input
            ("serial_number", lambda v: v == 12345678),  # uint32 input
            ("relay1", lambda v: v in (True, False)),  # coil
            ("grid_connected", lambda v: v is True),  # discrete input
            ("calibration_factor", lambda v: abs(v - 1.0) < 0.01),  # float32 big_swap
        ],
    )
    def test_read(self, link_client, name, check):
        reg = link_client.register_map.get_by_name(name)
        run = asyncio.get_event_loop().run_until_complete
        value = run(link_client.read_register_value(reg))
        assert value is not None and check(value)

    @pytest.mark.parametrize(
        ("name", "values"),
        [
            ("voltage_high_limit", [245]),  # uint16
            ("power_limit", [-10000]),  # int32, signed
            ("offset_value", [3.14159]),  # float32 big_swap
            ("coil3", [True, False]),  # the simulator drives coils 0-2 only
        ],
    )
    def test_write_reads_back(self, link_client, name, values):
        if name == "coil3":
            reg = Register(address=3, name=name, type="coil", datatype="bool", writable=True)
        else:
            reg = link_client.register_map.get_by_name(name)

        async def run():
            got = []
            for value in values:
                assert await link_client.write_register_value(reg, value) is True
                got.append(await link_client.read_register_value(reg))
            return got

        got = asyncio.get_event_loop().run_until_complete(run())
        assert got == [pytest.approx(v, abs=1e-3) if isinstance(v, float) else v for v in values]

    def test_poll_all_events(self, link_client):
        results = asyncio.get_event_loop().run_until_complete(poll_once(link_client))
        assert set(results) >= {
            "voltage",
            "current",
            "power",
            "status",
            "inputs",
            "digital_inputs",
            "setpoints",
            "swapped_floats",
        }
        assert {"L1", "L2", "L3"} <= set(results["voltage"])
        assert 200 < results["voltage"]["L1"] < 260

    @pytest.mark.parametrize("name", ["firmware_version", "door_open"])
    def test_write_read_only_refused(self, register_map, name):
        """Input and discrete input writes are refused before any I/O."""
        dev = _device(register_map=register_map)
        reg = register_map.get_by_name(name)
        assert reg.writable is False
        run = asyncio.get_event_loop().run_until_complete
        assert run(dev.write_register_value(reg, 1)) is False

    def test_request_delay_spaces_requests(self, demo_server, register_map):
        """request_delay_ms is a minimum gap between consecutive requests."""
        client = _demo_client(demo_server, register_map, request_delay_ms=100)

        async def run():
            await client.connection.connect()
            try:
                start = time.monotonic()
                for _ in range(3):
                    assert await client._read_range("holding", 0, 2) is not None
                return time.monotonic() - start
            finally:
                await client.connection.disconnect()

        # Two gaps between three requests; the first request is not delayed.
        assert asyncio.get_event_loop().run_until_complete(run()) >= 0.2


# =============================================================================
# Block Reads Integration Tests
# =============================================================================


def _spy_reads(client):
    """Wrap a client's reads to record (address, count) per read kind."""
    calls = {"holding": [], "input": [], "coil": [], "discrete": []}
    kinds = {"holding": "holding", "input": "input", "coil": "coil", "discrete_input": "discrete"}
    fetch = client._fetch

    async def wrapper(reg_type, address, count):
        calls[kinds[reg_type]].append((address, count))
        return await fetch(reg_type, address, count)

    client._fetch = wrapper
    return calls


class TestBlockReadsIntegration:
    """Block-read polling against the demo server."""

    def test_demo_map_coalesces_transactions(self, client):
        """The demo map polls in the expected coalesced transactions."""
        calls = _spy_reads(client)
        results = asyncio.get_event_loop().run_until_complete(poll_once(client))

        assert sorted(calls["holding"]) == [(0, 21), (100, 6), (110, 4)]
        assert calls["input"] == [(0, 5)]
        assert calls["coil"] == [(0, 3)]
        assert calls["discrete"] == [(0, 3)]
        # Results are still keyed by event/field.
        assert 200 < results["voltage"]["L1"] < 260

    def test_block_reads_false_one_call_per_register(self, demo_server, register_map):
        """With block_reads disabled there is one transaction per register."""
        client = _demo_client(demo_server, register_map, block_reads=False)
        calls = _spy_reads(client)
        results = _connected_poll(client)

        holding_regs = [r for r in register_map.registers if r.type == "holding"]
        assert len(calls["holding"]) == len(holding_regs)
        # Same result shape and cache as block mode.
        assert 200 < results["voltage"]["L1"] < 260
        assert client.last_values["power/energy"][0] == results["power"]["energy"]

    def test_failed_block_skips_only_its_registers(self, demo_server):
        """A block whose address is beyond the datastore skips only its registers."""
        data = {
            "name": "failskip",
            "events": {
                "good": [{"name": "v", "address": 1, "datatype": "float32"}],
                "bad": [{"name": "x", "address": 5001, "datatype": "uint16"}],
            },
        }
        client = _demo_client(demo_server, RegisterMap.from_dict(data))
        results = _connected_poll(client)
        assert "v" in results.get("good", {})
        assert "bad" not in results

    def test_max_block_size_limits_counts(self, demo_server, register_map):
        """max_block_size caps every transaction's count while keeping full coverage."""
        client = _demo_client(demo_server, register_map, max_block_size=8)
        calls = _spy_reads(client)
        results = _connected_poll(client)

        all_counts = [count for kind in calls.values() for (_, count) in kind]
        assert all_counts  # something was read
        assert all(c <= 8 for c in all_counts)
        # Full coverage: every event still produced values.
        for event in register_map.event_names:
            assert event in results


class TestSharedConnection:
    """Two devices on one connection share the link, one request at a time."""

    def test_two_units_poll_serialized(self, register_map):
        """Both unit ids poll their own data and requests never overlap on the wire."""
        server = DemoServer(unit_ids=(1, 2))
        server.start()
        conn = ModbusConnection(host=server.host, port=server.port)
        devices = [ModbusDevice(conn, unit_id=u, register_map=register_map) for u in (1, 2)]
        in_flight, max_in_flight, unit_ids = 0, 0, []

        def spy(call):
            async def wrapped(*args, device_id, **kwargs):
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                unit_ids.append(device_id)
                try:
                    return await call(*args, device_id=device_id, **kwargs)
                finally:
                    in_flight -= 1

            return wrapped

        async def run():
            await conn.connect()
            for method in ("read_holding_registers", "read_input_registers", "read_coils"):
                setattr(conn._client, method, spy(getattr(conn._client, method)))
            conn.start("", None)
            poll = asyncio.create_task(conn.run_async())
            # Action-style reads race the poll loop from both devices.
            await asyncio.gather(*(d._read_range("holding", 0, 2) for d in devices * 5))
            await asyncio.sleep(1.5)
            conn.stop()
            await poll

        try:
            asyncio.get_event_loop().run_until_complete(run())
        finally:
            server.stop()

        assert max_in_flight == 1
        assert set(unit_ids) == {1, 2}
        serials = [d.last_values["inputs/serial_number"][0] for d in devices]
        assert serials == [12345679, 12345680]  # each unit's own datastore
        assert all(d._poll_count >= 2 for d in devices)


# =============================================================================
# Poll Scheduler Tests (fake clock)
# =============================================================================


def _fake_device(events, conn=None, latency=0.0, bad=(), dead=False, **kwargs):
    """A device over ``events`` with a fake ``_fetch``; no network.

    Returns (device, reads) where reads collects (unit, address, count, start).
    A read covering an address in ``bad`` answers exception 02; a ``dead``
    device never answers (raises after ``latency``).
    """
    conn = conn or ModbusConnection(name="c")
    dev = ModbusDevice(
        conn,
        register_map=RegisterMap.from_dict({"name": "sched", "events": events}),
        **kwargs,
    )
    reads: list[tuple[int, int, int, float]] = []

    async def fetch(reg_type, address, count):
        reads.append((dev.unit_id, address, count, time.monotonic()))
        if latency:
            await asyncio.sleep(latency)
        if dead:
            raise ModbusIOException("no response")
        if any(address <= a < address + count for a in bad):
            return 2
        return [123] * count

    dev._fetch = fetch
    return dev, reads


def _auto_device(tables, conn=None, broken=None, **kwargs):
    """An auto-scanning device over fake ``tables`` ({table: {wire: value}}); no network.

    Like the scan target: reads past SCAN_TARGET_MAX_BLOCK answer 03, holes 02. A
    holding read covering an address in ``broken`` answers that code, or never
    (None). Returns (device, reads) with reads as in ``_fake_device``.
    """
    dev = ModbusDevice(conn or ModbusConnection(name="c"), auto_scan=True, **kwargs)
    reads: list[tuple[int, int, int, float]] = []
    broken = {} if broken is None else broken

    async def fetch(reg_type, address, count):
        reads.append((dev.unit_id, address, count, time.monotonic()))
        await asyncio.sleep(0)
        span = range(address, address + count)
        hit = sorted(broken.keys() & set(span)) if reg_type == "holding" else []
        if hit and broken[hit[0]] is None:
            raise ModbusIOException("no response")
        if hit or count > SCAN_TARGET_MAX_BLOCK:
            return broken[hit[0]] if hit else 3
        values = tables.get(reg_type, {})
        return [values[a] for a in span] if all(a in values for a in span) else 2

    dev._fetch = fetch
    return dev, reads


def _discover_all(dev, now=0.0):
    run = asyncio.get_event_loop().run_until_complete
    while dev.scanning:
        run(dev._discover(now))


async def _run_for(conn, seconds):
    """Run ``conn``'s poll loop for ``seconds`` against fake devices (link always up)."""

    async def up():
        conn.connected = True
        return True

    conn.ensure_connected = up
    conn._running = True
    task = asyncio.create_task(conn.run_async())
    await asyncio.sleep(seconds)
    conn.stop()
    await task


class TestPollScheduler:
    """Per-rate block scheduling, demotion and illegal-address isolation."""

    def test_due_set_membership(self):
        """Due blocks follow each register's own rate; a stall leaves no backlog."""
        dev, reads = _fake_device(
            {
                "fast": [{"name": "f", "address": 1}],
                "slow": [{"name": "s", "address": 2, "rate": 5.0}],
            },
            block_reads=False,
        )
        run = asyncio.get_event_loop().run_until_complete

        def polled(now):
            reads.clear()
            run(poll_once(dev, now=now))
            return sorted(r[1] for r in reads)

        assert polled(0.0) == [0, 1]
        assert polled(1.0) == [0]  # slow not due until 5.0
        assert polled(12.0) == [0, 1]  # each once, rescheduled from now
        assert sorted(b.next_due for b in dev._blocks) == [13.0, 17.0]

    def test_unpolled_registers_never_read(self):
        """rate 0 on a register, or on the device for registers without one, is not polled."""
        dev, reads = _fake_device(
            {"e": [{"name": "on", "address": 1, "rate": 1.0}, {"name": "off", "address": 2}]},
            rate=0,
        )
        for now in range(5):
            asyncio.get_event_loop().run_until_complete(poll_once(dev, now=float(now)))
        assert {r[1] for r in reads} == {0}
        assert list(dev.polled_events["e"]) == [dev.register_map.get_by_name("on")]

    def test_slow_blocks_spread(self):
        """Slower blocks go one per tick: the fast point never waits behind a burst.

        Reading all 20 slow blocks at once would hold the fast point back
        20 x 20 ms on top of its 0.2 s rate.
        """
        events = {"fast": [{"name": "f", "address": 1}]}
        events |= {f"s{k}": [{"name": "v", "address": 10 * k + 10, "rate": 1.0}] for k in range(20)}
        dev, reads = _fake_device(events, rate=0.2, latency=0.02)
        asyncio.get_event_loop().run_until_complete(_run_for(dev.connection, 2.6))

        # A burst puts >= 0.2 + 20 x 0.02 = 0.6 s between fast reads; spread keeps it
        # near 0.22 s. The midpoint leaves ~0.18 s of headroom for a loaded machine.
        fast = [r[3] for r in reads if r[1] == 0]
        assert max(b - a for a, b in zip(fast, fast[1:], strict=False)) < 0.4
        assert {r[1] for r in reads if r[1]} == {10 * k + 9 for k in range(20)}

    def test_dead_device_demoted(self, monkeypatch, caplog):
        """A dead unit is demoted after demote_after timeouts; then only probes cost link time.
        Silent since start, it is unreachable: one ERROR, then retried like a later drop."""
        monkeypatch.setattr("zelos_extension_modbus.client.DEMOTE_BACKOFF", 0.5)
        conn = ModbusConnection(name="c")
        events = {"e": [{"name": "v", "address": 1}]}
        live, live_reads = _fake_device(events, conn, unit_id=1, rate=0.1)
        dead, dead_reads = _fake_device(events, conn, unit_id=2, rate=0.1, dead=True, latency=0.2)
        with caplog.at_level(logging.WARNING):
            asyncio.get_event_loop().run_until_complete(_run_for(conn, 2.0))
        (error,) = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert error.startswith("Device 'c/unit2' (unit 2): no response since start")
        assert (live.answered, dead.answered) == (True, False)
        assert dead.last_error.startswith("no response since start")

        # 3 timeouts, then probes after 0.5 s and 1 s more (backoff doubles).
        assert 3 <= len(dead_reads) <= 5
        assert dead.rate_status()["demoted"] and dead.rate_status()["retry_in_s"] > 0
        # Structural, not wall-clock: once demoted, at most one probe between live reads.
        demoted_at = dead_reads[2][3]
        merged = sorted(live_reads + dead_reads, key=lambda r: r[3])
        after = [r[0] for r in merged if r[3] > demoted_at]
        live_at = [i for i, u in enumerate(after) if u == 1]
        assert len(live_at) >= 3
        assert max(b - a - 1 for a, b in zip(live_at, live_at[1:], strict=False)) <= 1
        assert live.rate_status()["demoted"] is False

    def test_demoted_units_cost_one_probe_per_tick(self, monkeypatch):
        """Demoted units due together: the fast reads first, then ONE probe per tick."""
        monkeypatch.setattr("zelos_extension_modbus.client.DEMOTE_BACKOFF", 0.3)
        conn = ModbusConnection(name="c")
        events = {"e": [{"name": "v", "address": 1}]}
        _, live_reads = _fake_device(events, conn, unit_id=1, rate=0.1)
        dead_reads = []
        for unit in range(2, 7):
            dead_reads.append(_fake_device(events, conn, unit_id=unit, dead=True, latency=0.1)[1])
        asyncio.get_event_loop().run_until_complete(_run_for(conn, 3.5))

        demoted_at = max(reads[2][3] for reads in dead_reads)
        merged = sorted(live_reads + sum(dead_reads, []), key=lambda r: r[3])
        after = [r[0] for r in merged if r[3] > demoted_at]
        live_at = [i for i, u in enumerate(after) if u == 1]
        assert len(after) - len(live_at) >= 5  # every unit probed
        assert max(b - a - 1 for a, b in zip(live_at, live_at[1:], strict=False)) <= 1

    def test_demoted_map_build_is_one_request(self):
        """A demoted unit awaiting its map is probed with one read, not a full discovery."""
        answer = [None]
        calls = []

        async def loader(dev):
            calls.append("discover")
            raise ModbusIOException("no response")

        async def fetch(reg_type, address, count):
            calls.append(address)
            if answer[0] is None:
                raise ModbusIOException("no response")
            return answer[0]

        dev = ModbusDevice(ModbusConnection(name="c"), map_loader=loader)
        dev._fetch = fetch
        run = asyncio.get_event_loop().run_until_complete
        for _ in range(dev.demote_after):
            run(dev.load_map(0.0))
        assert dev.demoted and calls == ["discover"] * 3
        calls.clear()
        run(dev.load_map(100.0))
        assert calls == [40000] and dev.demoted  # still silent: one request, backs off
        assert not dev.answered
        answer[0] = 2  # an exception answer: the unit is present
        run(dev.load_map(200.0))
        assert calls == [40000, 40000] and not dev.demoted and dev.map_pending
        assert (dev.answered, dev.last_error) == (True, None)
        conn = dev.connection
        assert conn._batch(200.0) == [(dev, None)]  # discovery is now normal work

    def test_headline_is_the_worst_tier(self):
        """Top-level rates are the most overloaded tier's; tiers keep the detail."""
        events = {"e": [{"name": "f", "address": 1}, {"name": "s", "address": 2, "rate": 5.0}]}
        dev, _ = _fake_device(events)
        dev._schedule(0.0)
        dev._tiers[1.0].interval, dev._tiers[5.0].interval = 1.1, 15.0
        status = dev.rate_status()
        assert (status["requested_rate"], status["overload_pct"]) == (5.0, 200.0)
        assert [t["requested_rate"] for t in status["tiers"]] == [1.0, 5.0]

    def test_link_down_misses_reads(self):
        """While the link is down, every read that falls due fails; no rate is achieved."""
        events = {"e": [{"name": "f", "address": 1}, {"name": "s", "address": 2, "rate": 5.0}]}
        dev, _ = _fake_device(events)
        dev._schedule(0.0)
        dev._tiers[1.0].interval = 1.0
        dev._missed(2.5)  # 1 s block due at 0, 1, 2; 5 s block at 0
        dev._missed(2.9)
        assert dev.failed_reads == 4 and dev.rate_status()["achieved_rate"] is None

    def test_refused_block_deactivated(self, caplog):
        """Exception 02 deactivates its whole block (static size): one warning in
        the map's base, its fields unlogged, the rest keep polling, retried every 10 min."""
        events = {"e": [{"name": f"v{a}", "address": a} for a in range(1, 9)]}
        dev, reads = _fake_device(events, bad={3}, max_block_size=4)  # wire 3 = map 4
        run = asyncio.get_event_loop().run_until_complete

        with caplog.at_level(logging.WARNING):
            values = run(poll_once(dev, now=0.0))["e"]
            run(poll_once(dev, now=1.0))
            run(poll_once(dev, now=REFUSED_RETRY))  # retried, still refused
        assert sorted(values) == ["v5", "v6", "v7", "v8"]
        assert caplog.text.count("Device refuses holding 1-4") == 1
        assert "verify" in caplog.text
        assert [(r[1], r[2]) for r in reads] == [(0, 4), (4, 4), (4, 4), (0, 4), (4, 4)]
        assert (dev.successful_reads, dev.failed_reads) == (3, 2)
        (row,) = dev.rate_status()["refused"]
        assert (row["range"], row["code"]) == ("holding 1-4", 2)
        # A probe never takes the refused block before its 10 min retry, and
        # recovering keeps that backoff.
        refused = next(b for b in dev._blocks if b.refused)
        due = refused.next_due
        for _ in range(dev.demote_after):
            dev._timed_out(REFUSED_RETRY, "no response")
        assert dev._probe(REFUSED_RETRY + 0.5) is None  # nothing but the refused block due
        t = REFUSED_RETRY + 20
        probe = dev._probe(t)
        assert probe is not refused
        run(dev._read_block(probe, t))
        assert not dev.demoted and refused.next_due == due
        # The answered probe is due one rate on, and no rate is claimed until
        # a normal interval passes.
        assert probe.next_due == t + 1.0 and dev.rate_status()["achieved_rate"] is None
        run(dev._read_block(probe, t + 1.0))
        assert dev.rate_status()["achieved_rate"] == 1.0

    def test_auto_scan_polls_as_it_finds(self, monkeypatch):
        """Auto-scan finds the scan target's ranges one read per tick, polling each find
        at its slow rate at once, while a mapped meter on the link keeps its fast rate."""
        monkeypatch.setattr("zelos_extension_modbus.scan.TCP_WINDOWS", ((0, 1999),))
        conn = ModbusConnection(name="c")
        _, meter_reads = _fake_device(
            {"e": [{"name": "v", "address": 1}]}, conn, unit_id=2, rate=0.1
        )
        dev, _ = _auto_device(ScanTarget().tables, conn, rate=1.0)
        progress = []
        discover = dev._discover

        async def step(now):
            await discover(now)
            progress.append((dev.scanning, dev.successful_reads))

        dev._discover = step
        asyncio.get_event_loop().run_until_complete(_run_for(conn, 2.5))

        assert any(scanning and polled for scanning, polled in progress)  # incremental
        assert dev.auto_scan_status() == {
            "state": "done",
            "table": None,
            "found": sum(hi - lo + 1 for runs in SCAN_TARGET_RANGES.values() for lo, hi in runs),
            "ignored": 0,
        }
        want = {
            f"{prefix}/{a + 1}"
            for table, prefix in [
                ("holding", "holding_registers"),
                ("input", "input_registers"),
                ("coil", "coils"),
                ("discrete_input", "discrete_inputs"),
            ]
            for lo, hi in SCAN_TARGET_RANGES[table]
            for a in range(lo, hi + 1)
        }
        assert set(dev._discovered) == want  # holes absent, 1-based
        assert dev.max_block_size == SCAN_TARGET_MAX_BLOCK  # learned from the 03 answers
        assert all(b.rate == 1.0 for b in dev._blocks)
        # Discovered blocks at most once a second; the meter never waits behind a burst.
        assert dev.successful_reads <= 3 * len(dev._blocks)
        fast = [r[3] for r in meter_reads]
        assert max(b - a for a, b in zip(fast, fast[1:], strict=False)) < 0.3

    @pytest.mark.parametrize(("code", "polls"), [(0x02, 1), (None, 3)], ids=["refused", "silent"])
    def test_auto_scan_drops_a_failing_register(self, monkeypatch, code, polls):
        """A discovered register the device later refuses, or never answers while answering
        the rest, is dropped from polling (retried every 10 min); the rest keep polling."""
        monkeypatch.setattr("zelos_extension_modbus.scan.TCP_WINDOWS", ((0, 99),))
        broken = {}
        tables = {"holding": {a: a for a in (*range(5), *range(10, 15))}}
        dev, _ = _auto_device(tables, broken=broken, rate=1.0, demote_after=3)
        _discover_all(dev)
        broken[11] = code
        run = asyncio.get_event_loop().run_until_complete
        for t in range(polls):
            values = run(poll_once(dev, now=10.0 + t))
        assert "holding_registers/1" in values and "holding_registers/11" not in values
        status = dev.auto_scan_status()
        assert (status["found"], status["ignored"]) == (10, 5)
        (row,) = dev.rate_status()["refused"]
        assert (row["range"], row["code"]) == ("holding 11-15", code)
        assert not dev.demoted
        assert list(dev.discovered_map()["events"]) == [
            f"holding_registers/{a}" for a in range(1, 6)
        ]

    @pytest.mark.parametrize(("code", "demoted", "warnings"), [(0x0B, True, 0), (0x04, False, 1)])
    def test_exception_answers(self, caplog, code, demoted, warnings):
        """A gateway's 0A/0B is no response (demotion); other codes warn once, keep polling."""
        dev, _ = _fake_device({"e": [{"name": "v", "address": 1}]}, demote_after=3)
        del dev._fetch  # the real one, over a fake link
        dev.connection.request = AsyncMock(return_value=ExceptionResponse(3, code))
        run = asyncio.get_event_loop().run_until_complete
        with caplog.at_level(logging.WARNING):
            for now in range(3):
                run(poll_once(dev, now=float(now)))
        assert dev.demoted is demoted
        assert caplog.text.count(f"fails with exception {code:02X}") == warnings
        run(poll_once(dev, now=100.0))
        assert dev.connection.request.await_args.args[2] is demoted  # a probe: one attempt


# =============================================================================
# Field Sanitization Tests
# =============================================================================


class TestFieldSanitization:
    """Trace-safe field naming."""

    @pytest.mark.parametrize(
        ("name", "field"),
        [("pcb.temp", "pcb_temp"), ("amps/phase a", "amps_phase_a"), ("...", "r7")],
    )
    def test_field_name(self, name, field):
        """Reserved characters collapse to '_'; nothing usable falls back to r<address>."""
        assert Register(address=6, name=name).field_name == field

    def test_dotted_name_roundtrips(self, demo_server):
        """A dotted name is a sanitized trace field; the cache keeps the raw name."""
        data = {
            "name": "dotted_rt",
            "events": {
                "temps": [
                    {"name": "pcb.temp", "address": 21, "datatype": "int16", "scale": 0.1},
                ]
            },
        }
        client = _demo_client(demo_server, RegisterMap.from_dict(data))
        assert _traced(client) == {"temps": {"pcb_temp"}}  # schema uses the sanitized name
        results = _connected_poll(client)
        assert "pcb_temp" in results.get("temps", {})
        assert "temps/pcb.temp" in client.last_values


# =============================================================================
# Action Tests
# =============================================================================


class TestReconnection:
    """Tests for connection error detection."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ConnectionError("anything"), True),
            (TimeoutError("read timed out"), True),
            (OSError("serial port not available"), True),
            (Exception("Connection timeout"), True),
            (Exception("No response received"), True),
            (Exception("Connection refused"), True),
            (Exception("connection reset by peer"), True),
            (Exception("device disconnected"), True),
            (Exception("Invalid address"), False),
            (Exception("Value out of range"), False),
            (ValueError("bad value"), False),
            (KeyError("missing_field"), False),
        ],
    )
    def test_is_connection_error(self, error, expected):
        assert _is_connection_error(error) is expected

    def test_dropped_link_never_reconnects_inside_pymodbus(self):
        """A link that drops after the connect check is marked down; pymodbus never
        reconnects on its own (that would skip our backoff and connect_delay_ms)."""
        conn = ModbusConnection(port=free_port(), name="c")
        conn.connected = True
        conn._ensure_connected = AsyncMock(return_value=True)  # the drop lands after it

        async def request():
            conn._client = conn._create_client()  # never connected: no transport
            conn._client.ctx.connect = AsyncMock(return_value=True)
            await conn.request("read_holding_registers", 1, address=0, count=1)

        with pytest.raises(ConnectionException, match="link dropped"):
            asyncio.get_event_loop().run_until_complete(request())
        assert not conn.connected
        conn._client.ctx.connect.assert_not_awaited()

    def test_unreachable_at_start_retries_and_recovers(self, monkeypatch, caplog):
        """No link at start: one ERROR, the device is unreachable and retried; once the
        endpoint appears it responds (INFO). A later drop is only retried, no ERROR."""
        monkeypatch.setattr("zelos_extension_modbus.client.RECONNECT_INITIAL", 0.2)
        port = free_port()
        conn = ModbusConnection(port=port, timeout=0.5, name="c")
        dev = ModbusDevice(
            conn, register_map=RegisterMap.from_dict({"events": {"e": [{"address": 1}]}})
        )
        server = DemoServer(port)

        async def until(check):
            for _ in range(100):
                if check():
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("timed out")

        async def main():
            conn._running = True
            task = asyncio.create_task(conn.run_async())
            await asyncio.sleep(0.5)
            assert not dev.answered
            assert dev.last_error.startswith(f"cannot connect to 127.0.0.1:{port}")
            server.start()
            await until(lambda: dev.answered and dev.last_error is None)
            server.stop()
            await until(lambda: not conn.connected)
            conn.stop()
            await task

        with caplog.at_level(logging.INFO):
            asyncio.get_event_loop().run_until_complete(main())
        errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].startswith(f"Connection 'c' (127.0.0.1:{port})")
        assert "Device 'c/unit1' (unit 1): responding" in caplog.messages

    def test_cancel_mid_request_stops_the_loop(self):
        """pymodbus turns a cancel into ModbusIOException; the poll loop must still exit."""

        async def main():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)  # never answers
            port = server.sockets[0].getsockname()[1]
            conn = ModbusConnection(host="127.0.0.1", port=port, timeout=10.0, name="c")
            ModbusDevice(
                conn, register_map=RegisterMap.from_dict({"events": {"e": [{"address": 1}]}})
            )
            conn._running = True
            task = asyncio.create_task(conn.run_async())
            await asyncio.sleep(0.5)  # connected, first read in flight
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3.0)
            server.close()
            return time.monotonic() - started, conn.connected

        loop = asyncio.new_event_loop()
        try:
            elapsed, connected = loop.run_until_complete(main())
        finally:
            loop.close()
        assert elapsed < 3.0 and not connected


# =============================================================================
# Constructor Backstop Clamp Tests
# =============================================================================


class TestConstructorClamps:
    """The constructor is the backstop for out-of-range block-read / poll knobs."""

    @pytest.mark.parametrize(
        ("knob", "value", "clamped"),
        [("max_block_size", 0, 1), ("max_block_size", 200, 125), ("max_read_gap", -1, 0)],
    )
    def test_clamped(self, knob, value, clamped):
        assert getattr(_device(**{knob: value}), knob) == clamped

    def test_rate_floor(self, caplog):
        """A rate below 0.01s (other than 0, not polled) is floored with a warning."""
        with caplog.at_level(logging.WARNING):
            client = _device(rate=0.001)
        assert client.rate == 0.01
        assert "rate" in caplog.text
        assert _device(rate=0).rate == 0


# =============================================================================
# Trace Source Init Tests
# =============================================================================


def _traced(client, prefix="Modbus"):
    """Declare ``client``'s trace events on a fresh source; return {event: fields}."""
    client.connection.start(prefix, zelos_sdk.TraceSource(prefix) if prefix else None)
    return {name: {f.name for f in ev.schema} for name, ev in client._events.items()}


class TestInitTraceSource:
    """Device trace events and the one trace_layout rule."""

    @pytest.mark.parametrize(
        ("prefix", "source", "event_prefix"),
        [("Modbus", "Modbus", "10_0_0_5/unit1"), ("", "10_0_0_5", "unit1")],
        ids=["prefix", "cleared"],
    )
    def test_trace_layout(self, prefix, source, event_prefix):
        """A prefix nests connection/device under one source; cleared, the connection owns it."""
        assert trace_layout(prefix, "10_0_0_5", "unit1") == (source, event_prefix)
        client = ModbusDevice(ModbusConnection(host="10.0.0.5"))
        _traced(client, prefix)
        assert client.trace_path == f"{source}/{event_prefix}"

    def test_event_names_nest_under_the_device(self):
        """Map events live at <connection>/<device>/<event> on the shared source."""
        data = {"events": {"sensors": [{"name": "temp", "address": 1}]}}
        client = _device(register_map=RegisterMap.from_dict(data))
        _traced(client)
        assert client._events["sensors"].name == "c/unit1/sensors"

    @pytest.mark.parametrize(
        ("reg_type", "reply", "event", "field"),
        [
            ("holding", {"registers": [7, 8]}, "holding_registers/123", "123_value"),
            ("input", {"registers": [7, 8]}, "input_registers/123", "123_value"),
            ("coil", {"bits": [True, False]}, "coils/123", "123_value"),
            ("discrete_input", {"bits": [True, False]}, "discrete_inputs/123", "123_value"),
        ],
    )
    def test_raw_read_traced(self, reg_type, reply, event, field):
        """read_register traces each register as its own event keyed by its address (map
        base); tables other than holding get their own prefix. A failed read traces nothing."""
        logged = {}

        class Source:
            def add_event(self, path, fields):
                logged[path] = [fields[0].name, fields[0].data_type]
                return SimpleNamespace(log=lambda **row: logged[path].append(row))

        dev = _device()
        dev._trace_target = (Source(), "c/unit1")
        dev.connection.request = AsyncMock(
            return_value=SimpleNamespace(isError=lambda: False, **reply)
        )
        run = asyncio.get_event_loop().run_until_complete
        run(dev.read_raw(reg_type, 122, 2))
        values = next(iter(reply.values()))
        bits = reg_type in ("coil", "discrete_input")
        dtype = zelos_sdk.DataType.Boolean if bits else zelos_sdk.DataType.UInt16
        second = event.replace("123", "124")
        assert logged == {
            f"c/unit1/{event}": [field, dtype, {field: values[0]}],
            f"c/unit1/{second}": ["124_value", dtype, {"124_value": values[1]}],
        }
        assert dev.last_values[f"{event}/{field}"][0] == values[0]
        dev.connection.request = AsyncMock(return_value=ExceptionResponse(3, 2))
        with pytest.raises(RequestFailed):
            run(dev.read_raw(reg_type, 999, 1))
        assert len(logged) == 2

    def test_all_disabled_event_not_added(self):
        """An event whose registers are all disabled is never added to the source."""
        data = {
            "name": "d",
            "events": {
                "e": [
                    {"name": "off1", "address": 1, "rate": 0},
                    {"name": "off2", "address": 2, "rate": 0},
                ]
            },
        }
        client = _device(register_map=RegisterMap.from_dict(data))
        assert _traced(client) == {}

    def test_mixed_event_keeps_only_polled_fields(self):
        """A mixed event advertises only its polled registers as fields."""
        data = {
            "name": "d",
            "events": {
                "e": [
                    {"name": "a", "address": 1, "datatype": "uint16"},
                    {"name": "b", "address": 2, "datatype": "uint16", "rate": 0},
                    {"name": "c", "address": 3, "datatype": "uint16", "rate": 5.0},
                ]
            },
        }
        client = _device(register_map=RegisterMap.from_dict(data))
        assert _traced(client) == {"e": {"a", "c"}}


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
                    {
                        "name": "limit",
                        "address": 101,
                        "datatype": "uint16",
                        "rate": 0,
                        "writable": True,
                    },
                ]
            },
        }
        client = _device(
            host=demo_server.host,
            port=demo_server.port,
            register_map=RegisterMap.from_dict(data),
        )
        reg = client.register_map.get_by_name("limit")
        assert client.rate_of(reg) == 0  # not polled, but still read/writable

        async def run():
            await client.connection.connect()
            ok = await client.write_register_value(reg, 231)
            value = await client.read_register_value(reg)
            await client.connection.disconnect()
            return ok, value

        ok, value = asyncio.get_event_loop().run_until_complete(run())
        assert ok is True
        assert value == 231


# =============================================================================
# Write Mode Tests
# =============================================================================


class TestWriteMode:
    """Tests for write_mode=fc16."""

    def test_fc16_mode_uses_write_registers(self, demo_server, register_map):
        """With write_mode='fc16', even single-register writes use FC 16."""
        client = _device(
            host=demo_server.host,
            port=demo_server.port,
            register_map=register_map,
            write_mode="fc16",
        )

        async def run():
            await client.connection.connect()
            reg = register_map.get_by_name("voltage_high_limit")
            # This is a uint16 (single register) but fc16 mode should still work
            success = await client.write_register_value(reg, 240)
            assert success is True
            value = await client.read_register_value(reg)
            assert value == 240
            await client.connection.disconnect()

        asyncio.get_event_loop().run_until_complete(run())


# =============================================================================
# TCP Reconnection Integration Tests
# =============================================================================


class TestTcpReconnection:
    """Test that the polling loop recovers when a TCP server goes offline and comes back."""

    def test_tcp_reconnect_after_server_restart(self, register_map):
        """Server goes offline, reads fail, server comes back, reads succeed again."""
        server = DemoServer()
        port = server.port
        server.start()

        client = _device(
            host="127.0.0.1",
            port=port,
            register_map=register_map,
            timeout=1.0,
        )

        async def run():
            # Phase 1: connect and read successfully
            await client.connection.connect()
            assert client.connected
            reg = register_map.get_by_name("voltage_high_limit")
            val = await client.read_register_value(reg)
            assert val is not None

            # Phase 2: kill server — next read should fail
            server.stop()
            await asyncio.sleep(0.5)

            with pytest.raises(RequestFailed):
                await client.read_register_value(reg)

            # Phase 3: mark disconnected (as _run_async would) then restart
            client.connection.connected = False

            server2 = DemoServer(port=port)
            server2.start()

            # Phase 4: reconnect succeeds and reads work again
            connected = await client.connection.ensure_connected()
            assert connected
            assert client.connected

            val = await client.read_register_value(reg)
            assert val is not None

            await client.connection.disconnect()
            server2.stop()

        asyncio.get_event_loop().run_until_complete(run())

    def test_tcp_poll_loop_survives_server_restart(self, register_map):
        """The _run_async polling loop reconnects automatically after server outage."""
        server = DemoServer()
        port = server.port
        server.start()

        client = _device(
            host="127.0.0.1",
            port=port,
            register_map=register_map,
            timeout=1.0,
            rate=0.5,
        )

        poll_results: list[dict] = []
        original_flush = client._flush

        def tracking_flush():
            result = original_flush()
            if result:
                poll_results.append(result)
            return result

        client._flush = tracking_flush

        async def run():
            loop = asyncio.get_event_loop()
            client.connection._loop = loop
            client.connection._running = True

            # Start polling in background
            poll_task = asyncio.create_task(client.connection.run_async())

            # Wait for a few successful polls
            for _ in range(40):
                if len(poll_results) >= 2:
                    break
                await asyncio.sleep(0.1)
            assert len(poll_results) >= 2, "Should have polled at least twice"

            # Kill server; let the loop see the outage and fail a reconnect
            server.stop()
            for _ in range(100):
                if not client.connected and client.connection._connect_failures:
                    break
                await asyncio.sleep(0.1)
            assert client.connection._connect_failures, "Should have failed a reconnect"
            good_count = len(poll_results)

            # Restart server on the same port
            server2 = DemoServer(port=port)
            server2.start()

            # Wait for recovery (next reconnect is within RECONNECT_INTERVAL)
            for _ in range(100):
                if len(poll_results) > good_count:
                    break
                await asyncio.sleep(0.1)

            assert len(poll_results) > good_count, (
                "Should have resumed polling after server restart"
            )
            assert client.connected

            # Shutdown
            client.connection._running = False
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
            server2.stop()

        asyncio.get_event_loop().run_until_complete(run())

    def test_tcp_connect_to_nonexistent_server(self, register_map):
        """Connecting to a server that doesn't exist returns False, doesn't hang."""
        client = _device(
            host="127.0.0.1",
            port=free_port(),  # nothing listening here
            register_map=register_map,
            timeout=1.0,
        )

        async def run():
            result = await client.connection.connect()
            assert result is False or not client.connected
            await client.connection.disconnect()

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


def _socat_pair(server_link: str, client_link: str) -> subprocess.Popen:
    """A virtual serial pair at the two links, once both exist."""
    proc = subprocess.Popen(
        ["socat", f"PTY,raw,echo=0,link={server_link}", f"PTY,raw,echo=0,link={client_link}"],
        stderr=subprocess.PIPE,
    )
    for link in (server_link, client_link):
        for _ in range(40):
            if Path(link).exists():
                break
            time.sleep(0.05)
        else:
            proc.terminate()
            pytest.fail(f"socat PTY link {link} did not appear")
    return proc


@pytest.fixture(scope="module")
def serial_ports(tmp_path_factory):
    """(server_port, client_port) of a socat virtual serial pair."""
    if shutil.which("socat") is None:
        pytest.skip("socat not installed")
    tmpdir = tmp_path_factory.mktemp("socat")
    links = str(tmpdir / "server"), str(tmpdir / "client")
    proc = _socat_pair(*links)
    yield links
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


# =============================================================================
# RTU Reconnection Tests
# =============================================================================


@requires_socat
class TestRtuReconnection:
    """RTU reads fail while the serial link is down and recover once it is back."""

    def test_rtu_reconnect_after_link_restored(self, register_map, tmp_path):
        server_link, client_link = str(tmp_path / "server"), str(tmp_path / "client")
        socat = _socat_pair(server_link, client_link)
        rtu_server = RtuDemoServer(serial_port=server_link)
        rtu_server.start()
        client = _device(
            transport="rtu", serial_port=client_link, timeout=2.0, register_map=register_map
        )
        reg = register_map.get_by_name("voltage_high_limit")

        async def run():
            await client.connection.connect()
            assert await client.read_register_value(reg) is not None

            # Link down: the read fails, it does not hang.
            rtu_server.stop()
            socat.terminate()
            socat.wait(timeout=5)
            await asyncio.sleep(0.5)
            with pytest.raises(RequestFailed):
                await client.read_register_value(reg)
            client.connection.connected = False

            socat2 = _socat_pair(server_link, client_link)
            rtu_server2 = RtuDemoServer(serial_port=server_link)
            rtu_server2.start()
            try:
                assert await client.connection.ensure_connected()
                assert await client.read_register_value(reg) is not None
            finally:
                await client.connection.disconnect()
                rtu_server2.stop()
                socat2.terminate()
                socat2.wait(timeout=5)

        asyncio.get_event_loop().run_until_complete(run())


# =============================================================================
# Action-Specific Edge Cases
# =============================================================================


class TestActionEdgeCases:
    """Edge case tests for action functions."""

    def test_write_registers_action_invalid_values(self):
        """Write Registers action with non-integer values returns error."""
        # Register a dummy device so the lookup succeeds
        client = _device(name="edge_test")
        registry.register(client)
        try:
            for values in (
                "abc,def",
                "1.5",
                "inf",
                "nan",
                "65536",
                "-32769",
                ",".join(["1"] * 124),
            ):
                result = actions.write_registers(device="c/edge_test", address=1, values=values)
                assert result["success"] is False and "error" in result, values
        finally:
            registry.clear()

    @pytest.mark.parametrize(("value", "word"), [(0, 0), (65535, 65535), (-1, 65535), ("7", 7)])
    def test_raw_word(self, value, word):
        """Raw writes take 0-65535, or -32768..-1 as two's complement."""
        assert actions._word(value) == word

    @pytest.mark.parametrize(
        ("value", "state"),
        [(True, True), (False, False), (1, True), (0.0, False), ("ON", True), ("OFF", False)]
        + [(v, None) for v in (0.5, -1, 2, "on", "1", "true", None)],
    )
    def test_coil_state(self, value, state):
        """A coil takes true/false, 0/1 or ON/OFF; anything else is refused, never coerced."""
        if state is None:
            with pytest.raises(ValueError, match="not a coil state"):
                coil_state(value)
            with pytest.raises(ValueError, match="not a coil state"):
                encode_register(Register(address=0, type="coil"), value)
        else:
            assert coil_state(value) is state


class TestActionsUnit:
    """Unit tests for SDK actions (no network)."""

    @pytest.fixture(autouse=True)
    def setup_actions(self):
        """Create client with register map and register in registry."""
        data = {
            "name": "test_device",
            "events": {
                "sensors": [
                    {"name": "temp", "address": 1, "type": "holding", "datatype": "uint16"},
                    {"name": "humidity", "address": 2, "type": "input", "datatype": "uint16"},
                ],
                "controls": [
                    {"name": "relay", "address": 1, "type": "coil", "writable": True},
                    {"name": "setpoint", "address": 11, "datatype": "float32", "writable": True},
                    {"name": "temp", "address": 13, "type": "holding", "datatype": "uint16"},
                ],
            },
        }
        reg_map = RegisterMap.from_dict(data)
        registry.register(_device(register_map=reg_map, name="test"))
        registry.register(_device(name="no_map"))
        yield
        registry.clear()

    @pytest.mark.parametrize(("base", "address", "wire"), [(1, 40001, 40000), (0, 40000, 40000)])
    def test_raw_address_in_map_base(self, base, address, wire):
        """Raw actions take the map's base; only the wire is 0-based. Failures carry a reason."""
        dev = registry.get_device("c/test")
        dev.register_map.device["address_base"] = base
        ok = SimpleNamespace(isError=lambda: False, registers=[7])
        dev.connection.request = AsyncMock(return_value=ok)

        def read(at, count=1):
            # Off the main thread: the action's asyncio.run would clear its loop.
            with ThreadPoolExecutor(1) as pool:
                kwargs = {"device": "c/test", "address": at, "reg_type": "holding", "count": count}
                return pool.submit(actions.read_register, **kwargs).result()

        result = read(address)
        assert (result["success"], result["address"], result["values"]) == (True, address, [7])
        dev.connection.request.assert_awaited_once_with(
            "read_holding_registers", 1, address=wire, count=1
        )
        below = read(base - 1)
        assert below["success"] is False and "address base" in below["error"]
        # Never truncated to a neighbor: 40001.9 is not 40001.
        for at, count in [(address + 0.9, 1), (float("nan"), 1), (float("inf"), 1), (address, 1.5)]:
            assert "is not an integer" in read(at, count)["error"]
        dev.connection.request.assert_awaited_once()
        for request, error in [
            (
                AsyncMock(return_value=ExceptionResponse(3, 2)),
                "device refused: exception 02 (illegal data address)",
            ),
            (AsyncMock(side_effect=ModbusIOException("timeout")), "no response from device"),
        ]:
            dev.connection.request = request
            assert read(address) == {"error": error, "success": False}

    def test_save_map(self, tmp_path, monkeypatch):
        """Save Map writes the loaded map, or auto-scan's finds as a map that loads and polls."""
        monkeypatch.setattr("zelos_extension_modbus.scan.TCP_WINDOWS", ((0, 1999),))
        target = ScanTarget().tables
        dev, reads = _auto_device(target, name="auto")
        registry.register(dev)
        _discover_all(dev)
        path = tmp_path / "auto.json"
        result = actions.save_map(device="c/auto", path=str(path))
        assert (result["success"], result["registers"]) == (True, 274)
        assert "set overwrite" in actions.save_map(device="c/auto", path=str(path))["error"]
        loaded = RegisterMap.from_file(path)
        assert loaded.device == {"address_base": 1, "max_block_size": 60}
        assert {r.rate for r in loaded.registers} == {1.0}
        assert not any(r.writable for r in loaded.registers)
        # Same signal paths, values and requests per sweep as the auto-scan it came from.
        run = asyncio.get_event_loop().run_until_complete
        dev_reads, dev_values = len(reads), run(poll_once(dev, now=0.0))
        dev_reads = len(reads) - dev_reads
        polled, polled_reads = _auto_device(target, register_map=loaded, max_block_size=60)
        values = run(poll_once(polled, now=0.0))
        assert values == dev_values and len(values) == 274
        assert {e: list(f) for e, f in values.items()} == {
            e: [r.field_name for r in regs] for e, regs in loaded.events.items()
        }
        assert values["holding_registers/1006"] == {"1006_value": target["holding"][1005]}
        assert values["coils/16"] == {"16_value": False}
        assert len(polled_reads) == dev_reads and polled.failed_reads == 0

        mapped = tmp_path / "mapped.json"
        assert actions.save_map(device="c/test", path=str(mapped))["success"]
        assert json.loads(mapped.read_text()) == registry.get_device("c/test").register_map.source
        error = actions.save_map(device="c/no_map", path=str(tmp_path / "x.json"))["error"]
        assert "not auto-scanned" in error

    def test_get_status_returns_info(self):
        """Get Status action returns expected fields."""
        result = actions.get_status(device="c/test")
        assert set(result) == {
            "device",
            "connection",
            "connected",
            "transport",
            "endpoint",
            "unit_id",
            "address_base",
            "poll_count",
            "successful_reads",
            "failed_reads",
            "error",
            "map_pending",
            "auto_scan",
            *_RATE_STATUS,
            "rate",
            "min_rate",
            "write_mode",
            "block_reads",
            "max_block_size",
            "max_bit_block_size",
            "max_read_gap",
            "registers",
            "success",
        }
        assert result["success"] is True
        assert result["registers"] == 5
        assert result["device"] == "c/test"
        assert result["endpoint"] == "127.0.0.1:502"
        # Block-read knobs are reported (defaults).
        assert result["block_reads"] is True
        assert result["max_block_size"] == 125
        assert result["max_read_gap"] == 0

    @pytest.mark.parametrize(
        ("action", "device", "name", "error"),
        [
            ("read_named_register", "c/no_map", "anything", "No register map"),
            ("read_named_register", "c/test", "sensors/nonexistent", "not found"),
            ("write_named_register", "c/no_map", "anything", "No register map"),
            ("write_named_register", "c/test", "sensors/nonexistent", "not found"),
            ("write_named_register", "c/test", "sensors/humidity", "read-only"),
            ("write_named_register", "c/test", "controls/relay", "not a coil state"),
            (
                "read_named_register",
                "c/test",
                "temp",
                "ambiguous; use one of: sensors/temp, controls/temp",
            ),
            ("write_named_register", "c/test", "temp", "ambiguous"),
        ],
    )
    def test_named_action_errors(self, action, device, name, error):
        kwargs = {"value": 100} if action.startswith("write") else {}
        result = getattr(actions, action)(device=device, name=name, **kwargs)
        assert result["success"] is False
        assert error in result["error"]
        assert result.get("outcome") == ("refused" if action.startswith("write") else None)

    @pytest.mark.parametrize(
        ("allow", "action", "kwargs", "response", "outcome", "error"),
        [
            (False, "write_single_register", {"address": 50, "value": 1}, None, "refused",
             "Raw writes are disabled"),
            (True, "write_single_register", {"address": 1, "value": 1}, None, "refused",
             "Address 1 is read-only in the device map (sensors/temp)"),
            # 12-13: the writable setpoint's second word, then read-only controls/temp.
            (True, "write_registers", {"address": 12, "values": "1,2"}, None, "refused",
             "(controls/temp)"),
            (True, "write_registers", {"address": 10, "values": "1,2"}, "ok", "ok", None),
            (True, "write_coil", {"address": 1, "value": "ON"}, "ok", "ok", None),
            (True, "write_coil", {"address": 2, "value": "ON"}, ExceptionResponse(5, 2),
             "refused", "exception 02"),
            (True, "write_single_register", {"address": 50, "value": 1},
             ModbusIOException("timeout"), "unknown", "may have landed"),
        ],
    )  # fmt: skip
    def test_raw_write_authorization_and_outcome(
        self, allow, action, kwargs, response, outcome, error
    ):
        """Raw writes need allow_raw_writes and never hit a mapped read-only register."""
        dev = registry.get_device("c/test")
        dev.allow_raw_writes = allow
        ok = SimpleNamespace(isError=lambda: False)
        if isinstance(response, Exception):
            dev.connection.request = AsyncMock(side_effect=response)
        else:
            dev.connection.request = AsyncMock(return_value=ok if response == "ok" else response)
        with ThreadPoolExecutor(1) as pool:  # the action's asyncio.run would clear this loop
            result = pool.submit(getattr(actions, action), device="c/test", **kwargs).result()
        assert (result["outcome"], result["success"]) == (outcome, outcome == "ok")
        assert error is None or error in result["error"]
        assert dev.connection.request.await_count == (response is not None)

    def test_unknown_device_returns_error(self):
        """Actions with unknown device return error."""
        result = actions.get_status(device="c/nonexistent")
        assert "not found" in result["error"]

    def test_multi_device_registry(self):
        """Multiple devices registered and isolated."""
        data2 = {
            "name": "device2",
            "events": {"temps": [{"name": "t1", "address": 1, "type": "holding"}]},
        }
        client2 = _device(register_map=RegisterMap.from_dict(data2), name="second")
        registry.register(client2)

        assert set(registry.all_devices()) == {"c/test", "c/no_map", "c/second"}

        r1 = actions.list_registers(device="c/test")
        r2 = actions.list_registers(device="c/second")
        assert r1["count"] == 5
        assert r2["count"] == 1

    def test_device_specific_dropdown(self):
        """Dropdown callables return per-device registers."""
        names = registry.device_registers("c/test")
        assert "sensors/temp" in names
        assert "controls/relay" in names

        writable = registry.device_writable_registers("c/test")
        assert "sensors/humidity" not in writable
        assert "controls/setpoint" in writable


class TestActionsIntegration:
    """Actions against the demo server."""

    def test_get_status_action(self, client):
        result = _call_action(client, actions.get_status)
        assert result["success"] is True
        assert result["connected"] is True
        assert result["transport"] == "tcp"
        assert result["registers"] > 0


# =============================================================================
# Webapp Integration Layer (last-values cache, list_devices, get_snapshot)
# =============================================================================


class _LoopThread:
    """A background asyncio loop the client can be bound to.

    Mirrors production: the client's loop runs elsewhere and sync actions bridge
    into it via ``_run_coro`` -> ``run_coroutine_threadsafe``.
    """

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        return self

    def run(self, coro):
        """Run a coroutine on the background loop and wait for it."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3)
        # Closing a running loop raises and would mask the real failure; a loop
        # that outlived its join is leaked deliberately (daemon thread).
        if not self.loop.is_running():
            self.loop.close()
        return False


def _call_action(client, action, **kwargs):
    """Call an action against ``client``, registered under its path for the call.

    Only that path is touched on the way out: the registry is global, so clearing
    it wholesale would evict entries a surrounding fixture owns.
    """
    previous = registry.get_device(client.path)
    registry.register(client)
    try:
        return action(device=client.path, **kwargs)
    finally:
        if previous is None:
            registry._devices.pop(client.path, None)
        else:
            registry.register(previous)


class TestLastValuesCache:
    """The client's ``_last_values`` snapshot cache."""

    def test_empty_before_any_poll(self, register_map):
        """A fresh client has no cached values."""
        assert _device(register_map=register_map).last_values == {}

    def test_poll_populates_cache(self, demo_server, register_map):
        """A sweep caches every value under an event/name path the dropdown offers."""
        client = _demo_client(demo_server, register_map)
        results = _connected_poll(client)
        cache = client.last_values

        assert {"voltage/L1", "status/temperature", "inputs/firmware_version"} <= set(cache)
        value, ts_ms = cache["voltage/L1"]
        assert value == results["voltage"]["L1"]
        now_ms = int(time.time() * 1000)
        assert now_ms - 10_000 < ts_ms <= now_ms
        registry.register(client)
        try:
            assert set(cache) <= set(registry.device_registers(client.path))
        finally:
            registry.clear()
        cache.clear()  # a copy
        assert client.last_values

    def test_disabled_register_absent_until_read(self, demo_server):
        """A register with polling disabled caches only after an on-demand read."""
        data = {
            "name": "ondemand",
            "events": {
                "cfg": [
                    {"name": "limit", "address": 101, "datatype": "uint16", "rate": 0},
                    {"name": "low", "address": 102, "datatype": "uint16"},
                ]
            },
        }
        client = _demo_client(demo_server, RegisterMap.from_dict(data))
        with _LoopThread() as lt:
            client.connection._loop = lt.loop
            lt.run(client.connection.connect())
            try:
                lt.run(poll_once(client))
                assert "cfg/low" in client.last_values
                assert "cfg/limit" not in client.last_values  # never polled

                result = _call_action(client, actions.read_named_register, name="cfg/limit")
                assert result["success"] is True
                value, ts_ms = client.last_values["cfg/limit"]
                assert value == result["value"]
                assert ts_ms <= int(time.time() * 1000)
            finally:
                lt.run(client.connection.disconnect())

    def test_bare_name_read_still_caches_under_its_event(self, demo_server):
        """The bare-name compat path has no event, so the map resolves it."""
        data = {
            "name": "bare",
            "events": {
                "cfg": [
                    {
                        "name": "limit",
                        "address": 101,
                        "datatype": "uint16",
                        "rate": 0,
                        "writable": True,
                    },
                ]
            },
        }
        client = _demo_client(demo_server, RegisterMap.from_dict(data))
        with _LoopThread() as lt:
            client.connection._loop = lt.loop
            lt.run(client.connection.connect())
            try:
                # "limit", not "cfg/limit": _resolve_register returns event=None here.
                result = _call_action(client, actions.read_named_register, name="limit")
            finally:
                lt.run(client.connection.disconnect())

        assert result["success"] is True
        assert client.last_values["cfg/limit"][0] == result["value"]

    def test_write_populates_cache(self, demo_server):
        """A successful named write caches the written value, fresh, right away."""
        data = {
            "name": "writeback",
            "events": {
                "cfg": [
                    {
                        "name": "limit",
                        "address": 101,
                        "datatype": "uint16",
                        "rate": 0,
                        "writable": True,
                    },
                ]
            },
        }
        client = _demo_client(demo_server, RegisterMap.from_dict(data))
        with _LoopThread() as lt:
            client.connection._loop = lt.loop
            lt.run(client.connection.connect())
            try:
                lt.run(poll_once(client))
                assert "cfg/limit" not in client.last_values  # polling disabled

                before = int(time.time() * 1000)
                result = _call_action(
                    client,
                    actions.write_named_register,
                    name="cfg/limit",
                    value=237,
                )
                assert result["success"] is True
                snapshot = _call_action(client, actions.get_snapshot)
            finally:
                lt.run(client.connection.disconnect())

        # Without the write-through, an unpolled setpoint never appears at all.
        row = snapshot["values"]["cfg/limit"]
        assert row["value"] == 237
        assert before <= row["ts_ms"] <= snapshot["captured_at_unix_ms"]

    def test_failed_write_does_not_touch_cache(self, register_map):
        """A write that the device rejected leaves the cache alone."""
        # Nothing listens on the default endpoint: connect fails, the write returns False.
        client = _device(register_map=register_map, name="nowrite")
        with _LoopThread() as lt:
            client.connection._loop = lt.loop
            result = _call_action(
                client,
                actions.write_named_register,
                name="setpoints/voltage_high_limit",
                value=242,
            )
        assert (result["success"], result["outcome"]) == (False, "refused")  # never sent
        assert client.last_values == {}


class TestListDevicesAction:
    """Modbus/list_devices."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        registry.clear()
        yield
        registry.clear()

    def test_empty_registry(self):
        """No devices registered yields an empty list."""
        result = actions.list_devices()
        assert result == {"devices": [], "count": 0, "success": True}

    def test_shape_and_registry_keys(self, register_map):
        """One row per device, keyed by the registry's <connection>/<device> path."""
        conn = ModbusConnection(host="10.0.0.5")
        meter = ModbusDevice(conn, unit_id=7, register_map=register_map)
        raw = ModbusDevice(conn, unit_id=2, name="aux", auto_scan=True)
        registry.register(meter)
        registry.register(raw)
        conn.start("Modbus", zelos_sdk.TraceSource("Modbus"))

        result = actions.list_devices()
        assert result["success"] is True
        assert [row["name"] for row in result["devices"]] == ["10_0_0_5/unit7", "10_0_0_5/aux"]

        rows = {row["name"]: row for row in result["devices"]}
        assert rows["10_0_0_5/unit7"] == {
            "name": "10_0_0_5/unit7",
            "connection": "10_0_0_5",
            "device": "unit7",
            "unit_id": 7,
            "address_base": 1,
            "transport": "tcp",
            "endpoint": "10.0.0.5:502",
            "connected": False,
            "successful_reads": 0,
            "failed_reads": 0,
            "trace_path": "Modbus/10_0_0_5/unit7",
            "error": None,
            "map_pending": False,
            "map_name": "power_meter",
            "register_count": len(register_map.registers),
            "auto_scan": None,
            "rate": 1.0,
            "write_mode": "auto",
            "raw_writes": False,
            "requested_rate": None,  # planned on the first tick
            "achieved_rate": None,
            "overload_pct": None,
            "demoted": False,
            "retry_in_s": None,
            "tiers": [],
            "refused": [],
        }
        assert rows["10_0_0_5/aux"]["map_name"] is None
        assert rows["10_0_0_5/aux"]["register_count"] == 0
        assert rows["10_0_0_5/aux"]["auto_scan"] == {
            "state": "scanning",
            "table": "holding",
            "found": 0,
            "ignored": 0,
        }

    def test_names_are_the_keys_other_actions_accept(self, register_map):
        """Each reported name resolves through the shared device lookup."""
        registry.register(_device(register_map=register_map))
        registry.register(_device(unit_id=2))
        for row in actions.list_devices()["devices"]:
            assert actions.get_status(device=row["name"]).get("error") is None


class TestGetSnapshotAction:
    """Modbus/get_snapshot."""

    SHAPE = {
        "device",
        "connection",
        "connected",
        "transport",
        "endpoint",
        "unit_id",
        "address_base",
        "poll_count",
        "successful_reads",
        "failed_reads",
        "error",
        "map_pending",
        "auto_scan",
        *_RATE_STATUS,
        "captured_at_unix_ms",
        "values",
        "success",
    }

    def test_unknown_device(self):
        """An unknown device uses the shared error shape."""
        result = actions.get_snapshot(device="c/nope")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_shape_and_empty_values_before_poll(self, register_map):
        """Shape is complete and values are empty until something is read."""
        client = _device(register_map=register_map, name="snap", unit_id=3)
        before = int(time.time() * 1000)
        result = _call_action(client, actions.get_snapshot)
        after = int(time.time() * 1000)

        assert set(result) == self.SHAPE
        assert result["success"] is True
        assert result["device"] == "c/snap"
        assert result["connection"] == "c"
        assert result["connected"] is False
        assert result["transport"] == "tcp"
        assert result["endpoint"] == "127.0.0.1:502"
        assert result["unit_id"] == 3
        assert result["poll_count"] == 0
        assert (result["successful_reads"], result["failed_reads"]) == (0, 0)
        assert before <= result["captured_at_unix_ms"] <= after
        assert result["values"] == {}

    def test_values_populated_after_poll(self, client):
        """After a sweep, values mirror the cache as {value, ts_ms} rows."""
        polled = asyncio.get_event_loop().run_until_complete(poll_once(client))
        result = _call_action(client, actions.get_snapshot)

        assert result["connected"] is True
        assert result["values"]
        assert set(result["values"]) == set(client.last_values)
        row = result["values"]["voltage/L1"]
        assert set(row) == {"value", "ts_ms"}
        assert row["value"] == polled["voltage"]["L1"]
        assert row["ts_ms"] <= result["captured_at_unix_ms"]

    def test_no_device_io(self, demo_server, register_map):
        """Served from cache without touching the bus; every ts_ms predates the capture."""
        client = _demo_client(demo_server, register_map)
        _connected_poll(client)
        calls = _spy_reads(client)
        result = _call_action(client, actions.get_snapshot)

        assert result["values"]
        assert all(not v for v in calls.values())
        assert all(
            row["ts_ms"] <= result["captured_at_unix_ms"] for row in result["values"].values()
        )


class TestNonFiniteValuePayloads:
    """Non-finite floats (NaN, ±Inf) must never reach an action payload.

    The SDK converts action results to JSON in Rust, which rejects non-finite
    floats outright and fails the whole action. get_snapshot aggregates every
    cached value, so one poisoned register (a float32 register reading
    0xFFFF,0xFFFF decodes to NaN) would otherwise kill every snapshot for as long
    as that value stayed cached.
    """

    TS = 1_700_000_000_000

    def _poisoned_client(self):
        """Client whose cache holds NaN, +Inf, -Inf and one healthy value."""
        client = _device(name="poison")
        client._last_values.update(
            {
                "float/nan": (float("nan"), self.TS),
                "float/inf": (float("inf"), self.TS + 1),
                "float/neg_inf": (float("-inf"), self.TS + 2),
                "float/ok": (12.5, self.TS + 3),
                "int/count": (7, self.TS + 4),
                "bool/relay": (True, self.TS + 5),
            }
        )
        return client

    def test_snapshot_nulls_non_finite_and_keeps_the_rest(self):
        """Only the unserializable values become null; timestamps are untouched."""
        client = self._poisoned_client()
        values = _call_action(client, actions.get_snapshot)["values"]

        assert values["float/nan"] == {"value": None, "ts_ms": self.TS}
        assert values["float/inf"]["value"] is None
        assert values["float/neg_inf"]["value"] is None
        assert values["float/ok"]["value"] == 12.5
        assert values["int/count"]["value"] == 7
        assert values["bool/relay"]["value"] is True
        # The cache keeps what the device reported.
        assert math.isnan(client.last_values["float/nan"][0])
        assert client.last_values["float/inf"][0] == float("inf")

    def test_snapshot_executes_through_the_sdk_registry(self):
        """End-to-end: the payload survives the SDK's Rust JSON conversion."""
        client = self._poisoned_client()
        registry.register(client)
        try:
            result = zelos_sdk.actions_registry.execute("get_snapshot", {"device": client.path})
        finally:
            registry._devices.pop(client.path, None)

        payload = result.value
        assert payload["success"] is True
        assert payload["values"]["float/nan"]["value"] is None
        assert payload["values"]["float/ok"]["value"] == 12.5

    def test_sdk_registry_rejects_a_raw_non_finite_float(self):
        """The failure mode being guarded against, pinned against SDK drift."""

        @zelos_sdk.action("Raw NaN Probe", "Returns an unsanitized NaN")
        def _raw_nan_probe() -> dict:
            return {"value": float("nan"), "success": True}

        # Nested functions are not auto-registered, so name it explicitly.
        zelos_sdk.actions_registry.register(_raw_nan_probe, name="tests/raw_nan_probe")
        with pytest.raises(RuntimeError, match="Invalid float value"):
            zelos_sdk.actions_registry.execute("tests/raw_nan_probe", {})

    def test_read_named_register_nulls_nan_but_still_succeeds(self):
        """A NaN reading is a successful read of a value JSON cannot carry."""
        data = {
            "name": "nan_read",
            "events": {"v": [{"name": "x", "address": 1, "datatype": "float32"}]},
        }
        client = _device(register_map=RegisterMap.from_dict(data), name="nan_read")

        async def _nan_read(_register):
            return float("nan")

        client.read_register_value = _nan_read  # no bus: the decode step itself is stubbed

        with _LoopThread() as lt:
            client.connection._loop = lt.loop
            result = _call_action(client, actions.read_named_register, name="v/x")
            snapshot = _call_action(client, actions.get_snapshot)

        assert result["value"] is None
        assert result["success"] is True  # the read succeeded; the value is just not JSON
        assert math.isnan(client.last_values["v/x"][0])  # cache keeps the raw reading
        assert snapshot["values"]["v/x"]["value"] is None


class TestRegisterCatalogRows:
    """Enriched list_registers / list_writable_registers rows."""

    ROW_KEYS = {
        "name",
        "event",
        "path",
        "address",
        "type",
        "datatype",
        "unit",
        "scale",
        "description",
        "writable",
        "byte_order",
        "rate",
    }

    @pytest.fixture(autouse=True)
    def catalog(self):
        """Client whose map covers all three rate cases."""
        data = {
            "name": "catalog_device",
            "events": {
                "sensors": [
                    {
                        "name": "temp",
                        "address": 6,
                        "datatype": "int16",
                        "unit": "°C",
                        "scale": 0.1,
                        "description": "PCB temperature",
                        "writable": True,
                    },
                    {"name": "rpm", "address": 7, "rate": 5.0},
                    {"name": "serial", "address": 8, "type": "input", "rate": 0},
                ],
                "controls/out": [{"name": "relay", "address": 1, "type": "coil", "writable": True}],
            },
        }
        client = _device(register_map=RegisterMap.from_dict(data), name="cat")
        registry.register(client)
        yield client
        registry.clear()

    def _rows(self, result):
        return {r["path"]: r for r in result["registers"]}

    def test_row_keys_and_map_name(self):
        """Every row carries the full key set; the map name is top level."""
        result = actions.list_registers(device="c/cat")
        assert set(result) == {"registers", "count", "map_name", "success"}
        assert result["success"] is True
        assert result["map_name"] == "catalog_device"
        assert result["count"] == 4
        for row in result["registers"]:
            assert set(row) == self.ROW_KEYS

    def test_event_and_path(self):
        """event/path identify the register the way the named actions expect."""
        rows = self._rows(actions.list_registers(device="c/cat"))
        assert set(rows) == {
            "sensors/temp",
            "sensors/rpm",
            "sensors/serial",
            "controls/out/relay",
        }
        assert rows["sensors/temp"]["event"] == "sensors"
        assert rows["sensors/temp"]["path"] == "sensors/temp"
        # A path from the catalog resolves through the named-action lookup.
        client = registry.get_device("c/cat")
        error, reg, event = actions._resolve_register(client, rows["controls/out/relay"]["path"])
        assert error is None
        assert reg.name == "relay"
        assert event == "controls/out"

    def test_scale_description_and_existing_keys(self):
        """New metadata is reported and the pre-existing keys are unchanged."""
        row = self._rows(actions.list_registers(device="c/cat"))["sensors/temp"]
        assert row["scale"] == 0.1
        assert row["description"] == "PCB temperature"
        assert row["name"] == "temp"
        assert row["address"] == 6  # the map's (1-based) address
        assert row["type"] == "holding"
        assert row["datatype"] == "int16"
        assert row["unit"] == "°C"
        assert row["writable"] is True
        assert row["byte_order"] == "big"

    def test_rate_cases(self):
        """rate is effective: the device rate when unset, 0 = not polled, else its own."""
        rows = self._rows(actions.list_registers(device="c/cat"))
        assert rows["sensors/temp"]["rate"] == 1.0
        assert rows["sensors/rpm"]["rate"] == 5.0
        assert rows["sensors/serial"]["rate"] == 0

    def test_writable_rows_consistent(self):
        """Writable rows use the same shape and exclude read-only registers."""
        result = actions.list_writable_registers(device="c/cat")
        assert set(result) == {"registers", "count", "map_name", "success"}
        assert result["success"] is True
        assert result["map_name"] == "catalog_device"
        assert result["count"] == 2
        rows = self._rows(result)
        assert set(rows) == {"sensors/temp", "controls/out/relay"}  # rpm unmarked, serial input
        for row in rows.values():
            assert set(row) == self.ROW_KEYS
            assert row["writable"] is True
        assert rows["controls/out/relay"]["event"] == "controls/out"

    def test_no_map_reports_null_map_name(self):
        """Raw mode still answers with the top-level map_name key."""
        registry.register(_device(name="raw"))
        for result in (
            actions.list_registers(device="c/raw"),
            actions.list_writable_registers(device="c/raw"),
        ):
            assert result == {"registers": [], "count": 0, "map_name": None, "success": True}
