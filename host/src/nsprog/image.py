"""Offline NAND image tools (no hardware needed).

A *raw* image stores every page as ``page + oob`` bytes (what ``nsprog read``
produces by default); a *main* image stores only the ``page`` bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, List, Optional

from .ecc import EccLayout

ProgressFn = Optional[Callable[[int, int], None]]


@dataclass
class Geometry:
    page: int
    oob: int
    ppb: int = 64

    @property
    def raw(self) -> int:
        return self.page + self.oob

    @classmethod
    def from_chip(cls, name: str) -> "Geometry":
        from . import chipdb

        c = chipdb.nand_by_name(name)
        if c is not None:
            return cls(c.page_size, c.spare_size, c.pages_per_block)
        for s in chipdb.spi_nand_chips():
            if s.name.lower() == name.lower():
                return cls(s.page_size, s.spare_size, s.pages_per_block)
        raise ValueError("unknown chip %r" % name)


def _pages(f: BinaryIO, size: int):
    while True:
        buf = f.read(size)
        if not buf:
            return
        if len(buf) < size:
            buf += b"\xff" * (size - len(buf))
        yield buf


# ---------------------------------------------------------------------------
@dataclass
class ImageInfo:
    pages: int = 0
    blocks: int = 0
    blank_pages: int = 0
    bad_blocks: List[int] = field(default_factory=list)
    trailing_bytes: int = 0
    magics: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        s = ["%d pages in %d blocks, %d blank pages" % (self.pages, self.blocks, self.blank_pages)]
        if self.bad_blocks:
            s.append("blocks with bad-block marker: %s%s" % (
                ", ".join(map(str, self.bad_blocks[:32])),
                " ..." if len(self.bad_blocks) > 32 else ""))
        if self.trailing_bytes:
            s.append("warning: %d trailing bytes (size is not a multiple of the page size)"
                     % self.trailing_bytes)
        if self.magics:
            s.append("found: " + ", ".join("%s @ 0x%X" % (k, v) for k, v in self.magics.items()))
        return "\n".join(s)


MAGICS = {b"UBI#": "UBI", b"hsqs": "SquashFS", b"\x31\x18\x10\x06": "UBIFS",
          b"\x27\x05\x19\x56": "U-Boot uImage", b"\xd0\x0d\xfe\xed": "FDT/FIT",
          b"\x85\x19\x03\x20": "JFFS2(le)", b"\x19\x85\x20\x03": "JFFS2(be)"}


def info(f: BinaryIO, geo: Geometry, raw: bool = True, bb_off: int = 0) -> ImageInfo:
    size = geo.raw if raw else geo.page
    out = ImageInfo()
    f.seek(0, 2)
    total = f.tell()
    f.seek(0)
    out.trailing_bytes = total % size
    for i, pg in enumerate(_pages(f, size)):
        out.pages += 1
        if pg.count(0xFF) == len(pg):
            out.blank_pages += 1
        main = pg[:geo.page]
        for magic, name in MAGICS.items():
            if name not in out.magics and main[:4] == magic:
                out.magics[name] = i * geo.page
        if raw and geo.oob and i % geo.ppb in (0, 1) and pg[geo.page + bb_off] != 0xFF:
            blk = i // geo.ppb
            if blk not in out.bad_blocks:
                out.bad_blocks.append(blk)
    out.blocks = (out.pages + geo.ppb - 1) // geo.ppb
    return out


def strip_oob(src: BinaryIO, dst: BinaryIO, geo: Geometry, skip_bad: bool = False,
              bb_off: int = 0, progress: ProgressFn = None) -> int:
    """raw -> main. With ``skip_bad`` blocks carrying a bad-block marker are dropped."""
    n = 0
    block = []
    for i, pg in enumerate(_pages(src, geo.raw)):
        block.append(pg)
        if len(block) == geo.ppb:
            n += _flush_block(block, dst, geo, skip_bad, bb_off)
            block = []
            if progress:
                progress(i + 1, 0)
    if block:
        n += _flush_block(block, dst, geo, skip_bad, bb_off)
    return n


def _flush_block(block, dst, geo, skip_bad, bb_off):
    if skip_bad and any(block[k][geo.page + bb_off] != 0xFF for k in (0, 1) if k < len(block)):
        return 0
    for pg in block:
        dst.write(pg[:geo.page])
    return len(block)


def split(src: BinaryIO, main: BinaryIO, oob: BinaryIO, geo: Geometry) -> int:
    n = 0
    for pg in _pages(src, geo.raw):
        main.write(pg[:geo.page])
        oob.write(pg[geo.page:])
        n += 1
    return n


def merge(main: BinaryIO, oob: Optional[BinaryIO], dst: BinaryIO, geo: Geometry,
          ecc: Optional[EccLayout] = None) -> int:
    """main (+ optional OOB file) -> raw; recompute ECC into the OOB if ``ecc`` is given."""
    n = 0
    pos = ecc.positions(geo.page, geo.oob) if ecc else []
    for pg in _pages(main, geo.page):
        spare = bytearray(oob.read(geo.oob) if oob else b"")
        spare += b"\xff" * (geo.oob - len(spare))
        raw = bytearray(pg + bytes(spare))
        if ecc is not None and pg.count(0xFF) != len(pg):
            for d, e, ln in pos:
                raw[e:e + ln] = ecc.calc(pg[d:d + ecc.step])
        dst.write(raw)
        n += 1
    return n


# ---------------------------------------------------------------------------
@dataclass
class EccReport:
    pages: int = 0
    steps: int = 0
    blank_steps: int = 0
    clean_steps: int = 0
    corrected_steps: int = 0
    corrected_bits: int = 0
    failed_steps: int = 0
    max_flips: int = 0
    failed_pages: List[int] = field(default_factory=list)
    histogram: Dict[int, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed_steps == 0

    def summary(self) -> str:
        s = ["%d pages, %d ECC steps: %d clean, %d blank, %d corrected (%d bit flips, max %d per step), "
             "%d uncorrectable" % (self.pages, self.steps, self.clean_steps, self.blank_steps,
                                   self.corrected_steps, self.corrected_bits, self.max_flips,
                                   self.failed_steps)]
        if self.failed_pages:
            s.append("uncorrectable pages: %s%s" % (", ".join(map(str, self.failed_pages[:32])),
                                                    " ..." if len(self.failed_pages) > 32 else ""))
        used = self.steps - self.blank_steps
        if used and self.failed_steps * 2 >= used:
            s.append("hint: every non-blank step fails - the ECC layout probably does not match "
                     "this image (try another --ecc preset or --ecc-offset)")
        return "\n".join(s)


def ecc_check(src: BinaryIO, geo: Geometry, lay: EccLayout, dst: Optional[BinaryIO] = None,
              strip: bool = False, progress: ProgressFn = None) -> EccReport:
    """Verify (and optionally correct into ``dst``) every page of a raw image."""
    rep = EccReport()
    pos = lay.positions(geo.page, geo.oob)
    for i, pg in enumerate(_pages(src, geo.raw)):
        rep.pages += 1
        raw = bytearray(pg)
        page_failed = False
        for d, e, ln in pos:
            rep.steps += 1
            data = raw[d:d + lay.step]
            stored = bytes(raw[e:e + ln])
            if data.count(0xFF) == len(data) and stored.count(0xFF) == len(stored):
                rep.blank_steps += 1
                continue
            buf = bytearray(data)
            r = lay.correct(buf, stored)
            if r == 0:
                rep.clean_steps += 1
            elif r > 0:
                rep.corrected_steps += 1
                rep.corrected_bits += r
                rep.max_flips = max(rep.max_flips, r)
                rep.histogram[r] = rep.histogram.get(r, 0) + 1
                raw[d:d + lay.step] = buf
            else:
                rep.failed_steps += 1
                page_failed = True
        if page_failed:
            rep.failed_pages.append(i)
        if dst is not None:
            dst.write(bytes(raw[:geo.page]) if strip else bytes(raw))
        if progress and i % 64 == 0:
            progress(i, 0)
    return rep
