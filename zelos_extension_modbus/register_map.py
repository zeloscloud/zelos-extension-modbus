"""Minimal JSON register map for human-readable Modbus register names.

The register map format uses user-defined events to group registers semantically:

{
  "name": "my_device",
  "device": {"max_block_size": 60, "write_mode": "fc16", "byte_order": "big_swap"},
  "events": {
    "temperature": [
      {"name": "pcb_temp", "type": "holding", "address": 123, "datatype": "uint16", "unit": "°C"},
      {"name": "overtemp", "type": "coil", "address": 456}
    ],
    "voltage/ac": [
      {"name": "phsA", "type": "holding", "address": 0, "datatype": "float32", "unit": "V"}
    ]
  }
}

Event names become Zelos trace events. Register names become fields within those events.
Register type (holding/input/coil/discrete_input) is just the Modbus protocol detail.

Required fields per register: address (in the map's address_base, default 1)
Optional fields: name (default: r<address>), type (default: holding),
datatype (default: uint16), unit, scale (default: 1.0),
rate (poll rate in seconds; default: the device rate; 0 or null = not polled,
actions still read and write it), writable (default false: read-only; true lets
the write actions set a holding register or coil), and:

- length: registers a "string" spans (required for strings). Strings decode
  2 ASCII bytes per register, high byte first (byte_order is ignored), end at
  the first NUL and drop trailing spaces. Strings are never writable.
- scale_ref: name of an integer register in the same event holding a power-of-10
  exponent; the value logs as raw * 10**exponent (null when the exponent is).
  Integer registers only, and not combined with scale. Both the register and
  its exponent register are read-only (a write could not know the exponent in
  force, and changing the exponent rescales the other value).
- invalid: raw values meaning "not implemented", logged as null. Compared
  against the unsigned value of the register's words (int16 -32768 is 32768).
  On a string only [0]: all NUL bytes.
- values: {"<int>": "<label>"} enum on an unscaled integer register, shown as
  labels in the trace.

Optional "device" block: quirks of the device model. max_block_size (1-125),
max_bit_block_size (1-2000), max_read_gap (>= 0) and write_mode (auto/fc16)
override the Advanced settings; byte_order is the default for registers that
don't set one. min_rate (s) floors every register's rate. close_after_sweep
closes the connection whenever polling goes idle (single-slot devices).
address_base (default 1) is the numbering of the map's addresses: 1 = the
user-layer convention of Kepware, Ignition and most vendor sheets (holding
40001 is wire address 40000), 0 = raw wire addresses. Everything user-facing
(names, catalog, logs, verify reports, raw actions) uses the map's base; only
the wire is 0-based. Unknown keys or bad values raise a ValueError at load.

Within a single event, register field names must be unique after sanitization
(the same rule the trace layer applies); duplicates raise a ValueError at load.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MIN_RATE,
    MODBUS_MAX_BIT_READ_COUNT,
    MODBUS_MAX_READ_COUNT,
    ByteOrder,
    RegisterType,
    WriteMode,
    sanitize_source_name,
)

logger = logging.getLogger(__name__)

# Supported register types (Modbus protocol)
REGISTER_TYPES = set(RegisterType)

# Supported data types and their register counts
DATATYPES = {
    "bool": 1,
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
    "uint64": 4,
    "int64": 4,
    "float64": 4,
    "string": None,  # spans `length` registers
}

INT_DATATYPES = {"uint16", "int16", "uint32", "int32", "uint64", "int64"}

# Supported byte orders for multi-register values
BYTE_ORDERS = set(ByteOrder)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Allowed map "device" keys -> (validator, expected-value description)
DEVICE_KEYS = {
    "max_block_size": (
        lambda v: _is_int(v) and 1 <= v <= MODBUS_MAX_READ_COUNT,
        f"an integer 1-{MODBUS_MAX_READ_COUNT}",
    ),
    "max_bit_block_size": (
        lambda v: _is_int(v) and 1 <= v <= MODBUS_MAX_BIT_READ_COUNT,
        f"an integer 1-{MODBUS_MAX_BIT_READ_COUNT}",
    ),
    "max_read_gap": (lambda v: _is_int(v) and v >= 0, "an integer >= 0"),
    "write_mode": (lambda v: v in set(WriteMode), f"one of {sorted(WriteMode)}"),
    "byte_order": (lambda v: v in BYTE_ORDERS, f"one of {sorted(BYTE_ORDERS)}"),
    "min_rate": (lambda v: _is_number(v) and v >= 0, "a number of seconds >= 0"),
    "close_after_sweep": (lambda v: isinstance(v, bool), "true or false"),
    "address_base": (lambda v: _is_int(v) and v in (0, 1), "0 or 1"),
}


def _parse_device(data: Any) -> dict[str, Any]:
    """Validate the optional map "device" block."""
    if not isinstance(data, dict):
        raise ValueError(f"'device' must be an object, got {data!r}")
    for key, value in data.items():
        if key not in DEVICE_KEYS:
            raise ValueError(f"Unknown 'device' key '{key}'. Must be one of {sorted(DEVICE_KEYS)}")
        valid, expected = DEVICE_KEYS[key]
        if not valid(value):
            raise ValueError(f"Invalid device {key} {value!r}: must be {expected}")
    return dict(data)


@dataclass
class Register:
    """A single Modbus register definition.

    ``address`` is the 0-based wire address; ``base`` is the map's numbering,
    used for everything shown to a user (``map_address``, the default name).
    """

    address: int
    name: str = ""
    type: str = RegisterType.HOLDING
    datatype: str = "uint16"
    unit: str = ""
    scale: float = 1.0
    byte_order: str = ByteOrder.BIG
    description: str = ""
    # Read-only unless the map opts in; input and discrete input are always read-only
    writable: bool = False
    # None = the device rate; 0 = not polled
    rate: float | None = None
    length: int | None = None
    scale_ref: str = ""
    invalid: list[int] = field(default_factory=list)
    values: dict[int, str] = field(default_factory=dict)
    base: int = 1
    # The scale_ref register, resolved by RegisterMap.from_dict
    ref: Register | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def map_address(self) -> int:
        """The address as the map numbers it (wire address + base)."""
        return self.address + self.base

    @property
    def count(self) -> int:
        """Number of 16-bit registers this value spans."""
        return self.length if self.datatype == "string" else DATATYPES.get(self.datatype, 1)

    @property
    def address_span(self) -> int:
        """Number of addresses this register occupies (1 for bits, count otherwise)."""
        return 1 if self.type in BIT_REGISTER_TYPES else self.count

    def __post_init__(self) -> None:
        """Validate register definition.

        A rate of 0 disables polling for this register; a negative value (or a
        positive value below MIN_RATE) is rejected.
        """
        if not self.name:
            self.name = f"r{self.map_address}"
        # Trace-safe field name, derived once (schema and rows must agree, and
        # this keeps the sanitizing regex out of the poll hot loop).
        self.field_name = sanitize_source_name(self.name, f"r{self.map_address}")
        # Reject a JSON string ("2") or bool (an int subclass) before the range check.
        if self.rate is not None and (not _is_number(self.rate) or self.rate < 0):
            raise ValueError(
                f"rate must be a number of seconds >= 0 (0 = not polled), got {self.rate!r}"
            )
        if self.rate is not None and 0 < self.rate < MIN_RATE:
            raise ValueError(f"rate must be 0 (not polled) or >= {MIN_RATE}s, got {self.rate}")
        if self.type not in REGISTER_TYPES:
            msg = f"Invalid register type '{self.type}'. Must be one of {REGISTER_TYPES}"
            raise ValueError(msg)
        if self.datatype not in DATATYPES:
            msg = f"Invalid datatype '{self.datatype}'. Must be one of {list(DATATYPES)}"
            raise ValueError(msg)
        if self.byte_order not in BYTE_ORDERS:
            msg = f"Invalid byte_order '{self.byte_order}'. Must be one of {BYTE_ORDERS}"
            raise ValueError(msg)
        if not isinstance(self.writable, bool):
            raise ValueError(f"Register '{self.name}': writable must be true or false")
        if not _is_number(self.scale) or not math.isfinite(self.scale) or self.scale == 0:
            raise ValueError(f"Register '{self.name}': scale must be a finite non-zero number")
        self._validate_decode()
        if self.address + self.address_span > 0x10000:
            raise ValueError(f"Register '{self.name}': spans past the last address")
        # Input registers and discrete inputs are read-only by Modbus spec
        if self.type in (RegisterType.INPUT, RegisterType.DISCRETE_INPUT):
            self.writable = False

    def _validate_decode(self) -> None:
        """Validate length, scale_ref, invalid and values against the datatype."""
        where = f"Register '{self.name}'"
        is_int = self.datatype in INT_DATATYPES and self.type not in BIT_REGISTER_TYPES
        if self.datatype == "string":
            if not _is_int(self.length) or not 1 <= self.length <= MODBUS_MAX_READ_COUNT:
                raise ValueError(
                    f"{where}: string needs 'length' (registers) 1-{MODBUS_MAX_READ_COUNT}"
                )
            if self.type in BIT_REGISTER_TYPES:
                raise ValueError(f"{where}: string must be a holding or input register")
            if self.writable:
                raise ValueError(f"{where}: string registers are not writable")
        elif self.length is not None:
            raise ValueError(f"{where}: 'length' only applies to datatype string")
        if self.scale_ref and (not is_int or self.scale != 1):
            raise ValueError(f"{where}: scale_ref needs an integer datatype and no 'scale'")
        if self.scale_ref and self.writable:
            raise ValueError(f"{where}: a scale_ref register is read-only")
        if self.invalid and (
            self.datatype == "bool"
            or self.type in BIT_REGISTER_TYPES
            or not all(_is_int(v) and v >= 0 for v in self.invalid)
            or (self.datatype == "string" and self.invalid != [0])
        ):
            raise ValueError(
                f"{where}: 'invalid' needs raw integers >= 0 on a numeric register, "
                "or [0] (all NUL) on a string"
            )
        if self.values:
            if not is_int or self.scale != 1 or self.scale_ref:
                raise ValueError(f"{where}: 'values' needs an unscaled integer register")
            try:
                self.values = {int(k): str(v) for k, v in self.values.items()}
            except ValueError:
                raise ValueError(f"{where}: 'values' keys must be integers") from None


def _resolve_scale_refs(event_name: str, registers: list[Register]) -> None:
    """Point each scale_ref register at its exponent register in the same event.

    The exponent is read-only: marking it writable is a load error.
    """
    by_name = {r.name: r for r in registers}
    for reg in registers:
        if not reg.scale_ref:
            continue
        ref = by_name.get(reg.scale_ref)
        is_int = ref is not None and ref.datatype in INT_DATATYPES
        if not is_int or ref is reg or ref.type in BIT_REGISTER_TYPES:
            msg = (
                f"Register '{reg.name}' in event '{event_name}': scale_ref '{reg.scale_ref}' "
                "must name an integer register in the same event"
            )
            raise ValueError(msg)
        if ref.writable:
            raise ValueError(
                f"Register '{ref.name}' in event '{event_name}': it is the scale_ref of "
                f"'{reg.name}', so it is read-only"
            )
        reg.ref = ref


@dataclass
class RegisterMap:
    """Collection of register definitions organized by user-defined events."""

    events: dict[str, list[Register]] = field(default_factory=dict)
    name: str = "modbus"
    description: str = ""
    # Validated "device" block (see module docstring)
    device: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> RegisterMap:
        """Load register map from JSON file.

        Args:
            path: Path to JSON file

        Returns:
            RegisterMap instance
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Register map file not found: {path}")

        with path.open() as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisterMap:
        """Load register map from dictionary.

        Args:
            data: Dictionary with event/register definitions

        Returns:
            RegisterMap instance
        """
        events: dict[str, list[Register]] = {}
        device = _parse_device(data.get("device", {}))
        default_byte_order = device.get("byte_order", ByteOrder.BIG)
        base = device.get("address_base", 1)

        for event_name, registers_data in data.get("events", {}).items():
            registers = []
            # Field names must be unique per event after sanitization, since the
            # trace layer collapses reserved characters and clobbers colliding
            # rows on log. Reject both raw and post-sanitization collisions here.
            seen_fields: dict[str, str] = {}
            for reg_data in registers_data:
                # Explicit JSON null means not polled (same as 0); an absent key
                # keeps the device rate (None).
                rate = reg_data.get("rate")
                if "rate" in reg_data and rate is None:
                    rate = 0.0
                address = reg_data["address"]
                if not _is_int(address) or not base <= address <= 0xFFFF + base:
                    msg = (
                        f"Invalid address {address!r} in event '{event_name}': must be an "
                        f"integer {base}-{0xFFFF + base} (address_base {base})"
                    )
                    raise ValueError(msg)
                reg = Register(
                    address=address - base,
                    base=base,
                    name=reg_data.get("name", ""),
                    type=reg_data.get("type", "holding"),
                    datatype=reg_data.get("datatype", "uint16"),
                    unit=reg_data.get("unit", ""),
                    scale=reg_data.get("scale", 1.0),
                    byte_order=reg_data.get("byte_order", default_byte_order),
                    description=reg_data.get("description", ""),
                    writable=reg_data.get("writable", False),
                    rate=rate,
                    length=reg_data.get("length"),
                    scale_ref=reg_data.get("scale_ref", ""),
                    invalid=reg_data.get("invalid", []),
                    values=reg_data.get("values", {}),
                )
                field_name = reg.field_name
                if field_name in seen_fields:
                    msg = (
                        f"Duplicate field name '{field_name}' in event '{event_name}': "
                        f"registers '{seen_fields[field_name]}' and '{reg.name}' collide "
                        "(give registers sharing an address explicit 'name' fields)"
                    )
                    raise ValueError(msg)
                seen_fields[field_name] = reg.name
                registers.append(reg)
            _resolve_scale_refs(event_name, registers)
            events[event_name] = registers

        return cls(
            events=events,
            name=data.get("name", "modbus"),
            description=data.get("description", ""),
            device=device,
        )

    @property
    def address_base(self) -> int:
        """Numbering of the map's addresses: 1 (default) or 0 (wire)."""
        return self.device.get("address_base", 1)

    @property
    def registers(self) -> list[Register]:
        """Flat list of all registers across all events."""
        all_regs = []
        for regs in self.events.values():
            all_regs.extend(regs)
        return all_regs

    @property
    def event_names(self) -> list[str]:
        """List of all event names."""
        return list(self.events.keys())

    def get_event(self, event_name: str) -> list[Register]:
        """Get all registers for an event.

        Args:
            event_name: Name of the event

        Returns:
            List of registers for this event
        """
        return self.events.get(event_name, [])

    def get_by_name(self, name: str) -> Register | None:
        """Find register by name across all events.

        Args:
            name: Register name

        Returns:
            Register if found, None otherwise
        """
        for regs in self.events.values():
            for reg in regs:
                if reg.name == name:
                    return reg
        return None

    @property
    def writable_registers(self) -> list[Register]:
        """Flat list of all writable registers."""
        return [r for r in self.registers if r.writable]
