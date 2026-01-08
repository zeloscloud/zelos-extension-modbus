"""Simulated industrial power meter for demo mode.

Uses pymodbus to run a local Modbus TCP server with realistic
power meter data that changes over time.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import struct
import threading
import time
from typing import TYPE_CHECKING

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

if TYPE_CHECKING:
    pass

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


def float32_to_registers(value: float) -> tuple[int, int]:
    """Convert float32 to two 16-bit registers (big-endian)."""
    packed = struct.pack(">f", value)
    return struct.unpack(">HH", packed)


def uint32_to_registers(value: int) -> tuple[int, int]:
    """Convert uint32 to two 16-bit registers (big-endian)."""
    packed = struct.pack(">I", value)
    return struct.unpack(">HH", packed)


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
            voltage_l1 * current_l1
            + voltage_l2 * current_l2
            + voltage_l3 * current_l3
        ) * power_factor / 1000.0  # kW

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
    ) -> None:
        """Initialize updater.

        Args:
            simulator: PowerMeterSimulator instance
            context: Modbus server context
            interval: Update interval in seconds
        """
        self.simulator = simulator
        self.context = context
        self.interval = interval
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
        slave = self.context[0]

        # Holding registers (float32 values as register pairs)
        hr = slave.store["h"]

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

        # Coils
        coils = slave.store["c"]
        coils.setValues(ADDR_COIL_RELAY1 + 1, [values["relay1"]])
        coils.setValues(ADDR_COIL_RELAY2 + 1, [values["relay2"]])
        coils.setValues(ADDR_COIL_ALARM + 1, [values["alarm"]])


def create_demo_context() -> ModbusServerContext:
    """Create Modbus server context with demo datastore."""
    # Initialize data blocks
    # Holding registers: 100 registers
    hr_block = ModbusSequentialDataBlock(0, [0] * 100)

    # Coils: 16 coils
    coil_block = ModbusSequentialDataBlock(0, [False] * 16)

    # Discrete inputs: 16 inputs
    di_block = ModbusSequentialDataBlock(0, [False] * 16)

    # Input registers: 100 registers
    ir_block = ModbusSequentialDataBlock(0, [0] * 100)

    slave = ModbusSlaveContext(
        di=di_block,
        co=coil_block,
        hr=hr_block,
        ir=ir_block,
    )

    return ModbusServerContext(slaves=slave, single=True)


async def run_demo_server(
    host: str = "127.0.0.1",
    port: int = 5020,
    running_flag: asyncio.Event | None = None,
) -> None:
    """Run the demo Modbus TCP server.

    Args:
        host: Server bind address
        port: Server port
        running_flag: Optional event to signal shutdown
    """
    context = create_demo_context()
    simulator = PowerMeterSimulator()
    updater = SimulatorUpdater(simulator, context)

    updater.start()

    logger.info(f"Starting demo Modbus server on {host}:{port}")

    try:
        await StartAsyncTcpServer(
            context=context,
            address=(host, port),
        )
    finally:
        updater.stop()
