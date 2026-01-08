"""CLI trace command for Zelos Modbus extension.

Allows running the extension directly from command line without Zelos App configuration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import rich_click as click

from zelos_extension_modbus.client import ModbusClient
from zelos_extension_modbus.register_map import RegisterMap

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@click.command()
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
@click.option("--unit-id", "-u", type=int, default=1, help="Modbus unit/slave ID")
@click.option(
    "--interval", "-i", type=float, default=1.0, help="Poll interval in seconds"
)
@click.option("--timeout", type=float, default=3.0, help="Request timeout in seconds")
@click.pass_context
def trace(
    ctx: click.Context,
    host_or_port: str,
    register_map_file: str | None,
    transport: str,
    port: int,
    baudrate: int,
    unit_id: int,
    interval: float,
    timeout: float,
) -> None:
    """Trace Modbus registers from command line.

    HOST_OR_PORT is either the TCP host address (e.g., 192.168.1.100) for TCP transport,
    or the serial port (e.g., /dev/ttyUSB0) for RTU transport.

    REGISTER_MAP_FILE is an optional path to a JSON register map file.

    Examples:

        # TCP with register map
        uv run main.py trace 192.168.1.100 registers.json

        # TCP with custom port
        uv run main.py trace 192.168.1.100 registers.json --port 5020

        # RTU serial
        uv run main.py trace /dev/ttyUSB0 registers.json --transport rtu --baudrate 19200

        # TCP without register map (raw mode)
        uv run main.py trace 192.168.1.100
    """
    # Load register map if provided
    register_map = None
    if register_map_file:
        try:
            register_map = RegisterMap.from_file(register_map_file)
            logger.info(f"Loaded register map with {len(register_map.registers)} registers")
        except Exception as e:
            logger.error(f"Failed to load register map: {e}")
            raise click.ClickException(f"Invalid register map: {e}") from e

    # Build client kwargs
    client_kwargs = {
        "transport": transport,
        "unit_id": unit_id,
        "timeout": timeout,
        "register_map": register_map,
        "poll_interval": interval,
    }

    if transport == "tcp":
        client_kwargs["host"] = host_or_port
        client_kwargs["port"] = port
    else:  # rtu
        client_kwargs["serial_port"] = host_or_port
        client_kwargs["baudrate"] = baudrate

    # Get shutdown handler from context if available
    shutdown_handler = ctx.obj.get("shutdown_handler") if ctx.obj else None

    client = ModbusClient(**client_kwargs)

    if shutdown_handler:
        shutdown_handler(client)

    client.start()
    client.run()
