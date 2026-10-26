"""Zelos Modbus Extension entry (extension.toml `agent.entry`).

The CLI lives in `zelos_extension_modbus.cli.main`, so the installed
`zelos-extension-modbus` script and `python main.py` run the same commands.
"""

from zelos_extension_modbus.cli.main import cli

if __name__ == "__main__":
    cli()
