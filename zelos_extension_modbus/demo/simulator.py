"""Simulated devices for demo mode and scan testing.

Uses pymodbus to run a local Modbus server (TCP, or RTU on a serial port)
with either a realistic power meter or a scan target: a sparse device with
holes, a 60-register read limit, device identification and seeded regions in
every byte order, for exercising ``scan``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import struct
import threading
import time
from collections.abc import Sequence
from typing import Any

from pymodbus import FramerType, ModbusDeviceIdentification
from pymodbus.constants import ExcCodes
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.datastore.store import BaseModbusDataBlock
from pymodbus.server import StartAsyncSerialServer, StartAsyncTcpServer

from zelos_extension_modbus.client import encode_value

logger = logging.getLogger(__name__)

# Demo register addresses (holding registers)
ADDR_VOLTAGE_L1 = 0  # float32 (2 registers)
ADDR_VOLTAGE_L2 = 2
ADDR_VOLTAGE_L3 = 4
ADDR_CURRENT_L1 = 6  # float32
ADDR_CURRENT_L2 = 8
ADDR_CURRENT_L3 = 10
ADDR_POWER_TOTAL = 12  # float32
ADDR_POWER_FACTOR = 14  # float32
ADDR_FREQUENCY = 16  # float32
ADDR_ENERGY_TOTAL = 18  # uint32 (2 registers) - Wh
ADDR_TEMPERATURE = 20  # int16 (scaled by 10)

# Coil addresses
ADDR_COIL_RELAY1 = 0
ADDR_COIL_RELAY2 = 1
ADDR_COIL_ALARM = 2

# Input register addresses (read-only)
ADDR_IR_FIRMWARE = 0  # uint16
ADDR_IR_SERIAL = 1  # uint32 (2 registers)
ADDR_IR_UPTIME = 3  # uint32 (2 registers)

# Discrete input addresses (read-only booleans)
ADDR_DI_DOOR = 0
ADDR_DI_FAULT = 1
ADDR_DI_GRID = 2

# Setpoint addresses (writable holding registers)
ADDR_VOLTAGE_HIGH = 100  # uint16
ADDR_VOLTAGE_LOW = 101  # uint16
ADDR_POWER_LIMIT = 102  # int32 (2 registers)
ADDR_ENERGY_RESET = 104  # uint32 (2 registers)

# Swapped float addresses (big_swap byte order)
ADDR_CAL_FACTOR = 110  # float32 big_swap
ADDR_OFFSET_VAL = 112  # float32 big_swap


def float32_to_registers(value: float) -> tuple[int, int]:
    """Convert float32 to two 16-bit registers (big-endian)."""
    packed = struct.pack(">f", value)
    return struct.unpack(">HH", packed)


def uint32_to_registers(value: int) -> tuple[int, int]:
    """Convert uint32 to two 16-bit registers (big-endian)."""
    packed = struct.pack(">I", value)
    return struct.unpack(">HH", packed)


def int32_to_registers(value: int) -> tuple[int, int]:
    """Convert int32 to two 16-bit registers (big-endian)."""
    packed = struct.pack(">i", value)
    return struct.unpack(">HH", packed)


def float32_to_registers_swapped(value: float) -> tuple[int, int]:
    """Convert float32 to two 16-bit registers (big-endian word-swapped)."""
    packed = struct.pack(">f", value)
    r1, r2 = struct.unpack(">HH", packed)
    return (r2, r1)  # Swap words


class PowerMeterSimulator:
    """Simulates a 3-phase power meter with realistic values."""

    def __init__(self) -> None:
        """Initialize simulator state."""
        self.start_time = time.time()

        # Base values (typical industrial 3-phase)
        self.nominal_voltage = 230.0  # V line-to-neutral
        self.nominal_frequency = 50.0  # Hz

        # Simulated load profile
        self.base_load = 50.0  # Base current in amps
        self.load_variation = 20.0  # Random variation

        # Accumulated energy (Wh)
        self.energy_total = 0.0

        # Temperature (ambient + self-heating)
        self.ambient_temp = 25.0

        # Relay states
        self.relay1 = False
        self.relay2 = False
        self.alarm = False

    def update(self, dt: float) -> dict:
        """Update simulation state and return current values.

        Args:
            dt: Time delta in seconds

        Returns:
            Dictionary of current register values
        """
        t = time.time() - self.start_time

        # Voltage with slight variation and phase offset
        voltage_l1 = self.nominal_voltage * (1.0 + 0.02 * math.sin(t * 0.1))
        voltage_l2 = self.nominal_voltage * (1.0 + 0.02 * math.sin(t * 0.1 + 2.094))
        voltage_l3 = self.nominal_voltage * (1.0 + 0.02 * math.sin(t * 0.1 + 4.189))

        # Current with load variation (simulates varying industrial load)
        load_factor = 1.0 + 0.3 * math.sin(t * 0.05)  # Slow load cycle
        noise = random.gauss(0, 0.05)

        current_l1 = max(0, self.base_load * load_factor * (1.0 + noise))
        current_l2 = max(0, self.base_load * load_factor * (1.0 + random.gauss(0, 0.05)))
        current_l3 = max(0, self.base_load * load_factor * (1.0 + random.gauss(0, 0.05)))

        # Power calculation (3-phase)
        power_factor = 0.85 + 0.1 * math.sin(t * 0.02)  # Varies 0.75-0.95
        power_total = (
            (voltage_l1 * current_l1 + voltage_l2 * current_l2 + voltage_l3 * current_l3)
            * power_factor
            / 1000.0
        )  # kW

        # Frequency with tiny drift
        frequency = self.nominal_frequency + 0.05 * math.sin(t * 0.3)

        # Accumulate energy
        self.energy_total += power_total * dt / 3600.0 * 1000  # Wh

        # Temperature rises with load
        avg_current = (current_l1 + current_l2 + current_l3) / 3
        self.ambient_temp = 25.0 + (avg_current / self.base_load) * 15.0

        # Alarm if over-temperature
        self.alarm = self.ambient_temp > 50.0

        return {
            "voltage_l1": voltage_l1,
            "voltage_l2": voltage_l2,
            "voltage_l3": voltage_l3,
            "current_l1": current_l1,
            "current_l2": current_l2,
            "current_l3": current_l3,
            "power_total": power_total,
            "power_factor": power_factor,
            "frequency": frequency,
            "energy_total": int(self.energy_total),
            "temperature": int(self.ambient_temp * 10),  # Scaled
            "relay1": self.relay1,
            "relay2": self.relay2,
            "alarm": self.alarm,
        }


class SimulatorUpdater:
    """Background thread that updates simulator values in the datastore."""

    def __init__(
        self,
        simulator: PowerMeterSimulator,
        context: ModbusServerContext,
        interval: float = 0.1,
        unit_id: int = 0,
    ) -> None:
        """Initialize updater.

        Args:
            simulator: PowerMeterSimulator instance
            context: Modbus server context
            interval: Update interval in seconds
            unit_id: Unit whose datastore to drive (any id on a single context)
        """
        self.simulator = simulator
        self.context = context
        self.interval = interval
        self.unit_id = unit_id
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start background update thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Simulator updater started")

    def stop(self) -> None:
        """Stop background update thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("Simulator updater stopped")

    def _run(self) -> None:
        """Update loop."""
        last_time = time.time()

        while self._running:
            now = time.time()
            dt = now - last_time
            last_time = now

            values = self.simulator.update(dt)
            self._update_datastore(values)

            time.sleep(self.interval)

    def _update_datastore(self, values: dict) -> None:
        """Write simulator values to Modbus datastore."""
        device = self.context[self.unit_id]

        # Holding registers (float32 values as register pairs)
        hr = device.store["h"]

        # Voltages
        r1, r2 = float32_to_registers(values["voltage_l1"])
        hr.setValues(ADDR_VOLTAGE_L1 + 1, [r1, r2])

        r1, r2 = float32_to_registers(values["voltage_l2"])
        hr.setValues(ADDR_VOLTAGE_L2 + 1, [r1, r2])

        r1, r2 = float32_to_registers(values["voltage_l3"])
        hr.setValues(ADDR_VOLTAGE_L3 + 1, [r1, r2])

        # Currents
        r1, r2 = float32_to_registers(values["current_l1"])
        hr.setValues(ADDR_CURRENT_L1 + 1, [r1, r2])

        r1, r2 = float32_to_registers(values["current_l2"])
        hr.setValues(ADDR_CURRENT_L2 + 1, [r1, r2])

        r1, r2 = float32_to_registers(values["current_l3"])
        hr.setValues(ADDR_CURRENT_L3 + 1, [r1, r2])

        # Power
        r1, r2 = float32_to_registers(values["power_total"])
        hr.setValues(ADDR_POWER_TOTAL + 1, [r1, r2])

        r1, r2 = float32_to_registers(values["power_factor"])
        hr.setValues(ADDR_POWER_FACTOR + 1, [r1, r2])

        # Frequency
        r1, r2 = float32_to_registers(values["frequency"])
        hr.setValues(ADDR_FREQUENCY + 1, [r1, r2])

        # Energy (uint32)
        r1, r2 = uint32_to_registers(values["energy_total"])
        hr.setValues(ADDR_ENERGY_TOTAL + 1, [r1, r2])

        # Temperature (int16, scaled)
        hr.setValues(ADDR_TEMPERATURE + 1, [values["temperature"] & 0xFFFF])

        # Coils — only write alarm (simulator-driven); relay1/relay2 are user-controlled
        coils = device.store["c"]
        coils.setValues(ADDR_COIL_ALARM + 1, [values["alarm"]])

        # Input registers (read-only values that change over time)
        ir = device.store["i"]
        # Uptime in hours (simulated from simulator start time)
        uptime_hours = int((time.time() - self.simulator.start_time) / 3600)
        r1, r2 = uint32_to_registers(uptime_hours)
        ir.setValues(ADDR_IR_UPTIME + 1, [r1, r2])

        # Discrete inputs (simulate occasional changes)
        di = device.store["d"]
        # Door randomly opens/closes (1% chance per update)
        if random.random() < 0.01:
            current = di.getValues(ADDR_DI_DOOR + 1, 1)[0]
            di.setValues(ADDR_DI_DOOR + 1, [not current])


