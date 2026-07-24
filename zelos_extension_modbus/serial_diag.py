"""Proactive serial-port failure diagnostics for RTU connections.

Best-effort, dependency-free probes that turn an opaque
``[Errno 5] Input/output error`` on a serial open into a compact block of
concrete, actionable findings plus suggested shell commands.

Every probe is guarded so a single failing check never aborts the rest, and all
filesystem roots are injectable so the whole module is testable against tmp
dirs with no real hardware. ``diagnose_serial_port`` is the seam; everything
else is a private helper.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

try:  # Unix-only; absent on Windows.
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-Unix
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

# Serial-port device-node glob patterns. ttyUSB*/ttyACM* are the Linux USB
# adapters; tty.*/cu.* are the macOS equivalents (only meaningful there).
_LINUX_GLOBS = ("ttyUSB*", "ttyACM*")
_MAC_GLOBS = ("tty.*", "cu.*")

# Kernel tty basenames whose liveness is visible under /sys/class/tty.
_SYSFS_TTY_PREFIXES = ("ttyUSB", "ttyACM")

# Open-probe outcomes, used to steer the later probes.
_OK = "ok"
_STALE = "stale"
_BUSY = "busy"
_OTHER = "other"

# Suggested follow-up commands + the usual suspects. Always appended on failure.
# These are NOT executed here (several need root); they are breadcrumbs.
_BREADCRUMBS = (
    "next: sudo dmesg | tail -50   # USB disconnect / re-enumeration lines",
    "next: journalctl -k --since '-10 min' | grep -iE 'usb|tty'",
    "next: ls -l /dev/serial/by-id/   # stable names -> current /dev nodes",
    "usual suspects: ModemManager probing the port, brltty grabbing CH341/CP210x "
    "adapters (newer Ubuntu), USB autosuspend, unpowered hub / marginal cable",
)


def diagnose_serial_port(port: str, *, dev_root: str = "/dev", sys_root: str = "/sys") -> list[str]:
    """Best-effort diagnosis of why a serial port can't be used.

    Runs a sequence of independent, guarded probes (node existence, an OS-level
    open with errno differentiation, stable-name advice, sysfs liveness, and
    holder detection) and returns short, single-line, human-actionable findings
    followed by suggested follow-up commands. Never raises; a failing probe just
    contributes nothing.

    Args:
        port: The serial device path that failed (e.g. ``/dev/ttyUSB9``).
        dev_root: Device-node root, injectable for testing (default ``/dev``).
        sys_root: sysfs root, injectable for testing (default ``/sys``).

    Returns:
        A compact list of finding lines (roughly <= 12).
    """
    findings: list[str] = []
    exists = _exists(port)
    status = _OTHER

    if not exists:
        # 1. Node existence — the path is gone; a sibling may have re-enumerated.
        with contextlib.suppress(Exception):
            findings.append(f"node missing: {port} is absent from {dev_root}")
            findings.append(_candidates_line(dev_root, port))
    else:
        # 2. Open probe with errno differentiation.
        with contextlib.suppress(Exception):
            status = _probe_open(port, dev_root, findings)

    # 3. Stable-name advice (Linux; silent when serial/by-id is absent).
    with contextlib.suppress(Exception):
        _probe_by_id(port, dev_root, findings)

    if exists:
        # 4. sysfs liveness — definitive stale-node signal for ttyUSB*/ttyACM*.
        stale = False
        with contextlib.suppress(Exception):
            stale = _probe_sysfs(port, dev_root, sys_root, findings, stale_known=status == _STALE)

        # A clean open only means anything when sysfs agrees the node is live
        # (a udev leftover can be openable yet point at nothing).
        if status == _OK and not stale:
            findings.append(
                "port opens cleanly at the OS level — failure is likely protocol / wiring / "
                "unit-id, not the device node"
            )

        # 5. Holder detection (best effort; announce "none" only when EBUSY).
        with contextlib.suppress(Exception):
            _probe_holders(port, findings, busy=status == _BUSY)

    # 6. Breadcrumb commands (always, on any failure).
    findings.extend(_BREADCRUMBS)
    return findings


# ---------------------------------------------------------------------------
# Node existence + candidate listing
# ---------------------------------------------------------------------------


def _exists(path: str) -> bool:
    """Whether ``path`` resolves to something (following symlinks); never raises."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def _serial_candidates(dev_root: str) -> list[str]:
    """Existing serial device nodes under ``dev_root`` (natural-sorted)."""
    patterns = list(_LINUX_GLOBS)
    if sys.platform == "darwin":
        patterns += list(_MAC_GLOBS)
    root = Path(dev_root)
    found: set[str] = set()
    for pattern in patterns:
        with contextlib.suppress(OSError):
            found.update(str(p) for p in root.glob(pattern))
    return _natural_sort(found)


