# Zelos Modbus

A Zelos extension implementing Modbus TCP/RTU protocol for reading, writing, and monitoring registers. Built with the [Zelos SDK](https://docs.zeloscloud.io/sdk).

## Features

- **Modbus TCP/RTU** - Both transport types supported
- **All Register Types** - Holding, input, coils, discrete inputs
- **User-Defined Events** - Group registers semantically for Zelos App
- **Read/Write Actions** - Interactive register access from Zelos App
- **Flexible Data Types** - 16/32/64-bit integers, floats, booleans
- **Byte Order Options** - Big/little endian with word-swap variants
- **Demo Mode** - Built-in power meter simulator

## Quick Start

```bash
# Demo mode (no hardware required)
uv run main.py demo

# TCP connection
uv run main.py trace 192.168.1.100 registers.json

# RTU serial
uv run main.py trace /dev/ttyUSB0 registers.json --transport rtu --baudrate 19200
```

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `transport` | string | `tcp` | `tcp` or `rtu` |
| `host` | string | `127.0.0.1` | TCP host address |
| `port` | int | `502` | TCP port |
| `serial_port` | string | `/dev/ttyUSB0` | Serial port for RTU |
| `baudrate` | int | `9600` | Serial baudrate |
| `unit_id` | int | `1` | Modbus unit/slave ID |
| `poll_interval` | float | `1.0` | Polling interval (seconds) |
| `timeout` | float | `3.0` | Request timeout (seconds) |
| `register_map_file` | string | - | Path to register map JSON |

## Register Map

Events group registers semantically. Event names become Zelos trace events.

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
| `name` | Yes | - | Field name in Zelos event |
| `address` | Yes | - | Register address (0-65535) |
| `type` | No | `holding` | `holding`, `input`, `coil`, `discrete_input` |
| `datatype` | No | `uint16` | See data types below |
| `unit` | No | - | Display unit |
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

| Order | Registers for 0x12345678 |
|-------|--------------------------|
| `big` | `[0x1234, 0x5678]` |
| `little` | `[0x5678, 0x1234]` |
| `big_swap` | `[0x5678, 0x1234]` |
| `little_swap` | `[0x1234, 0x5678]` |

Word-swapped variants common in Modicon/Schneider PLCs.

## Actions

| Action | Description |
|--------|-------------|
| Get Status | Connection status and statistics |
| Read Register | Read by address |
| Write Register | Write to address (holding only) |
| Read Named | Read by register name |
| Write Named | Write by register name |
| Write Coil | Write boolean to coil |
| List Registers | Show all mapped registers |
| List Writable | Show writable registers |

## Development

```bash
just install   # Install dependencies
just check     # Run linting
just test      # Run tests
just dev       # Run locally
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [Zelos SDK Guide](https://docs.zeloscloud.io/sdk)
- [Modbus Specification](https://modbus.org/specs.php)

## License

MIT License - see [LICENSE](LICENSE) for details.