def create_demo_context(unit_ids: Sequence[int] = ()) -> ModbusServerContext:
    """Create Modbus server context with demo datastore.

    No ``unit_ids``: one meter answers every unit id. Otherwise each id gets its
    own meter, told apart by serial number ``12345678 + unit_id``.
    """
    if not unit_ids:
        return ModbusServerContext(devices=_demo_device(12345678), single=True)
    devices = {uid: _demo_device(12345678 + uid) for uid in unit_ids}
    return ModbusServerContext(devices=devices, single=False)


def _demo_device(serial: int) -> ModbusDeviceContext:
    """One meter's datastore with its initial values."""
    # Initialize data blocks
    # Holding registers: 200 registers (to cover setpoints and swapped floats)
    hr_block = ModbusSequentialDataBlock(0, [0] * 200)

    # Coils: 16 coils
    coil_block = ModbusSequentialDataBlock(0, [False] * 16)

    # Discrete inputs: 16 inputs (initialized after context creation)
    di_block = ModbusSequentialDataBlock(0, [False] * 16)

    # Input registers: 100 registers (initialized after context creation)
    ir_block = ModbusSequentialDataBlock(0, [0] * 100)

    device = ModbusDeviceContext(
        di=di_block,
        co=coil_block,
        hr=hr_block,
        ir=ir_block,
    )

    # Set initial values for setpoints in holding registers
    hr = device.store["h"]
    hr.setValues(ADDR_VOLTAGE_HIGH + 1, [250])  # 250V high limit
    hr.setValues(ADDR_VOLTAGE_LOW + 1, [210])  # 210V low limit
    r1, r2 = int32_to_registers(50000)  # 50kW power limit
    hr.setValues(ADDR_POWER_LIMIT + 1, [r1, r2])
    r1, r2 = uint32_to_registers(0)  # energy reset counter
    hr.setValues(ADDR_ENERGY_RESET + 1, [r1, r2])
    # Swapped floats
    r1, r2 = float32_to_registers_swapped(1.0)  # calibration factor
    hr.setValues(ADDR_CAL_FACTOR + 1, [r1, r2])
    r1, r2 = float32_to_registers_swapped(0.0)  # offset value
    hr.setValues(ADDR_OFFSET_VAL + 1, [r1, r2])

    # Set initial values for input registers (read-only)
    ir = device.store["i"]
    ir.setValues(ADDR_IR_FIRMWARE + 1, [0x0102])  # Firmware v1.2
    r1, r2 = uint32_to_registers(serial)  # Serial number
    ir.setValues(ADDR_IR_SERIAL + 1, [r1, r2])

    # Set initial values for discrete inputs (read-only booleans)
    di = device.store["d"]
    di.setValues(ADDR_DI_DOOR + 1, [False])  # door closed
    di.setValues(ADDR_DI_FAULT + 1, [False])  # no fault
    di.setValues(ADDR_DI_GRID + 1, [True])  # grid connected

    return device


