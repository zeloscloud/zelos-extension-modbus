"""Tests for the xlsx -> register-map converter script.

The converter lives in scripts/ (not an importable package), so it is loaded by
file path. It imports openpyxl at module load, hence the importorskip.
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("openpyxl")

_SCRIPT = Path(__file__).parent.parent / "scripts" / "xlsx_to_register_map.py"


def _load_converter():
    spec = importlib.util.spec_from_file_location("xlsx_to_register_map", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conv = _load_converter()


def _row(idx, name, rw="R", value=None, unit=None, desc=None):
    """Build a spreadsheet row tuple matching the converter's column order."""
    # (Register Index, Register Name, Read/Write, Resettable, Value, Units, Description)
    return (idx, name, rw, None, value, unit, desc)


class TestBuildRegisterMap:
    """Each row becomes its own single-register event."""

    def test_each_row_is_its_own_event(self):
        rows = [_row(0, "Frequency"), _row(1, "Voltage")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert set(result["events"]) == {"frequency", "voltage"}
        # Single field per event; field name == event name.
        assert len(result["events"]["frequency"]) == 1
        assert result["events"]["frequency"][0]["name"] == "frequency"
        assert result["events"]["voltage"][0]["name"] == "voltage"

    def test_duplicate_names_uniquified(self):
        rows = [_row(0, "Exercise Time"), _row(5, "Exercise Time")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert set(result["events"]) == {"exercise_time", "exercise_time_2"}
        assert result["events"]["exercise_time"][0]["name"] == "exercise_time"
        assert result["events"]["exercise_time"][0]["address"] == 0
        assert result["events"]["exercise_time_2"][0]["name"] == "exercise_time_2"
        assert result["events"]["exercise_time_2"][0]["address"] == 5

    def test_identical_rows_deduped(self):
        rows = [_row(0, "Frequency"), _row(0, "Frequency")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert set(result["events"]) == {"frequency"}

    def test_text_index_accepted(self):
        rows = [_row("8505", "Serial")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert result["events"]["serial"][0]["address"] == 8505

    def test_range_index_skipped(self):
        rows = [_row("10010-10015", "Reserved")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert result["events"] == {}

    def test_writable_flag(self):
        rows = [_row(0, "Setpoint", rw="R/W")]
        result = conv.build_register_map(rows, "dev", "desc")
        assert result["events"]["setpoint"][0]["writable"] is True


class TestParseUnit:
    """Unit-cell scale idioms."""

    def test_x10_scale(self):
        assert conv.parse_unit("V DC x10") == ("V DC", 0.1)

    def test_star_100_scale(self):
        assert conv.parse_unit("A *100") == ("A", 0.01)

    def test_hours_per_bit(self):
        assert conv.parse_unit("0.05 Hours/bit") == ("Hours", 0.05)

    def test_plain_unit(self):
        assert conv.parse_unit("Hz") == ("Hz", 1.0)

    def test_empty_unit(self):
        assert conv.parse_unit(None) == ("", 1.0)
