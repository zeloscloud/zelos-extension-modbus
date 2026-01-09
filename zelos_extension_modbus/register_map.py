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

Required fields per register: address, name
Optional fields: type (default: holding), datatype (default: uint16), unit, scale (default: 1.0)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Supported register types (Modbus protocol)
REGISTER_TYPES = {"coil", "discrete_input", "input", "holding"}

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
# big: AB CD (standard Modbus)
# little: DC BA
# big_swap: CD AB (common in some PLCs - big endian with word swap)
# little_swap: BA DC
BYTE_ORDERS = {"big", "little", "big_swap", "little_swap"}


@dataclass
class Register:
    """A single Modbus register definition."""

    address: int
    name: str
    type: str = "holding"
    datatype: str = "uint16"
    unit: str = ""
    scale: float = 1.0
    byte_order: str = "big"
    description: str = ""
    writable: bool = True

    @property
    def count(self) -> int:
        """Number of 16-bit registers this value spans."""
        return DATATYPES.get(self.datatype, 1)

    def __post_init__(self) -> None:
        """Validate register definition."""
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
        if self.type in ("input", "discrete_input"):
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
            for reg_data in registers_data:
                reg = Register(
                    address=reg_data["address"],
                    name=reg_data["name"],
                    type=reg_data.get("type", "holding"),
                    datatype=reg_data.get("datatype", "uint16"),
                    unit=reg_data.get("unit", ""),
                    scale=reg_data.get("scale", 1.0),
                    byte_order=reg_data.get("byte_order", "big"),
                    description=reg_data.get("description", ""),
                    writable=reg_data.get("writable", True),
                )
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
