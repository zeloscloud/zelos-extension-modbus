"""Shared constants for the Modbus extension."""

import re
from enum import StrEnum

import zelos_sdk

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


#: Default leading trace-source name (`advanced.prefix`, the `trace` CLI).
DEFAULT_PREFIX = "Modbus"

#: Source the extension's logs take when the prefix is cleared; with a prefix
#: they land in it as `<prefix>/log`. Both names are reserved as connection names.
LOG_SOURCE_NAME = "modbus_log"
RESERVED_CONNECTION_NAMES = ("log", LOG_SOURCE_NAME)


def trace_layout(prefix: str, connection: str, device: str) -> tuple[str, str]:
    """The one trace-naming rule: (source name, event prefix) for a device.

    With a prefix, one shared source carries every connection and a device's
    events read `<connection>/<device>/<event>`. Cleared, each connection owns
    its source and the events read `<device>/<event>`.
    """
    if prefix:
        return prefix, f"{connection}/{device}"
    return connection, device


def name_error(value: str, label: str, reserved: tuple[str, ...] = ()) -> str | None:
    """Why user-typed `value` is not a legal trace name, or None if it is.

    Names become trace path segments, so a separator (`/ . @ :`) would silently
    re-nest the tree. Anything the SDK sanitizer rewrites is rejected, not renamed.
    """
    if not value:
        return None
    if value in reserved:
        return f"Invalid {label} {value!r}: reserved for the extension's own logs."
    clean = zelos_sdk.sanitize_name(value, kind="source")
    if clean == value:
        return None
    offender = next((c for c, ok in zip(value, clean, strict=False) if c != ok), value[-1])
    return (
        f"Invalid {label} {value!r}: {offender!r} is not allowed. "
        "Use letters, digits, space, '_' or '-'."
    )


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

#: Raw-register event prefix per table, the Modbus spec's table names, so input
#: register 5 never shares holding register 5's event.
RAW_EVENT_PREFIX = {
    RegisterType.HOLDING: "holding_registers",
    RegisterType.INPUT: "input_registers",
    RegisterType.COIL: "coils",
    RegisterType.DISCRETE_INPUT: "discrete_inputs",
}


def raw_names(reg_type: str, address: int) -> tuple[str, str]:
    """(event, field) of a register traced without a map: `holding_registers/123`, `123_value`.

    ``address`` is in the device's address base. Each register is its own event.
    """
    return f"{RAW_EVENT_PREFIX[reg_type]}/{address}", f"{address}_value"


# Modbus caps a single register read at 125 addresses, a bit read at 2000 and
# a register write (FC 16) at 123.
MODBUS_MAX_READ_COUNT = 125
MODBUS_MAX_BIT_READ_COUNT = 2000
MODBUS_MAX_WRITE_COUNT = 123

# Fastest poll rate (seconds) we accept; below this a rate is almost certainly a mistake.
MIN_RATE = 0.01

#: Rate (s) for identity, nameplate and settings points (SunSpec, scan drafts).
SLOW_RATE = 60.0


class ByteOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"
    BIG_SWAP = "big_swap"
    LITTLE_SWAP = "little_swap"


class WriteMode(StrEnum):
    AUTO = "auto"
    FC16 = "fc16"
