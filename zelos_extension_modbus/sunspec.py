"""SunSpec discovery: build a RegisterMap from a device's model chain.

A device with `"register_map": "sunspec"` gets its map here at connect. Reads
are FC3 only, through the connection's request choke point: find the `SunS`
marker, walk the (id, length) model headers to the 0xFFFF end marker, and map
each model's points from the pysunspec2 model definitions (only its JSON
definitions are used, not its client). The result is an ordinary read-only
RegisterMap; polling, decoding and tracing know nothing about SunSpec.

Mapping: one event per model, `<model name>_<id>` (`common_1`, `inverter_103`;
a repeat of the same model gets `_2`, `_3`). One field per point, named after
the point; repeating groups flatten to `<group>_<n>_<point>` (1-based), nested
ones to `<group>_<n>_<group>_<m>_<point>`. The model length gives a repeat count;
where it cannot (a nested group, the 700-series curve points) the count is read
from the model's count point. Scale
factor links become `scale_ref` (a fixed sf becomes `scale`), enums become `values`, and the SunSpec
not-implemented values become `invalid` (logged as null). A bitfield keeps its
integer field and adds a boolean `<point>_<symbol>` per named bit.

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

# SunSpec point type -> (datatype, not-implemented raw value): accumulators
# and addresses are 0, strings all NUL.
POINT_TYPES: dict[str, tuple[str, int | None]] = {
    "int16": ("int16", 0x8000),
    "sunssf": ("int16", 0x8000),
    "uint16": ("uint16", 0xFFFF),
    "count": ("uint16", 0xFFFF),
    "enum16": ("uint16", 0xFFFF),
    "bitfield16": ("uint16", 0xFFFF),
    "acc16": ("uint16", 0),
    "int32": ("int32", 0x80000000),
    "uint32": ("uint32", 0xFFFFFFFF),
    "enum32": ("uint32", 0xFFFFFFFF),
    "bitfield32": ("uint32", 0xFFFFFFFF),
    "acc32": ("uint32", 0),
    "ipaddr": ("uint32", 0),
    "int64": ("int64", 0x8000000000000000),
    "uint64": ("uint64", 0xFFFFFFFFFFFFFFFF),
    "bitfield64": ("uint64", 0xFFFFFFFFFFFFFFFF),
    "acc64": ("uint64", 0),
    "float32": ("float32", 0x7FC00000),
    "float64": ("float64", 0x7FF8000000000000),
    "string": ("string", 0),  # all NUL
}
ENUM_TYPES = {"enum16", "enum32"}
HEADER_POINTS = {"ID", "L"}  # model header, already known

#: Models polled at SLOW_RATE: identity (1 common), nameplate (120, and 702 DER
#: capacity, its 700-series successor), basic settings (121) and the 700-series
#: curve functions (705-710, 712: enable, adopt state and the curve points). They
#: change only on reconfiguration; status and controls (122-126, other 7xx) keep
#: the device rate.
SLOW_MODELS = {1, 120, 121, 702, 705, 706, 707, 708, 709, 710, 712}


class DiscoveryError(Exception):
    """The device is not a readable SunSpec device."""


async def _read(device: ModbusDevice, address: int, count: int) -> list[int] | None:
    """FC3 read; None on an exception response. No response (or a gateway's
    unit-absent answer) raises ModbusIOException."""
    raw = await device._fetch(RegisterType.HOLDING, address, count)
    return None if isinstance(raw, int) else raw


async def build_register_map(device: ModbusDevice) -> RegisterMap:
    """Discover the device's SunSpec models and map them.

    Only a whole chain, ended by the 0xFFFF marker, makes a map: a partial one
    would silently drop models, so the device retries discovery instead.

    Raises:
        DiscoveryError: no `SunS` marker at any base address, an exception
            answer for a model header, or no end marker.
        ModbusIOException: no response.
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
        if address + 2 > 0x10000:
            raise DiscoveryError(f"model chain runs past the last address at {address + 1}")
        header = await _read(device, address, 2)
        if header is None:
            raise DiscoveryError(f"exception answer for the model header at {address + 1}")
        model_id, length = header
        if model_id == END_MODEL_ID:
            break
        counts = await _read_counts(device, model_id, address, length)
        name, registers = _map_model(model_id, address, length, warned, counts)
        if registers:
            event, n = name, 1
            while event in events:
                n += 1
                event = f"{name}_{n}"
            events[event] = registers
        address += 2 + length
    else:
        raise DiscoveryError(f"no end marker after {MAX_MODELS} models")

    models = ", ".join(events) or "none"
    logger.info(f"SunSpec: base {base + 1}, models: {models}")
    return RegisterMap.from_dict({"name": "sunspec", "events": events})


@functools.cache
def _model_def(model_id: int) -> dict[str, Any] | None:
    """pysunspec2's definition for ``model_id``, or None if it has none."""
    path = resources.files("sunspec2") / "models" / "json" / mdef.to_json_filename(model_id)
    return mdef.from_json_file(str(path)) if path.is_file() else None


