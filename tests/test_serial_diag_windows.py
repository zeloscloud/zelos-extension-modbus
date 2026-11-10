"""Windows COM port diagnosis at the diagnose_serial_port seam (runs on any OS)."""

import pytest
from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

from zelos_extension_modbus import serial_diag


def _port(device, description):
    info = ListPortInfo(device, skip_link_detection=True)
    info.description = description
    return info


@pytest.mark.parametrize(
    ("port", "open_error", "expected"),
    [
        (
            "COM9",
            None,
            [
                "COM9 not found",
                "COM ports present: COM1 (Communications Port), USB Serial Device (COM4)",
            ],
        ),
        (
            "COM4",
            PermissionError,
            ["COM4 is in use by another program (Windows opens a COM port for one at a time)"],
        ),
        (r"\\.\com4", None, ["port opens cleanly — likely protocol/wiring/unit-id, not the port"]),
    ],
    ids=["missing", "busy", "opens"],
)
def test_com_port_findings(monkeypatch, port, open_error, expected):
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [_port("COM4", "USB Serial Device (COM4)"), _port("COM1", "Communications Port")],
    )
    monkeypatch.setattr(serial_diag.sys, "platform", "win32")

    def fake_open(path, flags):
        assert path == r"\\.\COM4"
        if open_error:
            raise open_error
        return -1

    monkeypatch.setattr(serial_diag.os, "open", fake_open)
    monkeypatch.setattr(serial_diag.os, "close", lambda fd: None)
    assert serial_diag.diagnose_serial_port(port) == [*expected, serial_diag._DEBUG_LINK]
