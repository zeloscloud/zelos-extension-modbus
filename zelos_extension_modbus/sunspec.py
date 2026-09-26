"""SunSpec discovery: build a RegisterMap from a device's model chain.

A device with `"register_map": "sunspec"` gets its map here at connect. Reads
are FC3 only, through the connection's request choke point: find the `SunS`
marker, walk the (id, length) model headers to the 0xFFFF end marker, and map
each model's points from the pysunspec2 model definitions (only its JSON
definitions are used, not its client). The result is an ordinary read-only
RegisterMap; polling, decoding and tracing know nothing about SunSpec.

Mapping: one event per model, `<model name>_<id>` (`common_1`, `inverter_103`;
a repeat of the same model gets `_2`, `_3`). One field per point, named after
the point; repeating groups flatten to `<group>_<n>_<point>` (1-based). Scale
factor links become `scale_ref` (a fixed sf becomes `scale`), enums become `values`, and the SunSpec
not-implemented values become `invalid` (logged as null).

Addresses: the walk runs on wire (0-based) addresses; the map, and every log
line, use the default address_base 1, matching SunSpec's documented 40001-based
numbering (the 'SunS' marker at 40001 is wire 40000).
"""

from __future__ import annotations

import functools
import logging
from importlib import resources
from typing import TYPE_CHECKING, Any

from sunspec2 import mdef

from zelos_extension_modbus.constants import SLOW_RATE, RegisterType
from zelos_extension_modbus.register_map import RegisterMap

if TYPE_CHECKING:
    from zelos_extension_modbus.client import ModbusDevice

logger = logging.getLogger(__name__)

#: 'SunS' marker, and the wire addresses it is probed at, in order.
SUNS_MARKER = [0x5375, 0x6E53]
BASE_ADDRESSES = (40000, 0, 50000)
END_MODEL_ID = 0xFFFF
MAX_MODELS = 100  # bound on a garbage chain

# SunSpec point type -> (datatype, not-implemented raw value or None)
POINT_TYPES: dict[str, tuple[str, int | None]] = {
    "int16": ("int16", 0x8000),
    "sunssf": ("int16", 0x8000),
    "uint16": ("uint16", 0xFFFF),
    "count": ("uint16", 0xFFFF),
    "enum16": ("uint16", 0xFFFF),
    "bitfield16": ("uint16", 0xFFFF),
    "acc16": ("uint16", None),
    "int32": ("int32", 0x80000000),
    "uint32": ("uint32", 0xFFFFFFFF),
    "enum32": ("uint32", 0xFFFFFFFF),
    "bitfield32": ("uint32", 0xFFFFFFFF),
    "acc32": ("uint32", None),
    "ipaddr": ("uint32", None),
    "int64": ("int64", 0x8000000000000000),
    "uint64": ("uint64", 0xFFFFFFFFFFFFFFFF),
    "bitfield64": ("uint64", 0xFFFFFFFFFFFFFFFF),
    "acc64": ("uint64", None),
    "float32": ("float32", 0x7FC00000),
    "float64": ("float64", 0x7FF8000000000000),
    "string": ("string", None),
}
ENUM_TYPES = {"enum16", "enum32"}
HEADER_POINTS = {"ID", "L"}  # model header, already known

#: Models polled at SLOW_RATE: identity (1 common), nameplate (120, and 702 DER
#: capacity, its 700-series successor) and basic settings (121). They change only
#: on reconfiguration; status and controls (122-126, 7xx) keep the device rate.
SLOW_MODELS = {1, 120, 121, 702}


class DiscoveryError(Exception):
    """The device is not a readable SunSpec device."""


async def _read(device: ModbusDevice, address: int, count: int) -> list[int] | None:
    """FC3 read; None on an exception response. No response (or a gateway's
    unit-absent answer) raises ModbusIOException."""
    raw = await device._fetch(RegisterType.HOLDING, address, count)
    return None if isinstance(raw, int) else raw


async def build_register_map(device: ModbusDevice) -> RegisterMap:
    """Discover the device's SunSpec models and map them.

    Raises:
        DiscoveryError: no `SunS` marker at any base address.
    """
    base = None
    for address in BASE_ADDRESSES:
        if await _read(device, address, 2) == SUNS_MARKER:
            base = address
            break
    if base is None:
        bases = ", ".join(str(a + 1) for a in BASE_ADDRESSES)
        raise DiscoveryError(f"no SunSpec marker 'SunS' at holding register {bases}")

    events: dict[str, list[dict[str, Any]]] = {}
    warned: set[str] = set()
    address = base + 2
    for _ in range(MAX_MODELS):
        header = await _read(device, address, 2) if address + 2 <= 0x10000 else None
        if header is None:
            logger.warning(f"SunSpec: no model header at {address + 1}; ending the model chain")
            break
        model_id, length = header
        if model_id == END_MODEL_ID:
            break
        name, registers = _map_model(model_id, address, length, warned)
        if registers:
            event, n = name, 1
            while event in events:
                n += 1
                event = f"{name}_{n}"
            events[event] = registers
        address += 2 + length
    else:
        logger.warning(f"SunSpec: stopped after {MAX_MODELS} models without an end marker")

    models = ", ".join(events) or "none"
    logger.info(f"SunSpec: base {base + 1}, models: {models}")
    return RegisterMap.from_dict({"name": "sunspec", "events": events})


@functools.cache
def _model_def(model_id: int) -> dict[str, Any] | None:
    """pysunspec2's definition for ``model_id``, or None if it has none."""
    path = resources.files("sunspec2") / "models" / "json" / mdef.to_json_filename(model_id)
    return mdef.from_json_file(str(path)) if path.is_file() else None


