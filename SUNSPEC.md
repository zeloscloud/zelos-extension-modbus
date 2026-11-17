# SunSpec

Set a device's Register Map to `sunspec` (`"register_map": "sunspec"`, instead of `register_map_file`) and the map is built at connect:

1. Find the `SunS` marker at holding register 40001, then 1, then 50001 (1-based, as SunSpec documents it; wire 40000, 0, 50000).
2. Walk the model chain (model ID, length) to the `0xFFFF` end marker.
3. Map every model with a [pysunspec2](https://github.com/sunspec/pysunspec2) definition.

Discovery only reads (FC 3). No marker, an exception answer for a model header, or a chain without the end marker is a device error (logged, `error` in `get_status`, `map_pending` stays true) retried every 30 s; a partial map is never used; a silent device is demoted like a polled one. The connection keeps polling its other devices. Identity, nameplate and settings models (1, 120, 121, 702) and the 700-series curve functions (705-710, 712) poll every 60 s; the rest at the device rate.

| SunSpec | Becomes |
|---------|---------|
| Model | Event `<name>_<id>` (`common_1`, `inverter_103`; a repeat gets `_2`) |
| Point | Field named after the point, with its units; read-only |
| Repeating group | `<group>_<n>_<point>`, nested `Crv_1_Pt_2_V`. Count from the model length; a nested group's from the model's count point (`NPt`: one extra read at connect) |
| `sf` scale factor | `scale_ref` to the `*_SF` field (read, not traced); a fixed integer `sf` becomes `scale` |
| `enum16`/`enum32` | `values` from the symbols |
| Not-implemented value | null (`0x8000`, `0xFFFF`, `0x80000000`, NaN, ...; `0` for accumulators and `ipaddr`, all NUL for strings) |
| `bitfield*` | Raw integer, plus a boolean `<point>_<symbol>` per named bit (`Evt1_GROUND_FAULT`) |

Skipped with a warning: models without a definition, point types `eui48`/`ipv6addr`, and a repeating group whose count neither the model length nor the device gives.
