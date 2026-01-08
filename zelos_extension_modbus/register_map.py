"""Minimal JSON register map for human-readable Modbus register names.

The register map format is intentionally simple:

{
  "registers": [
    {
      "address": 0,
      "name": "voltage",
      "type": "holding",
      "datatype": "uint16",
      "unit": "V",
      "scale": 0.1
    }
  ]
}

Required fields: address, name
Optional fields: type (default: holding), datatype (default: uint16), unit, scale (default: 1.0)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Supported register types
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


@dataclass
class Register:
    """A single Modbus register definition."""

    address: int
    name: str
    type: str = "holding"
    datatype: str = "uint16"
    unit: str = ""
    scale: float = 1.0
    description: str = ""

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


@dataclass
class RegisterMap:
    """Collection of register definitions loaded from JSON."""

    registers: list[Register] = field(default_factory=list)
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
            data: Dictionary with register definitions

        Returns:
            RegisterMap instance
        """
        registers = []
        for reg_data in data.get("registers", []):
            reg = Register(
                address=reg_data["address"],
                name=reg_data["name"],
                type=reg_data.get("type", "holding"),
                datatype=reg_data.get("datatype", "uint16"),
                unit=reg_data.get("unit", ""),
                scale=reg_data.get("scale", 1.0),
                description=reg_data.get("description", ""),
            )
            registers.append(reg)

        return cls(
            registers=registers,
            name=data.get("name", "modbus"),
            description=data.get("description", ""),
        )

    def get_by_type(self, register_type: str) -> list[Register]:
        """Get all registers of a specific type.

        Args:
            register_type: One of 'coil', 'discrete_input', 'input', 'holding'

        Returns:
            List of registers matching the type
        """
        return [r for r in self.registers if r.type == register_type]

    def get_by_name(self, name: str) -> Register | None:
        """Find register by name.

        Args:
            name: Register name

        Returns:
            Register if found, None otherwise
        """
        for reg in self.registers:
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
        for reg in self.registers:
            if reg.address == address and reg.type == register_type:
                return reg
        return None
