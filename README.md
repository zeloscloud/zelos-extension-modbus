# Zelos Modbus

A Zelos extension for the Modbus protocol. Read, write, and monitor registers from PLCs, power meters, sensors, and other Modbus devices over TCP or RS232/RS485 serial.

## Features

- 📡 **Modbus TCP & RTU**: Connect over Ethernet or RS232/RS485 serial
- 📊 **All register types**: Holding, input, coils, and discrete inputs
- 📄 **Register map files**: Define your device layout in a simple JSON file
- ☀️ **SunSpec discovery**: Inverters, meters and batteries map themselves at connect ([SUNSPEC.md](SUNSPEC.md))
- 🔍 **Auto-scan**: A device without a map finds its registers at start and streams them
- ✏️ **Read & write actions**: Interactive register access from the Zelos App
- 🔢 **Flexible data types**: 16/32/64-bit integers, floats, booleans
- 🔄 **Byte order options**: Big/little endian with word-swap variants

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** a connection (TCP or RTU) and its devices (unit ID; a register map file, SunSpec, or nothing: auto-scan)
3. **Start** the extension to begin streaming data
4. **View** real-time register values in your Zelos App

## Configuration

### Connections

A connection is a TCP endpoint or a serial port. Its devices share the link and are polled one request at a time.

| Setting | Default | Description |
|---------|---------|-------------|
| Transport | `tcp` | `tcp` or `rtu` |
| Name | endpoint | Trace name; defaults to the sanitized host or serial port (`10_0_0_5`, `dev_ttyUSB0`) |
| Host / Port | `127.0.0.1` / `502` | TCP only |
| Serial Port | | RTU only (`/dev/ttyUSB0`, `COM3`) |
| Baudrate / Parity / Stop Bits / Data Bits | `9600` / `N` / `1` / `8` | RTU only; must match the devices |
| Devices | | One or more, below |

### Devices

