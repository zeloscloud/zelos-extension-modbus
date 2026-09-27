"""Global device registry and dynamic dropdown helpers for actions.

Devices are registered here after creation so that free-standing ``@action``
functions can reference them via dynamic ``choices`` callbacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ModbusDevice

# Global registry: "<connection>/<device>" -> ModbusDevice
_devices: dict[str, ModbusDevice] = {}


def register(device: ModbusDevice) -> None:
    """Register a device under its ``<connection>/<device>`` path."""
    _devices[device.path] = device


def get_device(path: str) -> ModbusDevice | None:
    """Look up a registered device by path."""
    return _devices.get(path)


def clear() -> None:
    """Clear the registry (useful for tests)."""
    _devices.clear()


# --- Dynamic dropdown callbacks (passed as choices=callable) ---


def all_devices() -> list[str]:
    """Return paths of all registered devices."""
    return list(_devices.keys())


def _paths(device: str, writable_only: bool) -> list[str]:
    dev = _devices.get(device)
    if not dev or not dev.register_map:
        return []
    return [
        f"{event}/{reg.name}"
        for event, regs in dev.register_map.events.items()
        for reg in regs
        if reg.writable or not writable_only
    ]


def device_registers(device: str = "") -> list[str]:
    """Event/field paths of every register on ``device``."""
    return _paths(device, writable_only=False)


def device_writable_registers(device: str = "") -> list[str]:
    """Event/field paths of the writable registers on ``device``."""
    return _paths(device, writable_only=True)
