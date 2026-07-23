"""Block-read planner: coalesce individual register reads into range reads.

A naive poll issues one Modbus transaction per register. This planner groups
registers of the same type into contiguous (or nearly contiguous) address
ranges so each range can be read in a single transaction, collapsing hundreds
of transactions into a handful.
"""

from __future__ import annotations

from dataclasses import dataclass

from zelos_extension_modbus.constants import MODBUS_MAX_READ_COUNT
from zelos_extension_modbus.register_map import Register


@dataclass(frozen=True)
class ReadBlock:
    """A contiguous range of same-type registers read in one transaction."""

    type: str  # RegisterType value
    address: int  # starting address
    count: int  # number of addresses spanned
    registers: tuple[Register, ...]  # sorted by address


def plan_blocks(
    registers: list[Register],
    max_block_size: int = MODBUS_MAX_READ_COUNT,
    max_read_gap: int = 0,
) -> list[ReadBlock]:
    """Group registers into range-read blocks per register type.

    Registers are grouped by type, sorted by (address, count, name), then swept
    greedily: a new block starts when the next register's address is more than
    ``max_read_gap`` past the current block's end, or when extending the block
    would exceed ``max_block_size`` addresses. The first register of a block is
    always accepted, so a single span wider than ``max_block_size`` yields one
    oversized block rather than failing. Types are emitted in sorted order for
    deterministic output.

    Args:
        registers: Registers to coalesce (any mix of types).
        max_block_size: Maximum addresses per block (Modbus caps word reads at 125).
        max_read_gap: Maximum uncovered addresses to bridge within a block
            (0 = strictly contiguous).

    Returns:
        List of ReadBlock, ordered by type then address.
    """
    by_type: dict[str, list[Register]] = {}
    for reg in registers:
        by_type.setdefault(reg.type, []).append(reg)

    blocks: list[ReadBlock] = []
    for reg_type in sorted(by_type):
        regs = sorted(by_type[reg_type], key=lambda r: (r.address, r.count, r.name))
        # Seed the run with the first register; block start is always cur[0].address.
        cur: list[Register] = [regs[0]]
        end = regs[0].address + regs[0].address_span  # exclusive block end
        for reg in regs[1:]:
            reg_end = reg.address + reg.address_span
            new_end = max(end, reg_end)
            if reg.address - end > max_read_gap or (new_end - cur[0].address) > max_block_size:
                blocks.append(ReadBlock(reg_type, cur[0].address, end - cur[0].address, tuple(cur)))
                cur = [reg]
                end = reg_end
            else:
                cur.append(reg)
                end = new_end
        blocks.append(ReadBlock(reg_type, cur[0].address, end - cur[0].address, tuple(cur)))

    return blocks
