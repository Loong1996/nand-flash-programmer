"""ONFI parameter page decoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from .emulator import onfi_crc16  # single implementation shared with the models


@dataclass
class OnfiParams:
    revision: int
    manufacturer: str
    model: str
    jedec_id: int
    page_size: int
    spare_size: int
    pages_per_block: int
    blocks_per_lun: int
    luns: int
    row_cycles: int
    col_cycles: int
    bits_per_cell: int
    ecc_bits: int
    timing_modes: int
    t_prog_us: int
    t_bers_us: int
    t_r_us: int

    @property
    def blocks(self) -> int:
        return self.blocks_per_lun * self.luns


def parse_param_page(raw: bytes) -> Optional[OnfiParams]:
    """Return the first copy of the parameter page with a valid CRC."""
    for off in range(0, len(raw) - 255, 256):
        pp = raw[off:off + 256]
        if pp[:4] != b"ONFI":
            continue
        if struct.unpack_from("<H", pp, 254)[0] != onfi_crc16(pp[:254]):
            continue
        def u16(o: int, pp: bytes = pp) -> int:
            return struct.unpack_from("<H", pp, o)[0]

        def u32(o: int, pp: bytes = pp) -> int:
            return struct.unpack_from("<I", pp, o)[0]
        return OnfiParams(
            revision=u16(4),
            manufacturer=pp[32:44].decode("ascii", "replace").strip(),
            model=pp[44:64].decode("ascii", "replace").strip(),
            jedec_id=pp[64],
            page_size=u32(80),
            spare_size=u16(84),
            pages_per_block=u32(92),
            blocks_per_lun=u32(96),
            luns=pp[100] or 1,
            row_cycles=pp[101] & 0x0F,
            col_cycles=pp[101] >> 4,
            bits_per_cell=pp[102],
            ecc_bits=pp[112],
            timing_modes=u16(129),
            t_prog_us=u16(133),
            t_bers_us=u16(135),
            t_r_us=u16(137),
        )
    return None
