"""App mode runner for Zelos Modbus extension.

This module handles running the extension when launched from the Zelos App
with configuration loaded from config.json.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import zelos_sdk
from zelos_sdk.extensions import load_config

from zelos_extension_modbus.client import ModbusClient
from zelos_extension_modbus.register_map import RegisterMap

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)


def run_app_mode() -> None:
    """Run the extension in app mode with configuration from Zelos App."""
    # Load configuration
    config = load_config()

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

    # Create client based on transport type
    transport = config.get("transport", "tcp")

    client_kwargs: dict[str, Any] = {
        "transport": transport,
        "unit_id": config.get("unit_id", 1),
        "timeout": config.get("timeout", 3.0),
        "register_map": register_map,
        "poll_interval": config.get("poll_interval", 1.0),
    }

    if transport == "tcp":
        client_kwargs["host"] = config.get("host", "127.0.0.1")
        client_kwargs["port"] = config.get("port", 502)
    else:  # rtu
        client_kwargs["serial_port"] = config.get("serial_port", "/dev/ttyUSB0")
        client_kwargs["baudrate"] = config.get("baudrate", 9600)

    client = ModbusClient(**client_kwargs)

    # Register actions with SDK
    zelos_sdk.actions_registry.register(client)

    # Start and run
    client.start()
    client.run()
