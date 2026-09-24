"""JESD216 SFDP (Serial Flash Discoverable Parameters) decoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SfdpInfo:
    size: int
    erase_types: Dict[int, int] = field(default_factory=dict)   # size -> opcode
    addr_bytes: int = 3                                          # 3, 4, or 34 (either)
    page_size: int = 256
    quad_cmd: Optional[int] = None      # 1-1-4 fast read opcode (usually 6Bh)
    quad_dummy: int = 8                 # dummy + mode clocks for quad_cmd
    qer: int = 0                        # quad enable requirement (DWORD15 bits 22:20)


def parse_header(hdr: bytes):
    """Return (bfpt_offset, bfpt_dwords) or None."""
    if len(hdr) < 16 or hdr[:4] != b"SFDP":
        return None
    nph = hdr[6] + 1
    for i in range(nph):
        o = 8 + 8 * i
        if o + 8 > len(hdr):
            break
        id_lsb, _minor, _major, length = hdr[o], hdr[o + 1], hdr[o + 2], hdr[o + 3]
        ptp = hdr[o + 4] | (hdr[o + 5] << 8) | (hdr[o + 6] << 16)
        id_msb = hdr[o + 7]
        if id_lsb == 0x00 and id_msb == 0xFF:
            return ptp, length
    return None


def parse_bfpt(raw: bytes) -> Optional[SfdpInfo]:
    n = len(raw) // 4
    if n < 2:
        return None
    dw = struct.unpack("<%dI" % n, raw[:4 * n])
    d2 = dw[1]
    if d2 & 0x80000000:
        bits = 1 << (d2 & 0x7FFFFFFF)
    else:
        bits = d2 + 1
    size = bits // 8
    if size <= 0 or size > (1 << 32):
        return None
    info = SfdpInfo(size=size)
    ab = (dw[0] >> 17) & 3
    info.addr_bytes = {0: 3, 1: 34, 2: 4}.get(ab, 3)
    if n >= 9:
        for word in (dw[7], dw[8]):
            for sh in (0, 16):
                exp = (word >> sh) & 0xFF
                op = (word >> (sh + 8)) & 0xFF
                if exp and op not in (0x00, 0xFF):
                    info.erase_types[1 << exp] = op
    if not info.erase_types and (dw[0] & 3) == 1:
        info.erase_types[4096] = (dw[0] >> 8) & 0xFF
    if n >= 11:
        info.page_size = 1 << ((dw[10] >> 4) & 0xF)
    if dw[0] & (1 << 22) and n >= 3:
        op = dw[2] >> 24
        if op not in (0x00, 0xFF):
            info.quad_cmd = op
            info.quad_dummy = ((dw[2] >> 16) & 0x1F) + ((dw[2] >> 21) & 0x7)
    if n >= 15:
        info.qer = (dw[14] >> 20) & 0x7
    return info