def _natural_sort(paths: set[str]) -> list[str]:
    """Sort paths so ttyUSB9 precedes ttyUSB10; fall back to plain sort."""
    try:
        return sorted(paths, key=_natural_key)
    except TypeError:  # pragma: no cover - defensive against mixed chunks
        return sorted(paths)


def _natural_key(text: str) -> list[object]:
    return [int(chunk) if chunk.isdigit() else chunk for chunk in re.split(r"(\d+)", text)]


def _candidates_line(dev_root: str, port: str) -> str:
    """A single line naming other serial nodes (so a re-enumerated sibling shows)."""
    others = [c for c in _serial_candidates(dev_root) if c != port]
    if others:
        return f"live serial candidates: {', '.join(others)}"
    return f"no serial candidates found under {dev_root} (adapter may be fully gone)"


# ---------------------------------------------------------------------------
# Open probe with errno differentiation
# ---------------------------------------------------------------------------


def _probe_open(port: str, dev_root: str, findings: list[str]) -> str:
    """Attempt a non-blocking OS open and classify the failure by errno.

    Returns one of ``_OK``/``_STALE``/``_BUSY``/``_OTHER`` so later probes can
    corroborate rather than duplicate.
    """
    try:
        fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as exc:
        return _classify_open_error(exc, port, dev_root, findings)
    os.close(fd)
    return _OK


def _classify_open_error(exc: OSError, port: str, dev_root: str, findings: list[str]) -> str:
    code = exc.errno
    if code == errno.EACCES:
        findings.append(_permission_finding(port))
        return _OTHER
    if code == errno.EBUSY:
        findings.append(f"open failed (EBUSY): {port} is held by another process (see below)")
        return _BUSY
    if code == errno.EIO:
        findings.append(
            f"open failed (EIO): stale device node — {port} disconnected while open and "
            "has likely re-enumerated under a new name"
        )
        findings.append(_candidates_line(dev_root, port))
        findings.append("fix: configure the /dev/serial/by-id/ path so reconnects survive this")
        return _STALE
    if code == errno.ENOENT:
        findings.append(f"open failed (ENOENT): {port} raced away mid-probe (re-enumerating)")
        findings.append(_candidates_line(dev_root, port))
        return _STALE
    label = errno.errorcode.get(code, str(code))
    findings.append(f"open failed ({label}): {exc.strerror}")
    return _OTHER


def _permission_finding(port: str) -> str:
    """EACCES detail: node owner/group/mode + whether the user is in that group."""
    try:
        info = Path(port).stat()
    except OSError:
        return f"open failed (EACCES): permission denied on {port}"
    mode = stat.filemode(info.st_mode)
    owner = _uid_name(info.st_uid)
    group = _gid_name(info.st_gid)
    base = f"open failed (EACCES): {port} is owned {owner}:{group} mode {mode}"
    if _in_group(info.st_gid):
        return base
    return f"{base}; you are not in '{group}' — run: sudo usermod -aG {group} $USER (then re-login)"


def _uid_name(uid: int) -> str:
    if pwd is not None:
        with contextlib.suppress(KeyError, OSError):
            return pwd.getpwuid(uid).pw_name
    return str(uid)


def _gid_name(gid: int) -> str:
    if grp is not None:
        with contextlib.suppress(KeyError, OSError):
            return grp.getgrgid(gid).gr_name
    return str(gid)


def _in_group(gid: int) -> bool:
    try:
        return gid == os.getgid() or gid in os.getgroups()
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Stable-name advice (Linux /dev/serial/by-id)
# ---------------------------------------------------------------------------


