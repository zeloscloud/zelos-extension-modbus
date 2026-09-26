"""Zelos Modbus Extension - Read, write, and monitor Modbus registers."""

from zelos_extension_modbus.client import ModbusConnection, ModbusDevice
from zelos_extension_modbus.register_map import RegisterMap

#: Action namespace (`Modbus/<action>`), fixed regardless of the trace prefix.
#: Matches `name` in extension.toml.
ACTION_PREFIX = "Modbus"

__all__ = ["ACTION_PREFIX", "ModbusConnection", "ModbusDevice", "RegisterMap"]
