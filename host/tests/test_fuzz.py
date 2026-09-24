"""Protocol robustness: random / truncated streams and a lossy link, on the emulator."""

import random

import pytest

from nsprog import fuzz
from nsprog import protocol as P
from nsprog.device import Device, ProtocolError
from nsprog.emulator import Engine, EmulatorLink, NandModel, SpiNorModel
from nsprog.flash import detect
from nsprog.link import Link, LinkError


def make():
    dev = Device(EmulatorLink(NandModel(blocks=16), SpiNorModel()))
    dev.open(negotiate=False)
    return dev


def sane(dev):
    """Normal operation works after a fuzz round."""
    fuzz.restore(dev)
    b = P.Batch()
    rs = [b.echo(v) for v in range(0, 256, 17)]
    dev.run(b)
    assert [r.value for r in rs] == list(range(0, 256, 17))
    det = detect(dev)
    assert det.nand is not None and det.spi is not None, det.messages


@pytest.mark.parametrize("seed", range(12))
def test_random_streams_then_resync(seed):
    rng = random.Random(seed)
    dev = make()
    for _ in range(2):
        dev.link.write(fuzz.stream(rng, ops=40, safe=False))
        dev.resync()
        sane(dev)


def test_error_flags_report_unknown_opcode_and_timeout_abort():
    dev = make()
    dev.link.write(bytes([fuzz.UNKNOWN[0]]) + bytes([P.NAND_WRITE, 0x10, 0x00, 1, 2, 3]))   # truncated
    dev.resync()                      # the drain outlasts the 100 ms abort; INFO carries the flags
    assert dev.info.flags & 1 and dev.info.flags & 2
    dev.resync()
    assert dev.info.flags == 0        # INFO clears them
    sane(dev)


def test_safe_streams_never_touch_a_chip():
    walker = Engine()
    forbidden = 0
    for seed in range(200):
        s = fuzz.stream(random.Random(seed), ops=60, safe=True, truncate=False)
        pos = 0
        while pos < len(s):
            op = s[pos]
            n = walker._need(op, s[pos:])
            assert n is not None and pos + n <= len(s)
            f = s[pos:pos + n]
            if op in (P.NAND_CE, P.SPI_CS) and f[1] & 1:
                forbidden += 1
            if op in (P.PIN_TEST, P.SPI_POLL, P.SET_BAUD):
                forbidden += 1
            if op == P.SET_REG and f[1] == P.REG_PIN_CTRL:
                forbidden += 1
            pos += n
    assert forbidden == 0


class LossyLink(Link):
    """Wraps a link and drops or duplicates bytes on the way back to the host."""

    name = "lossy"

    def __init__(self, inner, rng, p=0.002):
        self.inner, self.rng, self.p, self.faults = inner, rng, p, 0

    def write(self, data):
        self.inner.write(data)

    def read(self, n, timeout):
        got = bytearray(self.inner.read(n, timeout))
        out = bytearray()
        for b in got:
            r = self.rng.random()
            if r < self.p:
                self.faults += 1                   # dropped
                continue
            out.append(b)
            if r > 1 - self.p:
                self.faults += 1
                out.append(b)                      # duplicated
        return bytes(out)


class FastDevice(Device):
    def _deadline(self, seg):
        import time
        return time.monotonic() + 0.3


def test_lossy_link_errors_are_detected_and_recovered():
    rng = random.Random(3)
    inner = EmulatorLink(NandModel(blocks=8), SpiNorModel())
    link = LossyLink(inner, rng)
    dev = FastDevice(link)
    link.p = 0
    dev.open(negotiate=False)
    link.p = 0.002
    ok = failed = 0
    for _ in range(60):
        b = P.Batch()
        vals = [rng.randrange(256) for _ in range(300)]
        rs = [b.echo(v) for v in vals]
        before = link.faults
        try:
            dev.run(b)
        except (ProtocolError, LinkError):
            failed += 1
            link.p, p = 0, link.p               # a clean line to get back in step
            dev.resync()
            link.p = p
            continue
        # a batch that "succeeds" must be correct unless a fault hit only the final marker window
        if link.faults == before:
            assert [r.value for r in rs] == vals
            ok += 1
    assert failed > 0 and ok > 0
    link.p = 0
    sane(dev)
