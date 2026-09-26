"""App mode runner for Zelos Modbus extension.

This module handles running the extension when launched from the Zelos App
with configuration loaded from config.json, including demo mode support.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_modbus import ACTION_PREFIX
from zelos_extension_modbus.client import ModbusConnection, ModbusDevice
from zelos_extension_modbus.constants import (
    DEFAULT_PREFIX,
    LOG_SOURCE_NAME,
    RESERVED_CONNECTION_NAMES,
    Transport,
    name_error,
)
from zelos_extension_modbus.register_map import RegisterMap
from zelos_extension_modbus.sunspec import build_register_map

logger = logging.getLogger(__name__)

# Demo server settings
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 5020

#: App-level `advanced` defaults. Tuning keys default in the constructors.
ADVANCED_DEFAULTS: dict[str, Any] = {"prefix": DEFAULT_PREFIX, "log_level": "INFO"}

#: `advanced` tuning keys, by the class that takes them. A map `device` block
#: overrides the device keys it sets.
CONNECTION_KEYS = ("timeout", "retries", "request_delay_ms", "connect_delay_ms")
DEVICE_KEYS = (
    "block_reads",
    "max_block_size",
    "max_bit_block_size",
    "max_read_gap",
    "write_mode",
    "demote_after",
    "demote_max_s",
)
#: Map `device` block keys that only the map sets.
MAP_DEVICE_KEYS = ("min_rate", "close_after_sweep")

TCP_KEYS = ("host", "port")
RTU_KEYS = ("serial_port", "baudrate", "parity", "stopbits", "bytesize")

# Keys a 0.1.x interface carried; any of them (or no devices) marks an old config.
_OLD_CONNECTION_KEYS = {
    "unit_id",
    "register_map_file",
    "poll_interval",
    *CONNECTION_KEYS,
    *DEVICE_KEYS,
}

OLD_CONFIG_ERROR = (
    "Config is from Modbus 0.1.x: interfaces are now connections, each with a devices list. "
    "Re-open the config form and set up the connections again."
)

#: Device `register_map` value that builds the map by SunSpec discovery.
SUNSPEC_MAP = "sunspec"

#: Seconds a stop waits for the poll loops to disconnect before giving up.
SHUTDOWN_TIMEOUT = 3.0


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """Merge the `advanced` object over its defaults.

    An absent `prefix` takes the default; a present-but-empty one clears it.
    """
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def _exit_on(error: str | None) -> None:
    """Exit with a one-line reason when ``error`` is set."""
    if error:
        logger.error(error)
        sys.exit(1)


def link_kwargs(conn: dict[str, Any]) -> dict[str, Any]:
    """ModbusConnection link kwargs for a configured connection."""
    transport = conn.get("transport", Transport.TCP)
    keys = RTU_KEYS if transport == Transport.RTU else TCP_KEYS
    return {"transport": transport, **{k: conn[k] for k in keys if k in conn}}


def _old_config_error(config: dict[str, Any]) -> str | None:
    """OLD_CONFIG_ERROR if ``config`` has the 0.1.x shape, else None."""
    old = (
        "log_level" in config
        or "interfaces" in config
        or any(
            "devices" not in conn or _OLD_CONNECTION_KEYS & conn.keys()
            for conn in config.get("connections") or []
        )
    )
    return OLD_CONFIG_ERROR if old else None


def get_demo_register_map_path() -> Path:
    """Get path to the bundled demo register map."""
    ref = resources.files("zelos_extension_modbus.demo").joinpath("power_meter.json")
    return Path(str(ref))


def start_demo_server() -> threading.Thread:
    """Start the demo Modbus server in a background thread.

    Returns:
        The server thread
    """
    from zelos_extension_modbus.demo.simulator import (
        PowerMeterSimulator,
        SimulatorUpdater,
        create_demo_context,
    )

    def run_server() -> None:
        """Run the server in its own event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        context = create_demo_context()
        simulator = PowerMeterSimulator()
        updater = SimulatorUpdater(simulator, context, interval=0.1)
        updater.start()

        from pymodbus.server import StartAsyncTcpServer

        try:
            loop.run_until_complete(
                StartAsyncTcpServer(
                    context=context,
                    address=(DEMO_HOST, DEMO_PORT),
                )
            )
        except Exception as e:
            logger.error(f"Demo server error: {e}")
        finally:
            updater.stop()
            loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    logger.info(f"Demo server started on {DEMO_HOST}:{DEMO_PORT}")

    # Give server time to start
    import time

    time.sleep(0.5)

    return thread


def _load_register_map(path_str: str | None) -> RegisterMap | None:
    """Load a register map from a file path string.

    An unset/empty path is a deliberate raw-address-mode choice and returns
    None. A configured-but-broken map (missing file, or a load/validation
    failure) is a config error and exits: silently degrading to no-data would
    hide a misconfiguration behind an empty signal tree.
    """
    if not path_str:
        return None
    try:
        # from_file raises FileNotFoundError for a missing path, caught below.
        reg_map = RegisterMap.from_file(path_str)
        logger.info(f"Loaded register map with {len(reg_map.registers)} registers")
        return reg_map
    except Exception as e:
        logger.error(f"Failed to load register map: {e}")
        sys.exit(1)


def _map_source(dev_config: dict[str, Any]) -> dict[str, Any]:
    """`register_map` or `map_loader` kwargs for a device's configured map source."""
    source = dev_config.get("register_map", "file")
    if source == SUNSPEC_MAP:
        if dev_config.get("register_map_file"):
            _exit_on("Set register_map_file or register_map 'sunspec' on a device, not both.")
        return {"map_loader": build_register_map}
    if source != "file":
        _exit_on(f"Invalid register_map {source!r}: must be 'file' or '{SUNSPEC_MAP}'.")
    return {"register_map": _load_register_map(dev_config.get("register_map_file"))}


