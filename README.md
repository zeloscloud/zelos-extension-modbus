# Zelos Modbus

A Zelos extension for the Modbus protocol. Read, write, and monitor registers from PLCs, power meters, sensors, and other Modbus devices over TCP or RS232/RS485 serial.

## Features

- 📡 **Modbus TCP & RTU**: Connect over Ethernet or RS232/RS485 serial
- 📊 **All register types**: Holding, input, coils, and discrete inputs
- 📄 **Register map files**: Define your device layout in a simple JSON file
- ☀️ **SunSpec discovery**: Inverters, meters and batteries map themselves at connect
- ✏️ **Read & write actions**: Interactive register access from the Zelos App
- 🔢 **Flexible data types**: 16/32/64-bit integers, floats, booleans
- 🔄 **Byte order options**: Big/little endian with word-swap variants

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** a connection (TCP or RTU) and its devices (unit ID + register map file, or SunSpec)
3. **Start** the extension to begin streaming data
4. **View** real-time register values in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Connections

A connection is a TCP endpoint or a serial port. Its devices share the link and are polled one request at a time (RS485 is half-duplex; many TCP gateways accept few connections).

| Setting | Default | Description |
|---------|---------|-------------|
| Transport | `tcp` | `tcp` or `rtu`; picks the connection fields below |
| Name | endpoint | Trace name; defaults to the sanitized host or serial port (`10_0_0_5`, `dev_ttyUSB0`), plus `_<port>` when two unnamed TCP connections share a host |
| Host / Port | `127.0.0.1` / `502` | TCP only |
| Serial Port | | RTU only (`/dev/ttyUSB0`, `COM3`) |
| Baudrate / Parity / Stop Bits / Data Bits | `9600` / `N` / `1` / `8` | RTU only; must match the devices |
| Devices | | One or more, below |

### Devices

