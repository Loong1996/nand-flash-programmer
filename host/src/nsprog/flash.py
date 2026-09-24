"""Flash drivers built on the micro-op protocol.

All drivers share a page/block view so the job layer (``jobs.py``) can treat
them alike:

* parallel NAND / SPI NAND: pages with spare (OOB) area, blocks, bad blocks;
* SPI NOR: ``page`` = program page, ``block`` = erase unit, no spare area.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import chipdb
from . import protocol as P
from .device import Device
from .onfi import OnfiParams, parse_param_page
from .sfdp import parse_bfpt, parse_header

log = logging.getLogger(__name__)

CLK_HZ = 27_000_000


class FlashError(IOError):
    pass


@dataclass
class OpResult:
    index: int
    ok: bool
    status: int = 0
    message: str = ""


class FlashDriver:
    kind = "?"
    name = "?"
    page_size = 0
    spare_size = 0
    pages_per_block = 1
    blocks = 0
    has_bad_blocks = False
    voltage = 3.3
    #: pages read per batch (bigger = faster, coarser progress)
    read_batch = 16

    def __init__(self, dev: Device):
        self.dev = dev

    # geometry ---------------------------------------------------------------
    @property
    def raw_page(self) -> int:
        return self.page_size + self.spare_size

    @property
    def block_size(self) -> int:
        return self.page_size * self.pages_per_block

    @property
    def size(self) -> int:
        return self.block_size * self.blocks

    @property
    def pages(self) -> int:
        return self.pages_per_block * self.blocks

    def describe(self) -> str:
        return self.name

    def info(self) -> dict:
        return {"kind": self.kind, "name": self.name, "size": self.size,
                "page_size": self.page_size, "spare_size": self.spare_size,
                "pages_per_block": self.pages_per_block, "blocks": self.blocks,
                "block_size": self.block_size, "voltage": self.voltage,
                "has_bad_blocks": self.has_bad_blocks, "description": self.describe()}

    # operations (overridden) ------------------------------------------------
    def begin_write(self) -> None:
        pass

    def end_write(self) -> None:
        pass

    def read_pages(self, first: int, count: int, oob: bool = True) -> Iterator[bytes]:
        raise NotImplementedError

    def program_pages(self, items: Sequence[Tuple[int, bytes]]) -> List[OpResult]:
        raise NotImplementedError

    def erase_blocks(self, blocks: Sequence[int]) -> List[OpResult]:
        raise NotImplementedError

    def bad_blocks(self, blocks: Sequence[int]) -> Dict[int, bool]:
        return {b: False for b in blocks}


# ============================================================================
# Parallel NAND
class ParallelNand(FlashDriver):
    kind = "nand"
    has_bad_blocks = True

    def __init__(self, dev: Device, chip: chipdb.NandChip, *, use_rb: bool = True,
                 id_bytes: bytes = b"", onfi: Optional[OnfiParams] = None):
        super().__init__(dev)
        self.chip = chip
        self.name = chip.name
        self.page_size = chip.page_size
        self.spare_size = chip.spare_size
        self.pages_per_block = chip.pages_per_block
        self.blocks = chip.blocks
        self.voltage = chip.voltage
        self.use_rb = use_rb
        self.id_bytes = id_bytes
        self.onfi = onfi
        self.read_batch = max(1, 65536 // chip.raw_page)

    def describe(self) -> str:
        return self.chip.describe()

    # helpers ----------------------------------------------------------------
    def _addr(self, col: int, row: int) -> bytes:
        c = self.chip
        return (col.to_bytes(4, "little")[:c.col_cycles] +
                row.to_bytes(4, "little")[:c.row_cycles])

    def _wait_ready(self, b: P.Batch, timeout_ms: int, return_to_read: Optional[int] = None):
        if self.use_rb:
            return b.nand_wait_rb(timeout_ms)
        r = b.nand_poll_status(0x40, 0x40, timeout_ms)
        if return_to_read is not None:
            b.nand_cmd(return_to_read)
        return r

    def _read_ops(self, b: P.Batch, row: int, col: int, n: int):
        c = self.chip
        cmd = c.cmd_read1
        if c.small_page:
            if col >= c.page_size:
                cmd, col = 0x50, col - c.page_size
            elif col >= 256:
                cmd, col = 0x01, col - 256
        b.nand_ce(True)
        b.nand_cmd(cmd)
        b.nand_addr(self._addr(col, row))
        if c.cmd_read2 is not None and not c.small_page:
            b.nand_cmd(c.cmd_read2)
        ready = self._wait_ready(b, 10, return_to_read=cmd)
        data = b.nand_read(n)
        b.nand_ce(False)
        return data, ready

    # identification ---------------------------------------------------------
    def reset(self) -> None:
        b = P.Batch()
        b.nand_ce(True)
        b.nand_cmd(self.chip.cmd_reset)
        self._wait_ready(b, 20)
        b.nand_ce(False)
        self.dev.run(b)

    @staticmethod
    def read_id_raw(dev: Device, n: int = 8) -> bytes:
        b = P.Batch()
        b.nand_ce(True)
        b.nand_cmd(0xFF)
        b.nand_wait_rb(20)
        b.nand_cmd(0x90)
        b.nand_addr(b"\x00")
        r = b.nand_read(n)
        b.nand_ce(False)
        dev.run(b)
        return r.value

    @staticmethod
    def read_onfi(dev: Device) -> Optional[OnfiParams]:
        b = P.Batch()
        b.nand_ce(True)
        b.nand_cmd(0x90)
        b.nand_addr(b"\x20")
        sig = b.nand_read(4)
        b.nand_ce(False)
        dev.run(b)
        if sig.value != b"ONFI":
            return None
        b = P.Batch()
        b.nand_ce(True)
        b.nand_cmd(0xEC)
        b.nand_addr(b"\x00")
        b.nand_wait_rb(10)
        raw = b.nand_read(768)
        b.nand_ce(False)
        dev.run(b)
        return parse_param_page(raw.value)

    # data -------------------------------------------------------------------
    def read_pages(self, first: int, count: int, oob: bool = True) -> Iterator[bytes]:
        n = self.raw_page if oob else self.page_size
        page = first
        end = first + count
        while page < end:
            b = P.Batch()
            items = []
            for row in range(page, min(end, page + self.read_batch)):
                items.append(self._read_ops(b, row, 0, n))
            self.dev.run(b)
            for data, ready in items:
                if self.use_rb and not ready.value:
                    raise FlashError("NAND stayed busy during read (check R/B# wiring)")
                yield data.value
            page += len(items)

    def begin_write(self) -> None:
        self.dev.set_pin_ctrl(self.dev.pin_ctrl | P.PIN_NAND_WP_HIGH)

    def end_write(self) -> None:
        self.dev.set_pin_ctrl(self.dev.pin_ctrl & ~P.PIN_NAND_WP_HIGH)

    def _check(self, idx: int, r: P.PollResult, what: str) -> OpResult:
        if not r.ok:
            return OpResult(idx, False, r.status, "%s timeout" % what)
        if not r.status & 0x80:
            return OpResult(idx, False, r.status, "chip is write protected (WP#)")
        if r.status & 0x01:
            return OpResult(idx, False, r.status, "%s failed" % what)
        return OpResult(idx, True, r.status)

    def program_pages(self, items: Sequence[Tuple[int, bytes]]) -> List[OpResult]:
        c = self.chip
        b = P.Batch()
        polls = []
        for row, data in items:
            b.nand_ce(True)
            if c.small_page:
                b.nand_cmd(0x00)
            b.nand_cmd(c.cmd_write1)
            b.nand_addr(self._addr(0, row))
            b.nand_write(data)
            b.nand_cmd(c.cmd_write2)
            polls.append((row, b.nand_poll_status(0x40, 0x40, 20)))
            b.nand_ce(False)
        self.dev.run(b)
        return [self._check(row, r.value, "program") for row, r in polls]

    def erase_blocks(self, blocks: Sequence[int]) -> List[OpResult]:
        c = self.chip
        b = P.Batch()
        polls = []
        for blk in blocks:
            row = blk * self.pages_per_block
            b.nand_ce(True)
            b.nand_cmd(c.cmd_erase1)
            b.nand_addr(row.to_bytes(4, "little")[:c.row_cycles])
            b.nand_cmd(c.cmd_erase2)
            polls.append((blk, b.nand_poll_status(0x40, 0x40, 100)))
            b.nand_ce(False)
        self.dev.run(b)
        return [self._check(blk, r.value, "erase") for blk, r in polls]

    def bad_blocks(self, blocks: Sequence[int]) -> Dict[int, bool]:
        """Factory bad-block markers: spare byte ``bb_mark_off`` of pages 0 and 1."""
        c = self.chip
        out = {}
        blocks = list(blocks)
        for i in range(0, len(blocks), 64):
            b = P.Batch()
            res = []
            for blk in blocks[i:i + 64]:
                marks = []
                for pg in (0, 1):
                    row = blk * self.pages_per_block + pg
                    data, _ = self._read_ops(b, row, c.page_size + c.bb_mark_off, 1)
                    marks.append(data)
                res.append((blk, marks))
            self.dev.run(b)
            for blk, marks in res:
                out[blk] = any(m.value[0] != 0xFF for m in marks)
        return out


def nand_chip_from_onfi(p: OnfiParams, id_bytes: bytes) -> chipdb.NandChip:
    name = p.model or "ONFI NAND"
    if p.manufacturer:
        name = "%s %s" % (p.manufacturer, name)
    chip = chipdb.NandChip(
        name=name, page_size=p.page_size, block_size=p.page_size * p.pages_per_block,
        total_size=p.page_size * p.pages_per_block * p.blocks, spare_size=p.spare_size,
        bb_mark_off=0, row_cycles=p.row_cycles, col_cycles=p.col_cycles,
        ids=tuple(id_bytes[:5]), voltage=chipdb.guess_voltage(name), source="onfi")
    return chip


# ============================================================================
# SPI NAND
class SpiNand(FlashDriver):
    kind = "spinand"
    has_bad_blocks = True

    def __init__(self, dev: Device, chip: chipdb.SpiNandChip, *, ecc: bool = False):
        super().__init__(dev)
        self.chip = chip
        self.name = chip.name
        self.page_size = chip.page_size
        self.spare_size = chip.spare_size
        self.pages_per_block = chip.pages_per_block
        self.blocks = chip.blocks
        self.voltage = chip.voltage
        self.ecc = ecc
        self.read_batch = max(1, 65536 // chip.raw_page)

    def describe(self) -> str:
        return self.chip.describe()

    @staticmethod
    def _cmd(b: P.Batch, data: bytes) -> None:
        b.spi_cs(True)
        b.spi_write(data)
        b.spi_cs(False)

    @staticmethod
    def _poll(b: P.Batch, timeout_ms: int):
        return b.spi_poll(b"\x0F\xC0", 0x01, 0x00, timeout_ms)

    def get_feature(self, reg: int) -> int:
        b = P.Batch()
        b.spi_cs(True)
        b.spi_write(bytes([0x0F, reg]))
        r = b.spi_read(1)
        b.spi_cs(False)
        self.dev.run(b)
        return r.value[0]

    def set_feature(self, reg: int, value: int) -> None:
        b = P.Batch()
        self._cmd(b, bytes([0x1F, reg, value]))
        self.dev.run(b)

    def setup(self) -> None:
        """Reset, unlock all blocks and set the on-die ECC as requested."""
        b = P.Batch()
        self._cmd(b, b"\xFF")
        self._poll(b, 10)
        self.dev.run(b)
        self.set_feature(0xA0, 0x00)
        cfg = self.get_feature(0xB0)
        cfg = (cfg | 0x10) if self.ecc else (cfg & ~0x10)
        self.set_feature(0xB0, cfg)

    def _col(self, row: int, col: int) -> int:
        if self.chip.plane_bit:
            col |= ((row // self.pages_per_block) & 1) << 12
        return col

    def _row(self, row: int) -> bytes:
        return row.to_bytes(3, "big")

    def _read_ops(self, b: P.Batch, row: int, col: int, n: int):
        self._cmd(b, b"\x13" + self._row(row))
        ready = self._poll(b, 10)
        col = self._col(row, col).to_bytes(2, "big")
        b.spi_cs(True)
        b.spi_write(b"\x03" + (b"\x00" + col if self.chip.read_dummy_first else col + b"\x00"))
        data = b.spi_read(n)
        b.spi_cs(False)
        return data, ready

    def read_pages(self, first: int, count: int, oob: bool = True) -> Iterator[bytes]:
        n = self.raw_page if oob else self.page_size
        page, end = first, first + count
        while page < end:
            b = P.Batch()
            items = [self._read_ops(b, row, 0, n) for row in range(page, min(end, page + self.read_batch))]
            self.dev.run(b)
            for data, ready in items:
                if not ready.value.ok:
                    raise FlashError("SPI NAND stayed busy during read")
                yield data.value
            page += len(items)

    def program_pages(self, items: Sequence[Tuple[int, bytes]]) -> List[OpResult]:
        b = P.Batch()
        polls = []
        for row, data in items:
            self._cmd(b, b"\x06")
            b.spi_cs(True)
            b.spi_write(b"\x02" + self._col(row, 0).to_bytes(2, "big"))
            b.spi_write(data)
            b.spi_cs(False)
            self._cmd(b, b"\x10" + self._row(row))
            polls.append((row, self._poll(b, 20)))
        self.dev.run(b)
        out = []
        for row, r in polls:
            v = r.value
            if not v.ok:
                out.append(OpResult(row, False, v.status, "program timeout"))
            elif v.status & 0x08:
                out.append(OpResult(row, False, v.status, "program failed (P_FAIL)"))
            else:
                out.append(OpResult(row, True, v.status))
        return out

    def erase_blocks(self, blocks: Sequence[int]) -> List[OpResult]:
        b = P.Batch()
        polls = []
        for blk in blocks:
            self._cmd(b, b"\x06")
            self._cmd(b, b"\xD8" + self._row(blk * self.pages_per_block))
            polls.append((blk, self._poll(b, 100)))
        self.dev.run(b)
        out = []
        for blk, r in polls:
            v = r.value
            if not v.ok:
                out.append(OpResult(blk, False, v.status, "erase timeout"))
            elif v.status & 0x04:
                out.append(OpResult(blk, False, v.status, "erase failed (E_FAIL)"))
            else:
                out.append(OpResult(blk, True, v.status))
        return out

    def bad_blocks(self, blocks: Sequence[int]) -> Dict[int, bool]:
        out = {}
        blocks = list(blocks)
        for i in range(0, len(blocks), 64):
            b = P.Batch()
            res = []
            for blk in blocks[i:i + 64]:
                marks = [self._read_ops(b, blk * self.pages_per_block + pg, self.page_size, 1)[0]
                         for pg in (0, 1)]
                res.append((blk, marks))
            self.dev.run(b)
            for blk, marks in res:
                out[blk] = any(m.value[0] != 0xFF for m in marks)
        return out


# ============================================================================
# SPI NOR
class SpiNor(FlashDriver):
    kind = "spinor"
    has_bad_blocks = False

    def __init__(self, dev: Device, chip: chipdb.SpiNorChip):
        super().__init__(dev)
        self.chip = chip
        self.name = chip.name
        self.page_size = chip.page_size
        self.spare_size = 0
        self.pages_per_block = max(1, chip.block_size // chip.page_size)
        self.blocks = chip.total_size // chip.block_size
        self.voltage = chip.voltage
        self.four = False
        self.read_batch = max(1, 65536 // chip.page_size)

    def describe(self) -> str:
        return self.chip.describe()

    # helpers ----------------------------------------------------------------
    def _dev_addr(self, off: int) -> int:
        c = self.chip
        if c.linear:
            return off
        return ((off // c.page_size) << c.page_off) | (off % c.page_size)

    def _abytes(self, addr: int) -> bytes:
        return addr.to_bytes(4 if self.four else 3, "big")

    def _poll(self, b: P.Batch, timeout_ms: int):
        c = self.chip
        mask = 1 << c.busy_bit
        value = 0 if c.busy_state == 1 else mask
        return b.spi_poll(bytes([c.status_cmd]), mask, value, timeout_ms)

    def _cmd(self, b: P.Batch, data: bytes) -> None:
        b.spi_cs(True)
        b.spi_write(data)
        b.spi_cs(False)

    def _wren(self, b: P.Batch) -> None:
        if self.chip.write_en_cmd is not None:
            self._cmd(b, bytes([self.chip.write_en_cmd]))

    # identification ---------------------------------------------------------
    @staticmethod
    def read_jedec(dev: Device, n: int = 5) -> bytes:
        b = P.Batch()
        b.spi_cs(True)
        b.spi_write(b"\x9F")
        r = b.spi_read(n)
        b.spi_cs(False)
        dev.run(b)
        return r.value

    @staticmethod
    def read_sfdp(dev: Device, addr: int, n: int) -> bytes:
        b = P.Batch()
        b.spi_cs(True)
        b.spi_write(b"\x5A" + addr.to_bytes(3, "big") + b"\x00")
        r = b.spi_read(n)
        b.spi_cs(False)
        dev.run(b)
        return r.value

    @classmethod
    def probe_sfdp(cls, dev: Device):
        hdr = cls.read_sfdp(dev, 0, 64)
        loc = parse_header(hdr)
        if not loc:
            return None
        ptp, length = loc
        return parse_bfpt(cls.read_sfdp(dev, ptp, 4 * min(length, 20)))

    def status(self) -> Tuple[int, int]:
        b = P.Batch()
        b.spi_cs(True)
        b.spi_write(bytes([self.chip.status_cmd]))
        s1 = b.spi_read(1)
        b.spi_cs(False)
        b.spi_cs(True)
        b.spi_write(b"\x35")
        s2 = b.spi_read(1)
        b.spi_cs(False)
        self.dev.run(b)
        return s1.value[0], s2.value[0]

    # write enable / protection ----------------------------------------------
    def begin_write(self) -> None:
        self.unprotect()

    def unprotect(self) -> bool:
        """Clear block-protection bits. Returns True if something was changed."""
        c = self.chip
        if not c.linear or c.status_cmd != 0x05:
            return False
        b = P.Batch()
        if c.unlock_cmd is not None:
            self._wren(b)
            self._cmd(b, bytes([c.unlock_cmd]))
            self._poll(b, 50)
            self.dev.run(b)
            return True
        s1, _ = self.status()
        if not s1 & 0xBC:
            return False
        self._wren(b)
        self._cmd(b, b"\x01\x00")
        self._poll(b, 200)
        self.dev.run(b)
        s1, _ = self.status()
        if s1 & 0x3C:
            raise FlashError("could not clear SPI flash protection bits (SR1=%02X); "
                             "check that WP# (pin 3) is high" % s1)
        return True

    def enter_4byte(self) -> None:
        if self.chip.addr_bytes >= 4 and self.chip.total_size > 16 * 1024 * 1024 and not self.four:
            b = P.Batch()
            self._wren(b)
            self._cmd(b, b"\xB7")
            self.dev.run(b)
            self.four = True

    def exit_4byte(self) -> None:
        if self.four:
            b = P.Batch()
            self._cmd(b, b"\xE9")
            self.dev.run(b)
            self.four = False

    # data -------------------------------------------------------------------
    def read(self, off: int, n: int) -> bytes:
        b = P.Batch()
        r = self._read_ops(b, off, n)
        self.dev.run(b)
        return r.value

    def _read_ops(self, b: P.Batch, off: int, n: int):
        c = self.chip
        b.spi_cs(True)
        b.spi_write(bytes([c.read_cmd]) + self._abytes(self._dev_addr(off)) + b"\x00" * c.read_dummy)
        r = b.spi_read(n)
        b.spi_cs(False)
        return r

    def read_pages(self, first: int, count: int, oob: bool = True) -> Iterator[bytes]:
        self.enter_4byte()
        chunk_pages = self.read_batch
        page, end = first, first + count
        while page < end:
            k = min(chunk_pages, end - page)
            b = P.Batch()
            r = self._read_ops(b, page * self.page_size, k * self.page_size)
            self.dev.run(b)
            data = r.value
            for i in range(k):
                yield data[i * self.page_size:(i + 1) * self.page_size]
            page += k

    def program_pages(self, items: Sequence[Tuple[int, bytes]]) -> List[OpResult]:
        self.enter_4byte()
        c = self.chip
        b = P.Batch()
        polls = []
        for page, data in items:
            self._wren(b)
            b.spi_cs(True)
            b.spi_write(bytes([c.write_cmd]) + self._abytes(self._dev_addr(page * self.page_size)))
            b.spi_write(data[:self.page_size])
            b.spi_cs(False)
            polls.append((page, self._poll(b, 50)))
        self.dev.run(b)
        return [OpResult(p, r.value.ok, r.value.status, "" if r.value.ok else "program timeout")
                for p, r in polls]

    def erase_blocks(self, blocks: Sequence[int]) -> List[OpResult]:
        """Erase ``block_size`` units, merging aligned runs into larger erases."""
        self.enter_4byte()
        c = self.chip
        blocks = sorted(set(blocks))
        todo = set(blocks)
        types = sorted((c.erase_types or {c.block_size: c.erase_cmd}).items(), reverse=True)
        b = P.Batch()
        polls = []
        for blk in blocks:
            if blk not in todo:
                continue
            off = blk * c.block_size
            for size, opcode in types:
                if size < c.block_size or size % c.block_size:
                    continue
                n = size // c.block_size
                if off % size == 0 and all(x in todo for x in range(blk, blk + n)):
                    self._wren(b)
                    self._cmd(b, bytes([opcode]) + self._abytes(self._dev_addr(off)))
                    polls.append((blk, n, self._poll(b, 3000 if size >= 32768 else 1000)))
                    todo.difference_update(range(blk, blk + n))
                    break
            else:
                self._wren(b)
                self._cmd(b, bytes([c.erase_cmd]) + self._abytes(self._dev_addr(off)))
                polls.append((blk, 1, self._poll(b, 1000)))
                todo.discard(blk)
        self.dev.run(b)
        out = []
        for blk, n, r in polls:
            for x in range(blk, blk + n):
                out.append(OpResult(x, r.value.ok, r.value.status,
                                    "" if r.value.ok else "erase timeout"))
        return out

    def erase_chip(self, progress=None) -> bool:
        c = self.chip
        if c.chip_erase_cmd is None:
            return False
        b = P.Batch()
        self._wren(b)
        self._cmd(b, bytes([c.chip_erase_cmd]))
        self.dev.run(b)
        for _ in range(20):                     # up to ~20 minutes
            b = P.Batch()
            r = self._poll(b, 60000)
            self.dev.run(b)
            if progress:
                progress()
            if r.value.ok:
                return True
        raise FlashError("chip erase did not finish")

    def end_write(self) -> None:
        self.exit_4byte()


# ============================================================================
@dataclass
class Detection:
    nand: Optional[FlashDriver] = None
    spi: Optional[FlashDriver] = None
    nand_id: bytes = b""
    spi_id: bytes = b""
    messages: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "nand": self.nand.info() if self.nand else None,
            "spi": self.spi.info() if self.spi else None,
            "nand_id": self.nand_id.hex().upper(),
            "spi_id": self.spi_id.hex().upper(),
            "messages": self.messages,
        }


def _blank_id(idb: bytes) -> bool:
    return not idb or all(x == 0xFF for x in idb) or all(x == 0x00 for x in idb)


def detect_nand(dev: Device, chip_name: Optional[str] = None, use_rb: bool = True,
                det: Optional[Detection] = None) -> Optional[ParallelNand]:
    det = det or Detection()
    idb = ParallelNand.read_id_raw(dev)
    det.nand_id = idb
    if chip_name:
        chip = chipdb.nand_by_name(chip_name)
        if chip is None:
            raise FlashError("unknown NAND chip %r" % chip_name)
        return ParallelNand(dev, chip, use_rb=use_rb, id_bytes=idb)
    if _blank_id(idb):
        det.messages.append("parallel NAND: no chip (ID %s)" % idb.hex().upper())
        return None
    onfi = ParallelNand.read_onfi(dev)
    chip = chipdb.find_nand(idb)
    if onfi is not None:
        # The parameter page comes from the chip itself: trust its geometry.
        ochip = nand_chip_from_onfi(onfi, idb)
        if chip is not None:
            import dataclasses

            same = (chip.page_size, chip.spare_size, chip.block_size, chip.total_size) == \
                   (ochip.page_size, ochip.spare_size, ochip.block_size, ochip.total_size)
            ochip = dataclasses.replace(ochip, name=chip.name, voltage=chip.voltage,
                                        bb_mark_off=chip.bb_mark_off, source="nando+onfi")
            det.messages.append("parallel NAND: %s (database + ONFI%s, ID %s)" % (
                chip.name, "" if same else "; geometry taken from ONFI",
                idb[:5].hex().upper()))
        else:
            det.messages.append("parallel NAND: %s (ONFI parameter page, ID %s)"
                                % (ochip.name, idb[:5].hex().upper()))
        if not ochip.voltage:
            det.messages.append("note: supply voltage unknown - check the datasheet "
                                "(this programmer is 3.3V only)")
        return ParallelNand(dev, ochip, use_rb=use_rb, id_bytes=idb, onfi=onfi)
    if chip is not None:
        det.messages.append("parallel NAND: %s (database, ID %s)" % (chip.name, idb[:5].hex().upper()))
        return ParallelNand(dev, chip, use_rb=use_rb, id_bytes=idb, onfi=onfi)
    det.messages.append("parallel NAND: unknown chip ID %s (not in database, no ONFI); "
                        "use --chip to choose a database entry" % idb[:5].hex().upper())
    return None


def detect_spi(dev: Device, chip_name: Optional[str] = None, ecc: bool = False,
               det: Optional[Detection] = None) -> Optional[FlashDriver]:
    det = det or Detection()
    # Wake from deep power-down / reset, then identify.
    b = P.Batch()
    for cmd in (b"\xAB", b"\x66", b"\x99"):
        b.spi_cs(True)
        b.spi_write(cmd)
        b.spi_cs(False)
    b.delay_us(100)
    dev.run(b)
    jedec = SpiNor.read_jedec(dev, 5)
    b = P.Batch()
    b.spi_cs(True)
    b.spi_write(b"\x9F\x00")
    nand_id = b.spi_read(3)
    b.spi_cs(False)
    dev.run(b)
    det.spi_id = jedec

    if chip_name:
        for c in chipdb.spi_nor_chips():
            if c.name.lower() == chip_name.lower():
                return SpiNor(dev, c)
        for c in chipdb.spi_nand_chips():
            if c.name.lower() == chip_name.lower():
                d = SpiNand(dev, c, ecc=ecc)
                d.setup()
                return d
        raise FlashError("unknown SPI chip %r" % chip_name)

    if _blank_id(jedec) and _blank_id(nand_id.value):
        det.messages.append("SPI: no chip (ID %s)" % jedec.hex().upper())
        return None

    nor = chipdb.find_spi_nor(jedec)
    sfdp = SpiNor.probe_sfdp(dev)
    if nor is None:
        nand = chipdb.find_spi_nand(jedec, nand_id.value)
        if nand is not None and sfdp is None:
            det.messages.append("SPI NAND: %s (%s, ID %s)" % (
                nand.name, "Linux kernel table" if nand.source == "linux" else "database",
                nand.ids.hex().upper()))
            if not nand.verified:
                det.messages.append("note: %s is an unverified database entry" % nand.name)
            if nand.note:
                det.messages.append("note: %s" % nand.note)
            if not nand.voltage:
                det.messages.append("note: supply voltage of %s unknown - check the datasheet "
                                    "(this programmer is 3.3V only)" % nand.name)
            d = SpiNand(dev, nand, ecc=ecc)
            d.setup()
            return d
    if nor is not None:
        if sfdp is not None and nor.linear:
            nor = _apply_sfdp(nor, sfdp)
        det.messages.append("SPI NOR: %s (database%s, ID %s)" % (
            nor.name, " + SFDP" if sfdp else "", jedec[:3].hex().upper()))
        return SpiNor(dev, nor)
    if sfdp is not None:
        chip = _apply_sfdp(chipdb.generic_spi_nor(jedec, sfdp.size), sfdp)
        det.messages.append("SPI NOR: unknown ID %s, geometry from SFDP (%d KiB)"
                            % (jedec[:3].hex().upper(), sfdp.size >> 10))
        return SpiNor(dev, chip)
    guess = chipdb.generic_spi_nor(jedec)
    if guess is not None:
        det.messages.append("SPI NOR: unknown ID %s, size guessed from ID as %d KiB; "
                            "verify before writing or use --chip" % (jedec[:3].hex().upper(),
                                                                     guess.total_size >> 10))
        return SpiNor(dev, guess)
    det.messages.append("SPI: unknown chip ID %s / %s" % (jedec.hex().upper(),
                                                          nand_id.value.hex().upper()))
    return None


def _apply_sfdp(chip: chipdb.SpiNorChip, s) -> chipdb.SpiNorChip:
    import dataclasses

    erase = dict(s.erase_types) or dict(chip.erase_types)
    small = min(erase) if erase else chip.block_size
    return dataclasses.replace(
        chip, total_size=s.size, erase_types=erase, block_size=small,
        erase_cmd=erase.get(small, chip.erase_cmd),
        addr_bytes=4 if s.size > 16 * 1024 * 1024 else 3,
        page_size=s.page_size if s.page_size in (256, 512) else chip.page_size)


def detect(dev: Device, *, nand_chip: Optional[str] = None, spi_chip: Optional[str] = None,
           use_rb: bool = True, ecc: bool = False, want: str = "all") -> Detection:
    det = Detection()
    if want in ("all", "nand"):
        det.nand = detect_nand(dev, nand_chip, use_rb, det)
    if want in ("all", "spi"):
        det.spi = detect_spi(dev, spi_chip, ecc, det)
    return det


def set_spi_clock(dev: Device, mhz: float) -> float:
    """Program SPI_DIV for the closest rate not above ``mhz``; returns the rate."""
    div = 0
    while CLK_HZ / (2 * (div + 1)) > mhz * 1e6 and div < 255:
        div += 1
    b = P.Batch()
    b.set_reg(P.REG_SPI_DIV, div)
    dev.run(b)
    return CLK_HZ / (2 * (div + 1)) / 1e6