# ---------------------------------------------------------------------------
# Scan target
# ---------------------------------------------------------------------------

#: Largest read the scan target serves; bigger reads get exception 03.
SCAN_TARGET_MAX_BLOCK = 60

#: Valid 0-based addresses per table as inclusive (first, last) ranges. Every
#: other address answers exception 02, or 0 in zero-fill mode.
SCAN_TARGET_RANGES = {
    "holding": [(0, 149), (200, 219), (300, 319), (400, 419), (1000, 1009)],
    "input": [(0, 29)],
    "coil": [(0, 15)],
    "discrete_input": [(0, 7)],
}

#: Holding runs of ten float32 values, by first address, in each byte order.
SCAN_TARGET_FLOATS = {0: "big", 200: "big_swap", 300: "little", 400: "little_swap"}
SCAN_TARGET_COUNTER = 20  # uint32 big, climbs 20000/s so the low word wraps
SCAN_TARGET_STRING = (30, "ZELOS SCAN TARGET")  # 17 chars -> 9 registers, NUL padded
SCAN_TARGET_IDENTITY = {
    "VendorName": "Zelos",
    "ProductCode": "ZSCAN-1",
    "MajorMinorRevision": "1.0",
    "VendorUrl": "https://zeloscloud.io",
    "ProductName": "Scan target",
    "ModelName": "ST-1",
    "UserApplicationName": "demo-server",
}
SUNSPEC_BASE = 40000
_SUNSPEC_WORDS = [0x5375, 0x6E53, 0xFFFF, 0x0000]  # 'SunS' + end-of-models marker