def _probe_by_id(port: str, dev_root: str, findings: list[str]) -> None:
    by_id = Path(dev_root) / "serial" / "by-id"
    try:
        entries = sorted(by_id.iterdir())
    except OSError:
        return  # No serial/by-id (macOS, or no adapters attached) — skip silently.
    if not entries:
        return
    port_target = _resolve(Path(port))
    names: list[str] = []
    matched: Path | None = None
    for entry in entries:
        names.append(entry.name)
        if _resolve(entry) == port_target:
            matched = entry
    if matched is not None:
        findings.append(f"stable name available: {matched} -> {port} (prefer this path in config)")
    else:
        listed = ", ".join(names[:4])
        findings.append(
            f"stable names present in serial/by-id ({listed}) but none resolve to {port}; "
            "configure a by-id path so reconnects survive re-enumeration"
        )


# ---------------------------------------------------------------------------
# sysfs liveness (Linux /sys/class/tty)
# ---------------------------------------------------------------------------


def _probe_sysfs(
    port: str, dev_root: str, sys_root: str, findings: list[str], *, stale_known: bool
) -> bool:
    """Append sysfs findings; return True when the node was flagged stale."""
    name = Path(port).name
    if not name.startswith(_SYSFS_TTY_PREFIXES):
        return False
    device = Path(sys_root) / "class" / "tty" / name / "device"
    if not _exists(str(device)):
        if stale_known:
            findings.append(f"sysfs confirms stale node: {device} is gone")
            return True
        others = [c for c in _serial_candidates(dev_root) if c != port]
        tail = f"; live candidates: {', '.join(others)}" if others else ""
        findings.append(
            f"stale node: {port} present in {dev_root} but missing from "
            f"{sys_root}/class/tty/{name}/device — adapter re-enumerated{tail}"
        )
        return True
    identity = _usb_identity(device)
    if identity:
        findings.append(f"adapter identified as {identity} (node is live in sysfs)")
    return False


def _usb_identity(device: Path) -> str:
    """Walk up from the tty ``device`` link to the USB device dir and read its ids."""
    node = _resolve(device)
    for _ in range(8):
        if (node / "idVendor").exists():
            break
        parent = node.parent
        if parent == node:
            return ""
        node = parent
    else:
        return ""
    vid = _read_attr(node / "idVendor")
    pid = _read_attr(node / "idProduct")
    product = _read_attr(node / "product")
    serial = _read_attr(node / "serial")
    parts: list[str] = []
    if vid or pid:
        parts.append(f"{vid or '????'}:{pid or '????'}")
    if product:
        parts.append(product)
    if serial:
        parts.append(f"serial {serial}")
    return " ".join(parts)


def _read_attr(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Holder detection
# ---------------------------------------------------------------------------


def _probe_holders(port: str, findings: list[str], *, busy: bool) -> None:
    # lsof enumerates every fd on the system (seconds on macOS) — only worth it
    # when the open probe actually reported EBUSY.
    holders = _scan_proc_holders(port) or (_scan_lsof(port) if busy else [])
    if holders:
        who = ", ".join(f"pid {pid} ({comm})" for pid, comm in holders)
        findings.append(f"port held by: {who}")
    elif busy:
        findings.append("no holder visible (holder scan is limited without root)")


def _scan_proc_holders(port: str) -> list[tuple[int, str]]:
    """Same-user processes with an fd symlinked to ``port`` via /proc (Linux)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    target = _resolve(Path(port))
    holders: list[tuple[int, str]] = []
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            fds = list((pid_dir / "fd").iterdir())
        except OSError:  # PermissionError included; other users' fds are opaque.
            continue
        for fd in fds:
            try:
                link = str(fd.readlink())
            except OSError:
                continue
            if link == port or _resolve(Path(link)) == target:
                holders.append((int(pid_dir.name), _comm(int(pid_dir.name))))
                break
    return holders


def _comm(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "comm").read_text().strip() or "?"
    except OSError:
        return "?"


def _scan_lsof(port: str) -> list[tuple[int, str]]:
    """Fall back to ``lsof <port>`` when available (2s timeout)."""
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    try:
        result = subprocess.run(
            [lsof, port], capture_output=True, text=True, timeout=2.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    by_pid: dict[int, str] = {}
    for line in result.stdout.splitlines()[1:]:  # drop the header row
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            by_pid.setdefault(int(parts[1]), parts[0])
    return list(by_pid.items())


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path