def _count_offsets(group: dict[str, Any]) -> dict[str, int]:
    """Count points the model length cannot stand in for -> offset in the model.

    Those of nested groups, and of a top-level group another group follows.
    """
    names: set[str] = set()

    def collect(g: dict[str, Any], top: bool) -> None:
        subgroups = g.get(mdef.GROUPS, [])
        for i, sub in enumerate(subgroups):
            if isinstance(sub.get(mdef.COUNT), str) and not (top and i == len(subgroups) - 1):
                names.add(sub[mdef.COUNT])
            collect(sub, False)

    collect(group, True)
    offsets, offset = {}, 0
    for point in group.get(mdef.POINTS, []):
        if point[mdef.NAME] in names:
            offsets[point[mdef.NAME]] = offset
        offset += point[mdef.SIZE]
    return offsets


async def _read_counts(
    device: ModbusDevice, model_id: int, address: int, length: int
) -> dict[str, int]:
    """The repeat counts only the device can give, read from the model's count points.

    One register each, inside the model. A count the device does not give
    (exception answer, 0, not implemented) is left out: its group is skipped.
    """
    model = _model_def(model_id)
    counts: dict[str, int] = {}
    for name, offset in _count_offsets(model[mdef.GROUP]).items() if model else ():
        if offset >= 2 + length:
            continue
        raw = await _read(device, address + offset, 1)
        if raw and 0 < raw[0] < 0xFFFF:
            counts[name] = raw[0]
    return counts


def _map_model(
    model_id: int, address: int, length: int, warned: set[str], counts: dict[str, int]
) -> tuple[str, list[dict[str, Any]]]:
    """(event name, register dicts) for one model starting at ``address``.

    ``counts``: the device's values for the count points `_read_counts` reads.
    """
    model = _model_def(model_id)
    if model is None:
        logger.warning(f"SunSpec: model {model_id} at {address + 1} has no definition; skipped")
        return str(model_id), []
    group = model[mdef.GROUP]
    out: list[dict[str, Any]] = []
    _Walk(model_id, address + 2 + length, warned, out, counts).group(group, address)
    name = f"{group[mdef.NAME]}_{model_id}"
    try:
        # Validate per model, so one odd model cannot fail the whole device.
        RegisterMap.from_dict({"events": {name: out}})
    except ValueError as e:
        logger.warning(f"SunSpec: model {model_id} at {address + 1} skipped: {e}")
        return name, []
    return name, out


def _instances(count: int | str, counts: dict[str, int]) -> int | None:
    """Instances of a repeating group: its fixed count, or the device's for a count
    point; None when unknown."""
    if isinstance(count, str):
        return counts.get(count)
    return count or None


def _group_len(group: dict[str, Any], counts: dict[str, int]) -> int | None:
    """Registers one instance of ``group`` spans; None if it holds a group of
    unknown count."""
    total = sum(p[mdef.SIZE] for p in group.get(mdef.POINTS, []))
    for sub in group.get(mdef.GROUPS, []):
        n = _instances(sub.get(mdef.COUNT, 1), counts)
        sub_len = _group_len(sub, counts)
        if n is None or sub_len is None:
            return None
        total += sub_len * n
    return total


class _Stop(Exception):
    """Mapping cannot continue past this point of the model."""


class _Walk:
    """Flattens one model's groups into register dicts."""

    def __init__(
        self,
        model_id: int,
        end: int,
        warned: set[str],
        out: list[dict[str, Any]],
        counts: dict[str, int],
    ):
        self.model_id = model_id
        self.end = end  # exclusive, from the header's length
        self.warned = warned
        self.out = out
        self.counts = counts

    def group(self, group: dict[str, Any], address: int) -> None:
        """Map the model's top ``group``: its points, then its subgroups."""
        try:
            self._group(group, address, "", [])
        except _Stop as e:
            logger.warning(f"SunSpec: model {self.model_id}: {e}; later points skipped")

    def _group(self, group: dict[str, Any], address: int, prefix: str, scopes: list) -> int:
        """Map ``group``'s points then its subgroups; return the next address."""
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
            sub_len = _group_len(sub, self.counts)
            if sub_len is None:
                raise _Stop(f"repeating group '{name}' has no fixed length")
            if isinstance(count, int) and count:
                pass  # fixed by the definition
            elif not prefix and i == len(subgroups) - 1:
                # Nothing follows: the model length gives the count.
                count, rest = divmod(self.end - address, sub_len)
                if rest:
                    logger.warning(
                        f"SunSpec: model {self.model_id}: {rest} register(s) after "
                        f"'{name}' do not fill a group; ignored"
                    )
            else:
                count = _instances(count, self.counts)
                if count is None or address + count * sub_len > self.end:
                    raise _Stop(f"repeating group '{name}' has no usable count")
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
        if ptype.startswith("bitfield"):
            for symbol in point.get(mdef.SYMBOLS, []):
                flag = {**reg, "name": f"{name}_{symbol[mdef.NAME]}", "bit": symbol[mdef.VALUE]}
                flag["description"] = symbol.get(mdef.LABEL) or symbol[mdef.NAME]
                self.out.append(flag)
