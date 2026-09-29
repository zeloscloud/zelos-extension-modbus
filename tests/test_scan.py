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

from zelos_extension_modbus.demo.simulator import (
    SCAN_TARGET_COUNTER,
    SCAN_TARGET_FLOATS,
    SCAN_TARGET_RANGES,
    SCAN_TARGET_STRING,
    run_demo_server,
)
from zelos_extension_modbus.register_map import RegisterMap
from zelos_extension_modbus.scan import (
    ALLOWED,
    SILENT_AFTER,
    RangeFinder,
    ScanLink,
    classify_words,
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


class TestInference:
    def test_run_start_learns_the_read_limit(self):
        """A run found past a long hole, before any read hit the size limit, keeps its start."""

        class Device(RangeFinder):
            async def _ok(self, table: str, address: int, count: int) -> bool:
                return address >= 1005 and address + count <= 2000 and count <= 60

        finder, runs = Device(None, 1), []
        _run(finder.window("holding", 0, 2000, runs))
        assert (runs, finder.learned_block) == ([(1005, 2000)], 60)

    def test_constant_words(self):
        """One sample: float32 words that print as text stay floats; real text is a string."""
        floats = [0x4366, 0x4142, 0x4148, 0x4344]  # 'CfABAHCD' = float32 230.3, 12.5
        raw = b"ZELOS SCAN TARGET\0"
        text = [int.from_bytes(raw[i : i + 2]) for i in range(0, len(raw), 2)]

        def kinds(words: list[int]) -> list[tuple[str, int | None]]:
            regs, _ = classify_words("holding", 0, [[w] for w in words])
            return [(r["datatype"], r.get("length")) for r in regs]

        assert kinds(floats) == [("float32", None), ("float32", None)]
        assert kinds(text) == [("string", 9)]


class TestScanTarget:
    def test_scan_recovers_the_seeded_device(self):
        result = _run(
            _with_sim(lambda ep: scan(ep, windows=[(0, 1999)], samples=4, period=1.5, delay_ms=0))
        )
        device = result["report"]["devices"][0]
        assert device["max_block_size"] == 60
        assert device["identity"]["device_id"]["ProductCode"] == "ZSCAN-1"
        # Report and draft are 1-based; the sim's constants are wire addresses.
        assert {t: [tuple(r) for r in v["ranges"]] for t, v in device["tables"].items()} == {
            t: [(lo + 1, hi + 1) for lo, hi in runs] for t, runs in SCAN_TARGET_RANGES.items()
        }

        draft = result["maps"][1]
        loaded = RegisterMap.from_dict(draft)
        assert loaded.device == {"max_block_size": 60}
        regs = {r.address: r for r in loaded.registers if r.type == "holding"}
        assert not any(r.writable for r in loaded.registers)
        for base, seeded in SCAN_TARGET_FLOATS.items():
            for k in range(10):
                reg = regs[base + 2 * k]
                assert (reg.datatype, reg.byte_order) == ("float32", seeded)
        assert regs[SCAN_TARGET_COUNTER].datatype == "uint32"
        start, text = SCAN_TARGET_STRING
        assert (regs[start].datatype, regs[start].length, regs[start].rate) == ("string", 9, 60)
        assert (regs[start].name, regs[start].map_address) == (f"hr{start + 1}", start + 1)
        assert regs[SCAN_TARGET_COUNTER].rate is None
        assert text in regs[start].description

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
        assert (report["ok"], report["unchecked"]) == (1, 0)
        issues = {p["path"]: p["issues"] for p in report["problems"]}
        assert {p["path"]: p["address"] for p in report["problems"]}["e/hole"] == 171
        assert set(issues) == {"e/counter", "e/zero", "e/hole"}
        assert issues["e/hole"] == ["exception 02"]
        assert issues["e/counter"][0].startswith("implausible float32")  # a uint32 read as float

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
            result = await scan(ep, max_seconds=1.0)
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
                    "message": "Found a:502 unit 1. Couldn't connect to b:502. New units without "
                    "SunSpec discover their registers at start (auto-scan).",
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
