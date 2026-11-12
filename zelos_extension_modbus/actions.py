"""Free-standing Modbus action functions for the Zelos SDK.

Every per-device action takes a ``device`` parameter first
(``<connection>/<device>``), which selects the target from the global registry;
unset, the only configured device.
Actions appear as Modbus/get_status, Modbus/read_register, etc.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import time
from pathlib import Path
from typing import Any

import zelos_sdk

from zelos_extension_modbus.client import (
    NO_RESPONSE,
    OUTCOME_UNKNOWN,
    RequestFailed,
    coil_state,
    encode_register,
    json_safe,
)
from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MODBUS_MAX_READ_COUNT,
    MODBUS_MAX_WRITE_COUNT,
    PROBE_TIMEOUT,
    RegisterType,
    Transport,
)
from zelos_extension_modbus.registry import (
    all_devices,
    device_registers,
    device_writable_registers,
    get_device,
)

logger = logging.getLogger(__name__)


def register_all() -> None:
    """Register all action functions.

    The namespace comes from zelos_sdk.init(name=ACTION_PREFIX), so actions
    appear as Modbus/get_status, Modbus/read_register, etc.
    """
    for fn in ALL_ACTIONS:
        zelos_sdk.actions_registry.register(fn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Action result when the device did not answer in time.
TIMED_OUT = {"error": NO_RESPONSE, "success": False}
#: Action result when the extension stops mid-request.
STOPPING = {"error": "extension stopping", "success": False}

#: Appended to every write action's description.
WRITE_OUTCOME_HELP = (
    " Result outcome: ok; refused (not written, see error); or unknown (no response, "
    "the write may have landed: read back before retrying)."
)
#: Appended to the raw (address) write actions' descriptions.
RAW_WRITE_HELP = (
    " Refused unless advanced.allow_raw_writes is on, and for any address the device "
    "map marks read-only."
)


def _write_action(fn: Any) -> Any:
    """Stamp a write action's result with ``outcome``: ok, refused or unknown.

    OPC UA Good/Bad/Uncertain: unknown (set by _run_coro) means no definite
    answer, so the write may have landed.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = fn(*args, **kwargs)
        result.setdefault("outcome", "ok" if result["success"] else "refused")
        return result

    return wrapper


def _run_coro(coro: Any, dev: Any, write: bool = False) -> tuple[Any, dict | None]:
    """Run an async coroutine from a sync action handler: (result, None) or (None, error dict).

    Bridges the SDK's sync action thread to the connection's async event loop.
    ``write``: a write with no definite answer carries ``outcome: unknown``.
    """
    conn = dev.connection
    # Worst case per request: every attempt times out, after the pacing gap.
    # The link is serialized, so wait out the request ahead of ours too, plus
    # one reconnect (connect, plain TCP connect, retried connect, connect_delay_ms).
    per_request = conn.timeout * (1 + conn.retries) + conn.request_gap
    reconnect = 2 * conn.timeout + conn.connect_timeout + conn.connect_delay_ms / 1000
    try:
        if not (conn._loop and conn._loop.is_running()):
            return asyncio.run(coro), None
        future = asyncio.run_coroutine_threadsafe(coro, conn._loop)
        try:
            return future.result(timeout=2 * per_request + reconnect + 5), None
        except TimeoutError:
            future.cancel()
            if not write:
                return None, TIMED_OUT
            err = {"error": NO_RESPONSE + OUTCOME_UNKNOWN, "success": False}
            return None, {**err, "outcome": "unknown"}
    except RequestFailed as e:
        err = {"error": str(e), "success": False}
        return None, {**err, "outcome": "unknown"} if e.unknown else err
    except concurrent.futures.CancelledError:  # the poll loop was cancelled at shutdown
        return None, {**STOPPING, "outcome": "unknown"} if write else STOPPING


def _integral(name: str, value: Any) -> int:
    """``value`` as an int; ValueError unless a finite whole number (no truncation)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} {value!r} is not a number") from None
    if isinstance(value, bool) or not number.is_integer():
        raise ValueError(f"{name} {value!r} is not an integer")
    return int(number)


def _word(value: Any) -> int:
    """A raw register value: an integer 0-65535, or -32768..-1 as two's complement."""
    word = _integral("value", value)
    if not -0x8000 <= word <= 0xFFFF:
        raise ValueError(f"{word} is outside 0-65535 (or -32768..-1 as two's complement)")
    return word & 0xFFFF


def _resolve_register(dev: Any, path: str) -> tuple[str | None, Any, str | None]:
    """Resolve an 'event/field' path (or a unique bare name) to a Register on a device.

    Returns (error_message, register, event): ``error_message`` and ``register``
    are mutually exclusive. A bare name held by more than one event is refused.
    """
    events = dev.events
    if not events:
        return ("No register map loaded", None, None)

    if "/" in path:
        # Event names may contain "/" (e.g. "holding_registers/40"), so match
        # the map's own events rather than splitting at a fixed slash.
        for event_name, regs in events.items():
            if path.startswith(f"{event_name}/"):
                reg_name = path[len(event_name) + 1 :]
                for reg in regs:
                    if reg.name == reg_name:
                        return (None, reg, event_name)
        return (f"Register '{path}' not found", None, None)

    matches = [(event, reg) for event, regs in events.items() for reg in regs if reg.name == path]
    if not matches:
        return (f"Register '{path}' not found", None, None)
    if len(matches) > 1:
        paths = ", ".join(f"{event}/{reg.name}" for event, reg in matches)
        return (f"Register '{path}' is ambiguous; use one of: {paths}", None, None)
    event, reg = matches[0]
    return (None, reg, event)


#: Raw-action address fields: in the device map's base, converted at the wire.
ADDRESS_HELP = (
    "In the device map's address base: 1-based by default (holding 40001 is wire "
    "address 40000), 0-based if the map sets address_base 0. No map: 1-based."
)