_FLOAT_BASES = [230.0, 231.0, 229.0, 12.5, 13.1, 11.8, 50.0, 0.95, 1500.0, -3.2]


class _SparseBlock(BaseModbusDataBlock):
    """Datablock with holes and a per-read size limit, like a real device.

    pymodbus' device context passes ``address + 1``; the block keys by the
    0-based wire address.
    """

    def __init__(self, values: dict[int, Any], max_count: int, zero_fill: bool) -> None:
        self.values = values
        self.address = 0
        self.default_value = 0
        self.max_count = max_count
        self.zero_fill = zero_fill

    def getValues(self, address: int, count: int = 1) -> list[Any] | ExcCodes:  # noqa: N802
        if count > self.max_count:
            return ExcCodes.ILLEGAL_VALUE
        addrs = range(address - 1, address - 1 + count)
        if not self.zero_fill and any(a not in self.values for a in addrs):
            return ExcCodes.ILLEGAL_ADDRESS
        return [self.values.get(a, 0) for a in addrs]

    def setValues(self, address: int, values: list[Any]) -> ExcCodes:  # noqa: N802
        return ExcCodes.ILLEGAL_FUNCTION  # read-only device


class ScanTarget:
    """A sparse device seeded with what scan must recover (see SCAN_TARGET_*)."""

    def __init__(self, zero_fill: bool = False, sunspec: bool = False) -> None:
        self.start_time = time.time()
        self.tables: dict[str, dict[int, Any]] = {}
        for table, ranges in SCAN_TARGET_RANGES.items():
            is_bits = table in ("coil", "discrete_input")
            self.tables[table] = {
                a: (False if is_bits else 0) for lo, hi in ranges for a in range(lo, hi + 1)
            }
        hr, ir = self.tables["holding"], self.tables["input"]
        start, text = SCAN_TARGET_STRING
        raw = text.encode().ljust(18, b"\0")
        for i in range(9):
            hr[start + i] = int.from_bytes(raw[2 * i : 2 * i + 2])
        hr.update({24: 0x0102, 25: 7, 26: 42, 27: 1, 28: 500, 29: 3})
        hr.update({1005 + i: v for i, v in enumerate((10, 20, 30, 40, 50))})
        ir.update({20 + i: 100 * (i + 1) for i in range(10)})
        if sunspec:
            hr.update({SUNSPEC_BASE + i: w for i, w in enumerate(_SUNSPEC_WORDS)})
        self.context = ModbusServerContext(
            devices=ModbusDeviceContext(
                **{
                    key: _SparseBlock(self.tables[table], SCAN_TARGET_MAX_BLOCK, zero_fill)
                    for table, key in (
                        ("holding", "hr"),
                        ("input", "ir"),
                        ("coil", "co"),
                        ("discrete_input", "di"),
                    )
                }
            ),
            single=True,
        )
        self._running = False
        self._thread: threading.Thread | None = None
        self.update()

    def update(self) -> None:
        """Move the live values: floats wander, the counter climbs, bits toggle."""
        t = time.time() - self.start_time
        hr, ir = self.tables["holding"], self.tables["input"]
        for base, order in SCAN_TARGET_FLOATS.items():
            for k, nominal in enumerate(_FLOAT_BASES):
                value = nominal * (1 + 0.02 * math.sin(0.7 * t + k + base))
                hr[base + 2 * k], hr[base + 2 * k + 1] = encode_value(value, "float32", 1, order)
        hr[20], hr[21] = encode_value(3_000_000 + int(20_000 * t), "uint32")
        hr[22] = 1000 + int(50 * math.sin(t))  # analog uint16
        hr[23] = 1 << (int(t) % 4)  # one status bit at a time
        for k in range(5):
            ir[2 * k], ir[2 * k + 1] = encode_value(20.0 + k + math.sin(t + k), "float32")
        ir[10] = int(t) & 0xFFFF  # uptime seconds
        self.tables["coil"][0] = int(t) % 2 == 0
        self.tables["discrete_input"][1] = int(t / 2) % 2 == 0

    def start(self, interval: float = 0.1) -> None:
        """Update in a background thread."""
        self._running = True

        def run() -> None:
            while self._running:
                self.update()
                time.sleep(interval)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the update thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)