| Setting | Default | Description |
|---------|---------|-------------|
| Unit ID | `1` | Modbus slave/unit ID; unique per connection |
| Name | `unit<ID>` | Trace name; unique per connection |
| Register Map | `file` | `file` or `sunspec` (see [SunSpec](#sunspec)) |
| Register Map File | | JSON register map; empty = raw mode. `file` only |
| Rate | Advanced `default_rate` | Poll rate (s) for registers without their own `rate`; `0` = not polled (actions still work) |

Names are letters, digits, space, `_` or `-`; anything else is rejected at start. `log` and `modbus_log` are reserved connection names.

### Advanced (collapsed, applies to every connection and device)

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `Modbus` | Trace source every connection publishes under: `Modbus/10_0_0_5/unit1/voltage`, logs at `Modbus/log`. Cleared: one source per connection (`10_0_0_5/unit1/voltage`) and logs under `modbus_log` |
| `default_rate` | `1.0` | Poll rate (s) when neither the register nor the device sets one |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `timeout` | `3.0` | Seconds to wait for each response |
| `retries` | `1` | Extra attempts per request; a failed request costs `timeout x (1 + retries)` |
| `request_delay_ms` | `0` | Minimum gap between requests on a connection (RS485 gateways, slow RTU devices) |
| `connect_delay_ms` | `0` | Pause after each (re)connect before the first request |
| `demote_after` | `3` | Consecutive timeouts before a device is demoted (see [Polling](#polling)) |
| `demote_max_s` | `300` | Longest wait between probes of a demoted device |
| `block_reads` | on | Coalesce contiguous registers into range reads |
| `max_block_size` | `125` | Max registers per range read |
| `max_bit_block_size` | `2000` | Max coils / discrete inputs per range read |
| `max_read_gap` | `0` | Max uncovered registers bridged within a block |
| `write_mode` | `auto` | `auto` (FC 6 single / FC 16 multi) or `fc16` (always FC 16) |
| `allow_raw_writes` | off | Enable the raw write actions (`write_single_register`, `write_registers`, `write_coil`); addresses the map marks read-only stay refused. App config only |

Per key, a register map `device` block overrides Advanced. A config from 0.1.x (`interfaces`, per-interface unit ID/map, top-level `log_level`) fails at start: set the connections up again in the config form.

Troubleshooting serial/USB connection failures: see [DEBUG.md](DEBUG.md).

### Polling

Rate precedence: register `rate` > device Rate > `default_rate`; a map `min_rate` floors it. Each connection runs one scheduler across its devices:

- **Tick**: every due block at the connection's fastest rate first, then at most one other item, most overdue first: a due slower block, a demoted device's probe, or a SunSpec discovery. Slow work spreads over the ticks instead of bursting and stalling fast points; blocks never mix rates. A block holding a `scale_ref` exponent is read in the same tick as every block it scales.
- **Requested vs achieved**: `get_status` / `get_snapshot` / `list_devices` report `requested_rate`, `achieved_rate` (smoothed read interval) and `overload_pct` (100 x mean lateness / rate) for the worst tier, and every tier under `tiers`. A tier over 100% for 30 s warns once, and logs its recovery.
- **Demotion**: after `demote_after` consecutive timeouts or gateway exceptions 0A/0B (polling or SunSpec discovery) a device is skipped and its fields are not logged; one single-attempt probe (no retries) of a due block after 10 s, doubling to `demote_max_s` (a device awaiting SunSpec discovery is probed with one read at 40001); any answer resumes it, and its achieved rate is null until a normal interval passes. `get_status` shows `demoted` and `retry_in_s`.
- **Counters**: `successful_reads` and `failed_reads` per device (a timeout is a failed read). A failed read logs none of its block's fields that cycle.
- **Link down**: every read that falls due counts in `failed_reads`, and `achieved_rate` / `overload_pct` are null until reads resume. Reconnects wait 3 s, doubling to 60 s; a poll that keeps the link resets it. The first failure is logged, then only when the wait grows.
- **Illegal addresses** (Kepware "Deactivate Tags on Illegal Address"): block size is static (map `max_block_size` > Advanced > 125). A block answered with exception 02/03 logs one warning and is retried every 10 min, also across demotion; other blocks keep polling. `get_status` lists it under `refused` (`range`, `code`, `retry_in_s`). Run `verify` to find the bad registers, then fix the map or `max_block_size` (scan learns it). Other exception codes warn once per block and keep polling.
- **Shutdown**: SIGTERM/SIGINT cancels every connection mid-request and disconnects; past 3 s the process exits anyway.

## Register Map

A register map file defines which registers to read and how to decode them. Event names become Zelos trace events, and register names become fields within those events.

```json
{
  "name": "power_meter",
  "events": {
    "voltage": [
      {"name": "L1", "address": 1, "datatype": "float32", "unit": "V"},
      {"name": "L2", "address": 3, "datatype": "float32", "unit": "V"}
    ],
    "setpoints": [
      {"name": "limit", "address": 101, "datatype": "uint16", "writable": true}
    ],
    "status": [
      {"name": "firmware", "address": 1, "type": "input"},
      {"name": "door_open", "address": 1, "type": "discrete_input"}
    ]
  }
}
```

### Addressing

Addresses are 1-based by default, the Kepware/Ignition convention: holding register `40001` in a vendor sheet is `"type": "holding", "address": 40001`, sent as wire address 40000 (same for all four tables). Maps, default names (`r<address>`), `list_registers`, logs, verify reports, scan output and the raw actions (`read_register`, `write_*`) all use the map's base; a device without a map uses 1. A map numbered from wire address 0 sets `"device": {"address_base": 0}`.

### Register Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `address` | Yes | | Register address in the map's base (1–65536; 0–65535 with `address_base` 0) |
| `name` | No | `r<address>` | Field name in Zelos event (unique per event; duplicates fail at load) |
| `type` | No | `holding` | `holding`, `input`, `coil`, `discrete_input` |
| `datatype` | No | `uint16` | See data types below |
| `unit` | No | | Display unit |
| `scale` | No | `1.0` | Scale factor (finite, non-zero); a scaled integer decodes to a float. A write the register cannot hold exactly is refused, naming the nearest writable value |
| `rate` | No | device rate | Poll rate (seconds); `0` or `null` = not polled, actions still read/write it |
| `byte_order` | No | `big` | `big`, `little`, `big_swap`, `little_swap` |
| `writable` | No | `false` | `true` lets the write actions set this holding register or coil; everything else is read-only |
| `length` | strings | | Registers a `string` spans |
| `scale_ref` | No | | Name of an integer register in the same event holding a power-of-10 exponent, read in the same tick: value = raw x 10^exponent (null when the exponent is, or its read failed). Integer registers only, not with `scale`. The register and its exponent register are read-only (`writable: true` on either fails the load) |
| `invalid` | No | | Raw values that mean "not implemented", logged as null. Compared as the unsigned value of the words (int16 `-32768` is `32768`). On a string only `[0]`: all NUL bytes |
| `values` | No | | Enum labels for an unscaled integer register, `{"0": "off", "1": "on"}`; shown in the trace |

An optional top-level `device` block carries device-model quirks, e.g. `"device": {"max_block_size": 60, "byte_order": "big_swap"}`. Unknown keys or bad values fail the load.

| Key | Description |
|-----|-------------|
| `max_block_size`, `max_bit_block_size`, `max_read_gap`, `write_mode` | Override Advanced |
| `byte_order` | Default for registers without one |
| `min_rate` | Floor (s) on every register's rate: the device is never polled faster |
| `close_after_sweep` | Close the connection whenever polling goes idle, reopen on the next read (devices with one connection slot). Only when every device on the connection sets it |
| `address_base` | `1` (default) or `0`: how the map numbers addresses (see [Addressing](#addressing)) |

### Data Types

| Type | Registers | Type | Registers |
|------|-----------|------|-----------|
| `bool` | 1 | `uint32` | 2 |
| `uint16` | 1 | `int32` | 2 |
| `int16` | 1 | `float32` | 2 |
| `uint64` | 4 | `int64` | 4 |
| `float64` | 4 | `string` | `length` |

Strings decode 2 ASCII bytes per register, high byte first (`byte_order` does not apply), end at the first NUL, drop trailing spaces, and are never writable.

### Byte Order

Bytes of a 32-bit value as they arrive on the wire, A = most significant; 64-bit extends the same way (`little` = HGFEDCBA). Single-register values are never reordered.

| Order | Wire bytes | Description | Common in |
|-------|------------|-------------|-----------|
| `big` | AB CD | Standard Modbus | Most devices |
| `little` | DC BA | Full byte reversal | |
| `big_swap` | CD AB | Word-swapped, bytes within a word kept | Modicon/Schneider PLCs |
| `little_swap` | BA DC | Bytes swapped within each word, word order kept | |

## SunSpec

Set a device's Register Map to `sunspec` (`"register_map": "sunspec"`, instead of `register_map_file`) and the map is built at connect:

1. Find the `SunS` marker at holding register 40001, then 1, then 50001 (1-based, as SunSpec documents it; wire 40000, 0, 50000).
2. Walk the model chain (model ID, length) to the `0xFFFF` end marker.
3. Map every model with a [pysunspec2](https://github.com/sunspec/pysunspec2) definition.

Discovery only reads (FC 3). No marker, an exception answer for a model header, or a chain without the end marker is a device error (logged, `error` in `get_status`, `map_pending` stays true) retried every 30 s; a partial map is never used; a silent device is demoted like a polled one. The connection keeps polling its other devices. Identity, nameplate and settings models (1, 120, 121, 702) poll every 60 s; the rest at the device rate.

| SunSpec | Becomes |
|---------|---------|
| Model | Event `<name>_<id>` (`common_1`, `inverter_103`; a repeat gets `_2`) |
| Point | Field named after the point, with its units; read-only |
| Repeating group | `<group>_<n>_<point>`, count from the model length |
| `sf` scale factor | `scale_ref` to the `*_SF` field; a fixed integer `sf` becomes `scale` |
| `enum16`/`enum32` | `values` from the symbols |
| Not-implemented value | null (`0x8000`, `0xFFFF`, `0x80000000`, NaN, ...; `0` for accumulators and `ipaddr`, all NUL for strings) |
| `bitfield*` | Raw integer |

Skipped with a warning: models without a definition, point types `eui48`/`ipv6addr`, and repeating groups whose count needs a device read (nested or not last).

## Scan

Point scan at an unknown device to get its unit IDs, identity, valid address ranges and a draft register map. Scan only reads: function codes 01-04, 43/14 (device identification) and 17 (server ID), one request at a time, back to back on TCP and 50 ms apart on RTU (`--delay-ms`). A busy reply (06), or a unit that answered going silent (timeout, 0B), doubles the gap up to 500 ms; the report gives the settled `request_gap_ms`. Some devices clear latched alarms or counters on read; review before scanning production equipment.

```bash
uv run main.py scan 192.168.1.100 --out draft.json         # JSON report on stdout
uv run main.py scan /dev/ttyUSB0 -t rtu --autodetect --units 1-10
uv run main.py verify 192.168.1.100 registers.json --unit 3  # exits 1 on any problem
```

| Step | What it does |
|------|--------------|
| Units | TCP: the first of 1, 0, 255 to answer; a gateway (exception 0A/0B), or no answer, is swept. RTU is swept. A sweep covers 1-247 (`--units` to narrow it). 16 timeouts with no reply end it (RTU adds serial diagnostics) |
| Serial autodetect | The given settings, then 9600/19200 8N1/8E1, 38400 and 115200 8N1 |
| Identify | FC 43/14 objects and FC 17; a `SunS` marker reports "set register_map to sunspec" and skips the draft |
| Ranges | Per table over TCP 1-65536, or RTU 1-10000, 30001-31000, 40001-41000, 50001-51000 (`--range`, 1-based); learns the device's largest register and bit reads into the draft's `max_block_size` / `max_bit_block_size` |
| Classify | 10 samples over 5 s: float32/uint32 and byte order per block, ASCII strings, constant/counter/analog. Constant words that are all plausible float32 pairs stay floats, not text |
| Draft map | 1-based. Events `holding/b<start>`, registers `hr<address>` (`hr40001` = wire 40000; also `ir`, `co`, `di`), all `writable: false`, each with a `confidence` and its evidence in `description`; strings get `rate: 60` |

Stage budgets and `--max-seconds` cut a scan short with a `cutoffs` entry in the report; past `--max-seconds` every stage stops and what was sampled is still classified. Devices that read 0 at unmapped addresses make every address look valid; those registers are `low` confidence. Islands shorter than 10 (100) addresses deep inside a hole of 125 (1000) or more can be missed.

With the extension stopped, the same runs as actions. They return the report and draft maps inline and write no files.

| Action | Description |
|--------|-------------|
| `auto_config` | Quick, the config form's Auto-configure: sweeps each connection in the form (unsaved edits included; older apps: the saved config) for its configured unit, 1-10 and 247 (RTU: also serial settings), identifies them, keeps its devices and adds one per new unit, `register_map: sunspec` where the marker is found (also on a configured unit, unless it sets `register_map_file`). No connection: probes 127.0.0.1:502 only. One 25 s budget across connections; what did not fit is named in the message |
| `scan_device` | Comprehensive, slow: scan a host or serial port (empty: the first configured connection); units as in the CLI unless `units` is given. Time limit up to 1740 s |
| `verify_map` | Check a map file against the device register by register, naming each bad one (empty: the one configured for that unit). `ok` counts registers checked clean, `unchecked` those a cutoff (time limit, default 840 s; or 16 requests in a row unanswered) skipped; the CLI exits 1 on a cutoff |
| `list_serial_ports` | Serial ports on the agent's machine, as choices |

## Actions

The extension provides actions accessible from the Zelos App (and to app extensions as
`Modbus/<action>`, whatever the trace prefix). Every action but `list_devices` takes a
`device` selector, `<connection>/<device>` (e.g. `10_0_0_5/unit1`).

| Action | Description |
|--------|-------------|
| `list_devices` | One row per device: connection, unit ID, endpoint, trace path, map summary, `raw_writes` (raw write actions enabled), and health (`error`, `map_pending`, `refused`, demotion) |
| `get_status` | Connection status, `successful_reads`/`failed_reads`, requested vs achieved rate (worst tier), demotion, and block-read settings |
| `get_snapshot` | Last value per `event/name` (value + timestamp) from the poll cache, no device I/O |
| `read_register` | Read raw words/bits by address (map's base), register type, and count |
| `write_single_register` | Write one holding register (FC 6): an integer 0-65535, or -32768..-1 as two's complement. Raw addresses, counts and values must be whole numbers |
| `write_registers` | Write 1-123 holding registers (FC 16), values as for FC 6 |
| `write_coil` | Write `ON`/`OFF` (or true/false, 0/1) to a coil address (FC 5); anything else is refused |
| `read_named_register` | Read a mapped register by `event/name` (a bare name only if one event has it) |
| `write_named_register` | Write a mapped register by `event/name`; returns and caches the value actually written. A value the register cannot hold exactly (a fraction of a raw step) is refused; a coil takes only true/false or 0/1 |
| `list_registers` | Register catalog: `event/name` path, address, datatype, scale, unit, effective `rate` (0 = not polled) |
| `list_writable_registers` | Same catalog, writable registers only |

A failed request returns `success: false` with the reason in `error`: `no response from device`, `device refused: exception 02 (illegal data address)`, or `cannot connect to <endpoint>`.

Every write result carries `outcome` (OPC UA Good/Bad/Uncertain): `ok`; `refused` (not written: read-only, raw writes disabled, bad value, device exception, cannot connect); or `unknown` (no response, or gateway exception 0B: the write may have landed, so read back before retrying). `success` is true only for `ok`. The raw write actions need `allow_raw_writes` and never write an address the map marks read-only. Writes retry like reads, `retries` extra attempts (Kepware "Attempts Before Timeout"); FC 5/6/15/16 write absolute values, so a repeat is idempotent.

## Development

```bash
just install   # Install dependencies
just check     # Run linting
just format    # Auto-format code
just test      # Run tests
just sim       # Power-meter simulator to point a connection at (127.0.0.1:5020)
uv run main.py demo-server -u 1 -u 2   # Separate meters on unit IDs 1 and 2
uv run main.py demo-server --scan-target -p 5030   # Sparse device for scan (--zero-fill, --sunspec, --serial)
uv run python -m zelos_extension_modbus.demo.sunspec_sim --port 5021   # Full SunSpec inverter (register_map sunspec)
```

`demo-server --sunspec` only answers the SunSpec marker (enough for scan and Auto-configure); `sunspec_sim` serves full models for SunSpec discovery and polling.

For a live local test, run `just sim` and configure a connection at `127.0.0.1:5020` with a
device using register map `zelos_extension_modbus/demo/power_meter.json` (`config.json` is gitignored;
set it via the Zelos App config dialog or by hand).

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [Zelos SDK Guide](https://docs.zeloscloud.io/sdk)
- [Modbus Specification](https://modbus.org/specs.php)

## CLI Usage

For advanced command-line usage (tracing without the Zelos App), see [cli/README.md](cli/README.md).

## License

MIT License, see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
