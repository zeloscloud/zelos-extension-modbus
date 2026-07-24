"""Tests for the app-mode runner (zelos_extension_modbus/cli/app.py).

Covers the two seams that turn config.json into running clients:
- ``_create_clients`` threads interface config (including the block-read knobs
  and RTU serial fields) onto each ModbusClient.
- ``_load_register_map`` fails loud (sys.exit) on a broken/missing configured
  map rather than silently degrading to no-data.
"""

import json

import pytest

from zelos_extension_modbus.cli.app import _create_clients, _load_register_map


class TestCreateClients:
    """_create_clients builds ModbusClient instances from interface config."""

    def test_block_knobs_and_rtu_fields_threaded(self):
        """The three block knobs plus RTU serial fields land on the client."""
        config = {
            "interfaces": [
                {
                    "transport": "rtu",
                    "serial_port": "/dev/ttyUSB0",
                    "baudrate": 19200,
                    "parity": "E",
                    "stopbits": 2,
                    "bytesize": 7,
                    "unit_id": 7,
                    "timeout": 5.0,
                    "block_reads": False,
                    "max_block_size": 32,
                    "max_read_gap": 4,
                }
            ]
        }
        pairs = _create_clients(config)
        assert len(pairs) == 1
        client, _registry_name = pairs[0]

        # Block-read knobs
        assert client.block_reads is False
        assert client.max_block_size == 32
        assert client.max_read_gap == 4

        # RTU serial fields
        assert client.transport == "rtu"
        assert client.serial_port == "/dev/ttyUSB0"
        assert client.baudrate == 19200
        assert client.parity == "E"
        assert client.stopbits == 2
        assert client.bytesize == 7
        assert client.unit_id == 7
        assert client.timeout == 5.0

    def test_absent_keys_use_constructor_defaults(self):
        """Keys omitted from interface config fall back to ModbusClient defaults."""
        config = {"interfaces": [{"transport": "tcp", "host": "10.0.0.5"}]}
        pairs = _create_clients(config)
        client, _registry_name = pairs[0]

        assert client.host == "10.0.0.5"  # the one key we did set
        # Everything else is the constructor's default, not restated in app.py.
        assert client.port == 502
        assert client.unit_id == 1
        assert client.timeout == 3.0
        assert client.poll_interval == 1.0
        assert client.write_mode == "auto"
        assert client.block_reads is True
        assert client.max_block_size == 125
        assert client.max_read_gap == 0

    def test_no_interfaces_exits(self):
        """An empty interface list is a config error and exits."""
        with pytest.raises(SystemExit) as exc:
            _create_clients({"interfaces": []})
        assert exc.value.code == 1


class TestLoadRegisterMap:
    """_load_register_map: raw mode is opt-in, a broken configured map exits."""

    def test_empty_path_returns_none(self):
        """An unset/empty path is a deliberate raw-mode choice, not an error."""
        assert _load_register_map(None) is None
        assert _load_register_map("") is None

    def test_missing_file_exits(self):
        """A configured-but-missing map file exits rather than running mapless."""
        with pytest.raises(SystemExit) as exc:
            _load_register_map("/nonexistent/path/to/register_map.json")
        assert exc.value.code == 1

    def test_duplicate_name_map_exits(self, tmp_path):
        """A map that fails validation (duplicate field names) exits."""
        bad = tmp_path / "dup.json"
        bad.write_text(
            json.dumps(
                {"events": {"e": [{"name": "v", "address": 0}, {"name": "v", "address": 1}]}}
            )
        )
        with pytest.raises(SystemExit) as exc:
            _load_register_map(str(bad))
        assert exc.value.code == 1

    def test_valid_map_loads(self, tmp_path):
        """A well-formed configured map loads normally."""
        good = tmp_path / "ok.json"
        good.write_text(json.dumps({"events": {"e": [{"name": "v", "address": 0}]}}))
        reg_map = _load_register_map(str(good))
        assert reg_map is not None
        assert len(reg_map.registers) == 1
