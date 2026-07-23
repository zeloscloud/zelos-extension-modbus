"""Block-read planner: coalesce individual register reads into range reads.

A naive poll issues one Modbus transaction per register. This planner groups
registers of the same type into contiguous (or nearly contiguous) address
ranges so each range can be read in a single transaction, collapsing hundreds
of transactions into a handful.
"""

from __future__ import annotations

from dataclasses import dataclass

from zelos_extension_modbus.constants import RegisterType
from zelos_extension_modbus.register_map import Register

# Bit-addressable types span one address each regardless of datatype.
_BIT_TYPES = {RegisterType.COIL, RegisterType.DISCRETE_INPUT}


@dataclass(frozen=True)
class ReadBlock:
    """A contiguous range of same-type registers read in one transaction."""

    type: str  # RegisterType value
    address: int  # starting address
    count: int  # number of addresses spanned
    registers: tuple[Register, ...]  # sorted by address


def _span(reg: Register) -> int:
    """Number of addresses a register occupies (1 for bits, datatype size otherwise)."""
    return 1 if reg.type in _BIT_TYPES else reg.count


def plan_blocks(
    registers: list[Register],
    max_block_size: int = 125,
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
        cur: list[Register] = []
        block_start = 0
        block_end = 0  # exclusive: covers [block_start, block_end)
        for reg in regs:
            reg_end = reg.address + _span(reg)
            if not cur:
                cur = [reg]
                block_start = reg.address
                block_end = reg_end
                continue
            gap = reg.address - block_end
            new_end = max(block_end, reg_end)
            if gap > max_read_gap or (new_end - block_start) > max_block_size:
                blocks.append(_make_block(reg_type, block_start, block_end, cur))
                cur = [reg]
                block_start = reg.address
                block_end = reg_end
            else:
                cur.append(reg)
                block_end = new_end
        if cur:
            blocks.append(_make_block(reg_type, block_start, block_end, cur))

    return blocks


def _make_block(reg_type: str, start: int, end: int, regs: list[Register]) -> ReadBlock:
    """Build a ReadBlock from an accumulated register run."""
    return ReadBlock(
        type=reg_type,
        address=start,
        count=end - start,
        registers=tuple(regs),
    )
