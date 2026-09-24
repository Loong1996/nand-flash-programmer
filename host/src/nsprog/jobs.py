"""High level operations: read / write / erase / verify / blank check / bad-block scan.

Addressing is in erase blocks (``drv.block_size`` bytes of main area):
NAND block = chip block; SPI NOR block = smallest erase unit (usually 4 KiB).

Bad-block modes (NAND only):

* ``skip``  – bad blocks are skipped; file data continues in the next good
  block (same as U-Boot ``nand write``). Default for write/erase.
* ``keep``  – 1:1 physical mapping; bad blocks are read as-is, and on write
  their part of the file is dropped. Default for read (full raw backup).
* ``force`` – treat every block as good (can destroy factory bad-block
  markers; only with an explicit request).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, List, Optional

from .flash import FlashDriver, FlashError

log = logging.getLogger(__name__)

BB_MODES = ("skip", "keep", "force")


class Cancelled(Exception):
    pass


@dataclass
class Report:
    operation: str
    ok: bool = True
    bytes: int = 0
    seconds: float = 0.0
    bad_blocks: List[int] = field(default_factory=list)
    new_bad_blocks: List[int] = field(default_factory=list)
    skipped_blocks: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    first_mismatch: Optional[int] = None
    extra: Dict[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        rate = self.bytes / self.seconds / 1024 if self.seconds > 0 else 0
        parts = ["%s %s: %d bytes in %.1f s (%.0f KiB/s)" % (
            self.operation, "OK" if self.ok else "FAILED", self.bytes, self.seconds, rate)]
        if self.bad_blocks:
            parts.append("bad blocks: %s" % _fmt_blocks(self.bad_blocks))
        if self.new_bad_blocks:
            parts.append("blocks that failed now: %s" % _fmt_blocks(self.new_bad_blocks))
        if self.skipped_blocks:
            parts.append("skipped: %s" % _fmt_blocks(self.skipped_blocks))
        if self.first_mismatch is not None:
            parts.append("first difference at file offset 0x%X" % self.first_mismatch)
        parts.extend(self.errors[:10])
        return "\n".join(parts)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["summary"] = self.summary()
        return d


def _fmt_blocks(blocks: List[int]) -> str:
    s = ", ".join(str(b) for b in blocks[:32])
    return s + (" ... (%d total)" % len(blocks) if len(blocks) > 32 else "")


ProgressFn = Callable[[str, int, int], None]


class Context:
    """Progress reporting and cancellation shared by all jobs."""

    def __init__(self, progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None,
                 message: Optional[Callable[[str], None]] = None):
        self._progress = progress
        self.cancel = cancel or threading.Event()
        self._message = message

    def progress(self, phase: str, done: int, total: int) -> None:
        if self._progress:
            self._progress(phase, done, total)

    def msg(self, text: str) -> None:
        log.info(text)
        if self._message:
            self._message(text)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


# ---------------------------------------------------------------------------
def _block_range(drv: FlashDriver, start: int, count: Optional[int]) -> List[int]:
    if start < 0 or start >= drv.blocks:
        raise FlashError("start block %d out of range (0..%d)" % (start, drv.blocks - 1))
    if count is not None and count < 1:
        raise FlashError("block count must be at least 1 (got %d)" % count)
    end = drv.blocks if count is None else start + count
    if end > drv.blocks:
        raise FlashError("range ends at block %d, chip has %d blocks" % (end, drv.blocks))
    return list(range(start, end))


def scan_bad_blocks(drv: FlashDriver, ctx: Optional[Context] = None,
                    start: int = 0, count: Optional[int] = None) -> List[int]:
    ctx = ctx or Context()
    blocks = _block_range(drv, start, count)
    if not drv.has_bad_blocks:
        return []
    bad: List[int] = []
    step = 256
    for i in range(0, len(blocks), step):
        ctx.check()
        part = blocks[i:i + step]
        res = drv.bad_blocks(part)
        bad.extend(b for b in part if res[b])
        ctx.progress("scan", i + len(part), len(blocks))
    return bad


def _runs(blocks: List[int]):
    """Group consecutive block numbers."""
    run: List[int] = []
    for b in blocks:
        if run and b != run[-1] + 1:
            yield run
            run = []
        run.append(b)
    if run:
        yield run


def _check_voltage(drv: FlashDriver, allow: bool) -> None:
    if drv.voltage and drv.voltage < 2.5 and not allow:
        raise FlashError("%s is a %.1fV part; this programmer drives 3.3V signals. "
                         "Use a level-shifter adapter, then pass --allow-1v8." % (drv.name, drv.voltage))


# ---------------------------------------------------------------------------
def read(drv: FlashDriver, out: BinaryIO, *, start: int = 0, count: Optional[int] = None,
         oob: bool = True, bb: str = "keep", ctx: Optional[Context] = None,
         allow_1v8: bool = False) -> Report:
    ctx = ctx or Context()
    _check_voltage(drv, allow_1v8)
    t0 = time.monotonic()
    rep = Report("read")
    blocks = _block_range(drv, start, count)
    if drv.has_bad_blocks and bb == "skip":
        rep.bad_blocks = scan_bad_blocks(drv, ctx, start, len(blocks))
        blocks = [b for b in blocks if b not in set(rep.bad_blocks)]
    oob = oob and drv.spare_size > 0
    unit = drv.raw_page if oob else drv.page_size
    total = len(blocks) * drv.pages_per_block * unit
    done = 0
    for run in _runs(blocks):
        first = run[0] * drv.pages_per_block
        for data in drv.read_pages(first, len(run) * drv.pages_per_block, oob):
            out.write(data)
            done += len(data)
            ctx.progress("read", done, total)
            ctx.check()
    rep.bytes = done
    rep.seconds = time.monotonic() - t0
    return rep


def _load(data: bytes, drv: FlashDriver, oob: Optional[bool], ctx: Context):
    """Decide page layout of an image file; returns (oob, unit, pages list)."""
    if drv.spare_size == 0:
        oob = False
    elif oob is None:
        if len(data) % drv.raw_page == 0:
            oob = True
        elif len(data) % drv.page_size == 0:
            oob = False
            ctx.msg("image size is a multiple of the page size, writing main area only")
        else:
            oob = True
    unit = drv.raw_page if oob else drv.page_size
    if len(data) % unit:
        pad = unit - len(data) % unit
        ctx.msg("image is not a whole number of pages; padding %d bytes with FF" % pad)
        data = data + b"\xff" * pad
    pages = [data[i:i + unit] for i in range(0, len(data), unit)]
    return oob, unit, pages


def write(drv: FlashDriver, data: bytes, *, start: int = 0, oob: Optional[bool] = None,
          bb: str = "skip", erase: bool = True, verify: bool = True,
          ctx: Optional[Context] = None, allow_1v8: bool = False) -> Report:
    ctx = ctx or Context()
    _check_voltage(drv, allow_1v8)
    if bb not in BB_MODES:
        raise ValueError("bad-block mode must be one of %s" % (BB_MODES,))
    t0 = time.monotonic()
    rep = Report("write")
    if drv.spare_size == 0 and erase and len(data) % drv.page_size:
        # SPI NOR: fill the last partial page with what is already there.
        last = start * drv.pages_per_block + len(data) // drv.page_size
        if last < drv.pages:
            cur = next(iter(drv.read_pages(last, 1, False)))
            data = bytes(data) + cur[len(data) % drv.page_size:]
    oob, unit, pages = _load(data, drv, oob, ctx)
    ppb = drv.pages_per_block
    file_blocks = (len(pages) + ppb - 1) // ppb
    if start + file_blocks > drv.blocks:
        raise FlashError("image needs %d blocks from block %d, chip has %d" % (
            file_blocks, start, drv.blocks))

    bad = set()
    if drv.has_bad_blocks and bb != "force":
        rep.bad_blocks = scan_bad_blocks(drv, ctx, start, drv.blocks - start)
        bad = set(rep.bad_blocks)
        if bb == "skip" and file_blocks > drv.blocks - start - len(bad):
            raise FlashError("not enough good blocks for the image")

    if drv.has_bad_blocks and erase and len(pages) % ppb:
        ctx.msg("image ends inside a block: the remaining %d pages of that block will be erased"
                % (ppb - len(pages) % ppb))
    total = len(pages) * unit
    done = 0
    drv.begin_write()
    try:
        fb = 0
        phys = start
        # SPI NOR: process up to 16 erase units at a time so erases can merge.
        group = 1 if drv.has_bad_blocks else max(1, 65536 // drv.block_size)
        while fb < file_blocks:
            ctx.check()
            if phys >= drv.blocks:
                raise FlashError("ran out of good blocks at file block %d" % fb)
            if phys in bad:
                if bb == "keep":
                    rep.skipped_blocks.append(phys)
                    done += len(pages[fb * ppb:(fb + 1) * ppb]) * unit
                    fb += 1
                phys += 1
                continue
            n = min(group, file_blocks - fb, drv.blocks - phys)
            blk_list = list(range(phys, phys + n))
            chunk = pages[fb * ppb:(fb + n) * ppb]
            chunk = _merge_partial(drv, phys, chunk, n, oob, erase)
            ok, msg = _write_blocks(drv, blk_list, chunk, oob, erase, verify, rep, ctx)
            if not ok:
                if drv.has_bad_blocks and bb == "skip":
                    ctx.msg("block %d failed (%s); marking it bad and retrying on the next block"
                            % (phys, msg))
                    rep.new_bad_blocks.append(phys)
                    bad.add(phys)
                    phys += 1
                    continue
                rep.ok = False
                rep.errors.append("block %d: %s" % (phys, msg))
                break
            done += len(pages[fb * ppb:(fb + n) * ppb]) * unit
            ctx.progress("write", done, total)
            fb += n
            phys += n
    finally:
        drv.end_write()
    rep.bytes = done
    rep.seconds = time.monotonic() - t0
    return rep


def _merge_partial(drv, phys, chunk, n, oob, erase):
    """SPI NOR: keep existing data in the tail of a partially covered erase unit."""
    need = n * drv.pages_per_block
    if len(chunk) >= need or drv.has_bad_blocks or not erase:
        return chunk
    first = phys * drv.pages_per_block + len(chunk)
    tail = list(drv.read_pages(first, need - len(chunk), oob))
    return list(chunk) + tail


def _blank(page: bytes) -> bool:
    return page.count(0xFF) == len(page)


def _write_blocks(drv, blk_list, chunk, oob, erase, verify, rep, ctx):
    ppb = drv.pages_per_block
    if erase:
        for r in drv.erase_blocks(blk_list):
            if not r.ok:
                return False, r.message or "erase failed"
    first = blk_list[0] * ppb
    items = [(first + i, p) for i, p in enumerate(chunk) if not _blank(p)]
    for i in range(0, len(items), 16):
        for r in drv.program_pages(items[i:i + 16]):
            if not r.ok:
                return False, "page %d: %s" % (r.index, r.message or "program failed")
        ctx.check()
    if verify:
        got = list(drv.read_pages(first, len(chunk), oob))
        for i, (want, have) in enumerate(zip(chunk, got)):
            if want != have:
                off = next(k for k in range(len(want)) if want[k] != have[k])
                return False, "verify mismatch in page %d at byte %d (%02X != %02X)" % (
                    first + i, off, have[off], want[off])
    return True, ""


def erase(drv: FlashDriver, *, start: int = 0, count: Optional[int] = None, bb: str = "skip",
          ctx: Optional[Context] = None, allow_1v8: bool = False) -> Report:
    ctx = ctx or Context()
    _check_voltage(drv, allow_1v8)
    t0 = time.monotonic()
    rep = Report("erase")
    blocks = _block_range(drv, start, count)
    if drv.has_bad_blocks and bb != "force":
        rep.bad_blocks = scan_bad_blocks(drv, ctx, start, len(blocks))
        blocks = [b for b in blocks if b not in set(rep.bad_blocks)]
        rep.skipped_blocks = list(rep.bad_blocks)
    drv.begin_write()
    try:
        erase_chip = getattr(drv, "erase_chip", None)
        whole = (not drv.has_bad_blocks and start == 0 and len(blocks) == drv.blocks
                 and erase_chip is not None)
        if whole:
            ctx.msg("chip erase (this can take a while)")
            ticks = [0]

            def tick():
                ticks[0] += 1
                ctx.progress("erase", min(ticks[0], 19), 20)
            if erase_chip is not None and erase_chip(tick):
                ctx.progress("erase", 1, 1)
                blocks = []
        step = 64 if drv.has_bad_blocks else 16
        for i in range(0, len(blocks), step):
            ctx.check()
            for r in drv.erase_blocks(blocks[i:i + step]):
                if not r.ok:
                    rep.new_bad_blocks.append(r.index)
                    rep.errors.append("block %d: %s" % (r.index, r.message))
            ctx.progress("erase", i + step, len(blocks))
    finally:
        drv.end_write()
    rep.ok = not rep.errors
    rep.bytes = (count if count is not None else drv.blocks - start) * drv.block_size
    rep.seconds = time.monotonic() - t0
    return rep


def verify(drv: FlashDriver, data: bytes, *, start: int = 0, oob: Optional[bool] = None,
           bb: str = "skip", ctx: Optional[Context] = None) -> Report:
    ctx = ctx or Context()
    t0 = time.monotonic()
    rep = Report("verify")
    oob, unit, pages = _load(data, drv, oob, ctx)
    ppb = drv.pages_per_block
    file_blocks = (len(pages) + ppb - 1) // ppb
    bad = set()
    if drv.has_bad_blocks and bb != "force":
        rep.bad_blocks = scan_bad_blocks(drv, ctx, start, drv.blocks - start)
        bad = set(rep.bad_blocks)
    fb, phys, done = 0, start, 0
    total = len(pages) * unit
    while fb < file_blocks and rep.first_mismatch is None:
        ctx.check()
        if phys >= drv.blocks:
            rep.errors.append("image longer than the chip")
            break
        if phys in bad:
            if bb == "keep":
                rep.skipped_blocks.append(phys)
                fb += 1
            phys += 1
            continue
        want = pages[fb * ppb:(fb + 1) * ppb]
        got = drv.read_pages(phys * ppb, len(want), oob)
        for i, (w, h) in enumerate(zip(want, got)):
            if w != h:
                k = next(j for j in range(len(w)) if w[j] != h[j])
                rep.first_mismatch = (fb * ppb + i) * unit + k
                rep.errors.append("mismatch in block %d page %d byte %d: chip %02X, file %02X"
                                  % (phys, i, k, h[k], w[k]))
                break
            done += unit
        ctx.progress("verify", done, total)
        fb += 1
        phys += 1
    rep.ok = rep.first_mismatch is None and not rep.errors
    rep.bytes = done
    rep.seconds = time.monotonic() - t0
    return rep


def blank_check(drv: FlashDriver, *, start: int = 0, count: Optional[int] = None,
                ctx: Optional[Context] = None) -> Report:
    ctx = ctx or Context()
    t0 = time.monotonic()
    rep = Report("blank check")
    blocks = _block_range(drv, start, count)
    bad = set(scan_bad_blocks(drv, ctx, start, len(blocks))) if drv.has_bad_blocks else set()
    rep.bad_blocks = sorted(bad)
    total = len(blocks) * drv.block_size
    done = 0
    for run in _runs([b for b in blocks if b not in bad]):
        first = run[0] * drv.pages_per_block
        for i, page in enumerate(drv.read_pages(first, len(run) * drv.pages_per_block, False)):
            if not _blank(page):
                k = next(j for j in range(len(page)) if page[j] != 0xFF)
                rep.first_mismatch = (first + i) * drv.page_size + k
                rep.errors.append("not blank: block %d page %d byte %d = %02X" % (
                    (first + i) // drv.pages_per_block, (first + i) % drv.pages_per_block, k, page[k]))
                rep.ok = False
                break
            done += len(page)
            ctx.progress("blank", done, total)
            ctx.check()
        if not rep.ok:
            break
    rep.bytes = done
    rep.seconds = time.monotonic() - t0
    return rep


def bad_block_report(drv: FlashDriver, ctx: Optional[Context] = None) -> Report:
    t0 = time.monotonic()
    rep = Report("bad block scan")
    rep.bad_blocks = scan_bad_blocks(drv, ctx)
    rep.bytes = drv.size
    rep.seconds = time.monotonic() - t0
    return rep
