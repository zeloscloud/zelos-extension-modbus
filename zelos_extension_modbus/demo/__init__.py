"""Demo mode for Zelos Modbus extension.

Provides a simulated industrial power meter using pymodbus's simulator,
allowing testing without real hardware.
"""

from zelos_extension_modbus.demo.simulator import PowerMeterSimulator, run_demo_server

__all__ = ["PowerMeterSimulator", "run_demo_server"]
