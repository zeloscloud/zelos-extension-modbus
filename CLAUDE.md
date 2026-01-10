# CLAUDE.md

## Build & Development

```bash
just install      # Install deps + pre-commit hooks
just check        # Run ruff linter
just format       # Auto-format code
just test         # Run pytest
just dev          # Run extension locally
```

## Demo Mode

```bash
uv run main.py demo    # Start with simulated power meter
```

## Code Style

- **Linter**: ruff (strict)
- **Pre-commit**: Runs ruff-check + ruff-format on commit
- Use `contextlib.suppress(Exception)` instead of `try: ... except: pass`
- Imports must be sorted (ruff handles this)

## Key Files

- `zelos_extension_modbus/client.py` - Modbus client with Zelos SDK actions
- `zelos_extension_modbus/register_map.py` - Register definitions
- `zelos_extension_modbus/demo/simulator.py` - Demo server
- `tests/test_modbus.py` - All tests (unit + integration)

## Testing

Tests use a real TCP server (demo mode) for integration tests. All 78 tests should pass:

```bash
uv run pytest -v
```
