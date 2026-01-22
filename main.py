#!/usr/bin/env python3
"""Zelos Modbus Extension - CLI entry point.

This module provides the command-line interface for the Modbus extension.
It can run in several modes:

1. App mode (default): Loads configuration from config.json when run from Zelos App
2. Demo mode: Uses built-in power meter simulator (no hardware required)
3. CLI trace mode: Direct command-line usage with explicit arguments

Examples:
    # Run from Zelos App (uses config.json)
    uv run main.py

    # Demo mode (simulated power meter)
    uv run main.py demo

    # CLI trace mode
    uv run main.py trace 192.168.1.100 registers.json
"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType
from typing import TYPE_CHECKING

import rich_click as click
import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

if TYPE_CHECKING:
    from zelos_extension_modbus.client import ModbusClient

# Configure logging - INFO level prevents debug noise
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global client reference for shutdown handler
_client: ModbusClient | None = None


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Handle graceful shutdown on SIGTERM or SIGINT."""
    logger.info("Shutting down...")
    if _client:
        _client.stop()
    sys.exit(0)


def set_shutdown_client(client: ModbusClient) -> None:
    """Set the client for shutdown handling."""
    global _client
    _client = client


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with simulated power meter")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos Modbus Extension - Read, write, and monitor Modbus registers.

    When run without a subcommand, starts in app mode using configuration
    from the Zelos App (config.json).

    Use --demo flag or 'demo' subcommand for simulated power meter.
    Use 'trace' subcommand for direct CLI access without Zelos App.
    """
    ctx.ensure_object(dict)
    ctx.obj["shutdown_handler"] = set_shutdown_client
    ctx.obj["demo"] = demo

    if ctx.invoked_subcommand is None:
        # App mode - run with Zelos App configuration
        run_app_mode(ctx, demo=demo)


def run_app_mode(ctx: click.Context, demo: bool = False) -> None:
    """Run in app mode with Zelos SDK initialization."""
    # Initialize SDK
    zelos_sdk.init(name="zelos_extension_modbus", actions=True)

    # Add trace logging handler
    handler = TraceLoggingHandler("zelos_extension_modbus_logger")
    logging.getLogger().addHandler(handler)

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Import and run app mode
    from zelos_extension_modbus.cli.app import run_app_mode as _run_app_mode

    _run_app_mode(demo=demo)


@cli.command()
@click.pass_context
def demo(ctx: click.Context) -> None:
    """Run demo mode with simulated 3-phase power meter.

    Starts a local Modbus TCP server with simulated power meter data
    and connects to it. No hardware required.

    The simulated power meter includes:
    - 3-phase voltage (L1, L2, L3)
    - 3-phase current (L1, L2, L3)
    - Total power, power factor, frequency
    - Energy accumulator
    - Temperature and relay outputs
    """
    run_app_mode(ctx, demo=True)


@cli.command()
@click.argument("host", type=str)
@click.argument("register_map_file", type=click.Path(exists=True), required=False)
@click.option("--port", "-p", type=int, default=502, help="Modbus TCP port")
@click.option("--unit-id", "-u", type=int, default=1, help="Modbus unit/slave ID")
@click.option(
    "--interval", "-i", type=float, default=1.0, help="Poll interval in seconds"
)
@click.option("--timeout", type=float, default=3.0, help="Request timeout in seconds")
@click.pass_context
def trace(
    ctx: click.Context,
    host: str,
    register_map_file: str | None,
    port: int,
    unit_id: int,
    interval: float,
    timeout: float,
) -> None:
    """Trace Modbus registers from command line.

    HOST is the TCP host address (e.g., 192.168.1.100).

    REGISTER_MAP_FILE is an optional path to a JSON register map file.

    \b
    Examples:
        # TCP with register map
        uv run main.py trace 192.168.1.100 registers.json

        # TCP with custom port
        uv run main.py trace 192.168.1.100 registers.json --port 5020

        # TCP without register map (raw address mode)
        uv run main.py trace 192.168.1.100
    """
    from zelos_extension_modbus.client import ModbusClient
    from zelos_extension_modbus.register_map import RegisterMap

    # Initialize SDK for CLI mode
    zelos_sdk.init(name="zelos_extension_modbus", actions=True)

    # Add trace logging handler
    handler = TraceLoggingHandler("zelos_extension_modbus_logger")
    logging.getLogger().addHandler(handler)

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Load register map if provided
    register_map = None
    if register_map_file:
        try:
            register_map = RegisterMap.from_file(register_map_file)
            logger.info(f"Loaded register map with {len(register_map.registers)} registers")
        except Exception as e:
            raise click.ClickException(f"Invalid register map: {e}") from e

    # Build client kwargs (TCP only)
    client_kwargs = {
        "transport": "tcp",
        "host": host,
        "port": port,
        "unit_id": unit_id,
        "timeout": timeout,
        "register_map": register_map,
        "poll_interval": interval,
    }

    global _client
    _client = ModbusClient(**client_kwargs)

    # Register actions
    zelos_sdk.actions_registry.register(_client)

    logger.info(f"Starting Modbus trace: tcp://{host}:{port}")
    _client.start()
    _client.run()


if __name__ == "__main__":
    cli()
