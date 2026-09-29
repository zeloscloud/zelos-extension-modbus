"""Tests for the app-mode runner (zelos_extension_modbus/cli/app.py).

Covers the seams that turn config.json into running connections:
- ``_create_connections`` threads connection/device/advanced config onto each
  ModbusConnection and ModbusDevice, and rejects bad or duplicate names.
- ``_old_config_error`` flags a 0.1.x config.
- ``_load_register_map`` fails loud (sys.exit) on a broken/missing configured
  map rather than silently degrading to no-data.
"""

import asyncio
import json
from pathlib import Path

import pytest
from zelos_sdk.extensions.config import load_config

from zelos_extension_modbus.cli.app import (
    OLD_CONFIG_ERROR,
    _create_connections,
    _load_register_map,
    _old_config_error,
)

SCHEMA = Path(__file__).parent.parent / "config.schema.json"


def _tcp(**conn):
    """A one-device TCP connection config, overridable per key."""
    return {"transport": "tcp", "host": "10.0.0.5", "devices": [{}], **conn}


class TestCreateConnections:
    """_create_connections builds connections and their devices from config."""

    def test_fields_threaded(self):
        """RTU link fields and advanced knobs land on the connection; devices share it."""
        config = {
            "connections": [
                {
                    "transport": "rtu",
                    "serial_port": "/dev/ttyUSB0",
                    "baudrate": 19200,
                    "parity": "E",
                    "stopbits": 2,
                    "bytesize": 7,
                    "devices": [
                        {"unit_id": 7, "rate": 0.5},
                        {"unit_id": 8, "name": "b"},
                        {"unit_id": 9, "auto_scan": False},
                    ],
                }
            ]
        }
        advanced = {
            "timeout": 5.0,
            "retries": 2,
            "request_delay_ms": 20,
            "max_block_size": 32,
            "allow_raw_writes": True,
            "auto_scan_rate": 20.0,
        }
        (conn,) = _create_connections(config, advanced)

        assert (conn.name, conn.serial_port, conn.baudrate) == (
            "dev_ttyUSB0",
            "/dev/ttyUSB0",
            19200,
        )
        assert (conn.parity, conn.stopbits, conn.bytesize) == ("E", 2, 7)
        assert (conn.timeout, conn.retries, conn.request_delay_ms) == (5.0, 2, 20)
        assert [d.path for d in conn.devices] == [
            "dev_ttyUSB0/unit7",
            "dev_ttyUSB0/b",
            "dev_ttyUSB0/unit9",
        ]
        assert [d.unit_id for d in conn.devices] == [7, 8, 9]
        # No map: auto-scan at auto_scan_rate unless the device sets a rate or opts out.
        assert [d.rate for d in conn.devices] == [0.5, 20.0, 1.0]
        assert [d.scanning for d in conn.devices] == [True, True, False]
        assert all(d.max_block_size == 32 and d.connection is conn for d in conn.devices)
        assert all(d.allow_raw_writes for d in conn.devices)

    def test_absent_keys_use_constructor_defaults(self):
        """Keys omitted from config fall back to the constructor defaults."""
        (conn,) = _create_connections({"connections": [_tcp()]}, {})
        (dev,) = conn.devices
        assert (conn.name, conn.port, conn.timeout, conn.retries) == ("10_0_0_5", 502, 3.0, 1)
        assert (dev.name, dev.unit_id, dev.rate, dev.write_mode, dev.scanning) == (
            "unit1",
            1,
            10.0,
            "auto",
            True,
        )
        assert (dev.block_reads, dev.max_block_size, dev.max_read_gap) == (True, 125, 0)
        assert dev.allow_raw_writes is False

    def test_colliding_default_names_take_port(self):
        """Unnamed TCP connections to one host get `_<port>`; others keep their names."""
        config = {"connections": [_tcp(), _tcp(port=503), _tcp(host="10.0.0.6"), _tcp(name="x")]}
        names = [c.name for c in _create_connections(config, {})]
        assert names == ["10_0_0_5_502", "10_0_0_5_503", "10_0_0_6", "x"]

    @pytest.mark.parametrize(
        ("device", "advanced", "expected"),
        [
            ({"max_block_size": 20}, {"max_block_size": 30}, 20),
            ({}, {"max_block_size": 30}, 30),
            ({}, {}, 125),
        ],
        ids=["map-device", "advanced", "default"],
    )
    def test_setting_precedence(self, tmp_path, device, advanced, expected):
        """Per key: map device block > advanced > constructor default."""
        map_file = tmp_path / "map.json"
        map_file.write_text(json.dumps({"device": device, "events": {}}))
        config = {"connections": [_tcp(devices=[{"register_map_file": str(map_file)}])]}
        (conn,) = _create_connections(config, advanced)
        assert conn.devices[0].max_block_size == expected

    @pytest.mark.parametrize(
        ("connections", "message"),
        [
            ([], "No connections configured"),
            ([_tcp(devices=[])], "has no devices"),
            ([_tcp(name="a.b")], "'.' is not allowed"),
            ([_tcp(name="log")], "reserved"),
            ([_tcp(name="modbus_log")], "reserved"),
            ([_tcp(devices=[{"name": "x/y"}])], "'/' is not allowed"),
            ([_tcp(name="a"), _tcp(name="a", port=503)], "Duplicate connection name 'a'"),
            ([_tcp(devices=[{"unit_id": 2}, {"unit_id": 2, "name": "b"}])], "Duplicate unit ID 2"),
            (
                [_tcp(devices=[{"unit_id": 1}, {"unit_id": 2, "name": "unit1"}])],
                "Duplicate device name",
            ),
            (
                [_tcp(devices=[{"register_map": "sunspec", "register_map_file": "m.json"}])],
                "not both",
            ),
        ],
        ids=[
            "none",
            "no-devices",
            "bad-char",
            "reserved-log",
            "reserved-log-source",
            "bad-device-char",
            "dup-connection",
            "dup-unit",
            "dup-device-name",
            "two-map-sources",
        ],
    )
    def test_invalid_config_exits(self, caplog, tmp_path, connections, message):
        """Illegal names, duplicates or two map sources exit with one line naming the problem.

        Through the SDK's load_config: its defaults must not fill an empty list.
        """
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"connections": connections}))
        config = load_config(config_path=path, schema_path=SCHEMA)
        with pytest.raises(SystemExit) as exc:
            _create_connections(config, {})
        assert exc.value.code == 1
        assert message in caplog.text


