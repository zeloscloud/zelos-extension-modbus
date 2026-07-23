# Modbus CLI Usage

The extension includes a command-line interface for tracing Modbus registers without the Zelos App.

## Trace

```bash
# TCP with register map
uv run main.py trace 192.168.1.100 registers.json

# TCP with custom port and unit ID
uv run main.py trace 192.168.1.100 registers.json --port 5020 --unit-id 2

# RTU serial (RS232/RS485)
uv run main.py trace /dev/ttyUSB0 registers.json -t rtu -b 9600

# RTU with full serial config (e.g. 9600 8N1)
uv run main.py trace /dev/ttyUSB0 registers.json -t rtu -b 9600 --parity N --stopbits 1 --bytesize 8

# TCP without register map (raw address mode)
uv run main.py trace 192.168.1.100
```

### Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--transport` | `-t` | `tcp` | Transport type (`tcp` or `rtu`) |
| `--port` | `-p` | `502` | TCP port |
| `--baudrate` | `-b` | `9600` | Serial baudrate (RTU only) |
| `--parity` | | `N` | Serial parity: `N` (none), `E` (even), `O` (odd) |
| `--stopbits` | | `1` | Stop bits: `1` or `2` |
| `--bytesize` | | `8` | Data bits: `7` or `8` |
| `--unit-id` | `-u` | `1` | Modbus slave/unit ID |
| `--interval` | `-i` | `1.0` | Poll interval in seconds |
| `--timeout` | | `3.0` | Request timeout in seconds |
| `--block-reads` / `--no-block-reads` | | on | Coalesce contiguous registers into range reads |
| `--max-block-size` | | `125` | Max registers per range read (1–125) |
| `--max-read-gap` | | `0` | Max uncovered registers to bridge within a block |
