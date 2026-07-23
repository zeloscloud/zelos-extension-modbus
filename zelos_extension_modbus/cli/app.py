"""App mode runner for Zelos Modbus extension.

This module handles running the extension when launched from the Zelos App
with configuration loaded from config.json, including demo mode support.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_modbus.client import ModbusClient
from zelos_extension_modbus.constants import Transport, WriteMode, sanitize_source_name
from zelos_extension_modbus.register_map import RegisterMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Demo server settings
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 5020


def get_demo_register_map_path() -> Path:
    """Get path to the bundled demo register map."""
    ref = resources.files("zelos_extension_modbus.demo").joinpath("power_meter.json")
    # Use as_posix_path for on-disk packages; as_file for zip-bundled
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
    """Load a register map from a file path string."""
    if not path_str:
        return None
    map_path = Path(path_str)
    if not map_path.exists():
        logger.warning(f"Register map file not found: {path_str}")
        return None
    try:
        reg_map = RegisterMap.from_file(map_path)
        logger.info(f"Loaded register map with {len(reg_map.registers)} registers")
        return reg_map
    except Exception as e:
        logger.error(f"Failed to load register map: {e}")
        return None


def _create_clients(config: dict[str, Any]) -> list[tuple[ModbusClient, str]]:
    """Create ModbusClient instances for all configured interfaces.

    Returns list of (client, registry_name) tuples.
    """
    interfaces = config.get("interfaces", [])
    if not interfaces:
        logger.error("No interfaces configured. Add at least one interface.")
        sys.exit(1)

    is_multi = len(interfaces) > 1
    seen_names: set[str] = set()
    clients: list[tuple[ModbusClient, str]] = []

    for i, iface_config in enumerate(interfaces):
        transport = iface_config.get("transport", Transport.TCP)

        # Determine interface name. Names become trace-source / actions path
        # segments, so they are sanitized — host addresses ("192.168.1.100")
        # and serial paths ("/dev/ttyUSB0") contain reserved "." and "/".
        config_name = (iface_config.get("name") or "").strip() or None
        if config_name:
            iface_name = sanitize_source_name(config_name, f"{transport}{i}")
        elif is_multi:
            # Default: host for TCP, serial_port for RTU
            if transport == Transport.TCP:
                raw_name = iface_config.get("host", f"tcp{i}")
            else:
                raw_name = iface_config.get("serial_port", f"rtu{i}")
            iface_name = sanitize_source_name(raw_name, f"{transport}{i}")
        else:
            # Single interface, no explicit name
            iface_name = None

        # Validate uniqueness for multi-interface
        if is_multi:
            if iface_name in seen_names:
                logger.error(
                    f"Duplicate interface name '{iface_name}'. Each interface must"
                    "have a unique name."
                )
                sys.exit(1)
            seen_names.add(iface_name)

        # Load register map
        register_map = _load_register_map(iface_config.get("register_map_file"))

        # Build client kwargs
        client_kwargs: dict[str, Any] = {
            "transport": transport,
            "unit_id": iface_config.get("unit_id", 1),
            "timeout": iface_config.get("timeout", 3.0),
            "register_map": register_map,
            "poll_interval": iface_config.get("poll_interval", 1.0),
            "write_mode": iface_config.get("write_mode", WriteMode.AUTO),
            "block_reads": iface_config.get("block_reads", True),
            "max_block_size": iface_config.get("max_block_size", 125),
            "max_read_gap": iface_config.get("max_read_gap", 0),
            "interface_name": iface_name,
        }

        if transport == Transport.RTU:
            client_kwargs["serial_port"] = iface_config.get("serial_port", "/dev/ttyUSB0")
            client_kwargs["baudrate"] = iface_config.get("baudrate", 9600)
            client_kwargs["parity"] = iface_config.get("parity", "N")
            client_kwargs["stopbits"] = iface_config.get("stopbits", 1)
            client_kwargs["bytesize"] = iface_config.get("bytesize", 8)
        else:
            client_kwargs["host"] = iface_config.get("host", "127.0.0.1")
            client_kwargs["port"] = iface_config.get("port", 502)

        client = ModbusClient(**client_kwargs)

        # Registry name: use interface name, fall back to register map name or "modbus"
        if iface_name:
            registry_name = iface_name
        elif register_map:
            registry_name = sanitize_source_name(register_map.name, "modbus")
        else:
            registry_name = "modbus"

        clients.append((client, registry_name))
        display = iface_name or registry_name
        logger.info(f"Created interface: {display} ({transport}:{client._connection_str})")

    return clients


async def _run_clients_async(clients: list[ModbusClient]) -> None:
    """Run multiple clients concurrently."""
    for client in clients:
        client.start()

    tasks = [asyncio.create_task(client._run_async()) for client in clients]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Client tasks cancelled")
    finally:
        for client in clients:
            client.stop()


def run_app_mode(demo: bool = False) -> None:
    """Run the extension in app mode with configuration from Zelos App.

    Args:
        demo: If True, use built-in demo mode with simulated power meter
    """
    # Load configuration
    config = load_config()

    # Apply global log level
    log_level = config.get("log_level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

    # Demo mode: inject a demo interface
    if demo:
        logger.info("Demo mode: using built-in power meter simulator")
        start_demo_server()
        demo_map_path = get_demo_register_map_path()
        config["interfaces"] = [
            {
                "transport": Transport.TCP,
                "host": DEMO_HOST,
                "port": DEMO_PORT,
                "name": "demo",
                "register_map_file": str(demo_map_path),
            }
        ]

    # Create all clients
    client_pairs = _create_clients(config)
    clients = [c for c, _ in client_pairs]

    # Register in global registry
    from zelos_extension_modbus import registry

    for client, reg_name in client_pairs:
        registry.register(reg_name, client)

    # Register actions BEFORE init — init advertises them to the agent
    from zelos_extension_modbus import actions

    actions.register_all()
    zelos_sdk.init(name="modbus", actions=True)

    # Add trace logging handler (after init)
    handler = TraceLoggingHandler("zelos_extension_modbus_logger")
    logging.getLogger().addHandler(handler)

    iface_count = len(clients)
    logger.info(
        f"Starting Modbus extension with {iface_count} interface{'s' if iface_count > 1 else ''}"
    )

    # Run all clients concurrently
    asyncio.run(_run_clients_async(clients))