@pytest.mark.parametrize(
    "config",
    [
        {"interfaces": [_tcp()]},
        {"log_level": "INFO", "connections": [_tcp()]},
        {"connections": [{"transport": "tcp", "host": "10.0.0.5"}]},
        {"connections": [_tcp(unit_id=1)]},
        {"connections": [_tcp(register_map_file="m.json")]},
        {"connections": [_tcp(timeout=3)]},
    ],
    ids=[
        "interfaces",
        "top-level-log_level",
        "no-devices",
        "unit_id",
        "register_map_file",
        "advanced-key",
    ],
)
def test_old_config_shape_is_an_error(config):
    """A 0.1.x config is a hard error with one actionable line."""
    assert _old_config_error(config) == OLD_CONFIG_ERROR
    assert _old_config_error({"connections": [_tcp()]}) is None


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
                {"events": {"e": [{"name": "v", "address": 1}, {"name": "v", "address": 2}]}}
            )
        )
        with pytest.raises(SystemExit) as exc:
            _load_register_map(str(bad))
        assert exc.value.code == 1

    def test_valid_map_loads(self, tmp_path):
        """A well-formed configured map loads normally."""
        good = tmp_path / "ok.json"
        good.write_text(json.dumps({"events": {"e": [{"name": "v", "address": 1}]}}))
        reg_map = _load_register_map(str(good))
        assert reg_map is not None
        assert len(reg_map.registers) == 1


def test_unreachable_server_at_start_exits(caplog):
    """A server absent at start stops the extension; retries start after first contact."""
    from conftest import free_port

    from zelos_extension_modbus.cli.app import run_connections
    from zelos_extension_modbus.client import ModbusConnection

    conn = ModbusConnection(name="gw", host="127.0.0.1", port=free_port(), timeout=0.5, retries=0)
    loop = asyncio.new_event_loop()  # private: later tests use the default loop
    with pytest.raises(SystemExit) as exc:
        loop.run_until_complete(run_connections([conn]))
    loop.close()
    assert exc.value.code == 1
    assert "Connection 'gw' (127.0.0.1:" in caplog.text and "cannot connect" in caplog.text
