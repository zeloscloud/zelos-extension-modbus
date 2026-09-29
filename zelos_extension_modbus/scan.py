"""Read-only device discovery (scan) and register-map verification.

Pipeline per device: S0 reach (unit sweep; RTU serial autodetect), S1 identify
(FC 43/14, FC 17), SunSpec marker detection, S3 valid ranges per table, S4
classify (sample and guess datatype/byte order), S5 emit a draft register map
plus a report. ``verify_map`` reads every register of an existing map once.

Safety: every request goes through ``ScanLink.request``, which only sends the
function codes in ``ALLOWED``: 01-04 reads, 43/14 device identification and
17 report server id. Never FC 08 (restart / listen-only). One request at a
time per link with an inter-request delay, per-stage request and wall-clock
budgets, and a global deadline checked before every request, so an abort
lands within one request timeout. Draft maps are always ``writable: false``.

``Discovery`` reuses the S3 range finder for a running device's auto-scan:
one read per poll-loop turn, through the device's own request path (which
only reads, FC 01-04).

Internally addresses are 0-based wire addresses; everything user-facing
(windows, report ranges, draft maps) is 1-based, the maps' default
address_base. Register names carry the table and that address: ``hr40001`` is
holding register 40001 (wire 40000), ``ir``/``co``/``di`` for input/coil/
discrete input.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymodbus.exceptions import ModbusException

from zelos_extension_modbus.client import (
    GATEWAY_ABSENT,
    READ_METHODS,
    ModbusConnection,
    decode_register,
    decode_value,
    encode_value,
    json_safe,
)
from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MODBUS_MAX_BIT_READ_COUNT,
    MODBUS_MAX_READ_COUNT,
    SLOW_RATE,
    ByteOrder,
    RegisterType,
    Transport,
)
from zelos_extension_modbus.register_map import Register, RegisterMap
from zelos_extension_modbus.serial_diag import diagnose_serial_port
from zelos_extension_modbus.sunspec import BASE_ADDRESSES as SUNSPEC_BASES
from zelos_extension_modbus.sunspec import SUNS_MARKER

logger = logging.getLogger(__name__)

#: The only requests scan and verify ever send: pymodbus client method -> FC.
ALLOWED: dict[str, int] = {
    "read_coils": 0x01,
    "read_discrete_inputs": 0x02,
    "read_holding_registers": 0x03,
    "read_input_registers": 0x04,
    "report_device_id": 0x11,
    "read_device_information": 0x2B,  # pymodbus only sends MEI type 14 here
}

#: Table -> register-name prefix, in scan order.
TABLES: dict[str, str] = {
    RegisterType.HOLDING: "hr",
    RegisterType.INPUT: "ir",
    RegisterType.COIL: "co",
    RegisterType.DISCRETE_INPUT: "di",
}

DEFAULT_TIMEOUT = 0.5
#: Gap between requests on RTU (Chipkin); TCP devices pace themselves. Busy
#: (06), or SLOW_AFTER no-replies in a row from a unit that answered, doubles it
#: (from at least GAP_STEP_MS) up to MAX_GAP_MS; SPEED_UP_AFTER replies in a
#: row halve it back toward the start gap.
RTU_DELAY_MS = 50
GAP_STEP_MS = 10
MAX_GAP_MS = 500
SLOW_AFTER = 2
SPEED_UP_AFTER = 20
DEFAULT_SAMPLES = 10
DEFAULT_PERIOD = 5.0
#: Consecutive timeouts that mark a link (scan: with nothing heard yet) or unit silent.
SILENT_AFTER = 16
#: verify_map's default wall clock, the verify action's longest.
VERIFY_MAX_SECONDS = 840.0

TCP_UNITS = (1, 0, 255)  # tried after the configured unit
RTU_UNITS = range(1, 248)
QUICK_UNITS = (*range(1, 11), 247)  # the ids devices ship with; seconds, not minutes
TCP_WINDOWS = ((0, 65535),)
RTU_WINDOWS = ((0, 9999), (30000, 30999), (40000, 40999), (50000, 50999))

#: Chipkin trial order: (baudrate, parity, stopbits).
SERIAL_COMBOS = (
    (9600, "N", 1),
    (9600, "E", 1),
    (19200, "N", 1),
    (19200, "E", 1),
    (38400, "N", 1),
    (115200, "N", 1),
)
AUTODETECT_UNITS = (1, 2, 247)  # tried after the configured unit

#: Per-stage (max requests, max seconds) per device.
BUDGETS: dict[str, tuple[int, float]] = {
    "autodetect": (60, 60.0),
    "reach": (600, 300.0),
    "identify": (20, 15.0),
    "ranges": (30000, 900.0),
    "classify": (20000, 600.0),
}

#: Hole probing: exact for the first 125 misses, then every 10th address,
#: then every 100th after 1000; a hit searches back for its run start.
#: Islands shorter than the stride deep inside a long hole can be missed.
_STRIDES = ((1000, 100), (125, 10))

DEVICE_ID_NAMES = {
    0x00: "VendorName",
    0x01: "ProductCode",
    0x02: "MajorMinorRevision",
    0x03: "VendorUrl",
    0x04: "ProductName",
    0x05: "ModelName",
    0x06: "UserApplicationName",
}

NOTES = [
    "Scan only reads, but some devices clear latched alarms or counters on read, "
    "or expose FIFO registers. Review before scanning production equipment.",
    "Devices that answer unmapped addresses with 0 cannot be told apart from real "
    "zeros; never-changing zero registers are marked low confidence.",
]

# Preference order when byte orders decode identically.
_ORDERS = (ByteOrder.BIG, ByteOrder.BIG_SWAP, ByteOrder.LITTLE, ByteOrder.LITTLE_SWAP)


class BudgetExceeded(Exception):
    """A stage budget or the global deadline ran out; the message says which."""


class DeadlineReached(BudgetExceeded):
    """max_seconds ran out: stop every stage and unit, keep what was found."""


class Unsupported(Exception):
    """The device answered exception 01: the table's function code is unsupported."""