| Setting | Default | Description |
|---------|---------|-------------|
| Unit ID | `1` | Modbus unit ID; unique per connection |
| Name | `unit<ID>` | Trace name; unique per connection |
| Register Map | `file` | `file` or `sunspec` (see [SUNSPEC.md](SUNSPEC.md)) |
| Register Map File | | JSON [register map](#register-map), path or inline; empty = [auto-scan](#auto-scan) |
| Auto-scan | on | Without a map file, discover and poll the device's registers; off = nothing polled |
| Rate | TCP 1 s / RTU 10 s | Poll rate (s) for registers without their own `rate`; `0` = not polled |

Names are letters, digits, space, `_` or `-`.

### Advanced

Applies to every connection and device; a register map `device` block overrides it per key.

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `Modbus` | Trace source: `Modbus/10_0_0_5/unit1/voltage`; cleared = one source per connection |
| `default_rate` | TCP `1.0`, RTU `10.0` | Poll rate (s) when neither register nor device sets one |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `timeout` | `3.0` | Seconds to wait for each response |
| `retries` | `1` | Extra attempts per request |
| `request_delay_ms` | `0` | Minimum gap between requests (RS485 gateways, slow RTU devices) |
| `connect_delay_ms` | `0` | Pause after each (re)connect |
| `demote_after` | `3` | Consecutive timeouts before a device is demoted |
| `demote_max_s` | `300` | Longest wait between probes of a demoted device |
| `block_reads` | on | Coalesce contiguous registers into range reads |
| `max_block_size` | `125` | Max registers per range read |
| `max_bit_block_size` | `2000` | Max coils / discrete inputs per range read |
| `max_read_gap` | `0` | Max unmapped registers bridged within a block |
| `write_mode` | `auto` | `auto` (FC 6 single / FC 16 multi) or `fc16` |
| `allow_raw_writes` | off | Enable the raw write actions; read-only map addresses stay refused |

Serial/USB troubleshooting: [DEBUG.md](DEBUG.md).

### Polling

Rate precedence: register `rate` > device Rate > `default_rate`; a map `min_rate` floors it.

| Condition | Behavior |
|-----------|----------|
| Rate not met | `get_status` reports requested vs achieved rate; over 100% for 30 s warns once |
| Device not answering | Demoted after `demote_after` timeouts: skipped, probed from 10 s doubling to `demote_max_s` |
| Link down | Reconnect from 3 s doubling to 60 s |
| Unreachable at start | Extension stops with an ERROR naming the connection |
| Exception 02/03 | Block skipped, retried every 10 min; `verify` finds the bad registers |

### Auto-scan

A device without a register map file discovers its registers at start (reads only) and polls each as it is found, as a [raw register](#raw-registers). It reruns on every start; `save_map` writes the finds as a map file. Each register is its own trace event (~0.75 MB each), so save and trim the map for large devices.

### Raw registers

Unmapped registers trace one event per register: holding register 123 is `holding_registers/123`, field `hr_123` (likewise `input_registers/` `ir_`, `coils/` `coil_`, `discrete_inputs/` `di_`), as raw uint16 or bool.

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

`register_map_file` is a path on the agent's host, or the map JSON inline (starting with `{`).

### Addressing

Addresses are 1-based, as in vendor sheets: holding register `40001` is sent as wire address 40000. A map numbered from wire 0 sets `"device": {"address_base": 0}`.

### Register Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `address` | Yes | | Register address |
| `name` | No | `r<address>` | Field name, unique per event |
| `type` | No | `holding` | `holding`, `input`, `coil`, `discrete_input` |
| `datatype` | No | `uint16` | See [Data Types](#data-types) |
| `unit` | No | | Display unit |
| `scale` | No | `1.0` | Scale factor; a scaled integer decodes to a float |
| `rate` | No | device rate | Poll rate (s); `0` = not polled |
| `byte_order` | No | `big` | See [Byte Order](#byte-order) |
| `writable` | No | `false` | Allow writes (holding registers and coils) |
| `length` | strings | | Registers a `string` spans |
| `scale_ref` | No | | Integer register in the same event holding a power-of-10 exponent |
| `invalid` | No | | Raw values meaning "not implemented", logged as null |
| `values` | No | | Enum labels, `{"0": "off", "1": "on"}` |

An optional top-level `device` block carries device quirks, e.g. `"device": {"max_block_size": 60, "byte_order": "big_swap"}`:

| Key | Description |
|-----|-------------|
| `max_block_size`, `max_bit_block_size`, `max_read_gap`, `write_mode` | Override Advanced |
| `byte_order` | Default for registers without one |
| `min_rate` | Floor (s) on every register's rate |
| `close_after_sweep` | Close the connection between sweeps (single-connection devices) |
| `address_base` | `1` (default) or `0` |

### Data Types

| Type | Registers | Type | Registers |
|------|-----------|------|-----------|
| `bool` | 1 | `uint32` | 2 |
| `uint16` | 1 | `int32` | 2 |
| `int16` | 1 | `float32` | 2 |
| `uint64` | 4 | `int64` | 4 |
| `float64` | 4 | `string` | `length` |

Strings are ASCII, 2 bytes per register, read-only.

### Byte Order

Wire order of a 32-bit value, A = most significant byte.

| Order | Wire bytes | Common in |
|-------|------------|-----------|
| `big` | AB CD | Most devices |
| `little` | DC BA | |
| `big_swap` | CD AB | Modicon/Schneider PLCs |
| `little_swap` | BA DC | |

## Scan

Point scan at an unknown device to find its unit IDs, identity, valid address ranges, and a draft register map. Scan only reads (FC 01-04, 43/14, 17). Some devices clear latched alarms or counters on read; review before scanning production equipment.

```bash
uv run main.py scan 192.168.1.100 --out draft.json         # JSON report on stdout
uv run main.py scan /dev/ttyUSB0 -t rtu --autodetect --units 1-10
uv run main.py verify 192.168.1.100 registers.json --unit 3  # exits 1 on any problem
```

The draft lists every readable address as a raw `uint16` / `bool`, read-only; set datatypes, byte order and scaling from the datasheet.

With the extension stopped, the same runs as actions:

| Action | Description |
|--------|-------------|
| `auto_config` | The config form's Auto-configure: finds units on each connection and adds them, SunSpec where detected |
| `scan_device` | Full scan of a host or serial port; slow |
| `verify_map` | Check a map file against the device register by register |
| `list_serial_ports` | Serial ports on the agent's machine |

## Actions

Actions are available from the Zelos App and to app extensions as `Modbus/<action>`. Every action but `list_devices` takes a `device` selector, `<connection>/<device>` (e.g. `10_0_0_5/unit1`).

| Action | Description |
|--------|-------------|
| `list_devices` | Every device with its endpoint, trace path, map, and health |
| `get_status` | Connection health, read counters, requested vs achieved rate |
| `get_snapshot` | Last polled value per `event/name`, no device I/O |
| `read_register` | Read raw registers or bits by address, type, and count |
| `write_single_register` | Write one holding register (FC 6) |
| `write_registers` | Write 1-123 holding registers (FC 16) |
| `write_coil` | Write `ON`/`OFF` to a coil (FC 5) |
| `read_named_register` | Read a mapped register by `event/name` |
| `write_named_register` | Write a mapped register by `event/name`, in engineering units |
| `list_registers` | Register catalog: path, address, datatype, scale, unit, rate |
| `list_writable_registers` | Same catalog, writable registers only |
| `save_map` | Write the device's current map (loaded or auto-scanned) to a JSON file |

Raw writes need `allow_raw_writes` and never touch an address the map marks read-only. Every write returns `outcome`: `ok`, `refused` (not written), or `unknown` (no response: the write may have landed, read back before retrying).

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
