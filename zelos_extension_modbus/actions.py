"""Free-standing Modbus action functions for the Zelos SDK.

Every action takes an ``interface`` parameter as its first argument, which
selects the target client from the global registry.  Actions appear as
modbus/get_status, modbus/read_register, etc.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import zelos_sdk

from zelos_extension_modbus.constants import MODBUS_MAX_READ_COUNT, RegisterType
from zelos_extension_modbus.registry import (
    all_interfaces,
    get_client,
    interface_registers,
    interface_writable_registers,
)

logger = logging.getLogger(__name__)


ALL_ACTIONS = []  # populated at module bottom after function definitions


def register_all() -> None:
    """Register all action functions.

    The namespace comes from zelos_sdk.init(name="modbus"), so actions
    appear as modbus/get_status, modbus/read_register, etc.
    """
    for fn in ALL_ACTIONS:
        zelos_sdk.actions_registry.register(fn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_coro(coro: Any, client: Any) -> Any:
    """Run an async coroutine from a sync action handler.

    Bridges the SDK's sync action thread to the client's async event loop.
    """
    if client._loop and client._loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, client._loop)
        return future.result(timeout=client.timeout + 5)
    return asyncio.run(coro)


def _json_safe(value: Any) -> Any:
    """Make one decoded value safe to put in an action payload.

    The SDK converts action results to JSON in Rust, which rejects non-finite
    floats outright ("Invalid float value") and fails the whole action. A single
    poisoned register (a float32 read back as 0xFFFF,0xFFFF decodes to NaN) would
    otherwise take down every aggregate payload, permanently. Non-finite floats
    become null on the wire; the client's cache keeps what the device reported.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _resolve_register(client: Any, path: str) -> tuple[str | None, Any, str | None]:
    """Resolve an 'event/field' path to a Register on a specific client.

    Returns (error_message, register, event) — ``error_message`` and ``register``
    are mutually exclusive. ``event`` is the owning event when the path carried
    one, else None (the bare-name compat path leaves it to the register map).
    """
    if not client.register_map:
        return ("No register map loaded", None, None)

    parts = path.split("/", 1)
    if len(parts) == 2:
        event_name, reg_name = parts
        for reg in client.register_map.get_event(event_name):
            if reg.name == reg_name:
                return (None, reg, event_name)
        return (f"Register '{path}' not found", None, None)

    # Fallback: bare name lookup (backwards compat)
    reg = client.register_map.get_by_name(path)
    if not reg:
        return (f"Register '{path}' not found", None, None)
    return (None, reg, None)


def _get_client_or_error(interface: str) -> tuple[Any | None, dict | None]:
    """Look up a client by interface name, returning an error dict on failure."""
    client = get_client(interface)
    if not client:
        return None, {"error": f"Interface '{interface}' not found", "success": False}
    return client, None


def _status_row(interface: str, client: Any) -> dict[str, Any]:
    """The identity/connection/counter keys get_status and get_snapshot share.

    ``connection`` is the prefix-free wire endpoint; get_status overrides it with
    the log-formatted string to keep its pre-existing surface.
    """
    return {
        "interface": interface,
        "connected": client._connected,
        "transport": client.transport,
        "connection": client.endpoint,
        "unit_id": client.unit_id,
        "poll_count": client._poll_count,
        "error_count": client._error_count,
    }


