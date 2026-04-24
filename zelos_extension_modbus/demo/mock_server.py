"""Generic mock Modbus TCP server driven by a JSON register map.

Given any register-map JSON the extension accepts, this module stands up a
local pymodbus TCP server that:

* sizes its datastore to cover every declared address across holding/input/
  coil/discrete_input spaces, and
* periodically randomizes every read-only register to a value within its
  datatype range (writable registers are left alone so user writes persist
  and can be observed on the next poll).

The encoding path reuses ``client.encode_value`` so the bytes the mock
publishes are exactly what the extension expects to decode on the wire,
including byte-order variants like ``big_swap``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

from zelos_extension_modbus.client import encode_value
from zelos_extension_modbus.constants import RegisterType
from zelos_extension_modbus.register_map import DATATYPES, Register, RegisterMap

logger = logging.getLogger(__name__)


# Map RegisterMap type → pymodbus datastore block key
_BLOCK_KEY = {
    RegisterType.HOLDING: "h",
    RegisterType.INPUT: "i",
    RegisterType.COIL: "c",
    RegisterType.DISCRETE_INPUT: "d",
}


def _random_raw_value(reg: Register) -> float | int | bool:
    """Return a randomized *post-scale* value appropriate for the register's datatype.

    The value is what a user would read back after scaling is applied; the
    caller passes it through ``encode_value`` which re-applies the scale to
    produce the raw register contents.
    """
    dt = reg.datatype
    if dt == "bool":
        return random.random() < 0.5
    if dt == "uint16":
        raw = random.randint(0, 0xFFFF)
    elif dt == "int16":
        raw = random.randint(-0x8000, 0x7FFF)
    elif dt == "uint32":
        raw = random.randint(0, 0xFFFF_FFFF)
    elif dt == "int32":
        raw = random.randint(-0x8000_0000, 0x7FFF_FFFF)
    elif dt == "uint64":
        raw = random.randint(0, 0xFFFF_FFFF_FFFF_FFFF)
    elif dt == "int64":
        raw = random.randint(-0x8000_0000_0000_0000, 0x7FFF_FFFF_FFFF_FFFF)
    elif dt in ("float32", "float64"):
        return random.uniform(-1000.0, 1000.0)
    else:
        raw = 0
    # encode_value re-applies scale by dividing, so feed it the scaled value
    return raw * reg.scale


@dataclass
class _TypeLayout:
    max_end: int
    block_size: int


def _size_blocks(register_map: RegisterMap, min_size: int = 16) -> dict[str, _TypeLayout]:
    """Compute datastore sizes per Modbus register type.

    pymodbus's server layer translates wire address N to block index N+1 before
    calling ``getValues``/``setValues``, so to back wire address ``max_addr``
    with ``count`` registers we need ``max_addr + count + 1`` slots.
    """
    ends: dict[str, int] = {}
    for reg in register_map.registers:
        ends[reg.type] = max(ends.get(reg.type, 0), reg.address + reg.count)
    layouts: dict[str, _TypeLayout] = {}
    for t in RegisterType:
        end = ends.get(t, 0)
        block_size = max(end + 1, min_size) if end > 0 else min_size
        layouts[t] = _TypeLayout(max_end=end, block_size=block_size)
    return layouts


def build_context(register_map: RegisterMap) -> ModbusServerContext:
    """Create a pymodbus ``ModbusServerContext`` sized for the given register map."""
    layouts = _size_blocks(register_map)
    device = ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(0, [0] * layouts[RegisterType.HOLDING].block_size),
        ir=ModbusSequentialDataBlock(0, [0] * layouts[RegisterType.INPUT].block_size),
        co=ModbusSequentialDataBlock(0, [False] * layouts[RegisterType.COIL].block_size),
        di=ModbusSequentialDataBlock(0, [False] * layouts[RegisterType.DISCRETE_INPUT].block_size),
    )
    return ModbusServerContext(devices=device, single=True)


def _write_register(
    context: ModbusServerContext,
    reg: Register,
    value: float | int | bool,
) -> None:
    """Encode and write a single register into the pymodbus datastore.

    pymodbus uses 1-based addressing in ``setValues``; the spec's address 0
    sits at index 1 in the block.
    """
    device = context[0]
    block = device.store[_BLOCK_KEY[reg.type]]
    if reg.type in (RegisterType.COIL, RegisterType.DISCRETE_INPUT):
        block.setValues(reg.address + 1, [bool(value)])
        return

    raw = encode_value(value, reg.datatype, reg.scale, reg.byte_order)
    block.setValues(reg.address + 1, raw)


def randomize_once(context: ModbusServerContext, register_map: RegisterMap) -> None:
    """Write one random value into every non-writable register in the map.

    Writable registers are skipped so that user writes against the mock
    server persist and can be observed on the next poll.
    """
    for reg in register_map.registers:
        if reg.writable:
            continue
        try:
            value = _random_raw_value(reg)
            _write_register(context, reg, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to randomize %s@%d: %s", reg.name, reg.address, exc)


class MockUpdater:
    """Background thread that periodically randomizes read-only registers."""

    def __init__(
        self,
        register_map: RegisterMap,
        context: ModbusServerContext,
        interval: float = 1.0,
    ) -> None:
        self.register_map = register_map
        self.context = context
        self.interval = interval
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        randomize_once(self.context, self.register_map)  # seed initial values
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("mock updater started (interval=%.2fs)", self.interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("mock updater stopped")

    def _run(self) -> None:
        while self._running:
            time.sleep(self.interval)
            if not self._running:
                break
            randomize_once(self.context, self.register_map)


async def run_mock_server(
    register_map: RegisterMap,
    host: str = "127.0.0.1",
    port: int = 5020,
    update_interval: float = 1.0,
) -> None:
    """Run a mock Modbus TCP server bound to ``host:port`` for the given map."""
    context = build_context(register_map)
    updater = MockUpdater(register_map, context, interval=update_interval)
    updater.start()

    total_regs = len(register_map.registers)
    writable = len(register_map.writable_registers)
    logger.info(
        "mock server on %s:%d — %d events, %d registers (%d writable)",
        host,
        port,
        len(register_map.events),
        total_regs,
        writable,
    )
    # Sanity: warn if the map declares datatypes we cannot encode
    unknown = {r.datatype for r in register_map.registers if r.datatype not in DATATYPES}
    if unknown:
        logger.warning("register map contains unknown datatypes: %s", unknown)

    try:
        await StartAsyncTcpServer(context=context, address=(host, port))
    finally:
        updater.stop()


def run_mock_server_sync(
    register_map: RegisterMap,
    host: str = "127.0.0.1",
    port: int = 5020,
    update_interval: float = 1.0,
) -> None:
    """Blocking wrapper around :func:`run_mock_server` for CLI use."""
    try:
        asyncio.run(run_mock_server(register_map, host, port, update_interval))
    except KeyboardInterrupt:
        logger.info("mock server stopped by user")