@dataclass
class Reply:
    """One request's outcome: values, or an exception code, or neither (no response)."""

    values: list[Any] | None = None
    exc: int | None = None
    response: Any = None

    @property
    def ok(self) -> bool:
        return self.exc is None and self.response is not None


class ScanLink:
    """The scan's one request path over a ModbusConnection, with the FC allowlist.

    ModbusConnection serializes requests and holds the inter-request delay;
    this adds the allowlist, request accounting and budgets.
    """

    def __init__(
        self,
        endpoint: dict[str, Any],
        timeout: float = DEFAULT_TIMEOUT,
        delay_ms: int | None = None,
        max_seconds: float | None = None,
    ) -> None:
        self.endpoint = dict(endpoint)
        self.timeout = timeout
        rtu = endpoint.get("transport") == Transport.RTU
        self.delay_ms = delay_ms if delay_ms is not None else RTU_DELAY_MS if rtu else 0
        self._start_delay_ms = self.delay_ms
        self._misses = 0  # no-replies in a row from answered units
        self._replies = 0  # replies in a row
        self.deadline = time.monotonic() + max_seconds if max_seconds else None
        self.requests = 0
        self.timeouts = 0
        self.silent = 0  # no responses in a row
        self.answered: set[int] = set()  # units heard from (a gateway 0A/0B is not)
        self.by_fc: Counter[int] = Counter()
        self.conn: ModbusConnection | None = None
        self._stage: list[Any] | None = None  # [name, requests left, deadline]

    @property
    def label(self) -> str:
        e = self.endpoint
        if e.get("transport") == Transport.RTU:
            return f"{e['serial_port']}@{e.get('baudrate', 9600)} {e.get('parity', 'N')}"
        return f"{e.get('host')}:{e.get('port', 502)}"

    async def open(self, **changes: Any) -> bool:
        """(Re)open the link, optionally with changed serial settings."""
        await self.close()
        self.endpoint.update(changes)
        self.conn = ModbusConnection(
            **self.endpoint, timeout=self.timeout, retries=0, request_delay_ms=self.delay_ms
        )
        return await self.conn.connect()

    async def close(self) -> None:
        if self.conn:
            await self.conn.disconnect()
            self.conn = None

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Apply ``BUDGETS[name]`` to the requests made inside."""
        requests, seconds = BUDGETS[name]
        self._stage = [name, requests, time.monotonic() + seconds]
        try:
            yield
        finally:
            self._stage = None

    @property
    def expired(self) -> bool:
        """The global deadline has passed."""
        return self.deadline is not None and time.monotonic() >= self.deadline

    def check(self) -> None:
        """Raise BudgetExceeded when the global deadline or stage budget is spent."""
        now = time.monotonic()
        if self.expired:
            raise DeadlineReached("max_seconds reached")
        if self._stage:
            name, left, deadline = self._stage
            if left <= 0:
                raise BudgetExceeded(f"{name} request budget ({BUDGETS[name][0]}) spent")
            if now >= deadline:
                raise BudgetExceeded(f"{name} time budget ({BUDGETS[name][1]:g}s) spent")

    async def sleep(self, seconds: float) -> None:
        """Sleep, cut short by the deadlines."""
        self.check()
        ends = [time.monotonic() + seconds, self.deadline, self._stage and self._stage[2]]
        await asyncio.sleep(max(0.0, min(e for e in ends if e) - time.monotonic()))

    async def request(self, method: str, unit: int, **kwargs: Any) -> Reply:
        """Send one allowlisted request; the single choke point for scan I/O."""
        fc = ALLOWED.get(method)
        if fc is None:
            raise PermissionError(f"scan never sends {method}")
        self.check()
        if self._stage:
            self._stage[1] -= 1
        self.requests += 1
        self.by_fc[fc] += 1
        try:
            response = await self.conn.request(method, unit, **kwargs)
        except (ModbusException, OSError):
            self.timeouts += 1
            self.silent += 1
            self._missed(unit)
            return Reply()  # no response
        exc = getattr(response, "exception_code", -1) if response.isError() else None
        self.silent = self.silent + 1 if exc in GATEWAY_ABSENT else 0
        if exc in GATEWAY_ABSENT:
            self._missed(unit)
        elif exc == 0x06:
            self._slow_down()
        else:
            self.answered.add(unit)
            self._replied()
        if exc is None:
            return Reply(values=_values(response, kwargs.get("count", 0)), response=response)
        return Reply(exc=exc, response=response)

    def _missed(self, unit: int) -> None:
        """No reply: SLOW_AFTER in a row from units that answered before double the gap."""
        self._replies = 0
        if unit not in self.answered:
            return
        self._misses += 1
        if self._misses >= SLOW_AFTER:
            self._slow_down()

    def _slow_down(self) -> None:
        self._set_gap(min(MAX_GAP_MS, max(GAP_STEP_MS, 2 * self.delay_ms)))

    def _replied(self) -> None:
        """A reply: SPEED_UP_AFTER in a row halve the gap back toward its start."""
        self._misses = 0
        self._replies += 1
        if self._replies >= SPEED_UP_AFTER and self.delay_ms > self._start_delay_ms:
            half = self.delay_ms // 2
            self._set_gap(max(self._start_delay_ms, half if half >= GAP_STEP_MS else 0))

    def _set_gap(self, ms: int) -> None:
        self._misses = self._replies = 0
        self.delay_ms = ms
        self.conn.request_delay_ms = ms

    async def read(self, table: str, unit: int, address: int, count: int) -> Reply:
        """Read ``count`` addresses of ``table``; exception 01 raises Unsupported."""
        reply = await self.request(READ_METHODS[table], unit, address=address, count=count)
        if reply.exc == 0x01:
            raise Unsupported(table)
        return reply


def _values(response: Any, count: int) -> list[Any] | None:
    if getattr(response, "registers", None):
        return list(response.registers)
    if getattr(response, "bits", None):
        return list(response.bits[:count])
    return None


def _ranges(runs: list[tuple[int, int]]) -> list[list[int]]:
    """Half-open wire runs -> inclusive 1-based [first, last] pairs for the report."""
    return [[lo + 1, hi] for lo, hi in runs]


def parse_ranges(text: str, bottom: int, top: int) -> list[tuple[int, int]]:
    """``"1-10000,40001-41000,7"`` -> inclusive (first, last) pairs within bottom..top."""
    out = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        lo, _, hi = part.partition("-")
        pair = (int(lo), int(hi or lo))
        if not bottom <= pair[0] <= pair[1] <= top:
            raise ValueError(f"bad range {part!r}: need first-last within {bottom}-{top}")
        out.append(pair)
    return out


def parse_windows(text: str) -> list[tuple[int, int]]:
    """1-based address windows -> inclusive wire (first, last) pairs."""
    return [(lo - 1, hi - 1) for lo, hi in parse_ranges(text, 1, 0x10000)]


def endpoint(
    target: str,
    transport: str = Transport.TCP,
    port: int = 502,
    baudrate: int = 9600,
    parity: str = "N",
    stopbits: int = 1,
    bytesize: int = 8,
) -> dict[str, Any]:
    """ModbusConnection link kwargs for a host (TCP) or serial port (RTU)."""
    if transport == Transport.TCP:
        return {"transport": transport, "host": target, "port": int(port)}
    return {
        "transport": transport,
        "serial_port": target,
        "baudrate": int(baudrate),
        "parity": parity,
        "stopbits": int(stopbits),
        "bytesize": int(bytesize),
    }


def quiet_pymodbus() -> None:
    """Scan expects timeouts and exceptions; pymodbus logs each one as an error."""
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def parse_units(text: str) -> list[int]:
    """``"1,2,10-20"`` -> unit ids in order."""
    return [u for lo, hi in parse_ranges(text, 0, 255) for u in range(lo, hi + 1)]


# ---------------------------------------------------------------------------
# S0 reach
# ---------------------------------------------------------------------------


async def probe_unit(link: ScanLink, unit: int) -> str:
    """FC03 @0 x1: 'present' (any reply), 'gateway_absent' (0A/0B) or 'timeout'."""
    reply = await link.request("read_holding_registers", unit, address=0, count=1)
    if reply.response is None:
        return "timeout"
    return "gateway_absent" if reply.exc in GATEWAY_ABSENT else "present"


async def find_units(link: ScanLink, units: list[int]) -> dict[str, Any]:
    """Sweep ``units``; stop early on a silent link."""
    found: dict[str, Any] = {"present": [], "gateway_absent": [], "silent": False}
    heard, timeouts = False, 0
    for unit in units:
        status = await probe_unit(link, unit)
        if status == "timeout":
            timeouts += 1
            if not heard and timeouts >= SILENT_AFTER:
                found["silent"] = True
                break
            continue
        heard = True
        found[status].append(unit)
    return found


async def autodetect_serial(
    link: ScanLink, units: list[int], combos: tuple[tuple[int, str, int], ...] = SERIAL_COMBOS
) -> dict[str, Any] | None:
    """Try the link's own serial settings, then ``combos``; the first reply wins.

    The link stays open on the winning settings.
    """
    e = link.endpoint
    own = (e.get("baudrate", 9600), e.get("parity", "N"), e.get("stopbits", 1))
    for baudrate, parity, stopbits in dict.fromkeys((own, *combos)):
        settings = {"baudrate": baudrate, "parity": parity, "stopbits": stopbits}
        if not await link.open(**settings):
            return None
        for unit in units:
            if await probe_unit(link, unit) != "timeout":
                return {**settings, "unit_id": unit}
    return None


# ---------------------------------------------------------------------------
# S1 identify, SunSpec marker
# ---------------------------------------------------------------------------


def _text(raw: bytes) -> str | None:
    """Printable ASCII rendering ('.' for other bytes), or None if mostly binary."""
    shown = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
    return shown if sum(32 <= b < 127 for b in raw) >= max(3, len(raw) // 2) else None


async def identify(link: ScanLink, unit: int) -> dict[str, Any]:
    """FC 43/14 objects (extended, else regular, else basic) and FC 17 server id."""
    ident: dict[str, Any] = {}
    objects: dict[int, Any] = {}
    for read_code in (3, 2, 1):
        object_id, reply = 0, Reply()
        for _ in range(16):  # "more follows" pages
            reply = await link.request(
                "read_device_information", unit, read_code=read_code, object_id=object_id
            )
            if not reply.ok:
                break
            objects.update(reply.response.information)
            if not reply.response.more_follows:
                break
            object_id = reply.response.next_object_id
        if objects or reply.exc in (0x01, None):
            break  # got them, unsupported, or no response: lower codes won't help
    if objects:
        ident["device_id"] = {
            DEVICE_ID_NAMES.get(k, f"0x{k:02X}"): _decode_object(v)
            for k, v in sorted(objects.items())
        }
    reply = await link.request("report_device_id", unit)
    if reply.ok:
        raw = bytes(reply.response.identifier)
        ident["server_id"] = {"hex": raw.hex(), "ascii": _text(raw)}
    return ident


def _decode_object(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_decode_object(v) for v in value)
    return value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value)


async def detect_sunspec(link: ScanLink, unit: int) -> int | None:
    """Wire (0-based) holding address of the 'SunS' marker, or None."""
    for base in SUNSPEC_BASES:
        reply = await link.request("read_holding_registers", unit, address=base, count=2)
        if reply.values == SUNS_MARKER:
            return base
    return None


# ---------------------------------------------------------------------------
# S3 ranges
# ---------------------------------------------------------------------------


class RangeFinder:
    """Valid address runs per table, learning the device's largest read on the way."""

    def __init__(self, link: ScanLink, unit: int, block: int = MODBUS_MAX_READ_COUNT) -> None:
        """One finder per table kind: bit and register reads have separate limits."""
        self.link = link
        self.unit = unit
        self.block = block
        self.learned_block: int | None = None

    async def _ok(self, table: str, address: int, count: int) -> bool:
        return (await self.link.read(table, self.unit, address, count)).ok

    async def _run_from(self, table: str, a: int, hi: int) -> tuple[int, bool]:
        """Valid addresses from known-valid-or-unknown ``a``: (count, next is invalid)."""
        n = min(self.block, hi - a)
        if await self._ok(table, a, n):
            return n, False
        if n == 1 or not await self._ok(table, a, 1):
            return 0, True
        lo, top = 1, n - 1  # longest ok prefix; monotone (holes and size limit alike)
        while lo < top:
            mid = (lo + top + 1) // 2
            lo, top = (mid, top) if await self._ok(table, a, mid) else (lo, mid - 1)
        if await self._ok(table, a + lo, 1):
            # [a, a+lo] is all valid yet failed as one read: the device's size limit.
            self.block = self.learned_block = lo
            return lo, False
        return lo, True

    async def _run_start(self, table: str, lo: int, a: int) -> int:
        """Smallest x in [lo, a] with [x, a] all valid (``a`` is valid).

        Binary search one read size back at a time. A valid ``top - 1`` that
        failed as part of one read is the device's size limit, learned here
        when no run was long enough to learn it forward.
        """
        while True:
            bound = max(lo, a - self.block + 1)
            floor, top = bound, a
            while floor < top:
                mid = (floor + top) // 2
                if await self._ok(table, mid, a - mid + 1):
                    top = mid
                else:
                    floor = mid + 1
            if top == lo or not await self._ok(table, top - 1, 1):
                return top
            if top > bound and self.learned_block is None:
                self.block = self.learned_block = a - top + 1
            a = top - 1

    async def window(self, table: str, lo: int, hi: int, runs: list[tuple[int, int]]) -> None:
        """Append valid half-open runs within [lo, hi) to ``runs``."""
        a, misses, last_miss = lo, 0, lo - 1
        while a < hi:
            if not misses:
                got, end_invalid = await self._run_from(table, a, hi)
                if got:
                    if runs and runs[-1][1] == a:
                        runs[-1] = (runs[-1][0], a + got)
                    else:
                        runs.append((a, a + got))
                a += got
                if end_invalid:
                    misses, last_miss, a = 1, a, a + 1
                continue
            stride = next((s for m, s in _STRIDES if misses >= m), 1)
            a = -(-a // stride) * stride
            if a >= hi:
                break
            if not await self._ok(table, a, 1):
                misses += a - last_miss
                last_miss, a = a, a + 1
                continue
            a = await self._run_start(table, last_miss + 1, a)
            misses = 0

    async def table(
        self, table: str, windows: list[tuple[int, int]], runs: list[tuple[int, int]]
    ) -> str:
        """Append ``table``'s valid runs over inclusive ``windows``; return a status.

        Runs found before a BudgetExceeded stay in ``runs``.
        """
        try:
            for lo, hi in windows:
                await self.window(table, lo, hi + 1, runs)
        except Unsupported:
            return "unsupported"
        return "ok"


class _Read:
    """One auto-scan read, awaited inside RangeFinder and answered by the poll loop."""

    def __init__(self, table: str, address: int, count: int) -> None:
        self.table, self.address, self.count = table, address, count

    def __await__(self) -> Any:
        return (yield self)


class _HandOff:
    """RangeFinder's link for Discovery: every read is yielded to the driver."""

    async def read(self, table: str, unit: int, address: int, count: int) -> Reply:
        reply = await _Read(table, address, count)
        if reply.exc == 0x01:
            raise Unsupported(table)
        return reply


class Discovery:
    """Auto-scan: the scan's range finder over the default windows, one read at a time.

    The finder coroutine is driven by hand, not by the event loop: ``pending``
    is its next read, which the device sends through its own request path when
    the scheduler gives it a turn, then hands back to ``answer``. Valid runs
    accumulate in ``runs``; ``found`` yields each new address once.
    """

    def __init__(self, rtu: bool, max_block: int, max_bit_block: int) -> None:
        link = _HandOff()
        self.words = RangeFinder(link, 0, max_block)
        self.bits = RangeFinder(link, 0, max_bit_block)
        self.windows = list(RTU_WINDOWS if rtu else TCP_WINDOWS)
        self.runs: dict[str, list[tuple[int, int]]] = {t: [] for t in TABLES}
        self.table: str | None = None
        self.misses = 0  # no-responses in a row to ``pending``
        self._reported = dict.fromkeys(TABLES, 0)  # wire addresses below are reported
        self._steps = self._run()
        self.pending: _Read | None = self._steps.send(None)

    async def _run(self) -> None:
        for table in TABLES:
            self.table = table
            finder = self.bits if table in BIT_REGISTER_TYPES else self.words
            await finder.table(table, self.windows, self.runs[table])
        self.table = None

    @property
    def done(self) -> bool:
        return self.pending is None

    def answer(self, values: list[Any] | int | None) -> None:
        """Feed ``pending``'s outcome (values, exception code, or None: a hole) and step."""
        self.misses = 0
        if isinstance(values, list):
            reply = Reply(values=values, response=values)
        else:
            reply = Reply(exc=values, response=values)
        try:
            self.pending = self._steps.send(reply)
        except StopIteration:
            self.pending = None
            self.table = None

    def found(self) -> Iterator[tuple[str, int]]:
        """(table, wire address) of every valid address not yielded before, in order."""
        for table, runs in self.runs.items():
            for lo, hi in runs:
                yield from ((table, a) for a in range(max(lo, self._reported[table]), hi))
            if runs:
                self._reported[table] = runs[-1][1]


# ---------------------------------------------------------------------------
# S4 classify
# ---------------------------------------------------------------------------


def _changes(col: list[int]) -> int:
    return sum(a != b for a, b in zip(col, col[1:], strict=False))


def _word_class(col: list[int]) -> str:
    if not col or _changes(col) == 0:
        return "constant"
    steps = [(b - a) & 0xFFFF for a, b in zip(col, col[1:], strict=False)]
    if all(s < 0x8000 for s in steps):
        return "counter"
    if all(bin(a ^ b).count("1") <= 1 for a, b in zip(col, col[1:], strict=False)):
        return "bitfield"
    return "analog"


def _plausible_float(x: float) -> bool:
    return math.isfinite(x) and (x == 0 or 1e-6 <= abs(x) <= 1e9)


#: Which of a 32-bit value's two words carries the high half, per order.
_MS_INDEX = {o: encode_value(0xFFFF0000, "uint32", byte_order=o).index(0xFFFF) for o in _ORDERS}


def _span(values: list[Any]) -> str:
    lo, hi = min(values), max(values)
    fmt = (lambda v: f"{v:.6g}") if isinstance(lo, float) else str
    return fmt(lo) if lo == hi else f"{fmt(lo)}..{fmt(hi)}"


def pair_verdict(w0: list[int], w1: list[int], order: str) -> dict[str, Any] | None:
    """float32 / counter uint32 verdict for two adjacent word columns, or None."""
    if not w0 or all(a == 0 and b == 0 for a, b in zip(w0, w1, strict=True)):
        return None
    ms, ls = (w0, w1) if _MS_INDEX[order] == 0 else (w1, w0)
    ms_moves, ls_moves = _changes(ms), _changes(ls)
    if ms_moves > ls_moves:
        return None  # the high word moves more than the low: wrong order or not a pair
    floats = [decode_value([a, b], "float32", 1, order) for a, b in zip(w0, w1, strict=True)]
    if all(_plausible_float(f) for f in floats) and any(f != 0 for f in floats):
        varying = ls_moves > 0
        return {
            "datatype": "float32",
            "class": "analog" if varying else "constant",
            "confidence": "high" if varying else "medium",
            "evidence": f"float32 {order} {_span(floats)}",
        }
    ints = [decode_value([a, b], "uint32", 1, order) for a, b in zip(w0, w1, strict=True)]
    rising = all(b >= a for a, b in zip(ints, ints[1:], strict=False)) and ints[-1] > ints[0]
    if rising and ms[0] != 0:
        carried = ms_moves > 0
        return {
            "datatype": "uint32",
            "class": "counter",
            "confidence": "high" if carried else "medium",
            "evidence": f"uint32 {order} {_span(ints)}" + (", low word carried" if carried else ""),
        }
    return None


def _string_at(cols: list[list[int]], i: int, order: str) -> int:
    """Registers in the constant ASCII string starting at ``i``, or 0.

    Words that are all plausible float32 pairs under ``order`` are not a
    string: a float32's high word is printable for any magnitude 2..1e9, so
    constant floats (a static device, or one sample) read as text.
    """

    def text(w: int, last: bool = False) -> bool:
        hi, lo = w >> 8, w & 0xFF
        return 32 <= hi < 127 and (32 <= lo < 127 or (last and lo == 0))

    def const(col: list[int]) -> bool:
        return bool(col) and _changes(col) == 0

    j = i
    while j < len(cols) and const(cols[j]) and text(cols[j][0]):
        j += 1
    if j < len(cols) and const(cols[j]) and text(cols[j][0], True):
        j += 1  # odd-length text ends in a NUL low byte
    chars = b"".join(c[0].to_bytes(2) for c in cols[i:j])
    if j - i < 3 or sum(chr(b).isalpha() for b in chars) < 3:
        return 0
    pairs = [[cols[k][0], cols[k + 1][0]] for k in range(i, j - 1, 2)]
    floats = [decode_value(p, "float32", 1, order) for p in pairs]
    return 0 if all(_plausible_float(f) and f != 0 for f in floats) else j - i


#: walk() verdict for a string run.
STRING: dict[str, Any] = {"datatype": "string"}


def classify_words(
    table: str, start: int, cols: list[list[int]]
) -> tuple[list[dict[str, Any]], str | None]:
    """Draft registers for one contiguous word run and the byte order picked.

    ``cols[i]`` holds the samples of wire address ``start + i``; the drafts are
    1-based. The run is walked greedily, under the one byte order that
    explains it best (devices rarely mix orders), for strings, then 32-bit
    pairs, then single words. Float32 pairs rank orders first: a plausible
    float needs its exponent byte in place, while a rising uint32 can pair any
    two unrelated words, and shifting an aligned float block by one word pairs
    every low word with the next high word, which also decodes plausibly.
    """
    prefix = TABLES[table]

    def walk(order: str) -> list[tuple[int, int, dict[str, Any] | None]]:
        out, i = [], 0
        while i < len(cols):
            if n := _string_at(cols, i, order):
                out.append((i, n, STRING))
                i += n
                continue
            v = pair_verdict(cols[i], cols[i + 1], order) if i + 1 < len(cols) else None
            out.append((i, 2 if v else 1, v))
            i += 2 if v else 1
        return out

    def score(order: str) -> tuple[int, int]:
        """(float32 points, all points); a high-confidence pair is 2 points."""
        points = [
            (v["datatype"], 2 if v["confidence"] == "high" else 1)
            for _, _, v in walk(order)
            if v and v is not STRING
        ]
        return sum(p for t, p in points if t == "float32"), sum(p for _, p in points)

    best = max(_ORDERS, key=lambda o: (*score(o), -_ORDERS.index(o)))
    used_order = best if score(best)[1] else None
    regs = []
    for i, width, verdict in walk(best):
        address = start + i + 1
        reg: dict[str, Any] = {"name": f"{prefix}{address}", "type": table, "address": address}
        col = cols[i]
        if verdict is STRING:
            text = b"".join(c[0].to_bytes(2) for c in cols[i : i + width])
            text = text.split(b"\0", 1)[0].decode("ascii")
            # Text is identity (model, serial, firmware): polled slowly. Numeric
            # constants keep the device rate: a quiet alarm looks the same.
            reg |= {"datatype": "string", "length": width, "rate": SLOW_RATE}
            info = ("constant", "high" if width >= 4 else "medium", f"ascii {text!r}")
        elif verdict:
            reg |= {"datatype": verdict["datatype"], "byte_order": best}
            info = (verdict["class"], verdict["confidence"], verdict["evidence"])
        else:
            reg["datatype"] = "uint16"
            cls = _word_class(col)
            if not col:
                info = ("unsampled", "low", "not sampled")
            elif cls == "constant" and col[0] == 0:
                info = (cls, "low", "always 0 (unmapped on zero-fill devices?)")
            else:
                info = (cls, "medium", f"uint16 {_span(col)}")
        cls, confidence, evidence = info
        n = len(col)
        reg |= {
            "writable": False,
            "confidence": confidence,
            "description": f"{cls}; {evidence}" + (f" ({n} samples)" if n else ""),
        }
        regs.append(reg)
    return regs, used_order


def classify_bits(table: str, start: int, cols: list[list[bool]]) -> list[dict[str, Any]]:
    """Draft registers (1-based) for one contiguous bit run from wire ``start``."""
    prefix = TABLES[table]
    regs = []
    for i, col in enumerate(cols):
        moves = _changes(col)
        if not col:
            confidence, desc = "low", "unsampled; not sampled"
        elif moves:
            confidence, desc = "high", f"toggling; {moves} changes"
        elif not col[0]:
            confidence, desc = "low", "constant; always off (unmapped on zero-fill devices?)"
        else:
            confidence, desc = "medium", "constant; always on"
        regs.append(
            {
                "name": f"{prefix}{start + i + 1}",
                "type": table,
                "address": start + i + 1,
                "datatype": "bool",
                "writable": False,
                "confidence": confidence,
                "description": desc + (f" ({len(col)} samples)" if col else ""),
            }
        )
    return regs


async def sample_runs(
    link: ScanLink,
    unit: int,
    table: str,
    runs: list[tuple[int, int]],
    block: int,
    samples: int,
    period: float,
    cols: dict[int, list[Any]],
) -> None:
    """Read every valid address ``samples`` times over ``period`` seconds.

    Fills ``cols`` (address -> samples), which keeps the samples taken before
    a BudgetExceeded. A sample whose read failed is dropped for the whole run,
    so columns within a run stay aligned.
    """
    for i in range(samples):
        if i:
            await link.sleep(period / max(1, samples - 1))
        for lo, hi in runs:
            values: list[Any] = []
            for a in range(lo, hi, block):
                reply = await link.read(table, unit, a, min(block, hi - a))
                if not reply.ok or reply.values is None:
                    break
                values.extend(reply.values)
            if len(values) == hi - lo:
                for a, v in zip(range(lo, hi), values, strict=True):
                    cols.setdefault(a, []).append(v)


# ---------------------------------------------------------------------------
# Orchestration + S5 emit
# ---------------------------------------------------------------------------


async def scan_device(
    link: ScanLink,
    unit: int,
    tables: list[str],
    windows: list[tuple[int, int]],
    samples: int,
    period: float,
    cutoffs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """S1-S5 for one present unit: (device report, draft map or None).

    Past the global deadline no stage sends more requests; what was found and
    sampled so far is still classified.
    """
    device: dict[str, Any] = {"unit_id": unit}
    started = time.monotonic()
    try:
        with link.stage("identify"):
            device["identity"] = await identify(link, unit)
            sunspec = await detect_sunspec(link, unit)
    except BudgetExceeded as e:
        cutoffs.append({"stage": "identify", "unit_id": unit, "reason": str(e)})
        device.setdefault("identity", {})
        sunspec = None
    if sunspec is not None:
        device["sunspec"] = {
            "base": sunspec + 1,
            "message": "SunSpec device: set register_map to sunspec",
        }
        return device, None  # SunSpec is its own map source; no draft

    words = RangeFinder(link, unit, MODBUS_MAX_READ_COUNT)
    bits = RangeFinder(link, unit, MODBUS_MAX_BIT_READ_COUNT)
    found: dict[str, list[tuple[int, int]]] = {}
    device["tables"] = {}
    with link.stage("ranges"):
        for table in tables:
            if link.expired:
                break
            finder = bits if table in BIT_REGISTER_TYPES else words
            runs: list[tuple[int, int]] = []
            try:
                status = await finder.table(table, windows, runs)
            except BudgetExceeded as e:
                cutoffs.append(
                    {"stage": "ranges", "unit_id": unit, "table": table, "reason": str(e)}
                )
                status = "cut short (budget)"
            found[table] = runs
            device["tables"][table] = {"status": status, "ranges": _ranges(runs)}

    events: dict[str, list[dict[str, Any]]] = {}
    orders: dict[str, str] = {}
    with link.stage("classify"):
        for table, runs in found.items():
            cols: dict[int, list[Any]] = {}
            block = (bits if table in BIT_REGISTER_TYPES else words).block
            try:
                if not link.expired:
                    await sample_runs(link, unit, table, runs, block, samples, period, cols)
            except BudgetExceeded as e:
                cutoffs.append(
                    {"stage": "classify", "unit_id": unit, "table": table, "reason": str(e)}
                )
            for lo, hi in runs:
                run_cols = [cols.get(a, []) for a in range(lo, hi)]
                if table in BIT_REGISTER_TYPES:
                    regs = classify_bits(table, lo, run_cols)
                else:
                    regs, order = classify_words(table, lo, run_cols)
                    if order:
                        orders[f"{table}/b{lo + 1}"] = order
                events[f"{table}/b{lo + 1}"] = regs

    device["max_block_size"] = words.learned_block
    device["max_bit_block_size"] = bits.learned_block
    device["word_orders"] = orders
    device["confidence"] = dict(Counter(r["confidence"] for regs in events.values() for r in regs))
    device["elapsed_s"] = round(time.monotonic() - started, 2)
    if not events:
        return device, None
    product = device["identity"].get("device_id", {}).get("ProductCode")
    draft: dict[str, Any] = {
        "name": product or link.label,
        "description": (
            f"Draft from scan of {link.label} unit {unit} at "
            f"{datetime.now(UTC).isoformat(timespec='seconds')}. "
            "Review names, types and scaling before use."
        ),
    }
    limits = {"max_block_size": words.learned_block, "max_bit_block_size": bits.learned_block}
    if limits := {k: v for k, v in limits.items() if v}:
        draft["device"] = limits
    draft["events"] = events
    return device, draft


async def _reach(
    link: ScanLink,
    report: dict[str, Any],
    units: list[int] | None,
    autodetect: bool,
    configured_unit: int | None,
    quick: bool = False,
) -> list[int]:
    """S0: open the link (autodetecting serial settings) and find the units.

    Without ``units``: TCP probes the configured unit, 1, 0 and 255 and keeps
    the first to answer (a direct device answers any id); a 0A/0B reply (a
    gateway) or no answer at all falls through to a sweep. RTU sweeps. A sweep
    covers the configured unit and 1-247, or with ``quick`` 1-10 and 247; a
    silent link stops it after SILENT_AFTER timeouts.
    """
    rtu = link.endpoint.get("transport") == Transport.RTU
    first = [configured_unit] if configured_unit is not None else []
    if autodetect and rtu:
        if not await link.open():  # no settings help a port that won't open
            report |= {"serial": None, "error": f"cannot open {link.label}"}
            return []
        with link.stage("autodetect"):
            likely = list(dict.fromkeys(first + list(AUTODETECT_UNITS)))
            report["serial"] = await autodetect_serial(link, likely)
        if not report["serial"]:
            return []
    elif not await link.open():
        report["error"] = f"cannot open {link.label}"
        return []
    report["endpoint"] = link.label
    wide = list(dict.fromkeys(first + list(QUICK_UNITS if quick else RTU_UNITS)))
    sweep = units or (wide if rtu else list(dict.fromkeys(first + list(TCP_UNITS))))
    with link.stage("reach"):
        found = await find_units(link, sweep)
        if not units and not rtu:
            if found["gateway_absent"] or not found["present"]:
                found = await find_units(link, wide)
            else:
                found["present"] = found["present"][:1]
    report["units"] = found
    return found["present"]


async def _finish(link: ScanLink, report: dict[str, Any], started: float) -> None:
    """Close the link; add serial diagnostics for a silent RTU link, and totals."""
    await link.close()
    silent = report.get("units", {}).get("silent") or report.get("serial", True) is None
    if link.endpoint.get("transport") == Transport.RTU and silent:
        report["diagnostics"] = await asyncio.to_thread(
            diagnose_serial_port, link.endpoint["serial_port"]
        )
    report |= {
        "requests": link.requests,
        "requests_by_fc": {f"{fc:02d}": n for fc, n in sorted(link.by_fc.items())},
        "no_response": link.timeouts,
        "request_gap_ms": link.delay_ms,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


async def scan(
    endpoint: dict[str, Any],
    *,
    units: list[int] | None = None,
    tables: list[str] | None = None,
    windows: list[tuple[int, int]] | None = None,
    samples: int = DEFAULT_SAMPLES,
    period: float = DEFAULT_PERIOD,
    timeout: float = DEFAULT_TIMEOUT,
    delay_ms: int | None = None,
    max_seconds: float | None = None,
    autodetect: bool = False,
    configured_unit: int | None = None,
) -> dict[str, Any]:
    """Scan one endpoint: ``{"report": ..., "maps": {unit id: draft map}}``.

    ``endpoint`` holds ModbusConnection link kwargs (transport, host/port or
    serial settings). Units as in ``_reach`` (full sweep); ``windows`` are
    inclusive wire pairs, defaulting per transport.
    """
    rtu = endpoint.get("transport") == Transport.RTU
    link = ScanLink(endpoint, timeout=timeout, delay_ms=delay_ms, max_seconds=max_seconds)
    started = time.monotonic()
    report: dict[str, Any] = {"endpoint": link.label, "transport": endpoint.get("transport")}
    cutoffs: list[dict[str, Any]] = []
    maps: dict[int, dict[str, Any]] = {}
    try:
        for unit in await _reach(link, report, units, autodetect, configured_unit):
            if link.expired:
                break  # the cutoff is already recorded
            device, draft = await scan_device(
                link,
                unit,
                tables or list(TABLES),
                windows or list(RTU_WINDOWS if rtu else TCP_WINDOWS),
                samples,
                period,
                cutoffs,
            )
            report.setdefault("devices", []).append(device)
            if draft:
                maps[unit] = draft
    except BudgetExceeded as e:
        cutoffs.append({"stage": "scan", "reason": str(e)})
    finally:
        await _finish(link, report, started)
    report |= {"cutoffs": cutoffs, "notes": NOTES}
    return {"report": report, "maps": maps}


async def sweep(
    endpoint: dict[str, Any],
    *,
    autodetect: bool = False,
    configured_unit: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    delay_ms: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Quick S0 (configured unit, 1-10, 247), identity and the SunSpec marker per unit."""
    link = ScanLink(endpoint, timeout=timeout, delay_ms=delay_ms, max_seconds=max_seconds)
    started = time.monotonic()
    report: dict[str, Any] = {"endpoint": link.label, "identity": {}, "sunspec": {}}
    cutoffs: list[dict[str, Any]] = []
    try:
        for unit in await _reach(link, report, None, autodetect, configured_unit, quick=True):
            report["identity"][unit] = {}
            try:
                with link.stage("identify"):
                    report["identity"][unit] = await identify(link, unit)
                    if (base := await detect_sunspec(link, unit)) is not None:
                        report["sunspec"][unit] = base + 1
            except DeadlineReached:
                raise
            except BudgetExceeded as e:  # this unit's identify budget; the next unit gets its own
                cutoffs.append({"stage": "identify", "unit_id": unit, "reason": str(e)})
    except BudgetExceeded as e:
        cutoffs.append({"stage": "sweep", "reason": str(e)})
    finally:
        await _finish(link, report, started)
    report["cutoffs"] = cutoffs
    return report


# ---------------------------------------------------------------------------
# Verify map
# ---------------------------------------------------------------------------


def _issues(reg: Register, raws: list[list[Any]]) -> list[str]:
    """What looks wrong about one register's reads; no inference beyond the datatype."""
    out = []
    if reg.type in BIT_REGISTER_TYPES:
        return out
    if all(not any(r) for r in raws):
        out.append("always 0 (unmapped on zero-fill devices?)")
    if reg.datatype in ("float32", "float64"):
        floats = [decode_value(r, reg.datatype, 1, reg.byte_order) for r in raws]
        if bad := [f for f in floats if not _plausible_float(f)]:
            out.append(f"implausible {reg.datatype} {json_safe(bad[0])}")
            if reg.datatype == "float32":
                good = [
                    o
                    for o in _ORDERS
                    if o != reg.byte_order
                    and all(_plausible_float(decode_value(r, "float32", 1, o)) for r in raws)
                ]
                if good:
                    out.append(f"plausible as byte_order {good[0]}")
    elif reg.datatype == "string":
        data = b"".join(w.to_bytes(2) for w in raws[0]).split(b"\0", 1)[0]
        if any(not 32 <= b < 127 for b in data):
            out.append("non-ASCII bytes in string")
    elif reg.invalid:
        pass  # the map already names its sentinels
    elif all(all(w == 0xFFFF for w in r) for r in raws):
        out.append("all ones (not-implemented sentinel?)")
    elif reg.datatype.startswith("int") and all(r[0] == 0x8000 for r in raws):
        out.append("0x8000 (not-implemented sentinel?)")
    return out


async def verify_map(
    endpoint: dict[str, Any],
    register_map: RegisterMap,
    unit: int = 1,
    *,
    samples: int = 2,
    interval: float = 1.0,
    timeout: float = DEFAULT_TIMEOUT,
    delay_ms: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Read every register of ``register_map`` ``samples`` times; report problems.

    Each register is read on its own, so an exception names its register. A
    cutoff (max_seconds, default VERIFY_MAX_SECONDS, or SILENT_AFTER requests
    in a row with no response, whatever answered before) leaves the registers
    not read yet out of ``ok``, counted in ``unchecked``.
    """
    max_seconds = max_seconds or VERIFY_MAX_SECONDS
    link = ScanLink(endpoint, timeout=timeout, delay_ms=delay_ms, max_seconds=max_seconds)
    started = time.monotonic()
    report: dict[str, Any] = {
        "endpoint": link.label,
        "unit_id": unit,
        "map": register_map.name,
        "registers": len(register_map.registers),
    }
    problems: list[dict[str, Any]] = []
    reads: dict[tuple[str, str], list[Reply]] = {}
    try:
        if not await link.open():
            report["error"] = f"cannot open {link.label}"
            return report
        for i in range(samples):
            if i:
                await link.sleep(interval)
            for event, regs in register_map.events.items():
                for reg in regs:
                    reply = await link.request(
                        READ_METHODS[reg.type], unit, address=reg.address, count=reg.address_span
                    )
                    reads.setdefault((event, reg.name), []).append(reply)
                    if link.silent >= SILENT_AFTER:
                        raise BudgetExceeded(f"no response to the last {link.silent} requests")
    except BudgetExceeded as e:
        report["cutoff"] = str(e)
    finally:
        await link.close()
    for event, regs in register_map.events.items():
        for reg in regs:
            replies = reads.get((event, reg.name))
            if not replies:
                continue
            row = {"path": f"{event}/{reg.name}", "type": reg.type, "address": reg.map_address}
            failed = next((r for r in replies if not r.ok), None)
            if failed:
                code = failed.exc
                issue = f"exception {code:02X}" if code is not None else "no response"
                problems.append(row | {"issues": [issue]})
                continue
            raws = [r.values for r in replies]
            if issues := _issues(reg, raws):
                value = json_safe(decode_register(reg, raws[-1]))
                problems.append(row | {"value": value, "issues": issues})
    report |= {
        "ok": len(reads) - len(problems),
        "unchecked": report["registers"] - len(reads),
        "problems": problems,
        "requests": link.requests,
        "request_gap_ms": link.delay_ms,
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    return report