def _map_model(
    model_id: int, address: int, length: int, warned: set[str]
) -> tuple[str, list[dict[str, Any]]]:
    """(event name, register dicts) for one model starting at ``address``."""
    model = _model_def(model_id)
    if model is None:
        logger.warning(f"SunSpec: model {model_id} at {address + 1} has no definition; skipped")
        return str(model_id), []
    group = model[mdef.GROUP]
    out: list[dict[str, Any]] = []
    _Walk(model_id, address + 2 + length, warned, out).group(group, address, "", [])
    name = f"{group[mdef.NAME]}_{model_id}"
    try:
        # Validate per model, so one odd model cannot fail the whole device.
        RegisterMap.from_dict({"events": {name: out}})
    except ValueError as e:
        logger.warning(f"SunSpec: model {model_id} at {address + 1} skipped: {e}")
        return name, []
    return name, out


def _group_len(group: dict[str, Any]) -> int | None:
    """Registers one instance of ``group`` spans; None if it holds a variable group."""
    total = sum(p[mdef.SIZE] for p in group.get(mdef.POINTS, []))
    for sub in group.get(mdef.GROUPS, []):
        count = sub.get(mdef.COUNT, 1)
        sub_len = _group_len(sub)
        if sub_len is None or not isinstance(count, int) or count == 0:
            return None
        total += sub_len * count
    return total


class _Stop(Exception):
    """Mapping cannot continue past this point of the model."""


class _Walk:
    """Flattens one model's groups into register dicts."""

    def __init__(self, model_id: int, end: int, warned: set[str], out: list[dict[str, Any]]):
        self.model_id = model_id
        self.end = end  # exclusive, from the header's length
        self.warned = warned
        self.out = out

    def group(self, group: dict[str, Any], address: int, prefix: str, scopes: list) -> int:
        """Map ``group``'s points then its subgroups; return the next address."""
        try:
            return self._group(group, address, prefix, scopes)
        except _Stop as e:
            logger.warning(f"SunSpec: model {self.model_id}: {e}; later points skipped")
            return self.end

    def _group(self, group: dict[str, Any], address: int, prefix: str, scopes: list) -> int:
        # Scale factors resolve in this group first, then outward.
        scope: dict[str, str] = {}
        scopes = [scope, *scopes]
        placed = []
        for point in group.get(mdef.POINTS, []):
            scope[point[mdef.NAME]] = prefix + point[mdef.NAME]
            placed.append((point, address))
            address += point[mdef.SIZE]
        for point, at in placed:
            self._point(point, at, prefix, scopes)

        subgroups = group.get(mdef.GROUPS, [])
        for i, sub in enumerate(subgroups):
            name = prefix + sub[mdef.NAME]
            count = sub.get(mdef.COUNT)
            if count is None:
                address = self._group(sub, address, f"{name}_", scopes)
                continue
            sub_len = _group_len(sub)
            if not isinstance(count, int) or count == 0:
                # Variable count: resolvable from the model length only when
                # nothing follows it.
                if sub_len is None or prefix or i != len(subgroups) - 1:
                    raise _Stop(f"repeating group '{name}' has no fixed length")
                count, rest = divmod(self.end - address, sub_len)
                if rest:
                    logger.warning(
                        f"SunSpec: model {self.model_id}: {rest} register(s) after "
                        f"'{name}' do not fill a group; ignored"
                    )
            for n in range(1, count + 1):
                address = self._group(sub, address, f"{name}_{n}_", scopes)
        return address

    def _point(self, point: dict[str, Any], address: int, prefix: str, scopes: list) -> None:
        ptype, size = point[mdef.TYPE], point[mdef.SIZE]
        name = prefix + point[mdef.NAME]
        if (not prefix and point[mdef.NAME] in HEADER_POINTS) or ptype == "pad":
            return
        if address + size > self.end:
            # A shorter legacy length (model 1 at 65 drops Pad) or a truncated model.
            logger.debug(f"SunSpec: model {self.model_id}: '{name}' past the model end")
            return
        if ptype not in POINT_TYPES:
            if ptype not in self.warned:
                self.warned.add(ptype)
                logger.warning(f"SunSpec: point type '{ptype}' is not supported; skipped")
            return
        datatype, sentinel = POINT_TYPES[ptype]
        reg: dict[str, Any] = {
            "name": name,
            "address": address + 1,  # map base 1
            "datatype": datatype,
            "writable": False,
            "description": point.get(mdef.LABEL, ""),
        }
        if datatype == "string":
            reg["length"] = size
        if self.model_id in SLOW_MODELS:
            reg["rate"] = SLOW_RATE
        if sentinel is not None:
            reg["invalid"] = [sentinel]
        if point.get(mdef.UNITS):
            reg["unit"] = point[mdef.UNITS]
        sf = point.get(mdef.SF)
        if isinstance(sf, int):
            if sf:
                reg["scale"] = 10.0**sf
        elif sf is not None:
            ref = next((s[sf] for s in scopes if sf in s), None)
            if ref is None:
                logger.warning(
                    f"SunSpec: model {self.model_id}: '{name}' scale factor {sf!r} "
                    "is not supported; skipped"
                )
                return
            reg["scale_ref"] = ref
        if ptype in ENUM_TYPES and point.get(mdef.SYMBOLS):
            reg["values"] = {str(s[mdef.VALUE]): s[mdef.NAME] for s in point[mdef.SYMBOLS]}
        self.out.append(reg)
