"""Chip databases.

* ``nando_parallel_chip_db.csv`` / ``nando_spi_chip_db.csv``: taken unchanged
  from bbogush/nand_programmer (GPLv3).
* ``nand_voltage.csv``: supply voltage for the NANDO parallel entries.
* ``spi_nor_ids.csv``: common SPI NOR parts (standard 25-series commands).
* ``spi_nand.csv``: SPI NAND parts.

Unknown parts are handled through ONFI (parallel NAND) and SFDP (SPI NOR).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Sequence

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _rows(name: str):
    with open(os.path.join(DATA_DIR, name), newline="", encoding="utf-8") as f:
        for row in csv.reader(f, skipinitialspace=True):
            if not row or row[0].lstrip().startswith("#"):
                continue
            yield [c.strip() for c in row]


def _int(v: str) -> Optional[int]:
    v = v.strip()
    if v in ("", "-"):
        return None
    return int(v, 0)


_VOLTAGE_RULES = [
    (r"W29N\d+[GK]V", 3.3), (r"W29N\d+[GK]Z", 1.8),
    (r"^K9\w{5}U", 3.3), (r"^K9\w{5}[RQ]", 1.8),
    (r"^MT29F\d+G(08|16)AB[AC]", 3.3), (r"^MT29F\d+G(08|16)AB[BD]", 1.8),
    (r"^MX30LF", 3.3), (r"^MX30UF", 1.8),
    (r"^S34ML", 3.3), (r"^S34MS", 1.8),
    (r"^T[CH]58NVG", 3.3), (r"^T[CH]58NYG", 1.8),
    (r"^F59L", 3.3), (r"^F59D", 1.8),
    (r"^HY27U", 3.3), (r"^HY27S", 1.8),
]


def guess_voltage(name: str) -> float:
    """Supply voltage from vendor part-number conventions (0 = unknown)."""
    import re

    n = (name or "").upper().replace(" ", "")
    for part in [n] + n.split("WINBOND")[1:] + n.split("MICRON")[1:]:
        for pat, v in _VOLTAGE_RULES:
            if re.search(pat, part):
                return v
    return 0.0


def _volts(v: float) -> str:
    return "%.1fV" % v if v else "voltage unknown (check datasheet)"


def _hexbytes(v: str) -> bytes:
    return bytes.fromhex(v.strip())


# ---------------------------------------------------------------------------
@dataclass
class NandChip:
    name: str
    page_size: int
    block_size: int                 # main area bytes per block
    total_size: int                 # main area bytes
    spare_size: int
    bb_mark_off: int
    row_cycles: int
    col_cycles: int
    cmd_read1: int = 0x00
    cmd_read2: Optional[int] = 0x30
    cmd_read_spare: Optional[int] = None
    cmd_read_id: int = 0x90
    cmd_reset: int = 0xFF
    cmd_write1: int = 0x80
    cmd_write2: int = 0x10
    cmd_erase1: int = 0x60
    cmd_erase2: int = 0xD0
    cmd_status: int = 0x70
    ids: Sequence[Optional[int]] = ()
    timings_ns: Dict[str, int] = field(default_factory=dict)
    voltage: float = 3.3
    source: str = "nando"
    t_prog_ms: int = 5
    t_bers_ms: int = 20

    @property
    def pages_per_block(self) -> int:
        return self.block_size // self.page_size

    @property
    def blocks(self) -> int:
        return self.total_size // self.block_size

    @property
    def pages(self) -> int:
        return self.total_size // self.page_size

    @property
    def raw_page(self) -> int:
        return self.page_size + self.spare_size

    @property
    def small_page(self) -> bool:
        return self.page_size <= 512

    def matches(self, idb: bytes) -> bool:
        ids = [i for i in self.ids if i is not None]
        return bool(ids) and len(idb) >= len(ids) and all(idb[k] == v for k, v in enumerate(ids))

    def describe(self) -> str:
        return "%s: %d MiB, page %d+%d, %d pages/block, %d blocks, %s" % (
            self.name, self.total_size >> 20, self.page_size, self.spare_size,
            self.pages_per_block, self.blocks, _volts(self.voltage))


_TIMING_COLS = ["tCS", "tCLS", "tALS", "tCLR", "tAR", "tWP", "tRP", "tDS", "tCH", "tCLH",
                "tALH", "tWC", "tRC", "tREA"]


@lru_cache(maxsize=None)
def nand_chips() -> List[NandChip]:
    volts = {r[0]: float(r[1]) for r in _rows("nand_voltage.csv")}
    chips = []
    for r in _rows("nando_parallel_chip_db.csv"):
        # name, page, block, total, spare, bb off, 14 timings, row cyc, col cyc,
        # read1, read2, read spare, read id, reset, write1, write2, erase1, erase2,
        # status, set feat, en ECC addr, en ECC val, dis ECC val, ID1..ID5
        t = {k: _int(v) for k, v in zip(_TIMING_COLS, r[6:20])}
        c = [_int(v) for v in r[20:]]
        chips.append(NandChip(
            name=r[0], page_size=int(r[1]), block_size=int(r[2]), total_size=int(r[3]),
            spare_size=int(r[4]), bb_mark_off=int(r[5]),
            row_cycles=c[0], col_cycles=c[1],
            cmd_read1=c[2], cmd_read2=c[3], cmd_read_spare=c[4], cmd_read_id=c[5],
            cmd_reset=c[6], cmd_write1=c[7], cmd_write2=c[8], cmd_erase1=c[9],
            cmd_erase2=c[10], cmd_status=c[11], ids=tuple(c[16:21]),
            timings_ns={k: v for k, v in t.items() if v is not None},
            voltage=volts.get(r[0], 3.3), source="nando"))
    return chips


def find_nand(idb: bytes) -> Optional[NandChip]:
    best = None
    for chip in nand_chips():
        if chip.matches(idb):
            n = len([i for i in chip.ids if i is not None])
            if best is None or n > len([i for i in best.ids if i is not None]):
                best = chip
    return best


def nand_by_name(name: str) -> Optional[NandChip]:
    for chip in nand_chips():
        if chip.name.lower() == name.lower():
            return chip
    return None


# ---------------------------------------------------------------------------
@dataclass
class SpiNorChip:
    name: str
    page_size: int                  # program page
    block_size: int                 # erase unit used by this driver
    total_size: int
    page_off: int = 0               # address = page << page_off (DataFlash); 0 = linear
    read_cmd: int = 0x03
    id_cmd: int = 0x9F
    write_cmd: int = 0x02
    write_en_cmd: Optional[int] = 0x06
    erase_cmd: int = 0x20
    status_cmd: int = 0x05
    busy_bit: int = 0
    busy_state: int = 1
    max_khz: int = 50000
    ids: bytes = b""
    voltage: float = 3.3
    source: str = "nando"
    erase_types: Dict[int, int] = field(default_factory=dict)   # size -> opcode
    chip_erase_cmd: Optional[int] = 0xC7
    addr_bytes: int = 3
    unlock_cmd: Optional[int] = None
    notes: str = ""

    @property
    def linear(self) -> bool:
        """True when flat file offsets equal device addresses (not DataFlash)."""
        return self.page_off == 0 or (1 << self.page_off) == self.page_size

    @property
    def read_dummy(self) -> int:
        return 1 if self.read_cmd == 0x0B else 0

    def describe(self) -> str:
        size = self.total_size
        s = "%d MiB" % (size >> 20) if size >= 1 << 20 else "%d KiB" % (size >> 10)
        return "%s: %s, page %d, erase %d, %s" % (self.name, s, self.page_size,
                                                  self.block_size, _volts(self.voltage))


@lru_cache(maxsize=None)
def spi_nor_chips() -> List[SpiNorChip]:
    chips = []
    for r in _rows("nando_spi_chip_db.csv"):
        # name, page, block, total, page off, read, id, write, write en, erase, status,
        # busy bit, busy state, max kHz, ID1..ID5
        c = [_int(v) for v in r[1:]]
        ids = bytes(i for i in c[13:18] if i is not None)
        chips.append(SpiNorChip(
            name=r[0], page_size=c[0], block_size=c[1], total_size=c[2], page_off=c[3],
            read_cmd=c[4], id_cmd=c[5], write_cmd=c[6], write_en_cmd=c[7], erase_cmd=c[8],
            status_cmd=c[9], busy_bit=c[10], busy_state=c[11], max_khz=c[12], ids=ids,
            source="nando", erase_types={c[1]: c[8]},
            chip_erase_cmd=0xC7 if (1 << c[3]) == c[0] else None))
    for r in _rows("spi_nor_ids.csv"):
        size = int(r[2])
        chips.append(SpiNorChip(
            name=r[0], page_size=256, block_size=4096, total_size=size, ids=_hexbytes(r[1]),
            voltage=float(r[3]), source="nsprog",
            erase_types={4096: 0x20, 32768: 0x52, 65536: 0xD8},
            addr_bytes=4 if size > 16 * 1024 * 1024 else 3,
            unlock_cmd=0x98 if "ULBPR" in (r[4] if len(r) > 4 else "") else None,
            notes=r[4] if len(r) > 4 else ""))
    return chips


def find_spi_nor(jedec: bytes) -> Optional[SpiNorChip]:
    best = None
    for chip in spi_nor_chips():
        if chip.ids and jedec[:len(chip.ids)] == chip.ids:
            # prefer the longest ID match, then NANDO entries (explicit commands)
            key = (len(chip.ids), chip.source == "nando")
            if best is None or key > (len(best.ids), best.source == "nando"):
                best = chip
    return best


def generic_spi_nor(jedec: bytes, size: Optional[int] = None) -> Optional[SpiNorChip]:
    """Guess a standard 25-series part from the JEDEC capacity byte."""
    if size is None:
        cap = jedec[2] if len(jedec) >= 3 else 0
        if 0x10 <= cap <= 0x1F:
            size = 1 << cap
        elif 0x20 <= cap <= 0x22:           # Micron / Winbond 512Mb..2Gb encoding
            size = 64 * 1024 * 1024 << (cap - 0x20)
        else:
            return None
    return SpiNorChip(name="SPI NOR %s" % jedec[:3].hex().upper(), page_size=256,
                      block_size=4096, total_size=size, ids=bytes(jedec[:3]),
                      source="generic", erase_types={4096: 0x20, 32768: 0x52, 65536: 0xD8},
                      addr_bytes=4 if size > 16 * 1024 * 1024 else 3, voltage=0.0)


# ---------------------------------------------------------------------------
@dataclass
class SpiNandChip:
    name: str
    ids: bytes
    page_size: int
    spare_size: int
    pages_per_block: int
    blocks: int
    plane_bit: bool = False
    read_dummy_first: bool = False
    voltage: float = 3.3
    verified: bool = False
    readid: str = "opcode_dummy"    # opcode | opcode_dummy | opcode_addr (Linux naming)
    source: str = "nsprog"
    note: str = ""
    t_rd_ms: int = 2
    t_prog_ms: int = 5
    t_bers_ms: int = 20

    @property
    def raw_page(self) -> int:
        return self.page_size + self.spare_size

    @property
    def total_size(self) -> int:
        return self.page_size * self.pages_per_block * self.blocks

    @property
    def pages(self) -> int:
        return self.pages_per_block * self.blocks

    def describe(self) -> str:
        return "%s: %d MiB, page %d+%d, %d pages/block, %d blocks, %.1fV%s" % (
            self.name, self.total_size >> 20, self.page_size, self.spare_size,
            self.pages_per_block, self.blocks, self.voltage,
            "" if self.verified else " (unverified entry)")

    def match(self, raw9f: bytes, id_after_addr: bytes) -> bool:
        """``raw9f``: bytes clocked right after 9Fh; ``id_after_addr``: after 9Fh 00h."""
        n = len(self.ids)
        if self.readid == "opcode":
            return raw9f[:n] == self.ids
        if self.readid == "opcode_addr":
            return id_after_addr[:n] == self.ids
        return id_after_addr[:n] == self.ids or raw9f[1:1 + n] == self.ids


@lru_cache(maxsize=None)
def spi_nand_chips() -> List[SpiNandChip]:
    """Linux kernel table first (verified in the field), then our own extras."""
    out = []
    seen = set()
    for r in _rows("spi_nand_linux.csv"):
        chip = SpiNandChip(
            name=r[0], ids=_hexbytes(r[1]), page_size=int(r[2]), spare_size=int(r[3]),
            pages_per_block=int(r[4]), blocks=int(r[5]), plane_bit=r[6] == "1",
            readid=r[7], voltage=float(r[8]), verified=True, source="linux",
            note=r[10] if len(r) > 10 else "")
        out.append(chip)
        seen.add(chip.ids)
    for r in _rows("spi_nand.csv"):
        ids = _hexbytes(r[1])
        if ids in seen:
            continue
        out.append(SpiNandChip(
            name=r[0], ids=ids, page_size=int(r[2]), spare_size=int(r[3]),
            pages_per_block=int(r[4]), blocks=int(r[5]), plane_bit=r[6] == "1",
            read_dummy_first=r[7] == "1", voltage=float(r[8]), verified=r[9] == "1"))
    return out


def find_spi_nand(raw9f: bytes, id_after_addr: bytes = b"") -> Optional[SpiNandChip]:
    """Match an SPI NAND by its ID (longest ID wins)."""
    best = None
    for chip in spi_nand_chips():
        if chip.match(raw9f, id_after_addr or raw9f[1:]):
            if best is None or len(chip.ids) > len(best.ids):
                best = chip
    return best


def all_chips() -> List[dict]:
    """Flat listing for the CLI / web UI."""
    out = []
    for c in nand_chips():
        out.append({"type": "nand", "name": c.name, "size": c.total_size, "voltage": c.voltage,
                    "source": c.source, "id": bytes(i for i in c.ids if i is not None).hex().upper(),
                    "detail": c.describe()})
    for c in spi_nor_chips():
        out.append({"type": "spinor", "name": c.name, "size": c.total_size, "voltage": c.voltage,
                    "source": c.source, "id": c.ids.hex().upper(), "detail": c.describe()})
    for c in spi_nand_chips():
        out.append({"type": "spinand", "name": c.name, "size": c.total_size, "voltage": c.voltage,
                    "source": c.source, "id": c.ids.hex().upper(), "detail": c.describe()})
    return out
