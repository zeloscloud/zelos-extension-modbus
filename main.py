#!/usr/bin/env python3
"""Zelos Modbus Extension - CLI entry point.

This module provides the command-line interface for the Modbus extension.
It can run in several modes:

1. App mode (default): Loads configuration from config.json when run from Zelos App
2. Demo mode (--demo): Uses built-in power meter simulator for testing
3. CLI trace mode: Direct command-line usage with explicit arguments

Examples:
    # Run from Zelos App (uses config.json)
    uv run main.py

    # Demo mode (simulated power meter, for testing)
    uv run main.py --demo

    # CLI trace mode
    uv run main.py trace 192.168.1.100 registers.json
    uv run main.py trace /dev/ttyUSB0 registers.json --transport rtu
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

from zelos_extension_modbus.constants import Transport

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


def _setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def _init_sdk() -> None:
    """Initialize Zelos SDK and logging handler. Call AFTER registering actions."""
    zelos_sdk.init(name="modbus", actions=True)
    handler = TraceLoggingHandler("zelos_extension_modbus_logger")
    logging.getLogger().addHandler(handler)


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, hidden=True, help="Run with built-in simulator (testing)")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos Modbus Extension - Read, write, and monitor Modbus registers.

    When run without a subcommand, starts in app mode using configuration
    from the Zelos App (config.json).

    Use 'trace' subcommand for direct CLI access without Zelos App.
    """
    ctx.ensure_object(dict)
    ctx.obj["demo"] = demo

    if ctx.invoked_subcommand is None:
        _setup_signal_handlers()
        from zelos_extension_modbus.cli.app import run_app_mode

        run_app_mode(demo=demo)


@cli.command()
@click.argument("host_or_port", type=str)
@click.argument("register_map_file", type=click.Path(exists=True), required=False)
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["tcp", "rtu"]),
    default="tcp",
    help="Modbus transport type",
)
@click.option("--port", "-p", type=int, default=502, help="TCP port (for tcp transport)")
@click.option(
    "--baudrate", "-b", type=int, default=9600, help="Serial baudrate (for rtu transport)"
)
@click.option("--parity", type=click.Choice(["N", "E", "O"]), default="N", help="Serial parity")
@click.option("--stopbits", type=click.Choice(["1", "2"]), default="1", help="Stop bits")
@click.option("--bytesize", type=click.Choice(["7", "8"]), default="8", help="Data bits")
@click.option("--unit-id", "-u", type=int, default=1, help="Modbus unit/slave ID")
@click.option("--interval", "-i", type=float, default=1.0, help="Poll interval in seconds")
@click.option("--timeout", type=float, default=3.0, help="Request timeout in seconds")
@click.pass_context
def trace(
    ctx: click.Context,
    host_or_port: str,
    register_map_file: str | None,
    transport: str,
    port: int,
    baudrate: int,
    parity: str,
    stopbits: str,
    bytesize: str,
    unit_id: int,
    interval: float,
    timeout: float,
) -> None:
    """Trace Modbus registers from command line.

    HOST_OR_PORT is either the TCP host address (e.g., 192.168.1.100) for TCP,
    or the serial port (e.g., /dev/ttyUSB0) for RTU.

    REGISTER_MAP_FILE is an optional path to a JSON register map file.

    \b
    Examples:
        # TCP with register map
        uv run main.py trace 192.168.1.100 registers.json

        # TCP with custom port
        uv run main.py trace 192.168.1.100 registers.json --port 5020

        # RTU serial
        uv run main.py trace /dev/ttyUSB0 registers.json -t rtu -b 19200

        # TCP without register map (raw address mode)
        uv run main.py trace 192.168.1.100
    """
    from zelos_extension_modbus.client import ModbusClient
    from zelos_extension_modbus.register_map import RegisterMap

    _setup_signal_handlers()

    # Load register map if provided
    register_map = None
    if register_map_file:
        try:
            register_map = RegisterMap.from_file(register_map_file)
            logger.info(f"Loaded register map with {len(register_map.registers)} registers")
        except Exception as e:
            raise click.ClickException(f"Invalid register map: {e}") from e

    # Build client kwargs
    client_kwargs = {
        "transport": transport,
        "unit_id": unit_id,
        "timeout": timeout,
        "register_map": register_map,
        "poll_interval": interval,
    }

    if transport == Transport.TCP:
        client_kwargs["host"] = host_or_port
        client_kwargs["port"] = port
    else:  # rtu
        client_kwargs["serial_port"] = host_or_port
        client_kwargs["baudrate"] = baudrate
        client_kwargs["parity"] = parity
        client_kwargs["stopbits"] = int(stopbits)
        client_kwargs["bytesize"] = int(bytesize)

    global _client
    _client = ModbusClient(**client_kwargs)

    # Register in global registry and register actions BEFORE init
    from zelos_extension_modbus import actions, registry

    registry.register("modbus", _client)
    actions.register_all()
    _init_sdk()

    logger.info(f"Starting Modbus trace: {transport}://{host_or_port}")
    _client.start()
    _client.run()


if __name__ == "__main__":
    cli()
