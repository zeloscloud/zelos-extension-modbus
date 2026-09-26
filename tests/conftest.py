"""Shared test helpers: ephemeral ports and server readiness."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any


def free_port() -> int:
    """An OS-assigned free TCP port on 127.0.0.1."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _accepts(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_listening(port: int, timeout: float = 5.0) -> None:
    """Block until 127.0.0.1:``port`` accepts connections."""
    end = time.monotonic() + timeout
    while not _accepts(port):
        if time.monotonic() > end:
            raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout}s")
        time.sleep(0.02)


async def await_listening(port: int, timeout: float = 5.0) -> None:
    """``wait_listening`` for a server running on the caller's own event loop."""
    end = time.monotonic() + timeout
    while not _accepts(port):
        if time.monotonic() > end:
            raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout}s")
        await asyncio.sleep(0.02)


async def poll_once(dev: Any, now: float | None = None) -> dict[str, dict[str, Any]]:
    """Read every due block of ``dev`` at once (unpaced, this device alone); its values."""
    now = time.monotonic() if now is None else now
    for block in dev._schedule(now):
        if block.next_due <= now and now >= dev._retry_at:
            await dev._read_block(block, now)
    return dev._flush()
