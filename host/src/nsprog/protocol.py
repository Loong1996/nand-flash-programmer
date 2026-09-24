"""NSP v1 wire protocol: opcodes, registers and a batch builder.

See docs/protocol.md. A :class:`Batch` is an ordered list of operations;
operations that return data hand back a :class:`Result` whose ``value`` is
filled in once the batch has been executed by :class:`nsprog.device.Device`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Opcodes
NOP = 0x00
ECHO = 0x01
INFO = 0x02
SET_REG = 0x03
DELAY_US = 0x04
SET_BAUD = 0x05
GET_PINS = 0x06
PIN_TEST = 0x07           # wiring diagnostics, gateware >= 1.2
NAND_CE = 0x10
NAND_CMD = 0x11
NAND_ADDR = 0x12
NAND_WRITE = 0x13
NAND_READ = 0x14
NAND_WAIT_RB = 0x15
NAND_POLL_STATUS = 0x16
SPI_CS = 0x20
SPI_WRITE = 0x21
SPI_READ = 0x22
SPI_XFER = 0x23
SPI_POLL = 0x24
SPI_READ4 = 0x25          # quad-input read (data phase of 6Bh), gateware >= 1.1
SPI_WIDE = 0x26           # dual / quad write or read (len:u16, flags), gateware >= 1.3

# INFO capability bits
CAP_NAND8 = 0x01
CAP_SPI = 0x02
CAP_SPI_WIDE = 0x04       # SPI_WIDE
CAP_FT245 = 0x08
CAP_UART = 0x10
CAP_QSPI = 0x20           # SPI_READ4
CAP_SYNC245 = 0x40        # FT232H 245 synchronous FIFO
CAP_PIN_TEST = 0x80       # PIN_TEST

# PIN_TEST modes
PT_OFF = 0                # normal operation
PT_RELEASE = 1            # all test pins released (read-only)
PT_LOW = 2                # selected pin driven low, others released
PT_HIGH = 3               # selected pin driven high, others released
PT_TOGGLE = 4             # selected pin toggles at 2 Hz, others released

# Registers
REG_T_SETUP = 0
REG_T_WP = 1
REG_T_WH = 2
REG_T_RP = 3
REG_T_REH = 4
REG_T_WHR = 5
REG_T_ADL = 6
REG_T_WB = 7
REG_SPI_DIV = 8
REG_PIN_CTRL = 9
REG_FT_PHASE = 10         # sync FIFO clock phase, 0..15 x 22.5 degrees (gateware >= 1.3)

# INFO flags
FLAG_BAD_OP = 0x01        # unknown opcode / register
FLAG_TIMEOUT = 0x02       # partly received operation dropped
FLAG_OVERRUN = 0x04       # UART receive overrun
FLAG_BAUD_REVERT = 0x08   # unconfirmed SET_BAUD undone
FLAG_PHASE_REVERT = 0x10  # unconfirmed REG_FT_PHASE undone

PIN_NAND_WP_HIGH = 1 << 0
PIN_SPI_IO2_HIGH = 1 << 1
PIN_SPI_IO3_HIGH = 1 << 2
PIN_NAND_PARK = 1 << 3
PIN_SPI_PARK = 1 << 4
PIN_SPI_SAMPLE_LATE = 1 << 5
PIN_CTRL_DEFAULT = PIN_SPI_IO2_HIGH | PIN_SPI_IO3_HIGH
#: register values after reset at 27 MHz (gateware < 1.3; docs/protocol.md section 3)
REG_DEFAULTS = {REG_T_SETUP: 2, REG_T_WP: 3, REG_T_WH: 2, REG_T_RP: 3, REG_T_REH: 2, REG_T_WHR: 6,
                REG_T_ADL: 8, REG_T_WB: 6, REG_SPI_DIV: 3, REG_PIN_CTRL: PIN_CTRL_DEFAULT}


def reg_defaults(clk_hz: int = 27_000_000) -> Dict[int, int]:
    """Register values after reset for an engine clock of ``clk_hz`` (cycle counts
    scale with the clock so the times stay the same)."""
    cm = max(1, (clk_hz + 13_500_000) // 27_000_000)
    regs = {r: v * cm for r, v in REG_DEFAULTS.items() if r <= REG_T_WB}
    regs[REG_SPI_DIV] = 4 * cm - 1
    regs[REG_PIN_CTRL] = PIN_CTRL_DEFAULT
    return regs


INFO_MAGIC = b"NSPG"
INFO_LEN = 16
MAX_CHUNK = 2048          # max data bytes carried by one write-type op
MAX_READ = 0xFFFF         # max bytes returned by one read-type op
BAUD_ACK = 0x55


class Result:
    """Placeholder for the data an operation returns."""

    __slots__ = ("parts", "decode", "_value", "_done")

    def __init__(self, decode: Optional[Callable[[bytes], object]] = None):
        self.parts: List[bytes] = []
        self.decode = decode
        self._value: object = None
        self._done = False

    def _finish(self) -> None:
        raw = b"".join(self.parts)
        self._value = self.decode(raw) if self.decode else raw
        self._done = True

    @property
    def value(self):
        if not self._done:
            raise RuntimeError("batch has not been executed yet")
        return self._value


@dataclass
class Op:
    data: bytes
    rlen: int = 0
    result: Optional[Result] = None
    last_part: bool = True       # finish the Result after this op
    wait_ms: int = 0             # worst-case time the op may block the engine


@dataclass
class PollResult:
    ok: bool
    status: int


def _poll_decode(raw: bytes) -> PollResult:
    return PollResult(ok=(raw[0] == 0), status=raw[1])


@dataclass
class Info:
    proto: int
    gw_version: str
    board: int
    clk_hz: int
    rx_fifo: int
    caps: int
    flags: int
    port: int

    @classmethod
    def parse(cls, raw: bytes) -> "Info":
        if len(raw) != INFO_LEN or raw[:4] != INFO_MAGIC:
            raise ValueError("bad INFO response: %r" % raw)
        return cls(
            proto=raw[4],
            gw_version="%d.%d" % (raw[5], raw[6]),
            board=raw[7],
            clk_hz=struct.unpack_from("<I", raw, 8)[0],
            rx_fifo=1 << raw[12],
            caps=raw[13],
            flags=raw[14],
            port=raw[15],
        )


@dataclass
class Pins:
    rb_ready: bool
    spi_do: bool
    port_ft: bool
    baud_pending: bool
    nand_io: int
    ft_oe_n: bool = True
    ft_siwu_n: bool = True
    ft_clkout_active: bool = False

    @classmethod
    def parse(cls, raw: bytes) -> "Pins":
        f = raw[0]
        return cls(bool(f & 1), bool(f & 2), bool(f & 4), bool(f & 8), raw[1],
                   bool(f & 0x10), bool(f & 0x20), bool(f & 0x40))


def _u16(v: int) -> bytes:
    if not 0 <= v <= 0xFFFF:
        raise ValueError("value out of u16 range: %d" % v)
    return struct.pack("<H", v)


@dataclass
class Batch:
    ops: List[Op] = field(default_factory=list)

    # ---------------------------------------------------------------- helpers
    def _op(self, data: bytes, rlen: int = 0, decode=None, wait_ms: int = 0) -> Optional[Result]:
        res = Result(decode) if rlen else None
        self.ops.append(Op(bytes(data), rlen, res, True, wait_ms))
        return res

    def _rop(self, data: bytes, rlen: int, decode=None, wait_ms: int = 0) -> Result:
        """Operation that returns ``rlen`` > 0 bytes."""
        res = Result(decode)
        self.ops.append(Op(bytes(data), rlen, res, True, wait_ms))
        return res

    def extend(self, other: "Batch") -> "Batch":
        self.ops.extend(other.ops)
        return self

    def __len__(self) -> int:
        return len(self.ops)

    @property
    def response_len(self) -> int:
        return sum(o.rlen for o in self.ops)

    # ---------------------------------------------------------------- misc
    def nop(self) -> None:
        self._op(bytes([NOP]))

    def echo(self, b: int) -> Result:
        return self._rop(bytes([ECHO, b & 0xFF]), 1, lambda r: r[0])

    def info(self) -> Result:
        return self._rop(bytes([INFO]), INFO_LEN, Info.parse)

    def set_reg(self, reg: int, value: int) -> None:
        self._op(bytes([SET_REG, reg]) + _u16(value))

    def delay_us(self, us: int) -> None:
        while us > 0:
            step = min(us, 0xFFFF)
            self._op(bytes([DELAY_US]) + _u16(step), wait_ms=step // 1000 + 1)
            us -= step

    def get_pins(self) -> Result:
        return self._rop(bytes([GET_PINS]), 2, Pins.parse)

    # ---------------------------------------------------------------- NAND
    def pin_test(self, pin: int, mode: int) -> Result:
        """Set the pin-test mode; returns the 21 pad levels (bit i = test pin i)
        sampled 10 us later."""
        if not 0 <= pin < 32 or not 0 <= mode <= PT_TOGGLE:
            raise ValueError("bad pin test arguments")
        return self._rop(bytes([PIN_TEST, pin, mode]), 3,
                        lambda raw: int.from_bytes(raw, "little"))

    def nand_ce(self, on: bool) -> None:
        self._op(bytes([NAND_CE, 1 if on else 0]))

    def nand_cmd(self, c: int) -> None:
        self._op(bytes([NAND_CMD, c & 0xFF]))

    def nand_addr(self, addr: bytes) -> None:
        addr = bytes(addr)
        for i in range(0, len(addr), 8):
            part = addr[i:i + 8]
            self._op(bytes([NAND_ADDR, len(part)]) + part)

    def nand_write(self, data: bytes) -> None:
        data = bytes(data)
        for i in range(0, len(data), MAX_CHUNK):
            part = data[i:i + MAX_CHUNK]
            self._op(bytes([NAND_WRITE]) + _u16(len(part)) + part)

    def nand_read(self, n: int) -> Result:
        return self._read_op(NAND_READ, n)

    def nand_wait_rb(self, timeout_ms: int) -> Result:
        return self._rop(bytes([NAND_WAIT_RB]) + _u16(timeout_ms), 1,
                        lambda r: r[0] == 0, wait_ms=timeout_ms)

    def nand_poll_status(self, mask: int, value: int, timeout_ms: int) -> Result:
        return self._rop(bytes([NAND_POLL_STATUS, mask, value]) + _u16(timeout_ms), 2,
                        _poll_decode, wait_ms=timeout_ms)

    # ---------------------------------------------------------------- SPI
    def spi_cs(self, on: bool) -> None:
        self._op(bytes([SPI_CS, 1 if on else 0]))

    def spi_write(self, data: bytes) -> None:
        data = bytes(data)
        for i in range(0, len(data), MAX_CHUNK):
            part = data[i:i + MAX_CHUNK]
            self._op(bytes([SPI_WRITE]) + _u16(len(part)) + part)

    def spi_read(self, n: int) -> Result:
        return self._read_op(SPI_READ, n)

    def spi_read4(self, n: int) -> Result:
        """Read ``n`` bytes on IO0-IO3 (4 bits per clock). IO0/IO2/IO3 stay
        released until the following ``spi_cs(False)``."""
        return self._read_op(SPI_READ4, n)

    #: SPI_WIDE flags: bus width code; bit 2 = read
    WIDE = {1: 0, 2: 1, 4: 2}

    def spi_wide_write(self, data: bytes, width: int) -> None:
        """Send ``data`` on 1, 2 (IO1..IO0) or 4 (IO3..IO0) lines."""
        data = bytes(data)
        for i in range(0, len(data), MAX_CHUNK):
            part = data[i:i + MAX_CHUNK]
            self._op(bytes([SPI_WIDE]) + _u16(len(part)) + bytes([self.WIDE[width]]) + part)

    def spi_wide_read(self, n: int, width: int) -> Result:
        """Read ``n`` bytes on 1, 2 or 4 lines; the lines stay released until ``spi_cs(False)``."""
        return self._read_op(SPI_WIDE, n, bytes([self.WIDE[width] | 4]))

    def spi_xfer(self, data: bytes) -> Result:
        data = bytes(data)
        res = Result()
        chunks = [data[i:i + MAX_CHUNK] for i in range(0, len(data), MAX_CHUNK)] or [b""]
        for i, part in enumerate(chunks):
            self.ops.append(Op(bytes([SPI_XFER]) + _u16(len(part)) + part, len(part), res,
                               i == len(chunks) - 1))
        return res

    def spi_poll(self, cmd: bytes, mask: int, value: int, timeout_ms: int) -> Result:
        cmd = bytes(cmd)
        if not 1 <= len(cmd) <= 4:
            raise ValueError("SPI_POLL command must be 1-4 bytes")
        return self._rop(bytes([SPI_POLL, len(cmd)]) + cmd + bytes([mask, value]) + _u16(timeout_ms),
                        2, _poll_decode, wait_ms=timeout_ms)

    # ---------------------------------------------------------------- internal
    def _read_op(self, opcode: int, n: int, suffix: bytes = b"") -> Result:
        res = Result()
        if n == 0:
            res._finish()
            return res
        remaining = n
        while remaining > 0:
            step = min(remaining, MAX_READ)
            remaining -= step
            self.ops.append(Op(bytes([opcode]) + _u16(step) + suffix, step, res, remaining == 0))
        return res
