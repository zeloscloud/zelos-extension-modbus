# CLAUDE.md

## Build & Development

```bash
just install      # Install deps + pre-commit hooks
just check        # Run ruff linter
just format       # Auto-format code
just test         # Run pytest (187 tests)
just package      # Build .tar.gz for marketplace
```

## Code Style

- **Linter**: ruff (strict, Python 3.11+, 100 char line length)
- **Pre-commit**: Runs ruff-check + ruff-format on commit
- Imports must be sorted (ruff handles this)

## Key Files

- `main.py` - CLI entry point (app mode, trace subcommand)
- `zelos_extension_modbus/client.py` - Modbus client with SDK actions (TCP + RTU)
- `zelos_extension_modbus/blocks.py` - Block-read planner (coalesce contiguous registers)
- `zelos_extension_modbus/register_map.py` - Register definitions and JSON parsing
- `scripts/xlsx_to_register_map.py` - Convert a register-map spreadsheet to JSON
- `zelos_extension_modbus/cli/app.py` - App mode runner (config loading, demo server)
- `zelos_extension_modbus/demo/simulator.py` - Power meter simulator for testing
- `config.schema.json` - Zelos App config UI (transport-dependent fields via oneOf)

## SDK Init Order (Critical)

Actions must be registered BEFORE `zelos_sdk.init()` — init advertises them to the agent:

```python
zelos_sdk.actions_registry.register(client)  # 1. Register actions
zelos_sdk.init(name="...", actions=True)      # 2. THEN init
handler = TraceLoggingHandler("...")           # 3. Logging handler after init
```

## Testing

Tests use a real TCP demo server for integration tests:

```bash
uv run pytest -v
```

## Demo Mode (Testing Only)

Demo mode is hidden from the Zelos App config UI. Use CLI only:

```bash
uv run main.py --demo
```
