# CLAUDE.md

## Build & Development

```bash
just install      # Install deps + pre-commit hooks
just check        # Run ruff linter
just format       # Auto-format code
just test         # Run pytest
just package      # Build .tar.gz for marketplace
```

## Code Style

- **Linter**: ruff (strict, Python 3.11+, 100 char line length)
- **Pre-commit**: Runs ruff-check + ruff-format on commit
- Imports must be sorted (ruff handles this)

## Key Files

- `main.py` - CLI entry point (app mode, trace subcommand)
- `zelos_extension_modbus/client.py` - `ModbusConnection` (one link, serialized requests, per-connection scheduler: fastest-rate blocks + one slower block per tick) and `ModbusDevice` (unit ID + map + trace events, rate tiers, demotion, refused-block deactivation)
- `zelos_extension_modbus/constants.py` - `trace_layout` (the one trace-naming rule), `raw_names` (per-register raw events) and `name_error`
- `zelos_extension_modbus/scan.py` - Read-only scan and verify; `Discovery` drives its range finder one read per tick for auto-scan (`ModbusDevice._discover`)
- `zelos_extension_modbus/actions.py` - SDK actions, `Modbus/<action>` with a `<connection>/<device>` selector
- `zelos_extension_modbus/blocks.py` - Block-read planner (coalesce contiguous registers)
- `zelos_extension_modbus/serial_diag.py` - RTU connect-failure diagnostics (stale node, perms, holders)
- `zelos_extension_modbus/register_map.py` - Register definitions and JSON parsing
- `zelos_extension_modbus/sunspec.py` - SunSpec discovery: model chain -> RegisterMap (`"register_map": "sunspec"`)
- `zelos_extension_modbus/demo/sunspec_sim.py` - SunSpec simulator (models 1, 103, 160)
- `scripts/xlsx_to_register_map.py` - Convert a register-map spreadsheet to JSON
- `zelos_extension_modbus/cli/app.py` - App mode runner (config loading, demo server)
- `zelos_extension_modbus/demo/simulator.py` - Power meter simulator for testing
- `config.schema.json` - Zelos App config UI: `connections[]` (transport fields via oneOf) -> `devices[]`; global tuning under `advanced`, resolved in `cli/app.py`

## SDK Init Order (Critical)

Actions must be registered BEFORE `zelos_sdk.init()`: init advertises them to the agent:

```python
actions.register_all()  # 1. Register actions
init_sdk(prefix)        # 2. THEN init (cli/app.py: global source, init, log handler)
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
