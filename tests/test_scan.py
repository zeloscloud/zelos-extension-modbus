"""Scan and verify-map: the FC allowlist, and runs against the scan-target simulator."""

from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import await_listening, free_port
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin
from pymodbus.exceptions import ModbusIOException

from zelos_extension_modbus.constants import raw_names
from zelos_extension_modbus.demo.simulator import SCAN_TARGET_RANGES, run_demo_server
from zelos_extension_modbus.register_map import RegisterMap
from zelos_extension_modbus.scan import (
    ALLOWED,
    SILENT_AFTER,
    RangeFinder,
    ScanLink,
    _issues,
    scan,
    verify_map,
)


def _run(coro: Any) -> Any:
    """Run on a private loop; the rest of the suite relies on the default one."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _with_sim(fn: Any, **sim: Any) -> Any:
    """Run ``fn(endpoint)`` against a scan-target simulator on a free port."""
    port = free_port()
    server = asyncio.create_task(run_demo_server(port=port, scan_target=True, **sim))
    await await_listening(port)
    try:
        return await fn({"transport": "tcp", "host": "127.0.0.1", "port": port})
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


class TestAllowlist:
    def test_only_read_and_identify_codes(self):
        assert set(ALLOWED.values()) == {0x01, 0x02, 0x03, 0x04, 0x11, 0x2B}

    def test_every_other_client_request_is_refused_before_the_wire(self):
        link = ScanLink({"transport": "tcp", "host": "127.0.0.1", "port": 1})
        link.conn = AsyncMock()
        methods = [
            name
            for name, fn in inspect.getmembers(ModbusClientMixin, inspect.isfunction)
            if not name.startswith("_") and name not in ALLOWED
        ]
        assert "write_register" in methods and "diag_restart_communication" in methods
        for name in methods:
            with pytest.raises(PermissionError):
                _run(link.request(name, 1))
        link.conn.request.assert_not_called()
        assert link.requests == 0

    def test_allowed_methods_send_their_function_code(self):
        """Pin the method -> PDU mapping, including MEI type 14 for FC 43."""
        sent = []

        async def execute(no_response_expected: bool, pdu: Any) -> None:
            sent.append(pdu)

        async def send_all() -> None:
            client = AsyncModbusTcpClient("127.0.0.1")
            client.execute = execute
            for name in ALLOWED:
                kwargs = {"address": 0, "count": 1} if name.startswith("read_") else {}
                if name == "read_device_information":
                    kwargs = {"read_code": 1}
                await getattr(client, name)(**kwargs)

        _run(send_all())
        assert [pdu.function_code for pdu in sent] == list(ALLOWED.values())
        assert sent[-1].sub_function_code == 0x0E


class TestRanges:
    def test_run_start_learns_the_read_limit(self):
        """A run found past a long hole, before any read hit the size limit, keeps its start."""

        class Device(RangeFinder):
            async def _ok(self, table: str, address: int, count: int) -> bool:
                return address >= 1005 and address + count <= 2000 and count <= 60

        finder, runs = Device(None, 1), []
        _run(finder.window("holding", 0, 2000, runs))
        assert (runs, finder.learned_block) == ([(1005, 2000)], 60)


class TestScanTarget:
    def test_scan_recovers_the_seeded_device(self):
        result = _run(_with_sim(lambda ep: scan(ep, windows=[(0, 1999)], delay_ms=0)))
        device = result["report"]["devices"][0]
        assert device["max_block_size"] == 60
        assert device["identity"]["device_id"]["ProductCode"] == "ZSCAN-1"
        # Report and draft are 1-based; the sim's constants are wire addresses.
        assert {t: [tuple(r) for r in v["ranges"]] for t, v in device["tables"].items()} == {
            t: [(lo + 1, hi + 1) for lo, hi in runs] for t, runs in SCAN_TARGET_RANGES.items()
        }

        # Every readable address, raw and read-only, named as auto-scan traces it.
        draft = result["maps"][1]
        assert draft["device"] == {"max_block_size": 60}
        expected = [
            (raw_names(t, a + 1), t, a + 1, "bool" if t in ("coil", "discrete_input") else "uint16")
            for t, runs in SCAN_TARGET_RANGES.items()
            for lo, hi in runs
            for a in range(lo, hi + 1)
        ]
        got = [
            ((event, reg["name"]), reg["type"], reg["address"], reg["datatype"])
            for event, [reg] in draft["events"].items()
        ]
        assert got == expected
        regs = [reg for [reg] in draft["events"].values()]
        assert all(set(reg) == {"name", "type", "address", "datatype", "writable"} for reg in regs)
        assert not any(r.writable for r in RegisterMap.from_dict(draft).registers)

    def test_verify_map_flags_only_the_broken_registers(self):
        reg_map = RegisterMap.from_dict(
            {
                "events": {
                    "e": [
                        {"name": "ok", "address": 1, "datatype": "float32"},
                        {"name": "counter", "address": 21, "datatype": "float32"},
                        {"name": "zero", "address": 101},
                        {"name": "hole", "address": 171},
                    ]
                }
            }
        )
        report = _run(_with_sim(lambda ep: verify_map(ep, reg_map, samples=1, delay_ms=0)))
        assert (report["ok"], report["unchecked"]) == (3, 0)
        # Only facts: a uint32 read as float32 and a zero word are values, not problems.
        assert report["problems"] == [
            {"path": "e/hole", "type": "holding", "address": 171, "issues": ["exception 02"]}
        ]
        assert set(report["values"]) == {"e/ok", "e/counter", "e/zero"}
        assert report["values"]["e/zero"] == 0

    def test_issues_are_facts_of_the_declared_datatype(self):
        def issues(words: list[int], **reg: Any) -> list[str]:
            regs = RegisterMap.from_dict({"events": {"e": [{"address": 1, **reg}]}}).registers
            return _issues(regs[0], [words])

        assert issues([0x7FC0, 0], datatype="float32") == ["non-finite float32 (nan)"]
        assert issues([0x7149, 0xF2CA], datatype="float32") == []  # 1e30: odd, but finite
        assert issues([0x4142, 0x0100], datatype="string", length=2) == [
            "non-ASCII bytes in string"
        ]

    def test_verify_cuts_off_a_unit_that_goes_silent(self, monkeypatch):
        """A unit that answers once and then goes silent is cut off, not read to the end."""
        answered = []

        async def request(method, unit, **kwargs):
            if answered:
                raise ModbusIOException("no response")
            answered.append(1)
            return SimpleNamespace(isError=lambda: False, registers=[1])

        async def open_link(self, **changes):
            self.conn = SimpleNamespace(request=request, disconnect=AsyncMock())
            return True

        monkeypatch.setattr(ScanLink, "open", open_link)
        reg_map = RegisterMap.from_dict({"events": {"e": [{"address": a} for a in range(1, 41)]}})
        report = _run(verify_map({"host": "x"}, reg_map, samples=1))
        assert report["cutoff"] == f"no response to the last {SILENT_AFTER} requests"
        assert (report["requests"], report["unchecked"]) == (
            1 + SILENT_AFTER,
            40 - 1 - SILENT_AFTER,
        )

    def test_budget_abort_is_bounded(self):
        async def timed(ep):
            started = time.monotonic()
            # Paced so the full scan (~4000 requests) far outlasts the deadline.
            result = await scan(ep, max_seconds=1.0, delay_ms=20)
            return result, time.monotonic() - started

        result, elapsed = _run(_with_sim(timed))
        assert elapsed < 3.0  # abort is bounded (~3 s convention), sim startup excluded
        assert any(c["reason"] == "max_seconds reached" for c in result["report"]["cutoffs"])


class TestAutoConfig:
    SAVED = [{"host": "saved", "port": 1502, "devices": []}]
    FORM = {
        "connections": [
            {"name": "panel", "host": "form", "port": 5020, "devices": [{"unit_id": 7}]}
        ]
    }

    @pytest.mark.parametrize(
        ("config", "answer", "swept", "connections"),
        [
            # Form data wins over the saved config; typed fields and devices kept.
            (
                FORM,
                [7, 2],
                ["form"],
                [{**FORM["connections"][0], "devices": [{"unit_id": 7}, {"unit_id": 2}]}],
            ),
            # Older app hosts pass nothing: the saved config.
            (None, [1], ["saved"], [{**SAVED[0], "devices": [{"unit_id": 1}]}]),
            # No connection anywhere: probe 127.0.0.1:502 only.
            (
                {},
                [1],
                ["127.0.0.1"],
                [
                    {
                        "transport": "tcp",
                        "host": "127.0.0.1",
                        "port": 502,
                        "devices": [{"unit_id": 1}],
                    }
                ],
            ),
            ({}, [], ["127.0.0.1"], None),
        ],
    )
    def test_connections_source(self, monkeypatch, config, answer, swept, connections):
        from zelos_extension_modbus import actions, scan

        hosts = []

        async def fake_sweep(endpoint, **kwargs):
            hosts.append(endpoint["host"])
            return {
                "endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "units": {"present": answer},
                "identity": {},
                "sunspec": {},
                "cutoffs": [],
            }

        monkeypatch.setattr(scan, "sweep", fake_sweep)
        monkeypatch.setattr(actions, "_configured_connections", lambda: list(self.SAVED))
        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        result = actions.auto_config() if config is None else actions.auto_config(config=config)
        assert hosts == swept
        if connections is None:
            assert result == {
                "status": "error",
                "message": "Nothing answered on 127.0.0.1:502, the default. Add a connection "
                "(host and port, or a serial port).",
            }
        else:
            assert result["config"]["connections"] == connections

    @pytest.mark.parametrize(
        ("answers", "result"),
        [
            # one link never opened, the other found a unit: still filled in, the failure named
            (
                {"a": [1], "b": None},
                {
                    "status": "success",
                    "message": "a:502: found unit 1; added 1. Couldn't connect to b:502.",
                },
            ),
            # opened but silent vs never opened: different fixes, so different words
            (
                {"a": [], "b": None},
                {
                    "status": "error",
                    "message": "No unit answered on a:502. Couldn't connect to b:502.",
                },
            ),
        ],
    )
    def test_unreachable_is_named(self, monkeypatch, answers, result):
        from zelos_extension_modbus import actions, scan

        async def fake_sweep(endpoint, **kwargs):
            present = answers[endpoint["host"]]
            report = {
                "endpoint": f"{endpoint['host']}:502",
                "identity": {},
                "sunspec": {},
                "cutoffs": [],
            }
            if present is None:
                return report | {"error": "cannot open", "units": {}}
            return report | {"units": {"present": present}}

        monkeypatch.setattr(scan, "sweep", fake_sweep)
        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        conns = [{"transport": "tcp", "host": h, "port": 502} for h in answers]
        got = actions.auto_config(config={"connections": conns})
        assert {k: got[k] for k in ("status", "message")} == result

    def test_rtu_without_a_port_asks_for_one(self, monkeypatch):
        from zelos_extension_modbus import actions

        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        got = actions.auto_config(config={"connections": [{"transport": "rtu", "serial_port": ""}]})
        assert got == {"status": "error", "message": "Choose a Serial Port to scan."}

    def test_rtu_port_that_wont_open_is_named(self, monkeypatch):
        from zelos_extension_modbus import actions

        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        conn = {"transport": "rtu", "serial_port": "/dev/tty.nope"}
        got = actions.auto_config(config={"connections": [conn]})
        assert got == {"status": "error", "message": "Couldn't open /dev/tty.nope."}

    @pytest.mark.parametrize(
        ("present", "known", "sunspec", "message"),
        [
            ([1, 2], [1], {}, "a:502: found units 1 (Zelos ZSCAN-1), 2; added 2."),
            ([1, 2], [], {}, "a:502: found units 1 (Zelos ZSCAN-1), 2; added 1, 2."),
            ([1, 2], [1, 2], {}, "a:502: found units 1 (Zelos ZSCAN-1), 2."),
            (
                [1],
                [],
                {1: 40001},
                "a:502: found unit 1 (Zelos ZSCAN-1, register_map set to sunspec); added 1.",
            ),
        ],
    )
    def test_found_message(self, monkeypatch, present, known, sunspec, message):
        from zelos_extension_modbus import actions, scan

        ident = {"device_id": {"VendorName": "Zelos", "ProductCode": "ZSCAN-1"}}

        async def fake_sweep(endpoint, **kwargs):
            return {
                "endpoint": "a:502",
                "units": {"present": present},
                "identity": {1: ident},
                "sunspec": sunspec,
                "cutoffs": [],
            }

        monkeypatch.setattr(scan, "sweep", fake_sweep)
        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        devices = [{"unit_id": u} for u in known]
        conn = {"transport": "tcp", "host": "a", "port": 502, "devices": devices}
        assert actions.auto_config(config={"connections": [conn]})["message"] == message
