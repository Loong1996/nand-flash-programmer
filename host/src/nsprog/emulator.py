"""Software emulator of the FPGA engine with behavioural flash models.

Used by the test-suite and for trying the CLI / web UI without hardware
(``--port emu`` or ``--port emu:spinand``). Time is virtual (microseconds).
"""

from __future__ import annotations

import struct
from typing import Dict, Optional, Union

from . import protocol as P
from .link import Link


# ---------------------------------------------------------------------------
# ONFI helpers shared with the RTL test models
def onfi_crc16(data: bytes) -> int:
    crc = 0x4F4E
    for byte in data:
        for i in range(7, -1, -1):
            bit = ((crc >> 15) ^ (byte >> i)) & 1
            crc = (crc << 1) & 0xFFFF
            if bit:
                crc ^= 0x8005
    return crc


def build_onfi_param_page(*, page: int, spare: int, ppb: int, blocks: int,
                          row_cycles: int = 3, col_cycles: int = 2,
                          manufacturer: str = "NSPROG", model: str = "EMU-NAND",
                          jedec_id: int = 0xEF, t_prog_us: int = 700,
                          t_bers_us: int = 10000, t_r_us: int = 25) -> bytes:
    pp = bytearray(256)
    pp[0:4] = b"ONFI"
    struct.pack_into("<H", pp, 4, 0x0002)            # ONFI 1.0
    pp[32:44] = manufacturer.ljust(12)[:12].encode()
    pp[44:64] = model.ljust(20)[:20].encode()
    pp[64] = jedec_id
    struct.pack_into("<I", pp, 80, page)
    struct.pack_into("<H", pp, 84, spare)
    struct.pack_into("<I", pp, 86, page // 4)
    struct.pack_into("<H", pp, 90, spare // 4)
    struct.pack_into("<I", pp, 92, ppb)
    struct.pack_into("<I", pp, 96, blocks)
    pp[100] = 1                                        # LUNs
    pp[101] = (col_cycles << 4) | row_cycles
    pp[102] = 1                                        # bits per cell
    struct.pack_into("<H", pp, 103, max(1, blocks // 50))
    pp[112] = 1                                        # ECC bits required
    struct.pack_into("<H", pp, 129, 0x0001)            # timing mode 0
    struct.pack_into("<H", pp, 133, t_prog_us)
    struct.pack_into("<H", pp, 135, t_bers_us)
    struct.pack_into("<H", pp, 137, t_r_us)
    struct.pack_into("<H", pp, 254, onfi_crc16(bytes(pp[:254])))
    return bytes(pp)


# ---------------------------------------------------------------------------
class NandModel:
    """8-bit asynchronous SLC NAND (large or small page)."""

    def __init__(self, *, page=2048, spare=64, ppb=64, blocks=64,
                 ids=b"\x2C\xF1\x80\x95\x04", onfi=True, bad_blocks=(3,),
                 row_cycles=3, col_cycles=2, small_page=False,
                 t_r=25, t_prog=300, t_bers=2000):
        self.page, self.spare, self.ppb, self.blocks = page, spare, ppb, blocks
        self.ids = bytes(ids)
        self.onfi = onfi
        self.row_cycles, self.col_cycles = row_cycles, col_cycles
        self.small_page = small_page
        self.t_r, self.t_prog, self.t_bers = t_r, t_prog, t_bers
        self.param = build_onfi_param_page(page=page, spare=spare, ppb=ppb, blocks=blocks,
                                           row_cycles=row_cycles, col_cycles=col_cycles)
        self.pages: Dict[int, bytearray] = {}
        for b in bad_blocks:
            pg = bytearray(b"\xff" * self.pb)
            pg[page] = 0x00
            self.pages[b * ppb] = pg
        self.busy_until = 0.0
        self.fail = False
        self.mode = "idle"
        self.addr = []
        self.col = 0
        self.row = 0
        self.reg = bytearray(b"\xff" * self.pb)
        self.out = b""
        self.wp_high = False

    @property
    def pb(self) -> int:
        return self.page + self.spare

    def page_data(self, row: int) -> bytearray:
        return self.pages.get(row, bytearray(b"\xff" * self.pb))

    def ready(self, now: float) -> bool:
        return now >= self.busy_until

    def _status(self, now: float) -> int:
        s = 0
        if self.ready(now):
            s |= 0x60
        if self.fail:
            s |= 0x01
        if self.wp_high:
            s |= 0x80
        return s

    def _split(self):
        col = 0
        for i in range(self.col_cycles):
            col |= self.addr[i] << (8 * i)
        row = 0
        for i in range(self.row_cycles):
            row |= self.addr[self.col_cycles + i] << (8 * i)
        return col, row

    def command(self, c: int, now: float) -> None:
        if not self.ready(now) and c not in (0x70, 0xFF):
            return
        if c == 0xFF:
            self.mode, self.fail = "idle", False
            self.busy_until = now + 5
        elif c == 0x90:
            self.mode = "id_addr"
        elif c == 0xEC:
            self.mode = "param_addr"
        elif c in (0x00, 0x01, 0x50):
            # 00h right after a status read (no address) resumes data output.
            self.resume = self.mode == "status" and getattr(self, "prev_mode", "") == "data"
            self.mode, self.addr = "read_addr", []
            self.small_area = c
        elif c == 0x30 and self.mode == "read_addr":
            self._load(now)
        elif c == 0x05:
            self.mode, self.addr = "rnd_addr", []
        elif c == 0xE0 and self.mode == "rnd_addr":
            self.col = self.addr[0] | (self.addr[1] << 8 if len(self.addr) > 1 else 0)
            self.mode = "data"
        elif c == 0x80:
            self.mode, self.addr = "prog_addr", []
            self.reg = bytearray(b"\xff" * self.pb)
        elif c == 0x85:
            self.mode, self.addr = "prog_rnd_addr", []
        elif c == 0x10 and self.mode in ("prog_data", "prog_addr"):
            self.fail = False
            if self.wp_high and self.row < self.ppb * self.blocks:
                cur = self.page_data(self.row)
                self.pages[self.row] = bytearray(a & b for a, b in zip(cur, self.reg))
            self.busy_until = now + self.t_prog
            self.mode = "idle"
        elif c == 0x60:
            self.mode, self.addr = "erase_addr", []
        elif c == 0xD0 and self.mode == "erase_addr":
            row = 0
            for i, a in enumerate(self.addr[:self.row_cycles]):
                row |= a << (8 * i)
            self.fail = False
            if self.wp_high:
                blk = row // self.ppb
                for r in range(blk * self.ppb, (blk + 1) * self.ppb):
                    self.pages.pop(r, None)
            self.busy_until = now + self.t_bers
            self.mode = "idle"
        elif c == 0x70:
            self.prev_mode = self.mode
            self.mode = "status"

    def _load(self, now: float) -> None:
        col, row = self._split()
        if self.small_page:
            col += {0x00: 0, 0x01: 256, 0x50: self.page}.get(getattr(self, "small_area", 0), 0)
        self.col, self.row = col, row
        self.reg = bytearray(self.page_data(row))
        self.busy_until = now + self.t_r
        self.mode = "data"

    def address(self, a: int, now: float) -> None:
        if self.mode == "id_addr":
            self.out = (self.ids + self.ids) if a == 0x00 else (b"ONFI" if self.onfi and a == 0x20 else b"")
            self.col, self.mode = 0, "id"
        elif self.mode == "param_addr":
            self.out = self.param * 3 if self.onfi else b""
            self.col, self.mode = 0, "param"
            self.busy_until = now + self.t_r
        elif self.mode in ("read_addr", "rnd_addr", "erase_addr"):
            self.addr.append(a)
            if (self.mode == "read_addr" and self.small_page and
                    len(self.addr) == self.col_cycles + self.row_cycles):
                self._load(now)
        elif self.mode in ("prog_addr", "prog_rnd_addr"):
            self.addr.append(a)
            need = self.col_cycles + (self.row_cycles if self.mode == "prog_addr" else 0)
            if len(self.addr) == need:
                if self.mode == "prog_addr":
                    self.col, self.row = self._split()
                else:
                    self.col = self.addr[0] | (self.addr[1] << 8 if len(self.addr) > 1 else 0)
                self.mode = "prog_data"

    def write(self, b: int, now: float) -> None:
        if self.mode == "prog_data" and self.col < self.pb:
            self.reg[self.col] = b
            self.col += 1

    def read(self, now: float) -> int:
        if self.mode == "status":
            return self._status(now)
        if self.mode == "read_addr" and not self.addr and getattr(self, "resume", False):
            self.mode = "data"
        if not self.ready(now):
            return 0xFF
        if self.mode in ("id", "param"):
            v = self.out[self.col] if self.col < len(self.out) else 0xFF
            self.col += 1
            return v
        if self.mode == "data":
            v = self.reg[self.col] if self.col < self.pb else 0xFF
            self.col += 1
            return v
        return 0xFF


# ---------------------------------------------------------------------------
def build_sfdp(size_bytes: int, four_byte: bool = False) -> bytes:
    sfdp = bytearray(b"\xff" * 0x100)
    sfdp[0:4] = b"SFDP"
    sfdp[4], sfdp[5], sfdp[6], sfdp[7] = 0x06, 0x01, 0x00, 0xFF   # rev 1.6, 1 header
    # parameter header 0: BFPT, rev 1.6, 16 dwords at 0x30
    sfdp[8:16] = bytes([0x00, 0x06, 0x01, 16, 0x30, 0x00, 0x00, 0xFF])
    dw = [0xFFFFFFFF] * 16
    addr_bits = 0b10 if four_byte else 0b00
    dw[0] = 0xFF00_0000 | (1 << 22) | (0x20 << 8) | (addr_bits << 17) | 0b01 | 0xE0
    dw[1] = size_bytes * 8 - 1
    dw[2] = 0x6B08_FFFF                                         # 1-1-4 read 6Bh, 8 dummy clocks
    dw[14] = 0xFFDF_FFFF                                        # QER 101b: QE = SR2 bit 1
    dw[7] = (0x52 << 24) | (15 << 16) | (0x20 << 8) | 12      # 4K 0x20, 32K 0x52
    dw[8] = 0x0000_0000 | (0xD8 << 8) | 16                      # 64K 0xD8
    dw[10] = 0xFFFF_FF00 | 0x81                                 # page size 256 (2^8 << 4)
    for i, v in enumerate(dw):
        struct.pack_into("<I", sfdp, 0x30 + 4 * i, v)
    return bytes(sfdp)


class SpiNorModel:
    def __init__(self, *, size=2 * 1024 * 1024, jedec=b"\xEF\x40\x15", sfdp=True,
                 protected=False, t_pp=700, t_se=45000, t_be=150000, t_ce=None):
        self.size = size
        self.jedec = bytes(jedec)
        self.sfdp = build_sfdp(size, size > 16 * 1024 * 1024) if sfdp else b"\xff" * 256
        self.mem = bytearray(b"\xff" * size)
        self.sr1 = 0x1C if protected else 0x00
        self.sr2 = 0x02
        self.wel = False
        self.four = False
        self.busy_until = 0.0
        self.t_pp, self.t_se, self.t_be = t_pp, t_se, t_be
        self.t_ce = t_ce or 20.0 * size / 1024
        self.sel = False
        self.buf = []

    def ready(self, now):
        return now >= self.busy_until

    def select(self, now):
        self.sel, self.buf = True, []

    def _alen(self):
        return 4 if self.four else 3

    def _addr(self):
        a = 0
        for x in self.buf[1:1 + self._alen()]:
            a = (a << 8) | x
        return a % self.size

    def xfer(self, b, now):
        if not self.sel:
            return 0xFF
        self.buf.append(b)
        cmd = self.buf[0]
        n = len(self.buf) - 1           # bytes after the opcode (this one included)
        busy = not self.ready(now)
        if cmd == 0x05:
            return (self.sr1 | (0x02 if self.wel else 0) | (0x01 if busy else 0)) if n else 0xFF
        if cmd == 0x35:
            return self.sr2 if n else 0xFF
        if busy:
            return 0xFF
        if cmd == 0x9F:
            return self.jedec[(n - 1) % 3] if n else 0xFF
        if cmd in (0x03, 0x0B, 0x5A, 0x6B):
            if cmd == 0x6B and not self.sr2 & 0x02:
                return 0xFF                    # QE clear: IO2/IO3 are WP#/HOLD#
            alen = 3 if cmd == 0x5A else self._alen()
            dummy = 0 if cmd == 0x03 else 1
            k = n - alen - dummy
            if k >= 1:
                a = 0
                for x in self.buf[1:1 + alen]:
                    a = (a << 8) | x
                if cmd == 0x5A:
                    return self.sfdp[(a + k - 1) % len(self.sfdp)]
                return self.mem[(a + k - 1) % self.size]
        return 0xFF

    def deselect(self, now):
        if not self.sel:
            return
        self.sel = False
        buf = self.buf
        if not buf or not self.ready(now):
            return
        cmd = buf[0]
        if cmd == 0x06:
            self.wel = True
        elif cmd == 0x04:
            self.wel = False
        elif cmd == 0xB7:
            self.four = True
        elif cmd == 0xE9:
            self.four = False
        elif cmd == 0x01 and self.wel and len(buf) >= 2:
            self.sr1 = buf[1] & 0xFC
            if len(buf) >= 3:
                self.sr2 = buf[2]
            self.wel = False
            self.busy_until = now + 5000
        elif cmd == 0x02 and self.wel and len(buf) > 1 + self._alen():
            a = self._addr()
            if not (self.sr1 & 0x1C):
                base = a & ~0xFF
                off = a & 0xFF
                for i, d in enumerate(buf[1 + self._alen():]):
                    p = base + ((off + i) & 0xFF)
                    self.mem[p] &= d
            self.wel = False
            self.busy_until = now + self.t_pp
        elif cmd in (0x20, 0x52, 0xD8) and self.wel and len(buf) >= 1 + self._alen():
            sz = {0x20: 4096, 0x52: 32768, 0xD8: 65536}[cmd]
            a = self._addr() & ~(sz - 1)
            if not (self.sr1 & 0x1C):
                self.mem[a:a + sz] = b"\xff" * sz
            self.wel = False
            self.busy_until = now + (self.t_se if sz == 4096 else self.t_be)
        elif cmd in (0xC7, 0x60) and self.wel:
            if not (self.sr1 & 0x1C):
                self.mem[:] = b"\xff" * self.size
            self.wel = False
            self.busy_until = now + self.t_ce


class SpiNandModel:
    """W25N01GV-style SPI NAND (buffer read mode)."""

    def __init__(self, *, ids=b"\xEF\xAA\x21", page=2048, spare=64, ppb=64, blocks=64,
                 bad_blocks=(5,), dummy_first=False, plane_bit=False,
                 t_rd=60, t_pp=250, t_be=2000):
        self.ids = bytes(ids)
        self.page, self.spare, self.ppb, self.blocks = page, spare, ppb, blocks
        self.dummy_first = dummy_first
        self.plane_bit = plane_bit
        self.pages: Dict[int, bytearray] = {}
        for b in bad_blocks:
            pg = bytearray(b"\xff" * self.pb)
            pg[page] = 0x00
            self.pages[b * ppb] = pg
        self.cache = bytearray(b"\xff" * self.pb)
        self.feat = {0xA0: 0x7C, 0xB0: 0x18, 0xC0: 0x00}
        self.busy_until = 0.0
        self.sel = False
        self.buf = []
        self.t_rd, self.t_pp, self.t_be = t_rd, t_pp, t_be

    @property
    def pb(self):
        return self.page + self.spare

    def ready(self, now):
        return now >= self.busy_until

    def select(self, now):
        self.sel, self.buf = True, []

    def _status(self, now):
        s = self.feat[0xC0] & ~0x01
        return s | (0 if self.ready(now) else 1)

    def xfer(self, b, now):
        if not self.sel:
            return 0xFF
        self.buf.append(b)
        cmd = self.buf[0]
        n = len(self.buf) - 1
        if cmd == 0x0F:
            if n >= 2:
                reg = self.buf[1]
                return self._status(now) if reg == 0xC0 else self.feat.get(reg, 0)
            return 0xFF
        if not self.ready(now):
            return 0xFF
        if cmd == 0x9F:
            return self.ids[(n - 2) % len(self.ids)] if n >= 2 else 0xFF
        if cmd in (0x03, 0x0B):
            hdr = 3
            if n > hdr:
                if self.dummy_first:
                    col = (self.buf[2] << 8) | self.buf[3]
                else:
                    col = (self.buf[1] << 8) | self.buf[2]
                col &= 0x0FFF
                k = n - hdr - 1
                return self.cache[(col + k) % self.pb]
        return 0xFF

    def deselect(self, now):
        if not self.sel:
            return
        self.sel = False
        buf = self.buf
        if not buf:
            return
        cmd = buf[0]
        if cmd == 0x1F and len(buf) >= 3:
            if buf[1] in (0xA0, 0xB0):
                self.feat[buf[1]] = buf[2]
            return
        if not self.ready(now):
            return
        st = self.feat[0xC0]
        if cmd == 0xFF:
            self.feat[0xC0] = 0
            self.busy_until = now + 5
        elif cmd == 0x06:
            self.feat[0xC0] = st | 0x02
        elif cmd == 0x04:
            self.feat[0xC0] = st & ~0x02
        elif cmd == 0x13 and len(buf) >= 4:
            row = (buf[1] << 16) | (buf[2] << 8) | buf[3]
            self.cache = bytearray(self.pages.get(row, b"\xff" * self.pb))
            self.busy_until = now + self.t_rd
        elif cmd in (0x02, 0x84) and len(buf) >= 3:
            if cmd == 0x02:
                self.cache = bytearray(b"\xff" * self.pb)
            col = ((buf[1] << 8) | buf[2]) & 0x0FFF
            for i, d in enumerate(buf[3:]):
                if col + i < self.pb:
                    self.cache[col + i] = d
        elif cmd == 0x10 and len(buf) >= 4:
            row = (buf[1] << 16) | (buf[2] << 8) | buf[3]
            st &= ~0x08
            if st & 0x02 and not (self.feat[0xA0] & 0x38) and row < self.ppb * self.blocks:
                cur = self.pages.get(row, bytearray(b"\xff" * self.pb))
                self.pages[row] = bytearray(a & c for a, c in zip(cur, self.cache))
            else:
                st |= 0x08
            self.feat[0xC0] = st & ~0x02
            self.busy_until = now + self.t_pp
        elif cmd == 0xD8 and len(buf) >= 4:
            row = (buf[1] << 16) | (buf[2] << 8) | buf[3]
            st &= ~0x04
            if st & 0x02 and not (self.feat[0xA0] & 0x38):
                blk = row // self.ppb
                for r in range(blk * self.ppb, (blk + 1) * self.ppb):
                    self.pages.pop(r, None)
            else:
                st |= 0x04
            self.feat[0xC0] = st & ~0x02
            self.busy_until = now + self.t_be


# ---------------------------------------------------------------------------
class Engine:
    """Byte-accurate interpreter of the NSP v1 protocol."""

    ARGS = {P.ECHO: 1, P.PIN_TEST: 2, P.SET_REG: 3, P.DELAY_US: 2, P.SET_BAUD: 2, P.NAND_CE: 1,
            P.NAND_CMD: 1, P.NAND_ADDR: 1, P.NAND_WRITE: 2, P.NAND_READ: 2,
            P.NAND_WAIT_RB: 2, P.NAND_POLL_STATUS: 4, P.SPI_CS: 1, P.SPI_WRITE: 2,
            P.SPI_READ: 2, P.SPI_XFER: 2, P.SPI_POLL: 1, P.SPI_READ4: 2}
    KNOWN = set(ARGS) | {P.NOP, P.INFO, P.GET_PINS}

    def __init__(self, nand: Optional[NandModel] = None, spi=None):
        self.nand = nand
        self.spi = spi
        self.inbuf = bytearray()
        self.out = bytearray()
        self.now = 0.0
        self.ce = False
        self.cs = False
        self.pin_ctrl = P.PIN_CTRL_DEFAULT
        self.flags = 0
        self.regs: Dict[int, int] = {}
        # pin test: mode, selected pin, and injectable wiring faults
        self.pt_mode = 0
        self.pt_sel = 0
        self.shorts: list = []          # [(test_bit_a, test_bit_b), ...]
        self.stuck: dict = {}           # {test_bit: level}  (short to GND / 3V3)
        self.probe: dict = {}           # {test_bit: level}  (weak: a finger on the pin)

    def pin_levels(self) -> int:
        """Pad levels of the 21 test pins in the current pin-test mode."""
        from . import wiring

        n = wiring.N_TEST
        parent = list(range(n))

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for a, b in self.shorts:
            parent[root(a)] = root(b)
        nets: dict = {}
        for i in range(n):
            nets.setdefault(root(i), []).append(i)
        drive = None
        if self.pt_mode in (P.PT_LOW, P.PT_HIGH, P.PT_TOGGLE):
            if self.pt_mode == P.PT_TOGGLE:
                val = int(self.now // 250_000) % 2
            else:
                val = 1 if self.pt_mode == P.PT_HIGH else 0
            drive = (self.pt_sel, val)
        idle = wiring.idle_levels()
        out = 0
        for members in nets.values():
            lv = None
            for m in members:                       # strongest first: a hard short
                if m in self.stuck:
                    lv = self.stuck[m]
            if lv is None and drive and drive[0] in members:
                lv = drive[1]
            if lv is None:
                for m in members:
                    if m in self.probe:
                        lv = self.probe[m]
            if lv is None:
                lv = (idle >> members[0]) & 1
            if lv:
                for m in members:
                    out |= 1 << m
        return out

    # -- bus helpers
    def _tick(self, us=0.2):
        self.now += us

    def _bus_nand(self) -> "Optional[NandModel]":
        """The NAND model when it is selected and the bus is not parked."""
        return self.nand if self._nand_on() else None

    def _nand_on(self):
        return self.nand is not None and self.ce and not (self.pin_ctrl & P.PIN_NAND_PARK)

    def _spi(self, b):
        self._tick(0.3)
        if self.spi is None or not self.cs:
            return 0xFF
        return self.spi.xfer(b, self.now)

    def _set_cs(self, on):
        if self.spi is not None:
            if on and not self.cs:
                self.spi.select(self.now)
            elif not on and self.cs:
                self.spi.deselect(self.now)
        self.cs = on

    # -- parser
    def feed(self, data: bytes) -> None:
        self.inbuf += data
        while self._step():
            pass

    def _need(self, op: int, avail: bytes) -> Optional[int]:
        """Total length of the op at the head of the buffer, or None if unknown yet."""
        if op not in self.KNOWN:
            return 1
        n = 1 + self.ARGS.get(op, 0)
        if op == P.NAND_ADDR:
            if len(avail) < 2:
                return None
            n = 2 + avail[1]
        elif op == P.SPI_POLL:
            if len(avail) < 2:
                return None
            n = 2 + avail[1] + 4
        elif op in (P.NAND_WRITE, P.SPI_WRITE, P.SPI_XFER):
            if len(avail) < 3:
                return None
            n = 3 + struct.unpack_from("<H", avail, 1)[0]
        return n

    def _step(self) -> bool:
        if not self.inbuf:
            return False
        op = self.inbuf[0]
        n = self._need(op, self.inbuf)
        if n is None or len(self.inbuf) < n:
            return False
        frame = bytes(self.inbuf[:n])
        del self.inbuf[:n]
        self._exec(frame)
        return True

    def _exec(self, f: bytes) -> None:
        op = f[0]

        def u16(i: int) -> int:
            return struct.unpack_from("<H", f, i)[0]
        if op not in self.KNOWN:
            self.flags |= 1
        elif op == P.ECHO:
            self.out.append(f[1])
        elif op == P.INFO:
            self.out += P.INFO_MAGIC + bytes([1, 1, 2, 1]) + struct.pack("<I", 27_000_000) + \
                bytes([12, 0xFB, self.flags, 0])
            self.flags = 0
        elif op == P.SET_REG:
            self.regs[f[1]] = u16(2)
            if f[1] == P.REG_PIN_CTRL:
                self.pin_ctrl = f[2]
                if self.nand is not None:
                    self.nand.wp_high = bool(self.pin_ctrl & P.PIN_NAND_WP_HIGH)
        elif op == P.DELAY_US:
            self._tick(u16(1))
        elif op == P.SET_BAUD:
            self.out.append(P.BAUD_ACK)
        elif op == P.PIN_TEST:
            self.pt_sel = f[1] & 0x1F
            self.pt_mode = f[2] if f[2] <= P.PT_TOGGLE else 0
            self._tick(10)
            self.out += self.pin_levels().to_bytes(3, "little")
        elif op == P.GET_PINS:
            rb = self.nand.ready(self.now) if self.nand else True
            self.out += bytes([int(rb) | 2 | 0x30, 0xFF])
        elif op == P.NAND_CE:
            self.ce = bool(f[1] & 1)
        elif op == P.NAND_CMD:
            self._tick()
            nd = self._bus_nand()
            if nd:
                nd.command(f[1], self.now)
        elif op == P.NAND_ADDR:
            for a in f[2:]:
                self._tick()
                nd = self._bus_nand()
                if nd:
                    nd.address(a, self.now)
        elif op == P.NAND_WRITE:
            for b in f[3:]:
                self._tick()
                nd = self._bus_nand()
                if nd:
                    nd.write(b, self.now)
        elif op == P.NAND_READ:
            n = u16(1)
            for _ in range(n):
                self._tick()
                nd = self._bus_nand()
                self.out.append(nd.read(self.now) if nd else 0xFF)
        elif op == P.NAND_WAIT_RB:
            self.out.append(self._wait(lambda: self.nand is None or self.nand.ready(self.now), u16(1)))
        elif op == P.NAND_POLL_STATUS:
            mask, val, tmo = f[1], f[2], u16(3)
            nd = self._bus_nand()
            if nd:
                nd.command(0x70, self.now)
            st = [0xFF]

            def cond():
                nd = self._bus_nand()
                st[0] = nd.read(self.now) if nd else 0xFF
                return (st[0] & mask) == val
            r = self._wait(cond, tmo)
            self.out += bytes([r, st[0]])
        elif op == P.SPI_CS:
            self._set_cs(bool(f[1] & 1))
        elif op == P.SPI_WRITE:
            for b in f[3:]:
                self._spi(b)
        elif op in (P.SPI_READ, P.SPI_READ4):
            for _ in range(u16(1)):
                self.out.append(self._spi(0xFF))
        elif op == P.SPI_XFER:
            for b in f[3:]:
                self.out.append(self._spi(b))
        elif op == P.SPI_POLL:
            k = f[1]
            cmd, mask, val, tmo = f[2:2 + k], f[2 + k], f[3 + k], u16(4 + k)
            st = [0xFF]

            def cond():
                self._set_cs(True)
                for c in cmd:
                    self._spi(c)
                st[0] = self._spi(0xFF)
                self._set_cs(False)
                return (st[0] & mask) == val
            r = self._wait(cond, tmo)
            self.out += bytes([r, st[0]])

    def _wait(self, cond, tmo_ms: int) -> int:
        """Poll ``cond`` with virtual time; jump to the next busy edge."""
        deadline = self.now + tmo_ms * 1000.0
        while True:
            if cond():
                return 0
            if self.now >= deadline:
                return 1
            nxt = deadline
            for m in (self.nand, self.spi):
                if m is not None and m.busy_until > self.now:
                    nxt = min(nxt, m.busy_until)
            self.now = max(self.now + 1.0, nxt)


class EmulatorLink(Link):
    name = "emulator"
    flow_controlled = True
    bytes_per_sec = 50_000_000

    def __init__(self, nand=None, spi=None):
        self.engine = Engine(nand, spi)

    @classmethod
    def from_spec(cls, spec: str = "all") -> "EmulatorLink":
        spec = (spec or "all").lower()
        nand: Optional[NandModel] = None
        spi: Union[SpiNorModel, SpiNandModel, None] = None
        if spec in ("all", "nand", "all-spinand"):
            nand = NandModel(blocks=256)
        if spec in ("all", "spinor"):
            spi = SpiNorModel()
        if spec in ("spinand", "all-spinand"):
            spi = SpiNandModel(blocks=128)
        if nand is None and spi is None:
            raise ValueError("unknown emulator spec %r (all, nand, spinor, spinand, all-spinand)" % spec)
        return cls(nand, spi)

    def write(self, data: bytes) -> None:
        self.engine.feed(bytes(data))

    def read(self, n: int, timeout: float) -> bytes:
        out = self.engine.out
        got = bytes(out[:n])
        del out[:n]
        return got
