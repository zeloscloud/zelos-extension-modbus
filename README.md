# Zelos Modbus

> Modbus TCP/RTU protocol extension for Zelos

A Zelos extension for reading, writing, and monitoring Modbus registers. Built with the [Zelos SDK](https://docs.zeloscloud.io/sdk).

## Features

- **Modbus TCP and RTU** transport support
- **All register types**: Holding registers, input registers, coils, discrete inputs
- **User-defined events** for semantic grouping of registers in the Zelos App
- **Real-time polling** with configurable intervals
- **Read and write actions** for interacting with registers from the Zelos App
- **Multiple data types**: uint16, int16, uint32, int32, float32, uint64, int64, float64, bool
- **Byte order options**: Support for big endian, little endian, and word-swapped formats
- **Demo mode**: Built-in power meter simulator for testing without hardware

## Quick Start

### Demo Mode (No Hardware Required)

```bash
# Run with simulated 3-phase power meter
uv run main.py demo
```

Demo mode starts a local Modbus TCP server with a simulated power meter that generates realistic data:
- 3-phase voltage and current
- Total power, power factor, frequency
- Energy accumulator
- Temperature and relay outputs

### CLI Mode

```bash
# TCP connection with register map
uv run main.py trace 192.168.1.100 examples/example_registers.json

# TCP with custom port
uv run main.py trace 192.168.1.100 registers.json --port 5020

# RTU serial connection
uv run main.py trace /dev/ttyUSB0 registers.json --transport rtu --baudrate 19200

# Without register map (raw address mode)
uv run main.py trace 192.168.1.100
```

### From Zelos App

Configure the extension through the Zelos App UI. Enable "Demo Mode" to use the built-in simulator, or configure your Modbus connection settings.

## Register Map Format

The register map uses **user-defined events** to group registers semantically. Event names become Zelos trace events, and register names become fields within those events.

```json
{
  "name": "my_device",
  "events": {
    "temperature": [
      {"name": "pcb_temp", "address": 123, "datatype": "uint16", "unit": "°C", "scale": 0.1},
      {"name": "overtemp", "address": 456, "type": "coil"}
    ],
    "voltage/ac": [
      {"name": "phsA", "address": 0, "datatype": "float32", "unit": "V", "byte_order": "big_swap"},
      {"name": "phsB", "address": 2, "datatype": "float32", "unit": "V", "byte_order": "big_swap"}
    ],
    "setpoints": [
      {"name": "target_temp", "address": 100, "datatype": "uint16", "unit": "°C", "scale": 0.1},
      {"name": "enable", "address": 50, "type": "coil"}
    ]
  }
}
```

This creates three Zelos events:
- `temperature` with fields `pcb_temp` (read-only input) and `overtemp` (coil)
- `voltage/ac` with fields `phsA` and `phsB` (word-swapped floats)
- `setpoints` with writable fields `target_temp` and `enable`

Registers of different Modbus types (holding, coil, input, discrete_input) can be mixed within the same event.

### Register Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | - | Field name in Zelos event |
| `address` | Yes | - | Modbus register address (0-65535) |
| `type` | No | `holding` | Modbus type: `holding`, `input`, `coil`, `discrete_input` |
| `datatype` | No | `uint16` | Data type (see below) |
| `unit` | No | `""` | Unit string for display |
| `scale` | No | `1.0` | Scale factor applied to value |
| `byte_order` | No | `big` | Byte order for multi-register types (see below) |
| `writable` | No | auto | Override write permission (auto-detected from type) |
| `description` | No | `""` | Description for documentation |

### Supported Data Types

| Type | Registers | Description |
|------|-----------|-------------|
| `bool` | 1 | Boolean (for coils/discrete inputs) |
| `uint16` | 1 | Unsigned 16-bit integer |
| `int16` | 1 | Signed 16-bit integer |
| `uint32` | 2 | Unsigned 32-bit integer |
| `int32` | 2 | Signed 32-bit integer |
| `float32` | 2 | IEEE 754 32-bit float |
| `uint64` | 4 | Unsigned 64-bit integer |
| `int64` | 4 | Signed 64-bit integer |
| `float64` | 4 | IEEE 754 64-bit float |

### Byte Order

Different Modbus devices use different byte orderings for multi-register values (32-bit and 64-bit types). The `byte_order` field controls how registers are interpreted:

| Byte Order | Description | Common Usage |
|------------|-------------|--------------|
| `big` | Standard big-endian (AB CD) | Modbus default |
| `little` | Little-endian (DC BA) | Some embedded devices |
| `big_swap` | Big-endian with word swap (CD AB) | Many PLCs (Siemens, Allen-Bradley) |
| `little_swap` | Little-endian with word swap (BA DC) | Rare |

**Example**: A float32 value `3.14` stored as bytes `40 48 F5 C3`:
- `big`: registers = `[0x4048, 0xF5C3]`
- `big_swap`: registers = `[0xF5C3, 0x4048]`

If you're seeing incorrect values for float32 or uint32 registers, try `big_swap` - it's the most common non-standard format.

### Write Permissions

By default, register writability is determined by Modbus type:
- **Writable**: `holding`, `coil`
- **Read-only**: `input`, `discrete_input`

You can override this with the `writable` field:
```json
{"name": "locked_setting", "address": 100, "type": "holding", "writable": false}
```

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `transport` | string | `tcp` | Transport type: `tcp` or `rtu` |
| `host` | string | `127.0.0.1` | TCP host address |
| `port` | integer | `502` | TCP port |
| `serial_port` | string | `/dev/ttyUSB0` | Serial port for RTU |
| `baudrate` | integer | `9600` | Serial baudrate for RTU |
| `unit_id` | integer | `1` | Modbus unit/slave ID |
| `poll_interval` | number | `1.0` | Polling interval in seconds |
| `timeout` | number | `3.0` | Request timeout in seconds |
| `register_map_file` | string | - | Path to JSON register map |
| `log_level` | string | `INFO` | Logging level |

## Actions

The extension provides these interactive actions in the Zelos App:

| Action | Description |
|--------|-------------|
| **Get Status** | View connection status and polling statistics |
| **Read Register** | Read register(s) by address |
| **Write Register** | Write a value to a holding register by address |
| **Read Named Register** | Read a register by name from the map |
| **Write Named Register** | Write a value to a register by name |
| **Write Coil** | Write ON/OFF to a coil by address |
| **List Registers** | List all registers in the loaded map |
| **List Writable Registers** | List only writable registers |

## Roadmap

### High Priority
- [ ] **RTU serial support** - Full RS-485/RS-232 serial transport (TCP implemented)
- [ ] **Multiple unit IDs** - Poll multiple devices on same connection
- [ ] **Reconnection logic** - Auto-reconnect with exponential backoff

### Medium Priority
- [ ] **Bit field extraction** - Parse individual bits from status word registers
- [ ] **String/ASCII registers** - Decode multi-register ASCII strings
- [ ] **Communication diagnostics** - Latency stats, error rates, exception logging

### Future
- [ ] **Zelos replay support** - Record/replay Modbus traffic for offline analysis
- [ ] **Trigger-based polling** - Poll on Zelos events rather than fixed interval
- [ ] **Import helpers** - Parse register lists from PLC/SCADA export formats

## Development

```bash
# Install dependencies
uv sync --extra dev

# Run linting
uv run ruff check .

# Run tests
uv run pytest tests/

# Run demo mode
uv run main.py demo
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-modbus/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