def _create_connections(config: dict[str, Any], advanced: dict[str, Any]) -> list[ModbusConnection]:
    """Build each configured connection with its devices.

    Tuning precedence per key: map `device` block > `advanced` > constructor
    default. Rate: the device's `rate` > `advanced.default_rate` > 1 s. Names
    must be legal trace names and unique (connections overall; device names
    and unit ids per connection); anything else exits. Default names that
    collide (TCP connections to one host) take a `_<port>` suffix.
    """
    configs = config.get("connections", [])
    if not configs:
        logger.error("No connections configured. Add at least one connection.")
        sys.exit(1)

    connections: list[ModbusConnection] = []
    named: list[bool] = []
    for conn_config in configs:
        name = (conn_config.get("name") or "").strip()
        _exit_on(name_error(name, "connection Name", RESERVED_CONNECTION_NAMES))
        conn = ModbusConnection(
            name=name or None,
            **link_kwargs(conn_config),
            **{k: advanced[k] for k in CONNECTION_KEYS if k in advanced},
        )
        connections.append(conn)
        named.append(bool(name))
    defaults = [c.name for c, n in zip(connections, named, strict=True) if not n]
    for conn, n in zip(connections, named, strict=True):
        if not n and conn.transport == Transport.TCP and defaults.count(conn.name) > 1:
            conn.name = f"{conn.name}_{conn.port}"

    for i, (conn, conn_config) in enumerate(zip(connections, configs, strict=True)):
        if any(c.name == conn.name for c in connections[:i]):
            _exit_on(f"Duplicate connection name '{conn.name}'; set Name on one of them.")
        if not conn_config["devices"]:
            _exit_on(f"Connection '{conn.name}' has no devices; add one (unit ID) or remove it.")
        for dev_config in conn_config["devices"]:
            dev_name = (dev_config.get("name") or "").strip()
            _exit_on(name_error(dev_name, "device Name"))
            map_source = _map_source(dev_config)
            register_map = map_source.get("register_map")
            map_device = register_map.device if register_map else {}
            settings = {k: advanced[k] for k in DEVICE_KEYS if k in advanced}
            settings.update(
                {k: map_device[k] for k in DEVICE_KEYS + MAP_DEVICE_KEYS if k in map_device}
            )
            dev = ModbusDevice(
                conn,
                **map_source,
                name=dev_name or None,
                unit_id=dev_config.get("unit_id", 1),
                rate=dev_config.get("rate", advanced.get("default_rate", 1.0)),
                **settings,
            )
            for other in conn.devices[:-1]:
                if dev.unit_id == other.unit_id:
                    _exit_on(f"Duplicate unit ID {dev.unit_id} on connection '{conn.name}'.")
                if dev.name == other.name:
                    _exit_on(f"Duplicate device name '{dev.name}' on connection '{conn.name}'.")
            logger.info(f"Created device {dev.path} ({conn.transport}:{conn.endpoint})")

    return connections


def init_sdk(prefix: str) -> zelos_sdk.TraceSource | None:
    """Init the SDK and route logs to the trace; return the shared prefix source.

    Same layout rule as the data: with a prefix, one source named after it
    carries every connection plus the logs (`<prefix>/log`); cleared, each
    connection owns a source and the logs get LOG_SOURCE_NAME. Register actions
    BEFORE calling this: init advertises them to the agent.
    """
    # init_global_source first, so init() below reuses it instead of creating
    # a source named after the action namespace.
    source = zelos_sdk.init_global_source(prefix or LOG_SOURCE_NAME)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)
    logging.getLogger().addHandler(TraceLoggingHandler(source))
    return source if prefix else None


async def run_connections(connections: list[ModbusConnection]) -> None:
    """Run every connection's poll loop until SIGTERM/SIGINT.

    A stop cancels the loops mid-request; each disconnects on the way out.
    Past SHUTDOWN_TIMEOUT the process exits anyway.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(conn.run_async()) for conn in connections]
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
    logger.info("Shutting down...")
    for conn in connections:
        conn.stop()
    for task in [*tasks, stopping]:
        task.cancel()
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT)
    if pending:
        logger.error(f"{len(pending)} connection(s) did not stop in {SHUTDOWN_TIMEOUT:g}s; exiting")
        logging.shutdown()
        os._exit(1)


def run_app_mode(demo: bool = False) -> None:
    """Run the extension in app mode with configuration from Zelos App.

    Args:
        demo: If True, use built-in demo mode with simulated power meter
    """
    from zelos_extension_modbus.scan import quiet_pymodbus

    quiet_pymodbus()  # its ERROR frame dumps would land in the trace log; ours cover it
    config = load_config()
    if demo:
        logger.info("Demo mode: using built-in power meter simulator")
        config["connections"] = [
            {
                "transport": Transport.TCP,
                "host": DEMO_HOST,
                "port": DEMO_PORT,
                "name": "demo",
                "devices": [{"register_map_file": str(get_demo_register_map_path())}],
            }
        ]
    _exit_on(_old_config_error(config))

    advanced = resolve_advanced(config)
    log_level = advanced["log_level"]
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
    prefix = str(advanced["prefix"] or "").strip()
    _exit_on(name_error(prefix, "Prefix"))

    connections = _create_connections(config, advanced)
    if demo:
        start_demo_server()

    from zelos_extension_modbus import actions, registry

    for conn in connections:
        for dev in conn.devices:
            registry.register(dev)
    actions.register_all()
    source = init_sdk(prefix)
    logger.info(f"Trace prefix: {prefix}" if prefix else "Trace prefix cleared")

    for conn in connections:
        conn.start(prefix, source)
    asyncio.run(run_connections(connections))
