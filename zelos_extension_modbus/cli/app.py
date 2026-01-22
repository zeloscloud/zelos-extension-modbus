"""App mode runner for Zelos Modbus extension.

This module handles running the extension when launched from the Zelos App
with configuration loaded from config.json, including demo mode support.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config

from zelos_extension_modbus.client import ModbusClient
from zelos_extension_modbus.register_map import RegisterMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Demo server settings
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 5020


def get_demo_register_map_path() -> Path:
    """Get path to the bundled demo register map."""
    with resources.as_file(
        resources.files("zelos_extension_modbus.demo").joinpath("power_meter.json")
    ) as path:
        return path


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


def run_app_mode(demo: bool = False) -> None:
    """Run the extension in app mode with configuration from Zelos App.

    Args:
        demo: If True, use built-in demo mode with simulated power meter
    """
    # Load configuration
    config = load_config()

    # Demo mode overrides config
    if demo or config.get("demo", False):
        logger.info("Demo mode: using built-in power meter simulator")
        _server_thread = start_demo_server()

        # Override connection settings for demo
        config["transport"] = "tcp"
        config["host"] = DEMO_HOST
        config["port"] = DEMO_PORT

        # Use bundled demo register map
        demo_map_path = get_demo_register_map_path()
        config["register_map_file"] = str(demo_map_path)

    # Set log level
    log_level = config.get("log_level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level))

    # Load register map if provided
    register_map = None
    map_file = config.get("register_map_file")
    if map_file:
        map_path = Path(map_file)
        if map_path.exists():
            try:
                register_map = RegisterMap.from_file(map_path)
                logger.info(f"Loaded register map with {len(register_map.registers)} registers")
            except Exception as e:
                logger.error(f"Failed to load register map: {e}")
        else:
            logger.warning(f"Register map file not found: {map_file}")

    # Create client (TCP only)
    client_kwargs: dict[str, Any] = {
        "transport": "tcp",
        "host": config.get("host", "127.0.0.1"),
        "port": config.get("port", 502),
        "unit_id": config.get("unit_id", 1),
        "timeout": config.get("timeout", 3.0),
        "register_map": register_map,
        "poll_interval": config.get("poll_interval", 1.0),
    }

    client = ModbusClient(**client_kwargs)

    # Register actions with SDK
    zelos_sdk.actions_registry.register(client)

    # Start and run
    client.start()
    client.run()
