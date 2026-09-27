"""SunSpec discovery against the simulator: model chain walk -> RegisterMap."""

import asyncio

import pytest
from conftest import await_listening, free_port, poll_once
from pymodbus.server import ModbusTcpServer

from zelos_extension_modbus.client import ModbusConnection, ModbusDevice
from zelos_extension_modbus.demo.sunspec_sim import BASE, build_context
from zelos_extension_modbus.sunspec import (
    MAX_MODELS,
    SUNS_MARKER,
    DiscoveryError,
    build_register_map,
)


async def _discover_and_poll() -> tuple[ModbusDevice, dict]:
    port = free_port()
    server = ModbusTcpServer(build_context(), address=("127.0.0.1", port))
    serving = asyncio.create_task(server.serve_forever())
    await await_listening(port)
    conn = ModbusConnection(host="127.0.0.1", port=port, name="sim")
    dev = ModbusDevice(conn, map_loader=build_register_map)
    fetch, busy = dev._fetch, [BASE + 2]

    async def flaky(reg_type, address, count):
        if address in busy:  # the first model header answers busy, once
            busy.remove(address)
            return 0x06
        return await fetch(reg_type, address, count)

    dev._fetch = flaky
    try:
        await conn.connect()
        await dev.load_map()
        assert dev.map_pending and "model header at 40003" in dev.last_error
        await dev.load_map()  # the retry gets the whole chain
        return dev, await poll_once(dev)
    finally:
        await conn.disconnect()
        await server.shutdown()
        serving.cancel()


def test_sunspec_map_from_sim():
    loop = asyncio.new_event_loop()
    dev, values = loop.run_until_complete(_discover_and_poll())
    loop.close()
    reg_map = dev.register_map

    assert not dev.map_pending and dev.last_error is None
    assert list(reg_map.events) == ["common_1", "inverter_three_phase_103", "mppt_160"]
    assert all(not r.writable for r in reg_map.registers)

    def reg(event, name):
        return next(r for r in reg_map.events[event] if r.name == name)

    mn = reg("common_1", "Mn")
    assert (mn.address, mn.datatype, mn.length, mn.rate) == (40004, "string", 16, 60)
    assert mn.map_address == 40005  # 1-based, as SunSpec documents Mn
    a = reg("inverter_three_phase_103", "A")
    assert (a.address, a.unit, a.scale_ref, a.ref.name, a.rate) == (
        40072,
        "A",
        "A_SF",
        "A_SF",
        None,
    )
    assert reg("inverter_three_phase_103", "St").values[4] == "MPPT"
    assert reg("inverter_three_phase_103", "VA").invalid == [0x8000]
    # Not implemented per SunSpec: accumulators 0, strings all NUL.
    assert reg("inverter_three_phase_103", "WH").invalid == mn.invalid == [0]
    # Repeating group: count from the model length, sf from the enclosing model.
    assert reg("mppt_160", "module_2_DCA").scale_ref == "DCA_SF"

    assert values["common_1"]["Mn"] == "Zelos"
    inverter = values["inverter_three_phase_103"]
    assert (inverter["PhVphA"], inverter["Hz"] > 59, inverter["VA"]) == (230.1, True, None)
    assert values["mppt_160"]["module_2_DCV"] == 379.5


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({BASE + 2: 0x02}, "exception answer for the model header at 40003"),
        ({}, f"no end marker after {MAX_MODELS} models"),
        ({BASE + 2: [65000, 30000]}, "past the last address"),
    ],
    ids=["exception", "no-end-marker", "past-last-address"],
)
def test_partial_chain_is_not_a_map(headers, error):
    """Only a whole chain makes a map; anything less raises so discovery is retried."""

    async def fetch(reg_type, address, count):
        if address == BASE:
            return SUNS_MARKER
        return headers.get(address, [65000, 0])  # an undefined model, length 0

    dev = ModbusDevice(ModbusConnection(name="c"), map_loader=build_register_map)
    dev._fetch = fetch
    loop = asyncio.new_event_loop()
    with pytest.raises(DiscoveryError, match=error):
        loop.run_until_complete(build_register_map(dev))
    loop.run_until_complete(dev.load_map(0.0))
    loop.close()
    assert dev.map_pending and dev.register_map is None and error in dev.last_error
