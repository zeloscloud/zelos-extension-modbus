# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path                                | Purpose                                                    |
| ----------------------------------- | ---------------------------------------------------------- |
| `main.py`                           | Entry point; runs the CLI in `zelos_extension_modbus/cli/` |
| `zelos_extension_modbus/client.py`  | One link per connection and one device per unit ID         |
| `zelos_extension_modbus/actions.py` | The Modbus actions                                         |
| `zelos_extension_modbus/scan.py`    | Read-only device scan and register-map verification        |
| `zelos_extension_modbus/sunspec.py` | SunSpec discovery                                          |
| `zelos_extension_modbus/demo/`      | Power-meter, scan-target, SunSpec and mock servers         |
| `scripts/xlsx_to_register_map.py`   | Convert a register-map spreadsheet to JSON                 |
| `examples/`                         | Example register maps                                      |

`cli/README.md` documents the `trace` command. `DEBUG.md` covers RTU serial failures, and `SUNSPEC.md` covers SunSpec discovery.

## Commands

| Command                | Description                                       |
| ---------------------- | ------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks     |
| `just format`          | Format code with ruff                             |
| `just check`           | Check formatting and lint with ruff               |
| `just test`            | Run tests (pytest)                                |
| `just package`         | Package for the Zelos marketplace                 |
| `just release VERSION` | Bump version, check, test, commit, tag            |
| `just clean`           | Remove build output, caches and packages          |
| `just run`             | Run the extension against `./config.json`         |
| `just sim`             | Run the power-meter simulator on `127.0.0.1:5020` |

`just --list` also shows the other recipes. `just install` installs the pre-commit hooks in
`.pre-commit-config.yaml`.

## Simulators

`just sim` serves a simulated power meter that answers every unit ID.
Run `uv run python main.py demo-server --help` for more units, a serial port, or the scan target.
To serve any register map, run the mock server:

```bash
uv run python main.py mock-server examples/example_registers.json --port 5020
```

## Local run

`just run` reads `./config.json`, and it needs at least one connection. Start the simulator in one terminal:

```bash
just sim
```

Then run the extension in another:

```bash
echo '{"connections": [{"transport": "tcp", "host": "127.0.0.1", "port": 5020, "devices": [{"unit_id": 1}]}]}' > config.json
just run
```

`just run` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`.
A device with no register map scans the simulator and polls what it finds.
`uv run python main.py --demo` starts the simulator and a connection to it in one process.

## Run it in Zelos

```bash
zelos extensions install-local .
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall <id>`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, checks that the archive's
`actions.json` lists the four standalone actions (`auto_config`, `list_serial_ports`,
`scan_device` and `verify_map`), then attaches the archive to a GitHub release.
