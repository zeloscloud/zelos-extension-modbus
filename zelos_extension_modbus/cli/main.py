"""Zelos Modbus Extension - CLI (`main.py` and the `zelos-extension-modbus` script).

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

    # Discover an unknown device (read-only) / check a map against a device
    uv run main.py scan 192.168.1.100 --out draft.json
    uv run main.py verify 192.168.1.100 registers.json
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from collections.abc import Callable
from types import FrameType

import rich_click as click

from zelos_extension_modbus import actions
from zelos_extension_modbus.constants import (
    DEFAULT_PREFIX,
    MIN_RATE,
    MODBUS_MAX_READ_COUNT,
    RESERVED_CONNECTION_NAMES,
    name_error,
)

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def shutdown_handler(signum: int, frame: FrameType | None) -> None:
    """Exit on SIGTERM or SIGINT before polling starts (``run_connections`` owns it then)."""
    logger.info("Shutting down...")
    sys.exit(0)


def _setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


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
@click.option("--name", default="", help="Connection name (default: sanitized host or serial port)")
@click.option("--device-name", default="", help="Device name (default: unit<id>)")
@click.option(
    "--rate",
    "-r",
    type=click.FloatRange(min=MIN_RATE),
    default=1.0,
    help="Poll rate in seconds for registers without their own rate",
)
@click.option("--timeout", type=float, default=3.0, help="Request timeout in seconds")
@click.option(
    "--retries",
    type=click.IntRange(0, 5),
    default=1,
    help="Extra attempts per request before it counts as failed",
)
@click.option(
    "--request-delay-ms",
    type=click.IntRange(0, 5000),
    default=0,
    help="Minimum gap between consecutive requests (ms)",
)
@click.option(
    "--block-reads/--no-block-reads",
    default=True,
    help="Coalesce contiguous registers into range reads (default: on)",
)
@click.option(
    "--max-block-size",
    type=click.IntRange(1, MODBUS_MAX_READ_COUNT),
    default=MODBUS_MAX_READ_COUNT,
    help="Maximum registers per range read",
)
@click.option(
    "--max-read-gap",
    type=click.IntRange(min=0),
    default=0,
    help="Maximum uncovered registers to bridge within a block (0 = strictly contiguous)",
)
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
    name: str,
    device_name: str,
    rate: float,
    timeout: float,
    retries: int,
    request_delay_ms: int,
    block_reads: bool,
    max_block_size: int,
    max_read_gap: int,
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
    import asyncio

    from zelos_extension_modbus.cli.app import device_settings, init_sdk, run_connections
    from zelos_extension_modbus.client import ModbusConnection, ModbusDevice
    from zelos_extension_modbus.register_map import RegisterMap
    from zelos_extension_modbus.scan import endpoint

    for value, label, reserved in (
        (name, "--name", RESERVED_CONNECTION_NAMES),
        (device_name, "--device-name", ()),
    ):
        if error := name_error(value, label, reserved):
            raise click.BadParameter(error)

    _setup_signal_handlers()

    register_map = None
    if register_map_file:
        try:
            register_map = RegisterMap.from_file(register_map_file)
            logger.info(f"Loaded register map with {len(register_map.registers)} registers")
        except Exception as e:
            raise click.ClickException(f"Invalid register map: {e}") from e

    connection = ModbusConnection(
        **endpoint(host_or_port, transport, port, baudrate, parity, stopbits, bytesize),
        timeout=timeout,
        retries=retries,
        request_delay_ms=request_delay_ms,
        name=name or None,
    )
    device = ModbusDevice(
        connection,
        unit_id=unit_id,
        register_map=register_map,
        rate=rate,
        name=device_name or None,
        # A map `device` block overrides these, as in app mode.
        **device_settings(
            register_map,
            {
                "block_reads": block_reads,
                "max_block_size": max_block_size,
                "max_read_gap": max_read_gap,
            },
        ),
    )

    # Register devices and actions BEFORE init: init advertises them.
    from zelos_extension_modbus import registry

    registry.register(device)
    actions.register_all()
    source = init_sdk(DEFAULT_PREFIX)

    logger.info(f"Starting Modbus trace: {transport}://{host_or_port}")
    connection.start(DEFAULT_PREFIX, source)
    asyncio.run(run_connections([connection]))


@cli.command("mock-server")
@click.argument("register_map_file", type=click.Path(exists=True))
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address")
@click.option("--port", "-p", type=int, default=5020, show_default=True, help="TCP port")
@click.option(
    "--interval",
    "-i",
    type=float,
    default=1.0,
    show_default=True,
    help="Seconds between randomization passes",
)
def mock_server(register_map_file: str, host: str, port: int, interval: float) -> None:
    """Run a generic mock Modbus TCP server driven by a register-map JSON file.

    Read-only registers are randomized every --interval seconds to values
    within their datatype range; writable registers are left untouched so
    writes from a client persist and can be observed on the next poll.

    \b
    Example:
        uv run main.py mock-server examples/example_registers.json --port 5020
    """
    from zelos_extension_modbus.demo.mock_server import run_mock_server_sync
    from zelos_extension_modbus.register_map import RegisterMap

    try:
        register_map = RegisterMap.from_file(register_map_file)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"Invalid register map: {e}") from e

    logger.info(
        "Loaded register map %r: %d events, %d registers",
        register_map.name,
        len(register_map.events),
        len(register_map.registers),
    )
    run_mock_server_sync(register_map, host=host, port=port, update_interval=interval)


def _link_options(fn: Callable) -> Callable:
    """Transport options shared by scan and verify."""
    options = [
        click.option("--transport", "-t", type=click.Choice(["tcp", "rtu"]), default="tcp"),
        click.option("--port", "-p", type=int, default=502, help="TCP port"),
        click.option("--baudrate", "-b", type=int, default=9600, help="Serial baudrate"),
        click.option("--parity", type=click.Choice(["N", "E", "O"]), default="N"),
        click.option("--stopbits", type=click.Choice(["1", "2"]), default="1"),
        click.option("--bytesize", type=click.Choice(["7", "8"]), default="8"),
        click.option("--timeout", type=float, default=0.5, show_default=True),
        click.option(
            "--delay-ms",
            type=click.IntRange(0, 5000),
            help="Gap between requests [default: TCP 0, RTU 50]",
        ),
        click.option("--max-seconds", type=float, help="Stop and report after this long"),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


@cli.command()
@click.argument("target")
@_link_options
@click.option("--autodetect", is_flag=True, help="RTU: try common serial settings first")
@click.option(
    "--units",
    default="",
    help="Unit ids to probe, e.g. 1,2,10-20 (default: TCP the first of 1, 0, 255 to answer, "
    "else a 1-247 sweep; RTU 1-247)",
)
@click.option(
    "--tables",
    default="holding,input,coil,discrete_input",
    show_default=True,
    help="Tables to scan, comma-separated",
)
@click.option(
    "--range", "ranges", default="", help="1-based address windows, e.g. 1-10000,40001-41000"
)
@click.option("--samples", type=click.IntRange(1, 100), default=10, show_default=True)
@click.option("--period", type=float, default=5.0, show_default=True, help="Sampling seconds")
@click.option("--out", type=click.Path(dir_okay=False), help="Write the draft map here")
def scan(
    target: str,
    transport: str,
    port: int,
    timeout: float,
    delay_ms: int | None,
    max_seconds: float | None,
    autodetect: bool,
    units: str,
    tables: str,
    ranges: str,
    samples: int,
    period: float,
    out: str | None,
    **serial: str,
) -> None:
    """Discover an unknown device with reads only; print a JSON report.

    TARGET is a host (TCP) or serial port (RTU). Only function codes 01-04,
    43/14 and 17 are sent. Addresses are 1-based (holding 40001 = wire 40000).
    Default windows: TCP 1-65536; RTU 1-10000, 30001-31000, 40001-41000,
    50001-51000. Default units: TCP the first of 1, 0, 255 that answers; a
    gateway reply or no answer sweeps 1-247 (a silent link stops after 16
    timeouts). RTU sweeps 1-247 (use --units to go faster).

    The draft map (1-based, every register writable: false) is written only to
    --out; several devices write <out>_unit<id>.json.

    \b
    Examples:
        uv run main.py scan 192.168.1.100 --out draft.json
        uv run main.py scan /dev/ttyUSB0 -t rtu --autodetect --units 1-10
    """
    import asyncio
    import json
    from pathlib import Path

    from zelos_extension_modbus.scan import (
        TABLES,
        endpoint,
        parse_units,
        parse_windows,
        quiet_pymodbus,
    )
    from zelos_extension_modbus.scan import scan as run_scan

    table_list = [t.strip() for t in tables.split(",") if t.strip()]
    if bad := [t for t in table_list if t not in TABLES]:
        raise click.BadParameter(f"unknown table(s) {bad}; use {list(TABLES)}")
    try:
        unit_list, windows = parse_units(units), parse_windows(ranges)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e
    quiet_pymodbus()
    result = asyncio.run(
        run_scan(
            endpoint(target, transport, port, **serial),
            units=unit_list or None,
            tables=table_list,
            windows=windows or None,
            samples=samples,
            period=period,
            timeout=timeout,
            delay_ms=delay_ms,
            max_seconds=max_seconds,
            autodetect=autodetect,
        )
    )
    click.echo(json.dumps(result["report"], indent=2))
    maps = result["maps"]
    if out and maps:
        path = Path(out)
        for unit, draft in maps.items():
            dest = path if len(maps) == 1 else path.with_stem(f"{path.stem}_unit{unit}")
            dest.write_text(json.dumps(draft, indent=2) + "\n")
            logger.info(f"Wrote draft map for unit {unit} to {dest}")
    elif out:
        logger.warning("No draft map written; see the report")


@cli.command()
@click.argument("target")
@click.argument("register_map_file", type=click.Path(exists=True, dir_okay=False))
@_link_options
@click.option("--unit", type=int, default=1, show_default=True, help="Unit id")
@click.option("--samples", type=click.IntRange(1, 100), default=2, show_default=True)
def verify(
    target: str,
    register_map_file: str,
    transport: str,
    port: int,
    timeout: float,
    delay_ms: int | None,
    max_seconds: float | None,
    unit: int,
    samples: int,
    **serial: str,
) -> None:
    """Read every register of a map; print a JSON report of problems.

    Flags exceptions, registers that read 0 on every sample, implausible
    floats and not-implemented sentinels. Reads only. Exits 1 on any problem
    or a cutoff (--max-seconds, default 840; or 16 requests in a row unanswered).

    \b
    Example:
        uv run main.py verify 192.168.1.100 registers.json --unit 3
    """
    import asyncio
    import json

    from zelos_extension_modbus.register_map import RegisterMap
    from zelos_extension_modbus.scan import endpoint, quiet_pymodbus, verify_map

    try:
        register_map = RegisterMap.from_file(register_map_file)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"Invalid register map: {e}") from e
    quiet_pymodbus()
    report = asyncio.run(
        verify_map(
            endpoint(target, transport, port, **serial),
            register_map,
            unit,
            samples=samples,
            timeout=timeout,
            delay_ms=delay_ms,
            max_seconds=max_seconds,
        )
    )
    click.echo(json.dumps(report, indent=2))
    if report.get("error") or report.get("problems") or report.get("cutoff"):
        sys.exit(1)


@cli.command("demo-server")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address")
@click.option("--port", "-p", type=int, default=5020, show_default=True, help="TCP port")
@click.option(
    "--unit-id",
    "-u",
    "unit_ids",
    type=int,
    multiple=True,
    help="Serve a separate meter per unit id (repeatable; default: one meter for every id)",
)
@click.option("--scan-target", is_flag=True, help="Serve the scan target instead of the meter")
@click.option("--zero-fill", is_flag=True, help="Scan target: unmapped addresses read 0")
@click.option("--sunspec", is_flag=True, help="Scan target: 'SunS' marker at 40001")
@click.option("--serial", "serial_port", help="Serve RTU on this serial port instead of TCP")
@click.option("--baudrate", "-b", type=int, default=9600, show_default=True)
@click.option("--parity", type=click.Choice(["N", "E", "O"]), default="N", show_default=True)
@click.option("--stopbits", type=click.Choice(["1", "2"]), default="1", show_default=True)
def demo_server(
    host: str,
    port: int,
    unit_ids: tuple[int, ...],
    scan_target: bool,
    zero_fill: bool,
    sunspec: bool,
    serial_port: str | None,
    baudrate: int,
    parity: str,
    stopbits: str,
) -> None:
    """Run the standalone power-meter simulator (matches demo/power_meter.json).

    Unlike mock-server's random read-only values, this drives realistic
    3-phase electrical values, so writable registers (setpoints, relays)
    persist and read-only ones move like a live device. Point a connection at
    it for an end-to-end test against an agent-hosted install of this
    extension.

    --scan-target serves a sparse read-only device for testing scan: holes
    (exception 02), a 60-register read limit, FC 43/14 identification, float32
    runs in all four byte orders, a counter and an ASCII string. It answers
    every unit id.

    \b
    Example:
        uv run main.py demo-server
        uv run main.py demo-server -u 1 -u 2   # two meters on one port
        uv run main.py demo-server --scan-target -p 5030
        uv run main.py demo-server --scan-target --serial /dev/ttys004 --parity E
    """
    from zelos_extension_modbus.demo.simulator import run_demo_server_sync

    serial = None
    if serial_port:
        serial = {
            "port": serial_port,
            "baudrate": baudrate,
            "parity": parity,
            "stopbits": int(stopbits),
        }
    try:
        run_demo_server_sync(
            host=host,
            port=port,
            unit_ids=unit_ids,
            scan_target=scan_target,
            zero_fill=zero_fill,
            sunspec=sunspec,
            serial=serial,
        )
    except (OSError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e
