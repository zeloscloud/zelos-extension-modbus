# Zelos Modbus

> Modbus TCP/RTU protocol extension for Zelos

A Zelos extension for reading, writing, and monitoring Modbus registers. Built with the [Zelos SDK](https://docs.zeloscloud.io/sdk).

## Features

- **Modbus TCP and RTU** transport support
- **All register types**: Holding registers, input registers, coils, discrete inputs
- **User-defined events** for semantic grouping of registers in the Zelos App
- **Real-time polling** with configurable intervals
- **Read/write actions** for interacting with registers from the Zelos App
- **Multiple data types**: uint16, int16, uint32, int32, float32, uint64, int64, float64, bool
- **Byte order options**: Big endian, little endian, and word-swapped variants for PLC compatibility
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
      {"name": "pcb_temp", "address": 123, "type": "holding", "datatype": "uint16", "unit": "°C", "scale": 0.1},
      {"name": "overtemp", "address": 456, "type": "coil"}
    ],
    "voltage/ac": [
      {"name": "phsA", "address": 0, "type": "holding", "datatype": "float32", "unit": "V"},
      {"name": "phsB", "address": 2, "type": "holding", "datatype": "float32", "unit": "V"}
    ],
    "setpoints": [
      {"name": "voltage_limit", "address": 100, "datatype": "uint16", "unit": "V", "writable": true},
      {"name": "calibration", "address": 110, "datatype": "float32", "byte_order": "big_swap"}
    ],
    "status": [
      {"name": "firmware", "address": 0, "type": "input", "datatype": "uint16"},
      {"name": "door_open", "address": 0, "type": "discrete_input"}
    ]
  }
}
```

This creates four Zelos events:
- `temperature` with fields `pcb_temp` and `overtemp`
- `voltage/ac` with fields `phsA` and `phsB`
- `setpoints` with writable registers and word-swapped byte order
- `status` with read-only input registers and discrete inputs

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
| `byte_order` | No | `big` | Byte order: `big`, `little`, `big_swap`, `little_swap` |
| `writable` | No | auto | Whether register can be written (defaults based on type) |
| `description` | No | `""` | Description for documentation |

**Note:** `writable` defaults to `true` for holding registers and coils, `false` for input registers and discrete inputs.

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

Multi-register values (32-bit and 64-bit types) can use different byte ordering depending on the device:

| Byte Order | Description | Example (0x12345678) |
|------------|-------------|----------------------|
| `big` | Big endian (default) | `[0x1234, 0x5678]` |
| `little` | Little endian | `[0x5678, 0x1234]` |
| `big_swap` | Big endian, word-swapped | `[0x5678, 0x1234]` |
| `little_swap` | Little endian, word-swapped | `[0x1234, 0x5678]` |

Word-swapped variants are common in Modicon/Schneider PLCs and some SCADA systems.

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
| **Write Named Register** | Write a value to a named register (holding/coil only) |
| **List Registers** | List all registers in the loaded map |
| **List Writable Registers** | List registers that can be written |

Write operations are only allowed on writable registers (holding registers and coils by default).

## Development

```bash
# Install dependencies
just install

# Run linting
just check

# Run tests
just test

# Run locally
just dev
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-modbus/issues)

## License

MIT License - see [LICENSE](LICENSE) for details.
