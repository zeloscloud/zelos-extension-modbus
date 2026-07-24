"""Tests for proactive serial-port diagnostics (serial_diag.py + client hook).

All probes are exercised at the helper seam ``diagnose_serial_port`` with
injected ``dev_root``/``sys_root`` tmp dirs so no real hardware is touched.
Linux-only signals (/proc, /sys) are guarded with ``sys.platform`` checks; on
other platforms the assertions verify graceful degradation, not absence of
output.
"""

import asyncio
import os
import pty
import sys

import pytest

from zelos_extension_modbus import client as client_module
from zelos_extension_modbus.client import ModbusClient
from zelos_extension_modbus.serial_diag import diagnose_serial_port


class TestNodeExistence:
    """Check 1: a missing node surfaces re-enumerated siblings."""

    def test_missing_node_lists_candidates(self, tmp_path):
        """An absent port names existing serial candidates in the fake dev_root."""
        dev_root = tmp_path / "dev"
        dev_root.mkdir()
        (dev_root / "ttyUSB10").write_text("")  # a re-enumerated sibling
        port = str(dev_root / "ttyUSB9")  # does not exist

        findings = diagnose_serial_port(
            port, dev_root=str(dev_root), sys_root=str(tmp_path / "sys")
        )

        assert any("node missing" in f for f in findings), findings
        assert any("ttyUSB10" in f for f in findings), findings


class TestOpenProbe:
    """Check 2: OS open with errno differentiation."""

    def test_eacces_names_group_and_mode(self, tmp_path):
        """A 000 node yields a permission finding naming its group and mode."""
        if os.getuid() == 0:
            pytest.skip("root bypasses permission bits")
        import grp

        dev_root = tmp_path / "dev"
        dev_root.mkdir()
        node = dev_root / "ttyUSB0"
        node.write_text("")
        node.chmod(0o000)

        findings = diagnose_serial_port(
            str(node), dev_root=str(dev_root), sys_root=str(tmp_path / "sys")
        )

        perm = [f for f in findings if "EACCES" in f]
        assert perm, findings
        group = grp.getgrgid(node.stat().st_gid).gr_name
        assert group in perm[0]
        assert "---" in perm[0]  # a mode string like '----------'

    def test_success_reports_clean_open(self, tmp_path):
        """A real, openable node (pty slave) reports it opens at the OS level."""
        master, slave = pty.openpty()
        try:
            slave_name = os.ttyname(slave)
            findings = diagnose_serial_port(
                slave_name, dev_root=str(tmp_path / "dev"), sys_root=str(tmp_path / "sys")
            )
        finally:
            os.close(master)
            os.close(slave)

        assert any("opens cleanly" in f for f in findings), findings


class TestSysfsLiveness:
    """Check 4: node in /dev but absent from /sys/class/tty is definitively stale."""

    def test_stale_node_detected(self, tmp_path):
        """Missing sysfs device link => explicit stale-node finding with candidates."""
        dev_root = tmp_path / "dev"
        dev_root.mkdir()
        (dev_root / "ttyUSB9").write_text("")  # present in /dev
        (dev_root / "ttyUSB10").write_text("")  # live sibling
        sys_root = tmp_path / "sys"  # no class/tty/ttyUSB9/device

        findings = diagnose_serial_port(
            str(dev_root / "ttyUSB9"), dev_root=str(dev_root), sys_root=str(sys_root)
        )

        stale = [f for f in findings if "stale node" in f]
        assert stale, findings
        assert "class/tty" in stale[0]
        assert "ttyUSB10" in stale[0]  # sibling surfaced inline


class TestByIdAdvice:
    """Check 3: /dev/serial/by-id entries recommend a stable path."""

    def test_recommends_by_id_when_none_match(self, tmp_path):
        """A by-id entry pointing at a different node recommends configuring by-id."""
        dev_root = tmp_path / "dev"
        dev_root.mkdir()
        node = dev_root / "ttyUSB0"
        node.write_text("")
        other = dev_root / "ttyUSB1"
        other.write_text("")
        by_id = dev_root / "serial" / "by-id"
        by_id.mkdir(parents=True)
        (by_id / "usb-FTDI_FT232R_ABC123-if00-port0").symlink_to(other)

        findings = diagnose_serial_port(
            str(node), dev_root=str(dev_root), sys_root=str(tmp_path / "sys")
        )

        byid = [f for f in findings if "serial/by-id" in f]
        assert byid, findings
        assert "usb-FTDI" in byid[0]


class TestHolderDetection:
    """Check 5: a same-process fd on the port is reported as a holder."""

    def test_holder_includes_own_pid(self, tmp_path):
        """Holding the pty slave open surfaces our pid (Linux); degrades gracefully else."""
        master, slave = pty.openpty()
        try:
            slave_name = os.ttyname(slave)
            findings = diagnose_serial_port(
                slave_name, dev_root=str(tmp_path / "dev"), sys_root=str(tmp_path / "sys")
            )
        finally:
            os.close(master)
            os.close(slave)

        if sys.platform == "linux":
            assert any(f"pid {os.getpid()}" in f for f in findings), findings
        else:
            # No /proc: no crash, still a usable block (breadcrumbs at minimum).
            assert isinstance(findings, list)
            assert findings


class _FakeFailClient:
    """Async client stub whose connect never succeeds."""

    connected = False

    async def connect(self) -> bool:
        return False

    def close(self) -> None:
        pass


class _FakeOkClient:
    """Async client stub that connects successfully."""

    connected = True

    async def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass


class TestConnectDiagnosticsThrottle:
    """The connect-failure hook runs diagnostics on failure 1 and 11, not 2-10."""

    def test_throttled_and_reset_on_success(self, monkeypatch):
        """Diagnostics fire on the 1st + every 10th failure and reset after a success."""
        calls: list[str] = []

        def fake_diag(port, **_kwargs):
            calls.append(port)
            return ["fake finding"]

        monkeypatch.setattr(client_module, "diagnose_serial_port", fake_diag)

        client = ModbusClient(transport="rtu", serial_port="/dev/ttyUSB9")

        async def scenario():
            client._create_client = lambda: _FakeFailClient()
            for _ in range(11):
                await client.connect()
            assert len(calls) == 2, calls  # failures 1 and 11 only

            client._create_client = lambda: _FakeOkClient()
            await client.connect()
            assert client._connect_failures == 0

            client._create_client = lambda: _FakeFailClient()
            await client.connect()
            assert len(calls) == 3, calls  # counter reset => fires on the 1st again

        asyncio.run(scenario())

    def test_tcp_transport_skips_diagnostics(self, monkeypatch):
        """A TCP connect failure never runs serial diagnostics."""
        calls: list[str] = []
        monkeypatch.setattr(
            client_module, "diagnose_serial_port", lambda port, **_k: calls.append(port) or []
        )

        client = ModbusClient(transport="tcp")

        async def scenario():
            client._create_client = lambda: _FakeFailClient()
            await client.connect()

        asyncio.run(scenario())
        assert calls == []
