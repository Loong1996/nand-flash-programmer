"""Protocol fuzzing: random, truncated and malformed command streams.

Used by the tests (emulator and RTL simulation) and by ``nsprog selftest
--fuzz`` on real hardware. The goal is not to exercise the flash chips but the
engine's framing: whatever arrives, the engine must never lock up, and the
host must always get back in step with :meth:`Device.resync`.

``safe=True`` (hardware) only generates operations that cannot touch a chip
in a socket: CE#/CS# are never asserted, WP# is never released, no PIN_TEST,
no SPI_POLL (it drives CS# by itself) and no raw random bytes.
"""

from __future__ import annotations

import random
import struct
from typing import List

from . import protocol as P

#: opcodes that are not defined (skipped as 1-byte NOPs, error flag bit 0)
UNKNOWN = [b for b in range(256) if b not in (
    P.NOP, P.ECHO, P.INFO, P.SET_REG, P.DELAY_US, P.SET_BAUD, P.GET_PINS, P.PIN_TEST,
    P.NAND_CE, P.NAND_CMD, P.NAND_ADDR, P.NAND_WRITE, P.NAND_READ, P.NAND_WAIT_RB,
    P.NAND_POLL_STATUS, P.SPI_CS, P.SPI_WRITE, P.SPI_READ, P.SPI_XFER, P.SPI_POLL, P.SPI_READ4,
    P.SPI_WIDE)]

#: opcodes whose arguments can make one operation take long (or change the UART rate);
#: raw garbage has them replaced by an unknown opcode to bound the run time
SLOW = {P.DELAY_US, P.SET_BAUD, P.NAND_WRITE, P.NAND_READ, P.NAND_WAIT_RB, P.NAND_POLL_STATUS,
        P.SPI_WRITE, P.SPI_READ, P.SPI_XFER, P.SPI_POLL, P.SPI_READ4, P.SPI_WIDE}


def _u16(v: int) -> bytes:
    return struct.pack("<H", v)


def random_op(rng: random.Random, safe: bool, max_len: int = 256) -> bytes:
    """One complete, valid operation with random (bounded) arguments."""
    ln = rng.randrange(0, max_len + 1)
    ms = rng.randrange(0, 3)                      # timeouts: at most 2 ms

    def rand(n: int) -> bytes:
        return bytes(rng.randrange(256) for _ in range(n))

    def nand_addr() -> bytes:
        n = rng.randrange(1, 9)
        return bytes([P.NAND_ADDR, n]) + rand(n)

    def spi_poll() -> bytes:
        n = rng.randrange(1, 5)
        return bytes([P.SPI_POLL, n]) + rand(n) + rand(2) + _u16(ms)
    choices = [
        lambda: bytes([P.NOP]),
        lambda: bytes([P.ECHO, rng.randrange(256)]),
        lambda: bytes([P.INFO]),
        lambda: bytes([P.GET_PINS]),
        lambda: bytes([P.DELAY_US]) + _u16(rng.randrange(0, 200)),
        lambda: bytes([P.NAND_CE, 0]),
        lambda: bytes([P.NAND_CMD, rng.randrange(256)]),
        nand_addr,
        lambda: bytes([P.NAND_WRITE]) + _u16(ln) + rand(ln),
        lambda: bytes([P.NAND_READ]) + _u16(ln),
        lambda: bytes([P.NAND_WAIT_RB]) + _u16(ms),
        lambda: bytes([P.NAND_POLL_STATUS, rng.randrange(256), rng.randrange(256)]) + _u16(ms),
        lambda: bytes([P.SPI_CS, 0]),
        lambda: bytes([P.SPI_WRITE]) + _u16(ln) + rand(ln),
        lambda: bytes([P.SPI_READ]) + _u16(ln),
        lambda: bytes([P.SPI_XFER]) + _u16(ln) + rand(ln),
        lambda: bytes([P.SPI_READ4]) + _u16(min(ln, 64)),
        lambda: bytes([P.SPI_WIDE]) + _u16(ln) + bytes([rng.randrange(3)]) + rand(ln),
        lambda: bytes([P.SPI_WIDE]) + _u16(ln) + bytes([4 | rng.randrange(3)]),
        lambda: bytes([P.SET_REG, rng.randrange(0, 9)]) + _u16(rng.randrange(0, 12)),   # timing regs only
    ]
    if not safe:
        choices += [
            lambda: bytes([P.NAND_CE, 1]),
            lambda: bytes([P.SPI_CS, 1]),
            lambda: bytes([P.SET_REG, P.REG_PIN_CTRL]) + _u16(rng.randrange(0, 64)),
            lambda: bytes([P.PIN_TEST, rng.randrange(0, 24), rng.randrange(0, 6)]),
            spi_poll,
        ]
    return rng.choice(choices)()


def garbage(rng: random.Random, n: int) -> bytes:
    """Random bytes with the slow opcodes replaced (simulation only)."""
    out = bytearray(rng.randrange(256) for _ in range(n))
    for i, b in enumerate(out):
        if b in SLOW:
            out[i] = rng.choice(UNKNOWN)
    return bytes(out)


def stream(rng: random.Random, ops: int = 40, safe: bool = True, truncate: bool = True,
           max_len: int = 256) -> bytes:
    """A mix of valid operations, unknown opcodes and (unless ``safe``) raw garbage,
    optionally ending in a truncated operation (the engine aborts it after 100 ms)."""
    parts: List[bytes] = []
    for _ in range(ops):
        r = rng.random()
        if r < 0.7:
            parts.append(random_op(rng, safe, max_len))
        elif r < 0.85 or safe:
            parts.append(bytes([rng.choice(UNKNOWN)]))
        else:
            parts.append(garbage(rng, rng.randrange(1, 32)))
    if truncate:
        op = random_op(rng, safe, max_len)
        while len(op) < 2:
            op = random_op(rng, safe, max_len)
        parts.append(op[:rng.randrange(1, len(op))])
    return b"".join(parts)


def restore(dev) -> None:
    """Put every register and pin back to the defaults after a fuzz round."""
    b = P.Batch()
    if dev.opened and dev.info.caps & P.CAP_PIN_TEST:
        b.pin_test(0, P.PT_OFF)
    b.nand_ce(False)
    b.spi_cs(False)
    for reg, val in P.reg_defaults(dev.info.clk_hz if dev.opened else 27_000_000).items():
        b.set_reg(reg, val)
    dev.run(b)
    dev.pin_ctrl = P.PIN_CTRL_DEFAULT
