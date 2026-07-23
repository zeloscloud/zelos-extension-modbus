"""Minimal JSON register map for human-readable Modbus register names.

The register map format uses user-defined events to group registers semantically:

{
  "name": "my_device",
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

Required fields per register: address
Optional fields: name (default: r<address>), type (default: holding),
datatype (default: uint16), unit, scale (default: 1.0),
poll_interval (per-register poll rate in seconds; default: interface interval;
0 or null disables polling)

Within a single event, register field names must be unique after sanitization
(the same rule the trace layer applies); duplicates raise a ValueError at load.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zelos_extension_modbus.constants import (
    BIT_REGISTER_TYPES,
    MIN_POLL_INTERVAL,
    ByteOrder,
    RegisterType,
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
}

# Supported byte orders for multi-register values
BYTE_ORDERS = set(ByteOrder)


@dataclass
class Register:
    """A single Modbus register definition."""

    address: int
    name: str = ""
    type: str = RegisterType.HOLDING
    datatype: str = "uint16"
    unit: str = ""
    scale: float = 1.0
    byte_order: str = ByteOrder.BIG
    description: str = ""
    writable: bool = True
    poll_interval: float | None = None

    @property
    def count(self) -> int:
        """Number of 16-bit registers this value spans."""
        return DATATYPES.get(self.datatype, 1)

    @property
    def address_span(self) -> int:
        """Number of addresses this register occupies (1 for bits, count otherwise)."""
        return 1 if self.type in BIT_REGISTER_TYPES else self.count

    @property
    def polled(self) -> bool:
        """Whether this register is polled (a poll_interval of 0 disables it)."""
        return self.poll_interval != 0

    def __post_init__(self) -> None:
        """Validate register definition.

        A poll_interval of 0 disables polling for this register; a negative
        value (or a positive value below MIN_POLL_INTERVAL) is rejected.
        """
        if not self.name:
            self.name = f"r{self.address}"
        # Trace-safe field name, derived once (schema and rows must agree, and
        # this keeps the sanitizing regex out of the poll hot loop).
        self.field_name = sanitize_source_name(self.name, f"r{self.address}")
        # Reject non-numeric poll_interval before the range check: a JSON string
        # ("2") would otherwise raise TypeError, and bool (an int subclass) would
        # sneak True/False through as 1/0.
        if self.poll_interval is not None and (
            isinstance(self.poll_interval, bool) or not isinstance(self.poll_interval, (int, float))
        ):
            msg = (
                "poll_interval must be a number of seconds (0 disables polling), "
                f"got {self.poll_interval!r}"
            )
            raise ValueError(msg)
        if self.poll_interval is not None and self.poll_interval < 0:
            msg = f"poll_interval must be >= 0 (0 disables polling), got {self.poll_interval}"
            raise ValueError(msg)
        if self.poll_interval is not None and 0 < self.poll_interval < MIN_POLL_INTERVAL:
            msg = (
                f"poll_interval must be 0 (disabled) or >= {MIN_POLL_INTERVAL}s, "
                f"got {self.poll_interval}"
            )
            raise ValueError(msg)
        if self.type not in REGISTER_TYPES:
            msg = f"Invalid register type '{self.type}'. Must be one of {REGISTER_TYPES}"
            raise ValueError(msg)
        if self.datatype not in DATATYPES:
            msg = f"Invalid datatype '{self.datatype}'. Must be one of {list(DATATYPES)}"
            raise ValueError(msg)
        if self.byte_order not in BYTE_ORDERS:
            msg = f"Invalid byte_order '{self.byte_order}'. Must be one of {BYTE_ORDERS}"
            raise ValueError(msg)
        # Input registers and discrete inputs are read-only by Modbus spec
        if self.type in (RegisterType.INPUT, RegisterType.DISCRETE_INPUT):
            self.writable = False


@dataclass
class RegisterMap:
    """Collection of register definitions organized by user-defined events."""

    events: dict[str, list[Register]] = field(default_factory=dict)
    name: str = "modbus"
    description: str = ""

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

        for event_name, registers_data in data.get("events", {}).items():
            registers = []
            # Field names must be unique per event after sanitization, since the
            # trace layer collapses reserved characters and clobbers colliding
            # rows on log. Reject both raw and post-sanitization collisions here.
            seen_fields: dict[str, str] = {}
            for reg_data in registers_data:
                # Explicit JSON null means "disabled" (same as 0); an absent key
                # keeps the interface-default rate (None).
                poll_interval = reg_data.get("poll_interval")
                if "poll_interval" in reg_data and poll_interval is None:
                    poll_interval = 0.0
                reg = Register(
                    address=reg_data["address"],
                    name=reg_data.get("name", ""),
                    type=reg_data.get("type", "holding"),
                    datatype=reg_data.get("datatype", "uint16"),
                    unit=reg_data.get("unit", ""),
                    scale=reg_data.get("scale", 1.0),
                    byte_order=reg_data.get("byte_order", "big"),
                    description=reg_data.get("description", ""),
                    writable=reg_data.get("writable", True),
                    poll_interval=poll_interval,
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
            events[event_name] = registers

        return cls(
            events=events,
            name=data.get("name", "modbus"),
            description=data.get("description", ""),
        )

    @property
    def registers(self) -> list[Register]:
        """Flat list of all registers across all events."""
        all_regs = []
        for regs in self.events.values():
            all_regs.extend(regs)
        return all_regs

    @property
    def polled_events(self) -> dict[str, list[Register]]:
        """Events mapped to only their polled registers, all-disabled events omitted."""
        result: dict[str, list[Register]] = {}
        for event_name, regs in self.events.items():
            polled = [r for r in regs if r.polled]
            if polled:
                result[event_name] = polled
        return result

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

    def get_by_address(self, address: int, register_type: str = "holding") -> Register | None:
        """Find register by address and type.

        Args:
            address: Register address
            register_type: Register type

        Returns:
            Register if found, None otherwise
        """
        for regs in self.events.values():
            for reg in regs:
                if reg.address == address and reg.type == register_type:
                    return reg
        return None

    @property
    def writable_registers(self) -> list[Register]:
        """Flat list of all writable registers."""
        return [r for r in self.registers if r.writable]

    @property
    def writable_names(self) -> list[str]:
        """List of all writable register names."""
        return [r.name for r in self.writable_registers]
