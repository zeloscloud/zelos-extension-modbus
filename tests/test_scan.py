"""Scan and verify-map: the FC allowlist, and runs against the scan-target simulator."""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import await_listening, free_port
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.client.mixin import ModbusClientMixin
from pymodbus.exceptions import ConnectionException, ModbusIOException

from zelos_extension_modbus.constants import raw_names
from zelos_extension_modbus.demo.simulator import SCAN_TARGET_RANGES, run_demo_server
from zelos_extension_modbus.register_map import RegisterMap
from zelos_extension_modbus.scan import (
    ALLOWED,
    SILENT_AFTER,
    RangeFinder,
    ScanLink,
    _issues,
    discover,
    parse_formats,
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


class FakeBus:
    """ModbusConnection stand-in for ``discover``: ``replies[(endpoint, unit)]``.

    The endpoint is ``(port, baudrate, parity, stopbits)`` or ``(host, port)``. A
    reply is register values, an exception code, "echo" (no values) or "lost"
    (the link drops for good). Port "bad" never opens; baudrate 1 never opens.
    """

    replies: dict = {}
    tried: list = []
    expire_after: int | None = None
    links: list = []

    def __init__(self, **kw: Any) -> None:
        rtu = kw["transport"] == "rtu"
        keys = ("serial_port", "baudrate", "parity", "stopbits") if rtu else ("host", "port")
        self.at = tuple(kw[k] for k in keys)
        self.connected, self.connect_error = False, ": no such port"
        self.disconnect = AsyncMock()
        self.ensure_connected = AsyncMock(return_value=False)
        FakeBus.links.append(self)

    async def connect(self) -> bool:
        self.connected = "bad" not in self.at and self.at[1] != 1
        return self.connected

    async def request(self, method: str, unit: int, **_: Any) -> Any:
        await asyncio.sleep(0)  # links take turns, as on real I/O
        FakeBus.tried.append((*self.at, unit))
        if len(FakeBus.tried) == FakeBus.expire_after:
            for link in ScanLink.made:
                link.deadline = 0.0  # the time limit passes during this request
        reply = FakeBus.replies.get((*self.at, unit))
        if reply == "lost":
            self.connected = False
            raise ConnectionException("link dropped")
        if reply is None:
            raise ModbusIOException("no response")
        if reply == "echo":
            return SimpleNamespace(isError=lambda: False, registers=[])
        if isinstance(reply, int):
            return SimpleNamespace(isError=lambda: True, exception_code=reply)
        return SimpleNamespace(isError=lambda: False, registers=reply)


@pytest.fixture
def bus(monkeypatch):
    """``find(replies, targets, **knobs)`` -> (discover's result, every try in order)."""
    from zelos_extension_modbus import scan as scan_module

    made = []
    init = ScanLink.__init__
    monkeypatch.setattr(ScanLink, "made", made, raising=False)
    monkeypatch.setattr(
        ScanLink, "__init__", lambda self, *a, **k: (init(self, *a, **k), made.append(self))[0]
    )
    monkeypatch.setattr(scan_module, "ModbusConnection", FakeBus)
    monkeypatch.setattr(ScanLink, "listen", AsyncMock())  # the waits: test_slow_device_on_a_pty
    monkeypatch.setattr(ScanLink, "settle", AsyncMock())
    monkeypatch.setattr(scan_module, "identify", AsyncMock(return_value={}))
    monkeypatch.setattr(scan_module, "detect_sunspec", AsyncMock(return_value=None))

    def find(replies, targets, expire_after=None, **knobs):
        FakeBus.replies, FakeBus.tried, FakeBus.links = replies, [], []
        FakeBus.expire_after = expire_after
        got = _run(discover(targets, **knobs))
        assert all(c.disconnect.await_count for c in FakeBus.links if c.connected)
        return got, FakeBus.tried

    return find


def _serial(port: str, *settings: tuple[int, str, int], first: tuple[int, ...] = ()) -> dict:
    link = {"transport": "rtu", "serial_port": port, "baudrate": 9600, "parity": "N", "stopbits": 1}
    return link | {"settings": list(settings) or [(9600, "N", 1)], "first": list(first)}


N, E = (9600, "N", 1), (19200, "E", 1)


class TestDiscover:
    def test_likely_units_on_every_setting_then_the_setting_that_answers(self, bus):
        """Unit 4 at 19200 8E1 is found in the second stage. Then only that setting:
        units 1-10 and 247 are finished before the first device ends the search."""
        replies = {("a", *E, 4): 2, ("a", *E, 9): [7], ("a", *E, 20): [1]}
        got, tried = bus(replies, [_serial("a", N, E)])
        likely = [1, 247, 2, 3, 10]
        assert tried == [
            *[("a", *s, u) for s in (N, E) for u in likely],
            *[("a", *N, u) for u in range(1, 33) if u not in likely],
            *[("a", *E, u) for u in range(4, 10)],
        ]
        assert [(f["at"], f["unit_id"], f["reply"]) for f in got["found"]] == [
            ("a 19200 8E1", 4, "exception 02 (illegal data address)"),
            ("a 19200 8E1", 9, "holding 1 = 7"),
        ]
        row = {"requests": 11, "bytes_received": 0, "replies": 2}
        assert got["tried"][1] == {"at": "a 19200 8E1", "link": "a", "transport": "rtu"} | row

    def test_first_values_lead_and_listed_units_bound_the_search(self, bus):
        got, tried = bus({}, [_serial("a", first=(40,))], units=[40, 41, 1])
        assert tried == [("a", *N, u) for u in (40, 1, 40, 1, 41)]  # one setting: likely twice
        assert (got["found"], got["cut"]) == ([], [])

    def test_every_device_keeps_to_the_setting_that_answered(self, bus):
        """An echo and a gateway's 0B are not devices; all ports are searched at once."""
        replies = {("a", *N, 1): [1], ("a", *N, 200): [2], ("a", *N, 6): 0x0B, ("b", *N, 2): "echo"}
        got, tried = bus(replies, [_serial("bad"), _serial("a", N, E), _serial("b")], max_devices=0)
        assert [(f["serial_port"], f["unit_id"]) for f in got["found"]] == [("a", 1), ("a", 200)]
        assert sorted(t[4] for t in tried if t[0] == "a") == list(range(1, 248))
        assert {t[1:4] for t in tried if t[0] == "a"} == {N}
        assert (got["unopened"], got["malformed"]) == (
            ["bad: no such port"],
            2,
        )  # b: one setting, tried twice

    def test_a_setting_that_wont_open_does_not_drop_the_port(self, bus):
        got, _ = bus({("a", *N, 5): [7]}, [_serial("a", (1, "N", 1), N)])
        assert [f["unit_id"] for f in got["found"]] == [5]
        assert got["unopened"] == ["a 1 8N1"]

    def test_a_dropped_link_ends_its_search(self, bus):
        got, tried = bus({("a", *N, 247): "lost"}, [_serial("a", N, E)])
        assert (got["found"], got["lost"]) == ([], ["a 9600 8N1"])
        assert tried == [("a", *N, 1), ("a", *N, 247)]

    def test_time_limit_returns_what_it_found(self, bus):
        got, tried = bus({("a", *N, 1): [7]}, [_serial("a")], expire_after=3, max_devices=0)
        assert ([f["unit_id"] for f in got["found"]], len(tried)) == ([1], 3)
        assert got["cut"] == ["a 9600 8N1"]

    def test_tcp_direct_device_or_gateway(self, bus):
        """A direct device answers any id: one device. A gateway's 0B: every id is tried."""
        tcp = {"transport": "tcp", "host": "h", "port": 502, "first": [7]}
        direct = {("h", 502, u): [1] for u in range(256)}
        got, tried = bus(direct, [tcp], max_devices=0)
        assert ([f["unit_id"] for f in got["found"]], len(tried)) == ([7], 4)
        other = {"transport": "tcp", "host": "g", "port": 502}
        got, _ = bus(direct | {("g", 502, 1): [1]}, [tcp, other], max_devices=2)
        assert [(f["host"], f["unit_id"]) for f in got["found"]] == [("h", 7), ("g", 1)]
        behind = {("h", 502, 0): 0x0B, ("h", 502, 1): [1], ("h", 502, 3): [1], ("h", 502, 40): [1]}
        got, tried = bus(behind, [tcp])  # the first device does not hide units 1-10
        assert ([f["unit_id"] for f in got["found"]], len(tried)) == ([1, 3], 13)
        got, tried = bus({("h", 502, 0): 0x0B, ("h", 502, 90): [1]}, [tcp], max_devices=0)
        assert [f["unit_id"] for f in got["found"]] == [90]
        assert [t[2] for t in tried] == [
            7,
            1,
            0,
            255,
            247,
            2,
            3,
            10,
            *range(4, 7),
            8,
            9,
            *range(11, 247),
        ]

    def test_a_configured_connection_is_searched_until_it_answers(self, bus):
        """The first device ends the guessed ports, not another connection of the form."""
        tcp = {"transport": "tcp", "host": "h", "port": 502, "connection": 0}
        targets = [tcp, _serial("a", N, E) | {"connection": 1}, _serial("b", N, E)]
        got, tried = bus({("h", 502, 1): [5], ("a", *E, 40): [7]}, targets)
        assert [(f["at"], f["unit_id"]) for f in got["found"]] == [
            ("h:502", 1),
            ("a 19200 8E1", 40),
        ]
        assert len([t for t in tried if t[0] == "b"]) <= 1

    def test_link_counts_bytes_and_notes_a_late_reply(self):
        link = ScanLink(_serial("a") | {"settings": None, "first": None})
        late = bytes.fromhex("0283023 0f1".replace(" ", ""))  # unit 2, exception 02, valid CRC
        link._trace(True, bytes.fromhex("0303000000018428"))
        for chunk in (late[:3], late, b"\x55"):  # the buffer grows, a frame is taken, noise
            link._trace(False, chunk)
        assert (link.rx, link.row["bytes_received"], link.late) == (6, 6, {2})
        link._trace(True, bytes.fromhex("020300000001843 9".replace(" ", "")))
        link._trace(False, late)  # the unit asked: a reply, not late
        assert link.late == {2} and link.rx == 11

    def test_a_probe_waits_the_probe_timeout_plus_the_frames(self):
        link = ScanLink({"transport": "rtu", "serial_port": "x", "baudrate": 1200})
        link.conn = SimpleNamespace(request=AsyncMock(side_effect=ModbusIOException("")))
        _run(link.request("read_holding_registers", 1, address=0, count=1))
        assert link.conn.request.await_args.kwargs["timeout"] is None  # the link's own
        assert link.conn.request_delay_ms == 50
        link.probe_timeout = 0.2
        link._asked = 1  # as the send hook leaves it
        _run(link.request("read_holding_registers", 1, address=0, count=1))
        tick = time.get_clock_info("monotonic").resolution  # Windows: 15.6 ms
        waited = link.conn.request.await_args.kwargs["timeout"]
        assert waited == pytest.approx(0.2 + 165 / 1200 + tick)
        assert link.conn.request_delay_ms == 5  # the last try met silence
        link._trace(False, bytes.fromhex("018302c0f1"))  # unit 1 answers before the next request
        assert link.late == {1}


class TestAutoConfig:
    SAVED = [{"host": "saved", "port": 1502, "devices": []}]
    RTU = {"transport": "rtu", "serial_port": "/dev/ttyX", "baudrate": 19200, "parity": "E"}

    def _run(self, monkeypatch, found=(), ports=("/dev/ttyA",), **result):
        """auto_config with ``discover`` faked: (call) -> (result, targets, knobs)."""
        from zelos_extension_modbus import actions, scan

        seen = {}

        async def fake_discover(targets, **knobs):
            seen.update(targets=targets, knobs=knobs)
            empty = {"tried": [], "unopened": [], "lost": [], "cut": [], "malformed": 0}
            return empty | {"found": [dict(f) for f in found]} | result

        monkeypatch.setattr(scan, "discover", fake_discover)
        monkeypatch.setattr(actions, "_configured_connections", lambda: list(self.SAVED))
        monkeypatch.setattr(actions, "_refuse_if_running", lambda: None)
        monkeypatch.setattr(actions, "_search_ports", lambda: (list(ports), ["/dev/console0"]))
        return lambda **kw: (actions.auto_config(**kw), seen.get("targets"), seen.get("knobs"))

    @staticmethod
    def _where(targets):
        return [t.get("serial_port") or f"{t['host']}:{t['port']}" for t in targets]

    @pytest.mark.parametrize(
        ("kwargs", "where", "seconds"),
        [
            # Nothing configured: every listed serial port and the default host.
            ({"config": {}}, ["/dev/ttyA", "127.0.0.1:502"], 30),
            # The form's connections only: a TCP form never touches serial.
            ({"config": {"connections": [{"host": "form", "port": 5020}]}}, ["form:5020"], 30),
            ({}, ["saved:1502"], 30),  # called without config: the saved one
            ({"config": {"connections": [RTU]}}, ["/dev/ttyX"], 60),
            # A serial connection with no port chosen: every listed port.
            ({"config": {"connections": [{"transport": "rtu"}]}}, ["/dev/ttyA"], 30),
            # Named endpoints are the only ones searched.
            ({"serial_ports": "COM3", "hosts": "h:1502"}, ["COM3", "h:1502"], 60),
            ({"transport": "rtu"}, ["/dev/ttyA"], 30),
            ({"hosts": "10.0.0.0/30"}, ["10.0.0.1:502", "10.0.0.2:502"], 30),
        ],
    )
    def test_what_is_searched(self, monkeypatch, kwargs, where, seconds):
        _, targets, knobs = self._run(monkeypatch)(**kwargs)
        assert (self._where(targets), knobs["max_seconds"]) == (where, seconds)

    def test_form_values_go_first_and_knobs_pin(self, monkeypatch):
        conn = self.RTU | {"devices": [{"unit_id": 9}]}
        call = self._run(monkeypatch)
        _, [target], _ = call(config={"connections": [conn]})
        assert (target["settings"][:2], target["first"]) == ([(19200, "E", 1), (9600, "N", 1)], [9])
        assert len(target["settings"]) == 20
        _, guessed, _ = call(config={})
        assert target["connection"] and {t["connection"] for t in guessed} == {None}
        _, [target], knobs = call(
            config={"connections": [conn]}, baudrates="115200", formats="8e1", units="1-3"
        )
        assert (target["settings"], knobs["units"]) == ([(115200, "E", 1)], [1, 2, 3])
        _, _, knobs = call(probe_type="input", probe_address=14)
        assert knobs["probe"] == ("input", 13)

    def test_found_devices_join_their_connection(self, monkeypatch):
        """Existing devices are kept, the setting that answered is filled in, and an
        endpoint with no connection gets one."""
        ident = {"device_id": {"VendorName": "Zelos", "ProductCode": "ZSCAN-1"}}
        serial = {"transport": "rtu", "serial_port": "/dev/ttyX", "baudrate": 9600}
        serial |= {
            "parity": "N",
            "stopbits": 1,
            "at": "/dev/ttyX 9600 8N1",
            "reply": "holding 1 = 7",
        }
        tcp = {
            "transport": "tcp",
            "host": "h",
            "port": 502,
            "at": "h:502",
            "reply": "holding 1 = 7",
        }
        found = [
            serial | {"unit_id": 9, "identity": ident},
            serial | {"unit_id": 2, "sunspec": 40001},
            tcp | {"unit_id": 1},
        ]
        conn = self.RTU | {"devices": [{"unit_id": 9, "register_map_file": "m.json"}]}
        got, _, _ = self._run(monkeypatch, found)(config={"connections": [conn]})
        assert got["status"] == "success"
        assert got["message"] == (
            "/dev/ttyX 9600 8N1: found units 9 (Zelos ZSCAN-1), 2 (register_map set to sunspec); "
            "added 2. h:502: found unit 1; added 1."
        )
        assert got["config"]["connections"] == [
            {"transport": "rtu", "serial_port": "/dev/ttyX", "baudrate": 9600, "parity": "N"}
            | {
                "stopbits": 1,
                "devices": [
                    {"unit_id": 9, "register_map_file": "m.json"},
                    {"unit_id": 2, "register_map": "sunspec"},
                ],
            },
            {"transport": "tcp", "host": "h", "port": 502, "devices": [{"unit_id": 1}]},
        ]

    def test_nothing_found_says_what_each_link_saw_and_what_to_give(self, monkeypatch):
        row = {"link": "COM3", "transport": "rtu", "requests": 252, "replies": 0}
        tried = [
            row | {"at": "COM3 9600 8N1", "bytes_received": 0},
            {"at": "h:502", "link": "h:502", "transport": "tcp", "requests": 4}
            | {"bytes_received": 0, "replies": 0},
        ]
        call = self._run(monkeypatch, tried=tried, unopened=["b:502: connection refused"])
        got, _, _ = call(serial_ports="COM3", baudrates="9600", formats="8N1", hosts="h,b")
        assert got["status"] == "error" and "config" not in got
        assert got["message"] == (
            "No device answered. COM3: 252 requests on 9600 8N1, nothing came back (check the "
            "A/B wiring, power, the adapter, and that Modbus RTU is enabled on the device). "
            "h:502: connected, no unit answered 4 requests. Couldn't open b:502: connection "
            "refused. To search further, give: the baudrate and format if they are not certain "
            "(empty searches them); the unit ID (slave address); whether Modbus RTU is "
            "enabled on the device, and a register it serves."
        )
        tried = [row | {"at": "COM3 9600 8N1", "bytes_received": 40}]
        got, _, _ = self._run(monkeypatch, tried=tried)(serial_ports="COM3")
        assert "Bytes came back with no valid reply on COM3 9600 8N1 (40 bytes)" in got["message"]
        assert got["ask"][0].startswith("the baudrate and format")

    def test_no_port_to_search(self, monkeypatch):
        got, _, _ = self._run(monkeypatch, ports=())(transport="rtu")
        assert got["message"].startswith("Nothing to search (/dev/console0 left alone")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_slow_device_on_a_pty():
    """Real pymodbus: unit 5 answers 0.18 s after each request, past the probe timeout.
    It is found, and its late replies are not taken for the identity answers."""
    import os
    import select
    import threading
    import tty

    from pymodbus.framer import FramerRTU

    master, slave = os.openpty()
    tty.setraw(slave)
    reply = bytes.fromhex("050302002a")
    reply += FramerRTU.compute_CRC(reply).to_bytes(2, "big")
    stop = threading.Event()

    def device() -> None:
        due: list[float] = []
        while not stop.is_set():
            if select.select([master], [], [], 0.01)[0] and os.read(master, 64)[:2] == b"\x05\x03":
                due.append(time.monotonic() + 0.18)
            if due and due[0] <= time.monotonic():
                os.write(master, reply)
                due.pop(0)

    threading.Thread(target=device, daemon=True).start()
    try:
        got = _run(discover([_serial(os.ttyname(slave))], units=[5]))
    finally:
        stop.set()
    assert [(f["unit_id"], f["reply"], "identity" in f) for f in got["found"]] == [
        (5, "holding 1 = 42", True)
    ]
    assert got["lost"] == []


def test_parse_hosts_caps_one_search():
    from zelos_extension_modbus.actions import _parse_hosts

    assert len(_parse_hosts("10.0.0.0/24, h:1502")) == 255
    for bad in ("10.0.0.0/24,10.0.1.0/24", "10.0.0.0/23", ":502", "h:x"):
        with pytest.raises(ValueError):
            _parse_hosts(bad)


def test_action_flags_are_pinned():
    """Pins each action's flags, so a new action or a changed flag is a deliberate choice."""
    from zelos_extension_modbus import actions

    expected = {
        "list_devices": (True, True),
        "get_status": (True, True),
        "get_snapshot": (True, True),
        "read_register": (True, False),
        "write_single_register": (False, False),
        "write_registers": (False, False),
        "read_named_register": (True, False),
        "write_named_register": (False, False),
        "write_coil": (False, False),
        "list_registers": (True, True),
        "list_writable_registers": (True, True),
        "save_map": (False, False),
        "scan_device": (False, True),
        "verify_map": (True, True),
        "auto_config": (True, True),
        "list_serial_ports": (True, True),
    }
    actual = {a.__name__: (a._action.read_only, a._action.hidden) for a in actions.ALL_ACTIONS}
    assert actual == expected


def test_parse_formats():
    assert parse_formats("8n1, 8E2") == [("N", 1), ("E", 2)]
    with pytest.raises(ValueError, match="7 is Modbus ASCII"):
        parse_formats("7E1")
