"""Page-by-page comparison of two NAND images (no hardware needed).

Typical uses: two reads of the same chip (bit flips / unstable wiring), a
backup against the file that was written, or two firmware versions.

Every differing page is classified:

* ``bitflip``  - only a few bits differ (at most ``flip_bits`` in every
  512-byte slice): read disturb, weak cells or marginal bus timing
* ``erased``   - the page is blank (all FF) in exactly one of the images
* ``changed``  - real content differences
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

from .image import Geometry

ProgressFn = Optional[Callable[[int, int], None]]

SLICE = 512                     # bit flips are judged per 512-byte slice (typical ECC step)


def _bits(a: bytes, b: bytes) -> Tuple[int, int]:
    """(bits 1 -> 0, bits 0 -> 1) going from ``a`` to ``b``."""
    x = int.from_bytes(a, "little")
    y = int.from_bytes(b, "little")
    d = x ^ y
    return bin(d & x).count("1"), bin(d & y).count("1")


@dataclass
class PageDiff:
    page: int
    kind: str                   # bitflip | erased | changed
    main_bytes: int             # differing bytes in the main area
    oob_bytes: int              # differing bytes in the spare area
    bits_10: int                # bits 1 -> 0 (A -> B)
    bits_01: int                # bits 0 -> 1
    first: int                  # offset of the first differing byte inside the page

    @property
    def bits(self) -> int:
        return self.bits_10 + self.bits_01

    def as_dict(self) -> dict:
        return {"page": self.page, "kind": self.kind, "main_bytes": self.main_bytes,
                "oob_bytes": self.oob_bytes, "bits_10": self.bits_10, "bits_01": self.bits_01,
                "first": self.first}


@dataclass
class DiffReport:
    geo: Geometry
    pages: int = 0              # pages compared (the longer image decides)
    size_a: int = 0
    size_b: int = 0
    same_pages: int = 0
    kinds: Dict[str, int] = field(default_factory=lambda: {"bitflip": 0, "erased": 0, "changed": 0})
    bits_10: int = 0
    bits_01: int = 0
    diffs: List[PageDiff] = field(default_factory=list)       # first ``keep`` differing pages
    blocks: Dict[int, Dict[str, int]] = field(default_factory=dict)   # block -> kind -> pages
    oob_only_pages: int = 0

    @property
    def identical(self) -> bool:
        return self.size_a == self.size_b and self.same_pages == self.pages

    @property
    def diff_pages(self) -> int:
        return sum(self.kinds.values())

    def summary(self) -> str:
        g = self.geo
        s = ["%d pages compared (page %d + OOB %d, %d pages/block)" % (self.pages, g.page, g.oob, g.ppb)]
        if self.size_a != self.size_b:
            s.append("sizes differ: A %d bytes, B %d bytes (missing pages count as blank)"
                     % (self.size_a, self.size_b))
        if self.identical:
            s.append("identical")
            return "\n".join(s)
        s.append("%d identical, %d different: %d bit-flip, %d erased in one image, %d changed"
                 % (self.same_pages, self.diff_pages, self.kinds["bitflip"], self.kinds["erased"],
                    self.kinds["changed"]))
        if self.kinds["bitflip"]:
            s.append("bit flips: %d bits 1->0, %d bits 0->1 (A -> B)" % (self.bits_10, self.bits_01))
        if self.oob_only_pages:
            s.append("%d pages differ only in the OOB (ECC / markers)" % self.oob_only_pages)
        blks = sorted(self.blocks)
        s.append("blocks with differences (%d): %s%s" % (
            len(blks), ", ".join(map(str, blks[:32])), " ..." if len(blks) > 32 else ""))
        hint = self.hint()
        if hint:
            s.append("hint: " + hint)
        return "\n".join(s)

    def hint(self) -> str:
        k = self.kinds
        if k["bitflip"] and not k["changed"] and not k["erased"]:
            if self.bits_01 == 0 or self.bits_10 == 0:
                return ("only isolated bit flips, all in one direction: typical of weak cells or read "
                        "disturb - ECC handles these; re-read to see whether they move")
            return ("only isolated bit flips in both directions: if both images are reads of the same "
                    "chip, suspect the wiring or bus timing (try --nand-timing safe, shorter leads)")
        if k["changed"] and self.pages and k["changed"] * 2 > self.pages:
            return "most pages differ: different firmware, or the images have different page/OOB layouts"
        return ""

    def block_rows(self) -> List[dict]:
        return [{"block": b, **v} for b, v in sorted(self.blocks.items())]


def diff(a: BinaryIO, b: BinaryIO, geo: Geometry, flip_bits: int = 4, keep: int = 1000,
         progress: ProgressFn = None) -> DiffReport:
    """Compare two images page by page (raw when ``geo.oob`` > 0, else main-only)."""
    rep = DiffReport(geo)
    for f, attr in ((a, "size_a"), (b, "size_b")):
        f.seek(0, 2)
        setattr(rep, attr, f.tell())
        f.seek(0)
    size = geo.raw
    total = (max(rep.size_a, rep.size_b) + size - 1) // size
    blank = b"\xff" * size
    for i in range(total):
        pa = a.read(size)
        pb = b.read(size)
        pa += blank[len(pa):]
        pb += blank[len(pb):]
        rep.pages += 1
        if pa == pb:
            rep.same_pages += 1
        else:
            d = _page_diff(i, pa, pb, geo, flip_bits)
            rep.kinds[d.kind] += 1
            if d.kind == "bitflip":
                rep.bits_10 += d.bits_10
                rep.bits_01 += d.bits_01
            if d.main_bytes == 0:
                rep.oob_only_pages += 1
            blk = rep.blocks.setdefault(i // geo.ppb, {"bitflip": 0, "erased": 0, "changed": 0})
            blk[d.kind] += 1
            if len(rep.diffs) < keep:
                rep.diffs.append(d)
        if progress and i % 256 == 0:
            progress(i, total)
    return rep


def _page_diff(i: int, pa: bytes, pb: bytes, geo: Geometry, flip_bits: int) -> PageDiff:
    main_a, main_b = pa[:geo.page], pb[:geo.page]
    main_n = sum(1 for x, y in zip(main_a, main_b) if x != y)
    oob_n = sum(1 for x, y in zip(pa[geo.page:], pb[geo.page:]) if x != y)
    first = next(k for k in range(len(pa)) if pa[k] != pb[k])
    b10, b01 = _bits(pa, pb)
    blank_a = main_a.count(0xFF) == len(main_a)
    blank_b = main_b.count(0xFF) == len(main_b)
    if blank_a != blank_b and (main_n > 8 or b10 + b01 > flip_bits):
        kind = "erased"
    else:
        worst = 0
        for off in range(0, len(pa), SLICE):
            x, y = _bits(pa[off:off + SLICE], pb[off:off + SLICE])
            worst = max(worst, x + y)
        kind = "bitflip" if worst <= flip_bits else "changed"
    return PageDiff(i, kind, main_n, oob_n, b10, b01, first)


def diff_files(path_a: str, path_b: str, geo: Geometry, **kw) -> DiffReport:
    with open(path_a, "rb") as a, open(path_b, "rb") as b:
        return diff(a, b, geo, **kw)