def _address_field(title: str = "Address") -> Any:
    return zelos_sdk.action.number(
        "address", minimum=0, maximum=65536, title=title, description=ADDRESS_HELP
    )


def _wire(dev: Any, address: Any, count: Any = 1) -> tuple[int | None, dict | None]:
    """Wire address for a user ``address`` in the device's base, or an error dict.

    ``address`` and ``count`` must be whole numbers: truncating 10.9 would
    target a neighboring register.
    """
    try:
        address, count = _integral("address", address), _integral("count", count)
    except ValueError as e:
        return None, {"error": str(e), "success": False}
    base = dev.address_base
    wire = address - base
    if wire < 0 or count < 1 or wire + count > 0x10000:
        return None, {
            "error": f"Address {address} (count {count}) is outside {base}-{0xFFFF + base} "
            f"(address base {base})",
            "success": False,
        }
    return wire, None


def _raw_write_refusal(dev: Any, reg_type: str, wire: int, count: int = 1) -> dict | None:
    """Error dict unless raw writes are on and ``wire..wire+count`` hits no read-only register."""
    if not dev.allow_raw_writes:
        return {
            "error": "Raw writes are disabled (advanced.allow_raw_writes); write a register "
            "the device map marks writable by name instead",
            "success": False,
        }
    if dev.map_pending:
        return {"error": "Device map not loaded yet; raw writes wait for it", "success": False}
    for event, regs in dev.register_map.events.items() if dev.register_map else ():
        for reg in regs:
            overlaps = reg.address < wire + count and wire < reg.address + reg.address_span
            if reg.type == reg_type and overlaps and not reg.writable:
                return {
                    "error": f"Address {reg.map_address} is read-only in the device map "
                    f"({event}/{reg.name})",
                    "success": False,
                }
    return None


def _device_field() -> Any:
    return zelos_sdk.action.select("device", choices=all_devices, title="Device", required=False)


def _get_device_or_error(device: str | None) -> tuple[Any | None, dict | None]:
    """Look up a device by path (unset: the only one), returning an error dict on failure."""
    dev = get_device(device)
    if dev:
        return dev, None
    if device:
        return None, {"error": f"Device '{device}' not found", "success": False}
    return None, {"error": f"Set device to one of: {', '.join(all_devices())}", "success": False}


def _status_row(dev: Any) -> dict[str, Any]:
    """The identity/connection/counter keys get_status and get_snapshot share."""
    return {
        "device": dev.path,
        "connection": dev.connection.name,
        "connected": dev.connected,
        "transport": dev.connection.transport,
        "endpoint": dev.connection.endpoint,
        "unit_id": dev.unit_id,
        "address_base": dev.address_base,
        "poll_count": dev._poll_count,
        "successful_reads": dev.successful_reads,
        "failed_reads": dev.failed_reads,
        "error": dev.last_error,
        "map_pending": dev.map_pending,
        "auto_scan": dev.auto_scan_status(),
        **dev.rate_status(),
    }


def _register_count(dev: Any) -> int:
    return sum(len(regs) for regs in dev.events.values())


def _datatype(reg: Any) -> str:
    """``bool`` for a coil or discrete input, whatever the map's datatype says."""
    return "bool" if reg.type in BIT_REGISTER_TYPES else reg.datatype