def _register_row(event: str, reg: Any) -> dict[str, Any]:
    """One catalog row for a register, including its named-action ``path``.

    ``poll_interval`` is the register's raw configured value: None (interface
    default), 0 (polling disabled), or its own rate in seconds.
    """
    return {
        "name": reg.name,
        "event": event,
        "path": f"{event}/{reg.name}",
        "address": reg.address,
        "type": reg.type,
        "datatype": reg.datatype,
        "unit": reg.unit,
        "scale": reg.scale,
        "description": reg.description,
        "writable": reg.writable,
        "byte_order": reg.byte_order,
        "poll_interval": reg.poll_interval,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@zelos_sdk.action(
    "List Interfaces",
    "List every configured Modbus interface — the names the other actions accept",
)
def list_interfaces() -> dict[str, Any]:
    """List all registered interfaces with their transport and register-map summary."""
    interfaces = []
    for name in all_interfaces():
        client = get_client(name)
        interfaces.append(
            {
                "name": name,
                "transport": client.transport,
                "connected": client._connected,
                "connection": client.endpoint,
                "unit_id": client.unit_id,
                "source": client.source_name,
                "map_name": client.register_map.name if client.register_map else None,
                "register_count": len(client.register_map.registers) if client.register_map else 0,
                "poll_interval": client.poll_interval,
                "write_mode": client.write_mode,
            }
        )
    return {"interfaces": interfaces, "count": len(interfaces), "success": True}


@zelos_sdk.action("Get Status", "Get connection and polling status")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
def get_status(interface: str) -> dict[str, Any]:
    """Get current client status."""
    client, err = _get_client_or_error(interface)
    if err:
        return err
    return {
        **_status_row(interface, client),
        # Pre-existing surface: this action reports the log-formatted connection
        # string (interface-prefixed), unlike list_interfaces / get_snapshot.
        "connection": client._connection_str,
        "poll_interval": client.poll_interval,
        "write_mode": client.write_mode,
        "block_reads": client.block_reads,
        "max_block_size": client.max_block_size,
        "max_read_gap": client.max_read_gap,
        "registers": len(client.register_map.registers) if client.register_map else 0,
        "success": True,
    }


@zelos_sdk.action(
    "Get Snapshot",
    "Cached status and last register values for one interface — no device I/O",
)
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
def get_snapshot(interface: str) -> dict[str, Any]:
    """Snapshot of one interface's status and last-seen values, straight from cache.

    Reads nothing from the bus: values come from the poll sweep's cache (and any
    on-demand named reads), so registers with polling disabled are absent until
    read once via read_named_register.
    """
    client, err = _get_client_or_error(interface)
    if err:
        return err
    # Copy the cache first, stamp second: every ts_ms in the payload is then a
    # value that existed before captured_at_unix_ms, by construction.
    cached = client.last_values
    captured_at_unix_ms = int(time.time() * 1000)
    # Extension id/version/state intentionally NOT included — that info is
    # canonical at the `extensions.list` bridge surface and the webapp consumes
    # it from there, not from this 1 Hz polled action.
    return {
        **_status_row(interface, client),
        "captured_at_unix_ms": captured_at_unix_ms,
        "values": {
            path: {"value": _json_safe(value), "ts_ms": ts_ms}
            for path, (value, ts_ms) in cached.items()
        },
        "success": True,
    }


@zelos_sdk.action("Read Register", "Read a single register by address")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
@zelos_sdk.action.select(
    "reg_type",
    choices=list(RegisterType),
    default=RegisterType.HOLDING,
    title="Register Type",
)
@zelos_sdk.action.number(
    "count", minimum=1, maximum=MODBUS_MAX_READ_COUNT, default=1, title="Count"
)
def read_register(interface: str, address: int, reg_type: str, count: int) -> dict[str, Any]:
    """Read register(s) by address."""
    client, err = _get_client_or_error(interface)
    if err:
        return err

    async def _read() -> list | None:
        if not client._connected:
            await client.connect()
        if reg_type == RegisterType.HOLDING:
            return await client.read_holding_registers(int(address), int(count))
        elif reg_type == RegisterType.INPUT:
            return await client.read_input_registers(int(address), int(count))
        elif reg_type == RegisterType.COIL:
            return await client.read_coils(int(address), int(count))
        else:  # discrete_input
            return await client.read_discrete_inputs(int(address), int(count))

    result = _run_coro(_read(), client)
    return {
        "address": address,
        "type": reg_type,
        "count": count,
        "values": result,
        "success": result is not None,
    }


@zelos_sdk.action(
    "Write Single Register (FC 6)", "Write one holding register using function code 6"
)
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
@zelos_sdk.action.number("value", title="Value")
def write_single_register(interface: str, address: int, value: int) -> dict[str, Any]:
    """Write a single register using FC 6."""
    client, err = _get_client_or_error(interface)
    if err:
        return err

    async def _write() -> bool:
        if not client._connected:
            await client.connect()
        return await client.write_register(int(address), int(value))

    success = _run_coro(_write(), client)
    return {
        "address": address,
        "value": value,
        "function_code": 6,
        "success": success,
    }


@zelos_sdk.action(
    "Write Registers (FC 16)", "Write one or more holding registers using function code 16"
)
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Start Address")
@zelos_sdk.action.text("values", title="Values (comma-separated)")
def write_registers(interface: str, address: int, values: str) -> dict[str, Any]:
    """Write registers using FC 16."""
    client, err = _get_client_or_error(interface)
    if err:
        return err
    try:
        int_values = [int(v.strip()) for v in values.split(",")]
    except ValueError:
        return {"error": "Values must be comma-separated integers", "success": False}

    async def _write() -> bool:
        if not client._connected:
            await client.connect()
        return await client.write_registers(int(address), int_values)

    success = _run_coro(_write(), client)
    return {
        "address": address,
        "values": int_values,
        "count": len(int_values),
        "function_code": 16,
        "success": success,
    }


@zelos_sdk.action("Read Named Register", "Read a register by event/name (e.g. voltage/L1)")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.select(
    "name", choices=interface_registers, depends_on="interface", title="Register"
)
def read_named_register(interface: str, name: str) -> dict[str, Any]:
    """Read a register by event/name path from the register map."""
    client, err = _get_client_or_error(interface)
    if err:
        return err

    error, reg, event = _resolve_register(client, name)
    if error:
        return {"error": error, "success": False}

    async def _read() -> Any:
        if not client._connected:
            await client.connect()
        return await client.read_register_value(reg)

    value = _run_coro(_read(), client)
    if value is not None:
        # Refresh the snapshot cache so an unpolled register shows a value too.
        client.record_value(reg, value, event=event)
    return {
        "name": name,
        "address": reg.address,
        "type": reg.type,
        "datatype": reg.datatype,
        # ``success`` tracks the read, not JSON-representability: a NaN reading is
        # a successful read of a value that cannot be serialized, so it reports
        # success with a null value.
        "value": _json_safe(value),
        "unit": reg.unit,
        "success": value is not None,
    }


@zelos_sdk.action("Write Named Register", "Write a value to a register by event/name")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.select(
    "name", choices=interface_writable_registers, depends_on="interface", title="Register"
)
@zelos_sdk.action.number("value", title="Value")
def write_named_register(interface: str, name: str, value: float) -> dict[str, Any]:
    """Write a value to a register by event/name path from the register map."""
    client, err = _get_client_or_error(interface)
    if err:
        return err

    error, reg, event = _resolve_register(client, name)
    if error:
        return {"error": error, "success": False}

    if not reg.writable:
        return {
            "error": f"Register '{name}' is not writable (type: {reg.type})",
            "success": False,
        }

    async def _write() -> bool:
        if not client._connected:
            await client.connect()
        return await client.write_register_value(reg, value)

    success = _run_coro(_write(), client)
    if success:
        # A written setpoint is the freshest thing we know about it; without this
        # an unpolled register would read stale in snapshots forever.
        client.record_value(reg, value, event=event)
    return {
        "name": name,
        "address": reg.address,
        "type": reg.type,
        "datatype": reg.datatype,
        "value": value,
        "unit": reg.unit,
        "success": success,
    }


@zelos_sdk.action("Write Coil", "Write a boolean value to a coil")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
@zelos_sdk.action.number("address", minimum=0, maximum=65535, title="Address")
@zelos_sdk.action.select("value", choices=["ON", "OFF"], default="OFF", title="Value")
def write_coil(interface: str, address: int, value: str) -> dict[str, Any]:
    """Write a coil by address."""
    client, err = _get_client_or_error(interface)
    if err:
        return err
    bool_value = value == "ON"

    async def _write() -> bool:
        if not client._connected:
            await client.connect()
        return await client.write_coil(int(address), bool_value)

    success = _run_coro(_write(), client)
    return {
        "address": address,
        "value": bool_value,
        "success": success,
    }


@zelos_sdk.action("List Registers", "List all registers in the map")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
def list_registers(interface: str) -> dict[str, Any]:
    """List all registers in the register map."""
    client, err = _get_client_or_error(interface)
    if err:
        return err
    if not client.register_map:
        return {"registers": [], "count": 0, "map_name": None, "success": True}

    regs = [
        _register_row(event, r)
        for event, event_regs in client.register_map.events.items()
        for r in event_regs
    ]
    return {
        "registers": regs,
        "count": len(regs),
        "map_name": client.register_map.name,
        "success": True,
    }


@zelos_sdk.action("List Writable Registers", "List all writable registers")
@zelos_sdk.action.select("interface", choices=all_interfaces, title="Interface")
def list_writable_registers(interface: str) -> dict[str, Any]:
    """List all writable registers in the register map."""
    client, err = _get_client_or_error(interface)
    if err:
        return err
    if not client.register_map:
        return {"registers": [], "count": 0, "map_name": None, "success": True}

    regs = [
        _register_row(event, r)
        for event, event_regs in client.register_map.events.items()
        for r in event_regs
        if r.writable
    ]
    return {
        "registers": regs,
        "count": len(regs),
        "map_name": client.register_map.name,
        "success": True,
    }


# Populate ALL_ACTIONS after all functions are defined
ALL_ACTIONS = [
    list_interfaces,
    get_status,
    get_snapshot,
    read_register,
    write_single_register,
    write_registers,
    read_named_register,
    write_named_register,
    write_coil,
    list_registers,
    list_writable_registers,
]
