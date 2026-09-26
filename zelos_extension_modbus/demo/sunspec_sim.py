"""SunSpec inverter simulator for tests and demos.

Holding registers from 40000: the `SunS` marker, model 1 (common, strings),
model 103 (three-phase inverter: scale factors, the St enum, not-implemented
points), model 160 (MPPT, two repeating modules), then the end marker.
Measurements move over time; every other value is fixed.

Run standalone: `uv run python -m zelos_extension_modbus.demo.sunspec_sim --port 5021`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import struct
import time
from importlib import resources

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

logger = logging.getLogger(__name__)

BASE = 40000
NI16, NU16, NU32 = 0x8000, 0xFFFF, 0xFFFFFFFF  # not implemented

COMMON = {"Mn": "Zelos", "Md": "SimInverter 3P", "Opt": "", "Vr": "1.2.3", "SN": "SN0001", "DA": 1}

# Raw point values. W, A*, Hz, WH and DCW move in `update`.
INVERTER = {
    "A": 1234, "AphA": 411, "AphB": 412, "AphC": 411, "A_SF": -2,
    "PPVphAB": NU16, "PPVphBC": NU16, "PPVphCA": NU16,
    "PhVphA": 2301, "PhVphB": 2299, "PhVphC": 2305, "V_SF": -1,
    "W": 5000, "W_SF": 0, "Hz": 6001, "Hz_SF": -2,
    "VA": NI16, "VA_SF": NI16, "VAr": NI16, "VAr_SF": NI16,
    "PF": 99, "PF_SF": 0, "WH": 12345, "WH_SF": 1,
    "DCA": 150, "DCA_SF": -1, "DCV": 4000, "DCV_SF": -1, "DCW": 6000, "DCW_SF": 0,
    "TmpCab": 452, "TmpSnk": NI16, "TmpTrns": NI16, "TmpOt": NI16, "Tmp_SF": -1,
    "St": 4, "StVnd": NU16, "Evt1": 0, "Evt2": 0,
    "EvtVnd1": NU32, "EvtVnd2": NU32, "EvtVnd3": NU32, "EvtVnd4": NU32,
}  # fmt: skip

MPPT = {"DCA_SF": -2, "DCV_SF": -1, "DCW_SF": 0, "DCWH_SF": 0, "Evt": 0, "N": 2, "TmsPer": NU16}
MODULES = [
    {"ID": 1, "IDStr": "string A", "DCA": 512, "DCV": 3801, "DCW": 1946, "DCWH": 1000,
     "Tms": 0, "Tmp": NI16, "DCSt": 4, "DCEvt": 0},
    {"ID": 2, "IDStr": "string B", "DCA": 498, "DCV": 3795, "DCW": 1890, "DCWH": 2000,
     "Tms": 0, "Tmp": NI16, "DCSt": 4, "DCEvt": 0},
]  # fmt: skip


def _points(model_id: int, group: str | None = None) -> list[dict]:
    """Point definitions of a model's top group, or of its subgroup ``group``."""
    path = resources.files("sunspec2") / "models" / "json" / f"model_{model_id}.json"
    top = json.loads(path.read_text())["group"]
    if group is None:
        return top["points"]
    return next(g for g in top["groups"] if g["name"] == group)["points"]


def _words(point: dict, value: int | str) -> list[int]:
    """Encode one point's raw value into its registers."""
    size = point["size"]
    if point["type"] == "string":
        data = value.encode().ljust(size * 2, b"\x00")
        return list(struct.unpack(f">{size}H", data))
    return [(value >> (16 * (size - 1 - i))) & 0xFFFF for i in range(size)]


def _encode(model_id: int, values: dict, group: str | None = None, length: int = 0) -> list[int]:
    """Registers for one model instance (or group instance), ID/L filled in."""
    out: list[int] = []
    for point in _points(model_id, group):
        name = point["name"]
        if group is None and name == "ID":
            value = model_id
        elif group is None and name == "L":
            value = length
        else:
            value = values.get(name, 0)
        out += _words(point, value)
    return out


def _image(inverter: dict) -> list[int]:
    """The whole register image from BASE."""
    common = _encode(1, COMMON, length=66)
    inv = _encode(103, inverter, length=50)
    modules = [w for m in MODULES for w in _encode(160, m, group="module")]
    mppt = _encode(160, MPPT, length=8 + len(modules))
    return [0x5375, 0x6E53, *common, *inv, *mppt, *modules, 0xFFFF, 0]


def build_context() -> ModbusServerContext:
    """A server context holding the SunSpec image (any unit id)."""
    image = _image(INVERTER)
    # pymodbus maps wire address N to block index N + 1.
    block = ModbusSequentialDataBlock(0, [0] * (BASE + len(image) + 2))
    block.setValues(BASE + 1, image)
    return ModbusServerContext(devices=ModbusDeviceContext(hr=block), single=True)


def update(context: ModbusServerContext, t: float) -> None:
    """Move the measurements to time ``t``."""
    w = int(5000 + 1000 * math.sin(t / 5))
    inverter = {
        **INVERTER,
        "W": w,
        "A": w * 100 // 405,  # A_SF -2
        "Hz": 6000 + int(3 * math.sin(t)),
        "WH": INVERTER["WH"] + int(t),
        "DCW": w + 150,
    }
    context[0].store["h"].setValues(BASE + 1, _image(inverter))


async def run_sunspec_server(host: str = "127.0.0.1", port: int = 5021, interval: float = 0.5):
    """Serve the simulator on ``host:port`` until cancelled."""
    context = build_context()
    start = time.monotonic()

    async def tick() -> None:
        while True:
            update(context, time.monotonic() - start)
            await asyncio.sleep(interval)

    ticker = asyncio.create_task(tick())
    logger.info(f"SunSpec simulator on {host}:{port} (base {BASE})")
    try:
        await StartAsyncTcpServer(context=context, address=(host, port))
    finally:
        ticker.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5021)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_sunspec_server(args.host, args.port))
