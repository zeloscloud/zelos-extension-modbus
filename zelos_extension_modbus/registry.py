"""Global client registry and dynamic dropdown helpers for actions.

Clients are registered here after creation so that free-standing ``@action``
functions can reference them via dynamic ``choices`` callbacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ModbusClient

# Global registry: interface_name -> ModbusClient instance
_clients: dict[str, ModbusClient] = {}


def register(name: str, client: ModbusClient) -> None:
    """Register a client for use by action dynamic dropdowns."""
    _clients[name] = client


def get_client(name: str) -> ModbusClient | None:
    """Look up a registered client by name."""
    return _clients.get(name)


def clear() -> None:
    """Clear the registry (useful for tests)."""
    _clients.clear()


# --- Dynamic dropdown callbacks (passed as choices=callable) ---


def all_interfaces() -> list[str]:
    """Return names of all registered interfaces."""
    return list(_clients.keys())


def interface_registers(interface: str) -> list[str]:
    """Return event/field paths for all registers on a given interface."""
    client = _clients.get(interface)
    if not client or not client.register_map:
        return []
    return [
        f"{event}/{reg.name}" for event, regs in client.register_map.events.items() for reg in regs
    ]


def interface_writable_registers(interface: str) -> list[str]:
    """Return event/field paths for writable registers on a given interface."""
    client = _clients.get(interface)
    if not client or not client.register_map:
        return []
    return [
        f"{event}/{reg.name}"
        for event, regs in client.register_map.events.items()
        for reg in regs
        if reg.writable
    ]