def _register_row(dev: Any, event: str, reg: Any) -> dict[str, Any]:
    """One catalog row for a register, including its named-action ``path``.

    ``address`` is in the map's base. ``rate`` is the effective requested poll
    rate in seconds (register, else device, floored by min_rate); 0 = not polled.
    """
    return {
        "name": reg.name,
        "event": event,
        "path": f"{event}/{reg.name}",
        "address": reg.map_address,
        "type": reg.type,
        "datatype": _datatype(reg),
        "unit": reg.unit,
        "scale": reg.scale,
        "description": reg.description,
        "writable": reg.writable,
        "byte_order": reg.byte_order,
        "rate": dev.rate_of(reg),
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@zelos_sdk.action(
    "List Devices",
    "List every configured Modbus device: the names the other actions accept",
    hidden=True,
    read_only=True,
)
def list_devices() -> dict[str, Any]:
    """List all registered devices with their connection and register-map summary."""
    devices = []
    for path in all_devices():
        dev = get_device(path)
        devices.append(
            {
                "name": path,
                "connection": dev.connection.name,
                "device": dev.name,
                "unit_id": dev.unit_id,
                "address_base": dev.address_base,
                "transport": dev.connection.transport,
                "endpoint": dev.connection.endpoint,
                "connected": dev.connected,
                "successful_reads": dev.successful_reads,
                "failed_reads": dev.failed_reads,
                "trace_path": dev.trace_path,
                "error": dev.last_error,
                "map_pending": dev.map_pending,
                "map_name": dev.register_map.name if dev.register_map else None,
                "register_count": _register_count(dev),
                "auto_scan": dev.auto_scan_status(),
                "rate": dev.rate,
                "write_mode": dev.write_mode,
                "raw_writes": dev.allow_raw_writes,
                **dev.rate_status(),
            }
        )
    return {"devices": devices, "count": len(devices), "success": True}


@zelos_sdk.action("Get Status", "Get connection and polling status", hidden=True, read_only=True)
@_device_field()
def get_status(device: str | None = None) -> dict[str, Any]:
    """Get current device status."""
    dev, err = _get_device_or_error(device)
    if err:
        return err
    return {
        **_status_row(dev),
        "rate": dev.rate,
        "min_rate": dev.min_rate,
        "write_mode": dev.write_mode,
        "block_reads": dev.block_reads,
        "max_block_size": dev.max_block_size,
        "max_bit_block_size": dev.max_bit_block_size,
        "max_read_gap": dev.max_read_gap,
        "registers": _register_count(dev),
        "success": True,
    }


@zelos_sdk.action(
    "Get Snapshot",
    "Cached status and last register values for one device, no device I/O",
    hidden=True,
    read_only=True,
)
@_device_field()
def get_snapshot(device: str | None = None) -> dict[str, Any]:
    """Snapshot of one device's status and last-seen values, straight from cache.

    Reads nothing from the bus: values come from the poll sweep's cache (and any
    on-demand named reads), so registers with polling disabled are absent until
    read once via read_named_register.
    """
    dev, err = _get_device_or_error(device)
    if err:
        return err
    # Copy the cache first, stamp second: every ts_ms in the payload is then a
    # value that existed before captured_at_unix_ms, by construction.
    cached = dev.last_values
    captured_at_unix_ms = int(time.time() * 1000)
    # Extension id/version/state intentionally NOT included: that info is
    # canonical at the `extensions.list` bridge surface and the webapp consumes
    # it from there, not from this 1 Hz polled action.
    return {
        **_status_row(dev),
        "captured_at_unix_ms": captured_at_unix_ms,
        "values": {
            path: {"value": json_safe(value), "ts_ms": ts_ms}
            for path, (value, ts_ms) in cached.items()
        },
        "success": True,
    }


@zelos_sdk.action(
    "Read Register",
    "Read registers by address, in the device map's base (default 1-based). Each value is "
    "also traced as event holding_registers/<address>, field hr_<address> (input_registers/ "
    "ir_, coils/ coil_, discrete_inputs/ di_ for the other tables).",
    read_only=True,
)
@_device_field()
@_address_field()
@zelos_sdk.action.select(
    "reg_type",
    choices=list(RegisterType),
    default=RegisterType.HOLDING,
    title="Register Type",
)
@zelos_sdk.action.number(
    "count", minimum=1, maximum=MODBUS_MAX_READ_COUNT, default=1, title="Count"
)
def read_register(
    device: str | None = None, *, address: int, reg_type: str, count: int
) -> dict[str, Any]:
    """Read register(s) by address."""
    dev, err = _get_device_or_error(device)
    if err:
        return err
    wire, err = _wire(dev, address, count)
    if err:
        return err
    count = int(count)

    result, err = _run_coro(dev.read_raw(reg_type, wire, count), dev)
    if err:
        return err
    return {
        "address": address,
        "type": reg_type,
        "count": count,
        "values": result,
        "success": True,
    }


@zelos_sdk.action(
    "Write Single Register (FC 6)",
    "Write one holding register using function code 6; address in the device map's base."
    + RAW_WRITE_HELP
    + WRITE_OUTCOME_HELP,
)
@_device_field()
@_address_field()
@zelos_sdk.action.number("value", title="Value")
@_write_action
def write_single_register(device: str | None = None, *, address: int, value: int) -> dict[str, Any]:
    """Write a single register using FC 6."""
    dev, err = _get_device_or_error(device)
    if err:
        return err
    wire, err = _wire(dev, address)
    if err:
        return err
    try:
        word = _word(value)
    except ValueError as e:
        return {"error": str(e), "success": False}
    if err := _raw_write_refusal(dev, RegisterType.HOLDING, wire):
        return err

    _, err = _run_coro(dev.write_register(wire, word), dev, write=True)
    if err:
        return err
    return {
        "address": address,
        "value": word,
        "function_code": 6,
        "success": True,
    }


@zelos_sdk.action(
    "Write Registers (FC 16)",
    "Write one or more holding registers using function code 16; address in the device "
    "map's base." + RAW_WRITE_HELP + WRITE_OUTCOME_HELP,
)
@_device_field()
@_address_field("Start Address")
@zelos_sdk.action.text("values", title="Values (comma-separated)")
@_write_action
def write_registers(device: str | None = None, *, address: int, values: str) -> dict[str, Any]:
    """Write registers using FC 16."""
    dev, err = _get_device_or_error(device)
    if err:
        return err
    try:
        int_values = [_word(v.strip()) for v in values.split(",")]
    except ValueError as e:
        return {"error": f"Values must be comma-separated integers: {e}", "success": False}
    if len(int_values) > MODBUS_MAX_WRITE_COUNT:
        return {
            "error": f"{len(int_values)} values; FC 16 writes at most {MODBUS_MAX_WRITE_COUNT}",
            "success": False,
        }
    wire, err = _wire(dev, address, len(int_values))
    if err:
        return err
    if err := _raw_write_refusal(dev, RegisterType.HOLDING, wire, len(int_values)):
        return err

    _, err = _run_coro(dev.write_registers(wire, int_values), dev, write=True)
    if err:
        return err
    return {
        "address": address,
        "values": int_values,
        "count": len(int_values),
        "function_code": 16,
        "success": True,
    }


@zelos_sdk.action(
    "Read Named Register", "Read a register by event/name (e.g. voltage/L1)", read_only=True
)
@_device_field()
@zelos_sdk.action.select("name", choices=device_registers, depends_on="device", title="Register")
def read_named_register(device: str | None = None, *, name: str) -> dict[str, Any]:
    """Read a register by event/name path from the register map."""
    dev, err = _get_device_or_error(device)
    if err:
        return err

    error, reg, event = _resolve_register(dev, name)
    if error:
        return {"error": error, "success": False}

    value, err = _run_coro(dev.read_register_value(reg), dev)
    if err:
        return err
    if value is not None:
        # Refresh the snapshot cache so an unpolled register shows a value too.
        dev.record_value(reg, value, event=event)
    return {
        "name": name,
        "address": reg.map_address,
        "type": reg.type,
        "datatype": _datatype(reg),
        # ``success`` tracks the read: a NaN or an ``invalid`` sentinel is a
        # successful read of a value with no JSON number, so it reports null.
        "value": json_safe(value),
        "unit": reg.unit,
        "success": True,
    }


@zelos_sdk.action(
    "Write Named Register",
    "Write a value to a register by event/name; only registers the device map marks "
    "writable." + WRITE_OUTCOME_HELP,
)
@_device_field()
@zelos_sdk.action.select(
    "name", choices=device_writable_registers, depends_on="device", title="Register"
)
@zelos_sdk.action.number("value", title="Value")
@_write_action
def write_named_register(device: str | None = None, *, name: str, value: float) -> dict[str, Any]:
    """Write a value to a register by event/name path from the register map."""
    dev, err = _get_device_or_error(device)
    if err:
        return err

    error, reg, event = _resolve_register(dev, name)
    if error:
        return {"error": error, "success": False}

    if not reg.writable:
        return {
            "error": f"Register '{name}' is read-only (the device map does not mark it writable)",
            "success": False,
        }

    try:
        _, written = encode_register(reg, value)  # refuse before any I/O
    except ValueError as e:  # out of range, or not a whole step
        return {"error": str(e), "success": False}
    _, err = _run_coro(dev.write_register_value(reg, value), dev, write=True)
    if err:
        return err
    # A written setpoint is the freshest thing we know about it; without this
    # an unpolled register would read stale in snapshots forever.
    dev.record_value(reg, written, event=event)
    return {
        "name": name,
        "address": reg.map_address,
        "type": reg.type,
        "datatype": _datatype(reg),
        "value": written,
        "unit": reg.unit,
        "success": True,
    }


@zelos_sdk.action(
    "Write Coil",
    "Write a boolean value to a coil; address in the device map's base."
    + RAW_WRITE_HELP
    + WRITE_OUTCOME_HELP,
)
@_device_field()
@_address_field()
@zelos_sdk.action.select("value", choices=["ON", "OFF"], default="OFF", title="Value")
@_write_action
def write_coil(device: str | None = None, *, address: int, value: str) -> dict[str, Any]:
    """Write a coil by address."""
    dev, err = _get_device_or_error(device)
    if err:
        return err
    try:
        bool_value = coil_state(value)
    except ValueError as e:
        return {"error": str(e), "success": False}
    wire, err = _wire(dev, address)
    if err:
        return err
    if err := _raw_write_refusal(dev, RegisterType.COIL, wire):
        return err

    _, err = _run_coro(dev.write_coil(wire, bool_value), dev, write=True)
    if err:
        return err
    return {
        "address": address,
        "value": bool_value,
        "success": True,
    }


def _register_list(device: str | None, writable_only: bool) -> dict[str, Any]:
    dev, err = _get_device_or_error(device)
    if err:
        return err
    regs = [
        _register_row(dev, event, r)
        for event, event_regs in dev.events.items()
        for r in event_regs
        if r.writable or not writable_only
    ]
    return {
        "registers": regs,
        "count": len(regs),
        "map_name": dev.register_map.name if dev.register_map else None,
        "success": True,
    }


@zelos_sdk.action(
    "List Registers",
    "List all registers in the map, or found by auto-scan",
    hidden=True,
    read_only=True,
)
@_device_field()
def list_registers(device: str | None = None) -> dict[str, Any]:
    """List all registers in the register map."""
    return _register_list(device, writable_only=False)


@zelos_sdk.action(
    "List Writable Registers", "List all writable registers", hidden=True, read_only=True
)
@_device_field()
def list_writable_registers(device: str | None = None) -> dict[str, Any]:
    """List all writable registers in the register map."""
    return _register_list(device, writable_only=True)


@zelos_sdk.action(
    "Save Map",
    "Write the device's current register map to a JSON file on the agent's host: its loaded "
    "map, or for an auto-scanned device the registers found so far (raw uint16 words and "
    "bits, read-only, one event per register with auto-scan's trace paths), ready to use "
    "as a Register Map File. "
    "Path absolute or starting with ~, in an existing directory; an existing file is "
    "replaced only with overwrite.",
)
@_device_field()
@zelos_sdk.action.text("path", title="Path", description="e.g. ~/maps/meter.json")
@zelos_sdk.action.boolean(
    "overwrite", title="Overwrite", required=False, default=False, widget="toggle"
)
def save_map(device: str | None = None, *, path: str, overwrite: bool = False) -> dict[str, Any]:
    """Write the device's loaded map, or its auto-scanned registers as a map."""
    from zelos_extension_modbus.register_map import write_map_file

    dev, err = _get_device_or_error(device)
    if err:
        return err
    if dev.register_map:
        data = dev.register_map.source
    elif dev.auto_scan_status():
        data = dev.discovered_map()
    else:
        return {
            "error": "No register map loaded, and the device is not auto-scanned",
            "success": False,
        }
    try:
        written = write_map_file(path.strip(), data, overwrite)
    except (ValueError, OSError) as e:
        return {"error": str(e), "success": False}
    return {
        "path": str(written),
        "events": len(data["events"]),
        "registers": sum(len(regs) for regs in data["events"].values()),
        "auto_scan": dev.auto_scan_status(),
        "success": True,
    }


# ---------------------------------------------------------------------------
# Standalone: run with the extension stopped (config form hooks, scan, verify)
# ---------------------------------------------------------------------------
#
# These never touch the registry's live links. A scan must not share a link
# with polling (RS485 is half-duplex, TCP devices cap connections), so they
# refuse while devices are registered, i.e. while the extension runs.

#: Default scan wall clock for the actions, under the AI tool bridge's 5 min clamp.
SCAN_ACTION_SECONDS = 240
#: Scan Device's action timeout; its time limit stays a minute under it.
SCAN_TIMEOUT = 1800.0
VERIFY_TIMEOUT = 900.0
#: Auto-configure's shared deadline across connections, under the app's 30 s.
AUTO_CONFIG_SECONDS = 25.0


def _refuse_if_running() -> None:
    if all_devices():
        raise RuntimeError("Stop the extension first: a scan must not share a link with polling.")


def _configured_connections() -> list[dict[str, Any]]:
    """Connections from the saved config (at-rest state), or [] when there is none."""
    try:
        from zelos_sdk.extensions.config import load_config

        return [c for c in (load_config() or {}).get("connections") or [] if isinstance(c, dict)]
    except Exception:  # no config yet, or it does not validate
        return []


def _endpoint(
    target: str, transport: str, port: float, baudrate: float, parity: str, stopbits: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(link kwargs, configured connection or {}) for an action's target.

    An empty target means the first configured connection.
    """
    from zelos_extension_modbus.cli.app import link_kwargs
    from zelos_extension_modbus.scan import endpoint

    if target.strip():
        return endpoint(target.strip(), transport, port, baudrate, parity, stopbits), {}
    connections = _configured_connections()
    if not connections:
        raise ValueError("No target given and no connection configured.")
    return link_kwargs(connections[0]), connections[0]


def _link_fields(fn: Any) -> Any:
    """Target fields shared by Scan Device and Verify Map."""
    fields = [
        zelos_sdk.action.text(
            "target",
            title="Host or serial port",
            description="Empty: the first configured connection",
            required=False,
            default="",
        ),
        zelos_sdk.action.select(
            "transport",
            choices=list(Transport),
            default=Transport.TCP,
            title="Transport",
            required=False,
        ),
        zelos_sdk.action.number(
            "port", minimum=1, maximum=65535, default=502, title="TCP port", required=False
        ),
        zelos_sdk.action.number(
            "baudrate", minimum=300, default=9600, title="Baudrate", required=False
        ),
        zelos_sdk.action.select(
            "parity", choices=["N", "E", "O"], default="N", title="Parity", required=False
        ),
        zelos_sdk.action.number(
            "stopbits", minimum=1, maximum=2, default=1, title="Stop bits", required=False
        ),
    ]
    for field in reversed(fields):
        fn = field(fn)
    return fn


@zelos_sdk.action(
    "Scan Device",
    "Comprehensive, slow (tens of seconds to minutes): discover an unknown device with "
    "reads only (FC 01-04, 43/14, 17) and return its units, identity, valid address ranges "
    "and a draft register map inline: every readable address as a raw uint16 word or bool, "
    "read-only, with no datatype or byte order guessed; set those from the datasheet. "
    "For just finding devices use Auto-configure; to check an existing map use Verify Map. "
    "Run with the extension stopped; writes nothing to the device. With out_path, also "
    "writes the draft map there (several units: <name>_unit<id>.json).",
    timeout=SCAN_TIMEOUT,
    standalone=True,
    hidden=True,
)
@_link_fields
@zelos_sdk.action.boolean(
    "autodetect",
    title="Autodetect serial settings",
    description="RTU: try the given settings, then 9600-115200 baud, 8N1/8E1. Only the "
    "first 3 listed units and 1, 2, 247 are tried for this: if none of them answers, "
    "nothing is scanned",
    required=False,
    default=False,
    widget="toggle",
)
@zelos_sdk.action.text(
    "units",
    title="Unit IDs",
    description="e.g. 1,2,10-20: every listed unit is tried. Empty: TCP the first of 1, 0, "
    "255 to answer, else (a gateway, or no answer) a 1-247 sweep; RTU sweeps 1-247, 1-10 "
    "and 247 first (about 70 s when nothing answers)",
    required=False,
    default="",
)
@zelos_sdk.action.number(
    "max_devices",
    title="Devices to find",
    description="Empty Unit IDs: the sweep stops once this many devices answer and only "
    "those are scanned. Several devices on one bus: raise it or list their unit IDs. "
    "0: sweep every unit ID",
    minimum=0,
    maximum=247,
    default=1,
    required=False,
)
@zelos_sdk.action.number(
    "probe_timeout",
    title="Probe timeout (s)",
    description="RTU: how long a unit search or Autodetect try waits for a reply (TCP and "
    "register reads wait 0.5 s). Raise it for a slow device",
    minimum=0.05,
    maximum=5,
    default=PROBE_TIMEOUT,
    required=False,
)
@zelos_sdk.action.text(
    "ranges",
    title="Address windows",
    description="1-based, e.g. 1-10000,40001-41000. Empty: TCP 1-65536; RTU 1-10000 and "
    "the 30001/40001/50001 blocks",
    required=False,
    default="",
)
@zelos_sdk.action.number(
    "max_seconds",
    title="Time limit (s)",
    description="Stops and reports what was found so far",
    minimum=5,
    maximum=SCAN_TIMEOUT - 60,
    default=SCAN_ACTION_SECONDS,
    required=False,
)
@zelos_sdk.action.text(
    "out_path",
    title="Save draft map to",
    description="Optional JSON path on the agent's host: absolute or starting with ~, in an "
    "existing directory",
    required=False,
    default="",
)
@zelos_sdk.action.boolean(
    "overwrite",
    title="Overwrite",
    description="Replace an existing file at the save path",
    required=False,
    default=False,
    widget="toggle",
)
def scan_device(
    target: str = "",
    transport: str = Transport.TCP,
    port: float = 502,
    baudrate: float = 9600,
    parity: str = "N",
    stopbits: float = 1,
    autodetect: bool = False,
    units: str = "",
    max_devices: float = 1,
    probe_timeout: float = PROBE_TIMEOUT,
    ranges: str = "",
    max_seconds: float = SCAN_ACTION_SECONDS,
    out_path: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Scan one endpoint; the report and ``maps`` (unit id -> draft map) inline.

    ``out_path``: also write the drafts, checked before the scan starts;
    ``saved`` maps each unit to its path, or why it was not written.
    """
    from zelos_extension_modbus.register_map import map_output_path, write_map_file
    from zelos_extension_modbus.scan import parse_units, parse_windows, quiet_pymodbus, scan

    _refuse_if_running()
    endpoint, conn = _endpoint(target, transport, port, baudrate, parity, stopbits)
    devices = conn.get("devices") or [{}]
    out = map_output_path(out_path.strip(), overwrite) if out_path.strip() else None
    quiet_pymodbus()
    result = asyncio.run(
        scan(
            endpoint,
            units=parse_units(units) or None,
            windows=parse_windows(ranges) or None,
            max_seconds=min(float(max_seconds), SCAN_TIMEOUT - 60),
            autodetect=autodetect,
            configured_unit=devices[0].get("unit_id"),
            max_devices=int(max_devices),
            probe_timeout=float(probe_timeout),
        )
    )
    if out:
        maps, result["saved"] = result["maps"], {}
        for unit, draft in maps.items():
            dest = out if len(maps) == 1 else out.with_stem(f"{out.stem}_unit{unit}")
            try:
                result["saved"][str(unit)] = str(write_map_file(str(dest), draft, overwrite))
            except (ValueError, OSError) as e:
                result["saved"][str(unit)] = f"not saved: {e}"
    return result


@zelos_sdk.action(
    "Verify Map",
    "Check an existing register map against the device: read every register twice and "
    "report exceptions, no responses, NaN/Inf in a declared float and non-ASCII bytes in "
    "a declared string, plus every decoded value. Reads only; run with the extension stopped.",
    timeout=VERIFY_TIMEOUT,
    standalone=True,
    hidden=True,
    read_only=True,
)
@_link_fields
@zelos_sdk.action.number(
    "unit_id", minimum=0, maximum=255, default=1, title="Unit ID", required=False
)
@zelos_sdk.action.text(
    "map_file",
    title="Register map",
    description="Empty: the map configured for this unit on the first connection",
    required=False,
    default="",
    widget="file_path_picker",
)
@zelos_sdk.action.number(
    "max_seconds",
    title="Time limit (s)",
    description="Stops and reports what was checked so far",
    minimum=5,
    maximum=VERIFY_TIMEOUT - 60,
    default=SCAN_ACTION_SECONDS,
    required=False,
)
def verify_map(
    target: str = "",
    transport: str = Transport.TCP,
    port: float = 502,
    baudrate: float = 9600,
    parity: str = "N",
    stopbits: float = 1,
    unit_id: float = 1,
    map_file: str = "",
    max_seconds: float = SCAN_ACTION_SECONDS,
) -> dict[str, Any]:
    """Verify a register map against a device; the report inline."""
    from zelos_extension_modbus.register_map import load_configured_map
    from zelos_extension_modbus.scan import quiet_pymodbus
    from zelos_extension_modbus.scan import verify_map as run_verify

    _refuse_if_running()
    endpoint, conn = _endpoint(target, transport, port, baudrate, parity, stopbits)
    unit = int(unit_id)
    if not map_file.strip():
        configured = [d for d in conn.get("devices") or [] if d.get("unit_id", 1) == unit]
        map_file = (configured or [{}])[0].get("register_map_file") or ""
    if not map_file:
        raise ValueError(f"No register map given and none configured for unit {unit}.")
    quiet_pymodbus()
    register_map = load_configured_map(map_file)
    max_seconds = min(float(max_seconds), VERIFY_TIMEOUT - 60)
    return asyncio.run(run_verify(endpoint, register_map, unit, max_seconds=max_seconds))


#: Probed when there is no connection: the Modbus TCP well-known port on this host.
DEFAULT_CONNECTION: dict[str, Any] = {"transport": Transport.TCP, "host": "127.0.0.1", "port": 502}


@zelos_sdk.action(
    "Auto-configure",
    "Quick device discovery for the config form (seconds): on each connection, find the "
    "units that answer among the configured unit, 1-10 and 247 (RTU: also serial "
    "settings), read their identity, and add a device per new unit. Where the SunSpec "
    "marker is found, register_map is set to sunspec (new or existing units; an existing "
    "unit with a register_map_file keeps it). Existing devices are kept. Returns the "
    "config; review, then save and start. Sweeps the form's connections (older apps: the "
    "saved config's); with none, probes 127.0.0.1:502. All within 25 s; what did not fit "
    "is reported. For a register map use Scan Device.",
    timeout=900.0,
    standalone=True,
    hidden=True,
    read_only=True,
)
@zelos_sdk.action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data. Empty: the saved config",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The app's auto-configure contract: ``config.connections`` replaces the form's."""
    from zelos_extension_modbus.cli.app import link_kwargs
    from zelos_extension_modbus.scan import ScanLink, quiet_pymodbus, sweep

    _refuse_if_running()
    if config is None:
        connections = _configured_connections()
    else:
        connections = [c for c in config.get("connections") or [] if isinstance(c, dict)]
    probe = not connections
    if probe:
        connections = [DEFAULT_CONNECTION]
    quiet_pymodbus()
    deadline = time.monotonic() + AUTO_CONFIG_SECONDS
    out, found, swept, seen, cut, unreachable, unopened = [], 0, [], [], [], [], []
    no_port = False  # an RTU connection with no serial port to open
    for conn in connections:
        rtu = conn.get("transport") == Transport.RTU
        if rtu and not conn.get("serial_port"):
            no_port = True
            out.append(conn)
            continue
        devices = [dict(d) for d in conn.get("devices") or [] if isinstance(d, dict)]
        known = {d.get("unit_id", 1) for d in devices}
        left = deadline - time.monotonic()
        if left <= 0:
            cut.append(ScanLink(link_kwargs(conn)).label)
            out.append(conn)
            continue
        result = asyncio.run(
            sweep(
                link_kwargs(conn),
                autodetect=rtu,
                configured_unit=devices[0].get("unit_id", 1) if devices else None,
                max_seconds=left,
            )
        )
        if result.get("error"):  # the link never opened
            if rtu:
                unopened.append(conn["serial_port"])
            else:
                unreachable.append(result["error"].removeprefix("cannot open "))
        else:
            swept.append(result["endpoint"])
        if result["cutoffs"]:
            cut.append(result["endpoint"])
        conn = dict(conn)
        if serial := result.get("serial"):
            conn |= {k: serial[k] for k in ("baudrate", "parity", "stopbits")}
        units, added = [], []
        for unit in result.get("units", {}).get("present", []):
            ids = result["identity"].get(unit, {}).get("device_id", {})
            who = " ".join(ids.get(k, "") for k in ("VendorName", "ProductCode")).strip()
            notes = [who] if who else []
            if unit not in known:
                devices.append({"unit_id": unit})
                added.append(unit)
            device = next(d for d in devices if d.get("unit_id", 1) == unit)
            if unit in result["sunspec"] and device.get("register_map") != "sunspec":
                if device.get("register_map_file"):
                    notes.append("SunSpec detected, register_map_file kept")
                else:
                    device["register_map"] = "sunspec"
                    notes.append("register_map set to sunspec")
            units.append((unit, notes))
        if units:
            found += len(units)
            seen.append(_found(result["endpoint"], units, added))
        conn["devices"] = devices
        out.append(conn)
    partial = f" Not fully swept in {AUTO_CONFIG_SECONDS:g} s: {', '.join(cut)}." if cut else ""
    failed = f" Couldn't connect to {'; '.join(unreachable)}." if unreachable else ""
    failed += f" Couldn't open {', '.join(unopened)}." if unopened else ""
    failed += " Choose a Serial Port to scan." if no_port else ""
    if not found and probe:
        host, port = DEFAULT_CONNECTION["host"], DEFAULT_CONNECTION["port"]
        return {
            "status": "error",
            "message": f"Nothing answered on {host}:{port}, the default. Add a connection "
            "(host and port, or a serial port).",
        }
    if not found:
        silent = f"No unit answered on {', '.join(swept)}." if swept else ""
        return {"status": "error", "message": f"{silent}{failed}{partial}".strip()}
    return {
        "status": "success",
        "message": f"{'; '.join(seen)}.{failed}{partial}",
        "config": {"connections": out},
    }


def _found(endpoint: str, units: list[tuple[int, list[str]]], added: list[int]) -> str:
    """``a:502: found units 1 (Acme X), 2; added 2``."""
    listed = ", ".join(f"{u} ({', '.join(n)})" if n else str(u) for u, n in units)
    tail = f"; added {', '.join(map(str, added))}" if added else ""
    return f"{endpoint}: found unit{'s' * (len(units) > 1)} {listed}{tail}"


def _console_nodes() -> set[str]:
    """Device nodes the Linux kernel has a console on; empty elsewhere."""
    try:
        names = Path("/sys/class/tty/console/active").read_text().split()
    except OSError:
        return set()
    return {os.path.realpath(f"/dev/{name}") for name in names}


@zelos_sdk.action(
    "Find Device",
    "Find RTU devices when the serial port, the serial settings or the unit ID is unknown: "
    "tries each combination with one read (FC 03, holding register 1) and reports what "
    "answers, with a connection to paste into the config. Fill in what you know; an empty "
    "field is searched. The common comes first: units 1, 2 and 247 on every setting, then "
    "3-10, then the rest, and 9600 8N1/8E1, 19200 8N1/8E1, 38400 8N1 and 115200 8N1 before "
    "other settings. A silent try costs about 0.3 s: every unit at one setting takes about "
    "70 s, every format and unit at one baudrate about 5 min, and everything on one port "
    "about 25 min (raise the time limit, or search one baudrate at a time). Stops at the "
    "first reply unless Find all. Run "
    "with the extension stopped. It transmits on every port searched, so nothing else may "
    "be using them.",
    timeout=SCAN_TIMEOUT,
    standalone=True,
    hidden=True,
)
@zelos_sdk.action.text(
    "ports",
    title="Serial ports",
    description="e.g. /dev/ttyUSB0 or COM3,COM4. Empty: every port List Serial Ports shows, "
    "but one the system console is on",
    required=False,
    default="",
)
@zelos_sdk.action.text(
    "baudrates",
    title="Baudrates",
    description="e.g. 115200 or 9600,19200. Empty: 9600, 19200, 38400, 57600, 115200",
    required=False,
    default="",
)
@zelos_sdk.action.text(
    "formats",
    title="Data formats",
    description="e.g. 8N1 or 8N1,8E1. Empty: 8N1, 8E1, 8O1, 8N2",
    required=False,
    default="",
)
@zelos_sdk.action.text(
    "units",
    title="Unit IDs",
    description="e.g. 1 or 1,2,10-20. Empty: 1-247",
    required=False,
    default="",
)
@zelos_sdk.action.boolean(
    "find_all",
    title="Find all",
    description="Keep searching after the first reply: every unit on a bus, every port",
    required=False,
    default=False,
    widget="toggle",
)
@zelos_sdk.action.number(
    "max_seconds",
    title="Time limit (s)",
    description="Stops and reports what was found and where it stopped",
    minimum=5,
    maximum=SCAN_TIMEOUT - 60,
    default=SCAN_ACTION_SECONDS,
    required=False,
)
@zelos_sdk.action.number(
    "probe_timeout",
    title="Probe timeout (s)",
    description="How long each try waits for a reply. Raise it for a slow device or gateway",
    minimum=0.05,
    maximum=5,
    default=PROBE_TIMEOUT,
    required=False,
)
def find_device(
    ports: str = "",
    baudrates: str = "",
    formats: str = "",
    units: str = "",
    find_all: bool = False,
    max_seconds: float = SCAN_ACTION_SECONDS,
    probe_timeout: float = PROBE_TIMEOUT,
) -> dict[str, Any]:
    """Search the dimensions left empty; ``connections`` is ready for the config."""
    from zelos_extension_modbus.scan import (
        find_devices,
        parse_formats,
        parse_units,
        quiet_pymodbus,
    )

    _refuse_if_running()
    named = [p.strip() for p in ports.split(",") if p.strip()]
    if ports.strip() and not named:
        raise ValueError(f"no port in {ports!r}: use e.g. /dev/ttyUSB0 or COM3,COM4")
    listed = named or [c["value"] for c in list_serial_ports()["choices"]]
    by_node: dict[str, str] = {}  # an alias and its node are one port
    for p in listed:
        by_node.setdefault(os.path.realpath(p), p)
    # Unasked, leave the system console alone: a login prompt is on that port.
    consoles = set() if named else _console_nodes()
    skipped = [p for node, p in by_node.items() if node in consoles]
    port_list = [p for p in by_node.values() if p not in skipped]
    if not port_list:
        console = f" ({', '.join(skipped)}: system console, name it to search it)" * bool(skipped)
        raise ValueError(
            f"No serial ports to search{console}. Plug in the adapter, or name a port."
        )
    bauds = [b.strip() for b in baudrates.split(",") if b.strip()]
    if bad := [b for b in bauds if not b.isdigit() or not int(b)]:
        raise ValueError(f"bad baudrate {bad[0]!r}: use e.g. 9600 or 9600,19200")
    quiet_pymodbus()
    result = asyncio.run(
        find_devices(
            port_list,
            [int(b) for b in bauds] or None,
            parse_formats(formats) or None,
            parse_units(units) or None,
            find_all=find_all,
            max_seconds=min(float(max_seconds), SCAN_TIMEOUT - 60),
            probe_timeout=float(probe_timeout),
        )
    )
    buses: dict[tuple[Any, ...], dict[str, Any]] = {}
    for f in result["found"]:
        key = (f["serial_port"], f["baudrate"], f["parity"], f["stopbits"])
        link = dict(zip(("serial_port", "baudrate", "parity", "stopbits"), key, strict=True))
        bus = buses.setdefault(key, {"transport": Transport.RTU} | link | {"devices": []})
        bus["devices"].append({"unit_id": f["unit_id"]})
    result["connections"] = list(buses.values())
    result["skipped"] = skipped
    result["searched"] = {
        "ports": port_list,
        "baudrates": baudrates.strip() or "9600,19200,38400,57600,115200",
        "formats": formats.strip().upper() or "8N1,8E1,8O1,8N2",
        "units": units.strip() or "1-247",
    }
    said = [
        f"Unit {f['unit_id']} on {f['serial_port']} at {f['baudrate']} "
        f"8{f['parity']}{f['stopbits']}: {f['reply']}."
        for f in result["found"]
    ]
    if any(f["reply"].startswith("exception") for f in result["found"]):
        said.append("An exception reply still proves the port, settings and unit ID.")
    if not said:
        s = result["searched"]
        said = [
            f"No device answered on {', '.join(port_list)} "
            f"(baudrates {s['baudrates']}; formats {s['formats']}; units {s['units']}; "
            f"probe timeout {float(probe_timeout):g} s)."
        ]
    if skipped:
        said.append(f"Skipped {', '.join(skipped)}: system console, name it to search it.")
    if result["unopened"]:
        said.append(f"Couldn't open {', '.join(result['unopened'])}.")
    if result["lost"]:
        said.append(
            f"The link dropped at {', '.join(result['lost'])}; "
            "the rest of that port was not searched."
        )
    if result["malformed"]:
        said.append(f"{result['malformed']} malformed replies: the adapter may echo what it sends.")
    if result["stopped_at"]:
        said.append(f"Time limit reached at {result['stopped_at']}; the rest was not searched.")
    status = "success" if result["found"] else "error"
    return {"status": status, "message": " ".join(said)} | result


@zelos_sdk.action(
    "List Serial Ports",
    "Serial ports on the machine running the agent, as choices for an RTU connection's "
    "Serial Port field, which also accepts a path typed by hand.",
    standalone=True,
    hidden=True,
    read_only=True,
)
def list_serial_ports() -> dict[str, Any]:
    """The app's `action-choices` contract: `choices` in the order to show."""
    from serial.tools import list_ports

    # include_links adds udev aliases (/dev/ttyRS485 -> ttyAMA4); listed first since
    # the alias names the port's purpose. Linux only; other platforms ignore it.
    ports = sorted(
        list_ports.comports(include_links=True),
        key=lambda p: (not Path(p.device).is_symlink(), p.device),
    )
    cu = {p.device for p in ports if p.device.startswith("/dev/cu.")}
    return {
        "status": "success",
        "choices": [
            {"value": p.device, "detail": _port_detail(p)}
            for p in ports
            # macOS pseudo ports, and tty.* twins of cu.* (cu doesn't wait on carrier).
            if "Bluetooth" not in p.device
            and "debug-console" not in p.device
            and "/dev/cu." + p.device.removeprefix("/dev/tty.") not in cu
        ],
    }


def _port_detail(p: Any) -> str:
    detail = p.description if p.description != "n/a" else ""
    if Path(p.device).is_symlink():
        target = f"-> {Path(p.device).resolve().name}"
        detail = f"{target} {detail}".strip()
    return detail


ALL_ACTIONS = [
    list_devices,
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
    save_map,
    scan_device,
    verify_map,
    auto_config,
    find_device,
    list_serial_ports,
]
