# Zelos Modbus

A Zelos extension for the Modbus protocol. Read, write, and monitor registers from PLCs, power meters, generators, sensors, and other Modbus devices over TCP or RS232/RS485 serial.

## Features

- 📡 **Modbus TCP & RTU** — Connect over Ethernet or RS232/RS485 serial
- 📊 **All register types** — Holding, input, coils, and discrete inputs
- 📄 **Register map files** — Define your device layout in a simple JSON file
- ✏️ **Read & write actions** — Interactive register access from the Zelos App
- 🔢 **Flexible data types** — 16/32/64-bit integers, floats, booleans
- 🔄 **Byte order options** — Big/little endian with word-swap variants

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** your transport (TCP or RTU), connection settings, and upload a register map file
3. **Start** the extension to begin streaming data
4. **View** real-time register values in your Zelos App

## Configuration

All configuration is managed through the Zelos App settings interface.

### Common Settings

- **Transport** — `tcp` or `rtu` (determines which connection fields appear)
- **Unit ID** — Modbus slave/unit ID (default: `1`)
- **Register Map File** — Path to a JSON register map file (`.json`)
- **Poll Interval** — How often to poll registers (default: `1.0s`)
- **Timeout** — Modbus request timeout (default: `3.0s`)
- **Log Level** — Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`)

### TCP Settings (when transport = tcp)

- **Host** — Modbus TCP host address (e.g. `192.168.1.100`)
- **Port** — Modbus TCP port (default: `502`)

### RTU Serial Settings (when transport = rtu)

- **Serial Port** — Device path (e.g. `/dev/ttyUSB0`, `COM3`)
- **Baudrate** — Serial baudrate (default: `9600`)
- **Parity** — None, Even, or Odd (default: `None`)
- **Stop Bits** — 1 or 2 (default: `1`)
- **Data Bits** — 7 or 8 (default: `8`)

## Register Map

A register map file defines which registers to read and how to decode them. Event names become Zelos trace events, and register names become fields within those events.

```json
{
  "name": "power_meter",
  "events": {
    "voltage": [
      {"name": "L1", "address": 0, "datatype": "float32", "unit": "V"},
      {"name": "L2", "address": 2, "datatype": "float32", "unit": "V"}
    ],
    "setpoints": [
      {"name": "limit", "address": 100, "datatype": "uint16", "writable": true}
    ],
    "status": [
      {"name": "firmware", "address": 0, "type": "input"},
      {"name": "door_open", "address": 0, "type": "discrete_input"}
    ]
  }
}
```

### Register Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Field name in Zelos event |
| `address` | Yes | — | Register address (0–65535) |
| `type` | No | `holding` | `holding`, `input`, `coil`, `discrete_input` |
| `datatype` | No | `uint16` | See data types below |
| `unit` | No | — | Display unit |
| `scale` | No | `1.0` | Scale factor |
| `byte_order` | No | `big` | `big`, `little`, `big_swap`, `little_swap` |
| `writable` | No | auto | Override write permission |

### Data Types

| Type | Registers | Type | Registers |
|------|-----------|------|-----------|
| `bool` | 1 | `uint32` | 2 |
| `uint16` | 1 | `int32` | 2 |
| `int16` | 1 | `float32` | 2 |
| `uint64` | 4 | `int64` | 4 |
| `float64` | 4 | | |

### Byte Order

| Order | Description | Common in |
|-------|-------------|-----------|
| `big` | Standard Modbus (AB CD) | Most devices |
| `little` | Full little endian (DC BA) | — |
| `big_swap` | Word-swapped (CD AB) | Modicon/Schneider PLCs |
| `little_swap` | Little + word-swapped (BA DC) | — |

## Actions

The extension provides actions accessible from the Zelos App:

- **Get Status** — Connection status and polling statistics
- **Read Register** — Read a register by address
- **Write Register** — Write to a holding register by address
- **Read Named Register** — Read a register by name from the map
- **Write Named Register** — Write a register by name from the map
- **Write Coil** — Write a boolean to a coil address
- **List Registers** — Show all mapped registers
- **List Writable Registers** — Show writable registers only

## Development

```bash
just install   # Install dependencies
just check     # Run linting
just format    # Auto-format code
just test      # Run tests
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [Zelos SDK Guide](https://docs.zeloscloud.io/sdk)
- [Modbus Specification](https://modbus.org/specs.php)

## CLI Usage

For advanced command-line usage (tracing without the Zelos App), see [cli/README.md](cli/README.md).

## License

MIT License — see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
