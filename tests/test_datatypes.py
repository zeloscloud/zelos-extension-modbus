"""string, scale_ref, invalid (not implemented) and values: load, decode, trace."""

import asyncio
import time

import pytest
from conftest import poll_once

from zelos_extension_modbus.client import ModbusConnection, ModbusDevice
from zelos_extension_modbus.register_map import RegisterMap

MAP = {
    "events": {
        "e": [
            {"name": "s", "address": 1, "datatype": "string", "length": 3, "invalid": [0]},
            # Disabled: read only because `v` needs it in the same sweep.
            {
                "name": "sf",
                "address": 4,
                "datatype": "int16",
                "invalid": [0x8000],
                "rate": 0,
            },
            {"name": "v", "address": 5, "scale_ref": "sf", "unit": "V"},
            {"name": "n", "address": 6, "datatype": "int16", "invalid": [0x8000]},
            {"name": "st", "address": 7, "values": {"1": "on", "2": "off"}},
        ]
    }
}
BASE = [0x4142, 0x4300, 0x2020, 0xFFFF, 2305, 7, 1]  # "ABC", sf -1, v, n, st


class _Source:
    name = "Modbus"

    def __init__(self):
        self.tables = []

    def add_event(self, name, fields):
        return None

    def add_value_table(self, event, field, table):
        self.tables.append((event, field, table))


@pytest.mark.parametrize(
    ("words", "field", "expected"),
    [
        ({}, "s", "ABC"),
        ({0: 0x2020, 1: 0x2020, 2: 0x2020}, "s", ""),
        ({0: 0, 1: 0, 2: 0}, "s", None),  # all NUL: not implemented
        ({}, "v", 230.5),
        ({3: 2}, "v", 230500.0),
        ({3: 0x8000}, "v", None),  # exponent not implemented
        ({}, "n", 7),
        ({5: 0x8000}, "n", None),
        ({}, "st", 1),
    ],
    ids=[
        "string",
        "blank-string",
        "nul-string",
        "sf-neg",
        "sf-pos",
        "sf-null",
        "int",
        "invalid",
        "enum",
    ],
)
@pytest.mark.parametrize("block_reads", [True, False], ids=["block", "single"])
def test_decode(words, field, expected, block_reads):
    image = list(BASE)
    for address, word in words.items():
        image[address] = word
    dev = ModbusDevice(
        ModbusConnection(name="c"), register_map=RegisterMap.from_dict(MAP), block_reads=block_reads
    )
    reads = []

    async def fake_fetch(reg_type, address, count):
        reads.append((address, count))
        return image[address : address + count]

    dev._fetch = fake_fetch
    source = _Source()
    dev.init_trace(source, "c/unit1")
    loop = asyncio.new_event_loop()
    values = loop.run_until_complete(poll_once(dev, now=0.0))["e"]
    loop.close()

    assert values[field] == expected
    assert "sf" not in values  # disabled: read for `v`, never logged
    assert source.tables == [("c/unit1/e", "st", {1: "on", 2: "off"})]
    if block_reads:
        assert reads == [(0, 7)]  # the exponent rides in the same block


@pytest.mark.parametrize(
    ("reg", "message"),
    [
        ({"datatype": "string"}, "needs 'length'"),
        ({"datatype": "string", "length": 0}, "needs 'length'"),
        ({"datatype": "string", "length": 126}, "needs 'length'"),
        ({"datatype": "string", "length": 2, "writable": True}, "not writable"),
        ({"length": 2}, "only applies to datatype string"),
        ({"scale_ref": "missing"}, "must name an integer register in the same event"),
        ({"scale_ref": "x", "scale": 0.1}, "no 'scale'"),
        ({"scale_ref": "x", "writable": True}, "scale_ref register is read-only"),
        ({"writable": "yes"}, "writable must be true or false"),
        ({"scale": 0}, "scale must be a finite non-zero number"),
        ({"scale": True}, "scale must be a finite non-zero number"),
        ({"scale": "2"}, "scale must be a finite non-zero number"),
        ({"address": 65536, "datatype": "uint32"}, "spans past the last address"),
        ({"datatype": "float32", "values": {"1": "a"}}, "unscaled integer"),
        ({"values": {"one": "a"}}, "keys must be integers"),
        ({"datatype": "string", "length": 1, "invalid": [1]}, "'invalid' needs"),
    ],
    ids=[
        "string-no-length",
        "string-zero-length",
        "string-too-long",
        "string-writable",
        "length-not-string",
        "dangling-scale-ref",
        "scale-and-scale-ref",
        "scale-ref-writable",
        "writable-not-bool",
        "scale-zero",
        "scale-bool",
        "scale-str",
        "past-last-address",
        "values-float",
        "values-bad-key",
        "invalid-on-string",
    ],
)
def test_validation(reg, message):
    events = {"e": [{"name": "x", "address": 1}, {"name": "r", "address": 2, **reg}]}
    with pytest.raises(ValueError, match=message):
        RegisterMap.from_dict({"events": events})


def test_scale_ref_pair_is_read_only():
    """A scaled value and its exponent are read-only; marking the exponent writable fails."""
    loaded = RegisterMap.from_dict(MAP)
    assert not loaded.get_by_name("v").writable and not loaded.get_by_name("sf").writable
    events = {
        "e": [
            {"name": "sf", "address": 1, "writable": True},
            {"name": "v", "address": 2, "scale_ref": "sf"},
        ]
    }
    with pytest.raises(ValueError, match="scale_ref of 'v', so it is read-only"):
        RegisterMap.from_dict({"events": events})


def test_scale_ref_read_with_its_value():
    """A slow value is scaled only by an exponent read in the same batch, even from
    another block that is not due; a failed exponent read nulls it."""
    events = {
        "e": [
            {"name": "f", "address": 50},  # the fast tier
            {"name": "v", "address": 1, "rate": 60, "scale_ref": "sf"},
            {"name": "sf", "address": 100, "datatype": "int16", "rate": 0},
        ]
    }
    conn = ModbusConnection(name="c")
    dev = ModbusDevice(conn, register_map=RegisterMap.from_dict({"events": events}))
    image = {0: 2305, 49: 1, 99: 0xFFFF}
    reads = []

    async def fetch(reg_type, address, count):
        reads.append(address)
        word = image[address]
        return -word if word < 0 else [word]  # negative: that exception code

    dev._fetch = fetch
    logged = []
    dev._log_values = logged.append
    loop = asyncio.new_event_loop()

    def tick():
        now = time.monotonic()
        for block in dev._schedule(now):  # the exponent's block is never due by itself
            block.next_due = now + 1000 if block.read.address == 99 else now
        loop.run_until_complete(conn._poll(conn._batch(now)))
        return logged[-1]["e"]["v"]

    assert tick() == 230.5
    image[99] = 0xFFFE  # exponent -1 -> -2
    assert tick() == 23.05
    image[99] = -4  # the exponent read fails (exception 04)
    assert tick() is None
    image[99] = -2  # refused: kept to its 10 min retry, not dragged along
    assert tick() is None
    reads.clear()
    assert tick() is None and 99 not in reads
    loop.close()