async def run_demo_server(
    host: str = "127.0.0.1",
    port: int = 5020,
    running_flag: asyncio.Event | None = None,
    unit_ids: Sequence[int] = (),
    scan_target: bool = False,
    zero_fill: bool = False,
    sunspec: bool = False,
    serial: dict[str, Any] | None = None,
) -> None:
    """Run the demo Modbus server.

    Args:
        host: Server bind address
        port: Server port
        running_flag: Optional event to signal shutdown
        unit_ids: One meter per id (see create_demo_context); empty = one for all
        scan_target: Serve the scan target instead of the power meter
        zero_fill: Scan target answers unmapped addresses with 0, not exception 02
        sunspec: Scan target carries the SunSpec 'SunS' marker at 40000
        serial: RTU on a serial port instead of TCP: ``port``, ``baudrate``,
            ``parity``, ``stopbits``
    """
    identity = None
    if scan_target:
        target = ScanTarget(zero_fill=zero_fill, sunspec=sunspec)
        context = target.context
        updaters: list[Any] = [target]
        identity = ModbusDeviceIdentification(info_name=SCAN_TARGET_IDENTITY)
    else:
        context = create_demo_context(unit_ids)
        updaters = [
            SimulatorUpdater(PowerMeterSimulator(), context, unit_id=uid) for uid in unit_ids or [0]
        ]
    for updater in updaters:
        updater.start()

    try:
        if serial:
            logger.info(f"Starting demo Modbus RTU server on {serial['port']}")
            await StartAsyncSerialServer(
                context=context, identity=identity, framer=FramerType.RTU, **serial
            )
        else:
            logger.info(f"Starting demo Modbus server on {host}:{port}")
            await StartAsyncTcpServer(context=context, identity=identity, address=(host, port))
    finally:
        for updater in updaters:
            updater.stop()


def run_demo_server_sync(host: str = "127.0.0.1", port: int = 5020, **kwargs: Any) -> None:
    """Blocking wrapper around :func:`run_demo_server` for CLI use.

    A failed bind raises a RuntimeError naming the endpoint. pymodbus reduces the
    underlying EADDRINUSE to a bare "Could not start listen, please check
    address.", which names neither the address nor the cause — and the cause is
    almost always a simulator already running there.

    Raises:
        RuntimeError: The server could not bind ``host:port``.
    """
    try:
        asyncio.run(run_demo_server(host=host, port=port, **kwargs))
    except KeyboardInterrupt:
        logger.info("Demo server stopped")
    except (OSError, RuntimeError) as e:
        raise RuntimeError(
            f"Could not start demo server on {host}:{port} ({e}) — "
            "a simulator may already be running there"
        ) from e
