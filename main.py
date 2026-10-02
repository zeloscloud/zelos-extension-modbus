#!/usr/bin/env python3
"""Zelos Modbus Extension entry (extension.toml `runtime.entry`).

The CLI lives in `zelos_extension_modbus.cli.main`, so the installed
`zelos-extension-modbus` script and `python main.py` run the same commands.
"""

# ACTION_PREFIX and the actions import are read by the SDK's at-rest harness,
# which imports this entry to find the standalone actions.
from zelos_extension_modbus import ACTION_PREFIX, actions  # noqa: F401
from zelos_extension_modbus.cli.main import cli

if __name__ == "__main__":
    cli()
