"""Shared constants for the Modbus extension."""

import re
from enum import StrEnum

# Trace source and action names are used as path segments by the Zelos agent
# (e.g. "source/event.field"), so "." and "/" are reserved separators.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_source_name(raw: str, fallback: str) -> str:
    """Return a trace-source/actions-safe name derived from ``raw``.

    Collapses runs of reserved/special characters to ``_`` and trims leading
    and trailing ``_``. Host addresses (``192.168.1.100``) and serial port
    paths (``/dev/ttyUSB0``) contain ``.`` and ``/``, which would otherwise
    fragment the path. Returns ``fallback`` if nothing usable remains.
    """
    cleaned = _UNSAFE_NAME_CHARS.sub("_", raw).strip("_")
    return cleaned or fallback


class Transport(StrEnum):
    TCP = "tcp"
    RTU = "rtu"


class RegisterType(StrEnum):
    HOLDING = "holding"
    INPUT = "input"
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"


# Bit-addressable types span one address each and return raw booleans (not words).
BIT_REGISTER_TYPES = {RegisterType.COIL, RegisterType.DISCRETE_INPUT}

# Modbus caps a single word/bit read at 125 addresses.
MODBUS_MAX_READ_COUNT = 125

# Fastest poll cadence we accept; below this a "rate" is almost certainly a mistake.
MIN_POLL_INTERVAL = 0.01


class ByteOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"
    BIG_SWAP = "big_swap"
    LITTLE_SWAP = "little_swap"


class WriteMode(StrEnum):
    AUTO = "auto"
    FC16 = "fc16"
