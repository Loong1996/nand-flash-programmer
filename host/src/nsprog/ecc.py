"""NAND ECC: Hamming (Linux software ECC) and binary BCH (Linux lib/bch).

Both follow the conventions of the Linux MTD software ECC engines so that
images produced here can be read by a Linux system using ``nand-ecc-mode =
"soft"`` with the matching algorithm, and vice versa:

* Hamming: 3 ECC bytes per 256 or 512 data bytes, inverted parity, the
  default (non-SmartMedia) byte order.
* BCH: generator polynomial over GF(2^m) with the Linux default primitive
  polynomials, data processed MSB first, ECC XOR-ed with the ECC of an
  erased (all 0xFF) sector so that erased pages carry all-0xFF ECC.

Compatibility with a *specific* SoC controller is not implied; hardware ECC
engines use their own layouts and polynomials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Hamming (Linux drivers/mtd/nand/ecc-sw-hamming.c, non-SMC order)

_PARITY = bytes(bin(i).count("1") & 1 for i in range(256))


def hamming_calc(data: bytes) -> bytes:
    """ECC for a 256 or 512 byte step (3 bytes)."""
    n = len(data)
    if n not in (256, 512):
        raise ValueError("Hamming step must be 256 or 512 bytes")
    idx_bits = 8 if n == 256 else 9
    rp = [0] * (2 * idx_bits)          # rp[2k] = index bit k is 0, rp[2k+1] = bit k is 1
    col = 0                             # XOR of all bytes
    for i, b in enumerate(data):
        col ^= b
        if _PARITY[b]:
            for k in range(idx_bits):
                rp[2 * k + ((i >> k) & 1)] ^= 1
    cp = [
        _PARITY[col & 0x55], _PARITY[col & 0xAA],
        _PARITY[col & 0x33], _PARITY[col & 0xCC],
        _PARITY[col & 0x0F], _PARITY[col & 0xF0],
    ]
    c0 = sum(rp[i] << i for i in range(8))
    c1 = sum(rp[8 + i] << i for i in range(8))
    c2 = (cp[5] << 7) | (cp[4] << 6) | (cp[3] << 5) | (cp[2] << 4) | (cp[1] << 3) | (cp[0] << 2)
    if n == 512:
        c2 |= (rp[17] << 1) | rp[16]
        last = ~c2 & 0xFF
    else:
        last = (~c2 & 0xFC) | 0x03           # unused low bits read as 1
    # Linux default (non-SmartMedia) order: rp15..rp8 first, then rp7..rp0.
    return bytes([~c1 & 0xFF, ~c0 & 0xFF, last])


def hamming_correct(data: bytearray, stored: bytes) -> int:
    """Correct ``data`` in place. Returns bit flips fixed (0/1), or -1 if uncorrectable."""
    calc = hamming_calc(bytes(data))
    s1, s0, s2 = (stored[0] ^ calc[0], stored[1] ^ calc[1], stored[2] ^ calc[2])
    if not (s0 | s1 | s2):
        return 0
    n = len(data)
    syn = s0 | (s1 << 8) | (s2 << 16)
    ones = bin(syn).count("1")
    need = 12 if n == 512 else 11
    if ones == need:
        # every parity pair differs: single bit error in the data
        byte = 0
        for k in range(8):
            byte |= ((syn >> (2 * k + 1)) & 1) << k
        if n == 512:
            byte |= ((s2 >> 1) & 1) << 8
        bit = ((s2 >> 3) & 1) | (((s2 >> 5) & 1) << 1) | (((s2 >> 7) & 1) << 2)
        data[byte] ^= 1 << bit
        return 1
    if ones == 1:
        return 1                         # the ECC bytes themselves had one flipped bit
    return -1


# ============================================================================
# BCH over GF(2^m)

#: Linux lib/bch.c default primitive polynomials
PRIM_POLY = {5: 0x25, 6: 0x43, 7: 0x83, 8: 0x11D, 9: 0x211, 10: 0x409, 11: 0x805,
             12: 0x1053, 13: 0x201B, 14: 0x402B, 15: 0x8003}


class BCH:
    def __init__(self, m: int, t: int, step: int):
        if m not in PRIM_POLY:
            raise ValueError("unsupported m=%d" % m)
        if step * 8 + m * t > (1 << m) - 1:
            raise ValueError("step too large for m=%d" % m)
        self.m, self.t, self.step = m, t, step
        n = (1 << m) - 1
        self.n = n
        exp = [0] * (2 * n)
        log = [0] * (n + 1)
        x = 1
        for i in range(n):
            exp[i] = x
            log[x] = i
            x <<= 1
            if x & (1 << m):
                x ^= PRIM_POLY[m]
        for i in range(n, 2 * n):
            exp[i] = exp[i - n]
        self.exp, self.log = exp, log
        self.gen = self._generator()
        self.deg = self.gen.bit_length() - 1
        self.ecc_bytes = (self.deg + 7) // 8
        self._table = self._make_table()
        self.eccmask = bytes(b ^ 0xFF for b in self._encode_raw(b"\xff" * step))

    # GF helpers
    def _mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.exp[self.log[a] + self.log[b]]

    def _minpoly(self, i: int) -> int:
        """Minimal polynomial (over GF(2)) of alpha^i, as an int bit mask."""
        conj = []
        e = i % self.n
        while e not in conj:
            conj.append(e)
            e = (e * 2) % self.n
        poly = [1]                     # coefficients in GF(2^m), lowest first
        for c in conj:
            root = self.exp[c]
            new = [0] * (len(poly) + 1)
            for k, a in enumerate(poly):
                new[k + 1] ^= a
                new[k] ^= self._mul(a, root)
            poly = new
        out = 0
        for k, a in enumerate(poly):
            if a not in (0, 1):
                raise ArithmeticError("minimal polynomial not binary")
            out |= a << k
        return out

    def _generator(self) -> int:
        seen = set()
        g = 1
        for i in range(1, 2 * self.t + 1):
            mp = self._minpoly(i)
            if mp in seen:
                continue
            seen.add(mp)
            g = _gf2_mul(g, mp)
        return g

    def _make_table(self) -> List[int]:
        deg, g = self.deg, self.gen
        mask = (1 << deg) - 1
        table = []
        for b in range(256):
            r = b << (deg - 8) if deg >= 8 else b >> (8 - deg)
            for _ in range(8):
                r <<= 1
                if r >> deg:
                    r = (r ^ g) & ((1 << (deg + 1)) - 1)
            table.append(r & mask)
        return table

    def _remainder(self, data: bytes) -> int:
        deg = self.deg
        mask = (1 << deg) - 1
        table = self._table
        r = 0
        if deg >= 8:
            sh = deg - 8
            for byte in data:
                r = ((r << 8) & mask) ^ table[(r >> sh) ^ byte]
        else:
            for byte in data:
                for i in range(7, -1, -1):
                    fb = ((r >> (deg - 1)) & 1) ^ ((byte >> i) & 1)
                    r = (r << 1) & mask
                    if fb:
                        r ^= self.gen & mask
        return r

    def _encode_raw(self, data: bytes) -> bytes:
        r = self._remainder(data)
        pad = self.ecc_bytes * 8 - self.deg
        return (r << pad).to_bytes(self.ecc_bytes, "big")

    def encode(self, data: bytes) -> bytes:
        raw = self._encode_raw(data)
        return bytes(a ^ b for a, b in zip(raw, self.eccmask))

    def correct(self, data: bytearray, stored: bytes) -> int:
        """Correct ``data`` (and implicitly the ECC) in place.

        Returns the number of corrected bit flips, or -1 if uncorrectable.
        """
        recv = bytes(a ^ b for a, b in zip(stored, self.eccmask))
        pad = self.ecc_bytes * 8 - self.deg
        ecc_int = int.from_bytes(recv, "big") >> pad
        rem = self._remainder(bytes(data)) ^ ecc_int
        if rem == 0:
            return 0
        # Syndromes S_j = rem(alpha^j); the codeword polynomial is data*x^deg + ecc.
        syn = []
        for j in range(1, 2 * self.t + 1):
            s = 0
            r, k = rem, 0
            while r:
                if r & 1:
                    s ^= self.exp[(j * k) % self.n]
                r >>= 1
                k += 1
            syn.append(s)
        locator = self._berlekamp_massey(syn)
        nerr = len(locator) - 1
        if nerr == 0 or nerr > self.t:
            return -1
        total_bits = self.step * 8 + self.deg
        roots = []
        for pos in range(total_bits):
            # error at codeword degree ``pos`` <=> locator(alpha^-pos) == 0
            xinv = self.exp[(self.n - pos) % self.n]
            v, xp = 0, 1
            for c in locator:
                v ^= self._mul(c, xp)
                xp = self._mul(xp, xinv)
            if v == 0:
                roots.append(pos)
                if len(roots) == nerr:
                    break
        if len(roots) != nerr:
            return -1
        data_bits = self.step * 8
        for pos in roots:
            if pos >= self.deg:
                bit = data_bits - 1 - (pos - self.deg)      # MSB-first data bit index
                data[bit // 8] ^= 0x80 >> (bit % 8)
        return nerr

    def _berlekamp_massey(self, s: List[int]) -> List[int]:
        c = [1]
        b = [1]
        L, m, bb = 0, 1, 1
        for n_ in range(len(s)):
            d = s[n_]
            for i in range(1, L + 1):
                if i < len(c):
                    d ^= self._mul(c[i], s[n_ - i])
            if d == 0:
                m += 1
                continue
            coef = self._mul(d, self.exp[(self.n - self.log[bb]) % self.n])
            t_ = c[:]
            shifted = [0] * m + b
            if len(shifted) > len(c):
                c = c + [0] * (len(shifted) - len(c))
            for i, x in enumerate(shifted):
                c[i] ^= self._mul(coef, x)
            if 2 * L <= n_:
                L = n_ + 1 - L
                b, bb, m = t_, d, 1
            else:
                m += 1
        while len(c) > 1 and c[-1] == 0:
            c.pop()
        return c


def _gf2_mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        b >>= 1
    return r


def bch_m_for(step: int) -> int:
    """Linux picks m = fls(8 * eccsize)."""
    return (step * 8).bit_length()


# ============================================================================
# Layouts

@dataclass
class EccLayout:
    """Where ECC lives in a raw (page + OOB) page."""

    scheme: str                  # "hamming" | "bch" | "none"
    step: int = 512              # data bytes per ECC step
    strength: int = 1            # correctable bits per step (BCH t)
    ecc_bytes: int = 3           # ECC bytes per step
    ecc_offset: Optional[int] = None   # offset in OOB; None = at the end (Linux default)
    name: str = ""
    _bch: Optional[BCH] = field(default=None, repr=False)

    def engine(self):
        if self.scheme == "bch" and self._bch is None:
            self._bch = BCH(bch_m_for(self.step), self.strength, self.step)
            self.ecc_bytes = self._bch.ecc_bytes
        return self._bch

    def positions(self, page: int, oob: int) -> List[Tuple[int, int, int]]:
        """[(data offset, ecc offset in the raw page, ecc length)] per step."""
        self.engine()
        steps = page // self.step
        total = steps * self.ecc_bytes
        if total > oob:
            raise ValueError("%d ECC bytes do not fit into %d OOB bytes" % (total, oob))
        base = (oob - total) if self.ecc_offset is None else self.ecc_offset
        if base + total > oob:
            raise ValueError("ECC area exceeds the OOB")
        return [(i * self.step, page + base + i * self.ecc_bytes, self.ecc_bytes)
                for i in range(steps)]

    def calc(self, data: bytes) -> bytes:
        if self.scheme == "hamming":
            return hamming_calc(data)
        return self.engine().encode(data)

    def correct(self, data: bytearray, stored: bytes) -> int:
        if self.scheme == "hamming":
            return hamming_correct(data, stored)
        return self.engine().correct(data, stored)

    def describe(self) -> str:
        if self.scheme == "hamming":
            return "Hamming, %d bytes/step, 3 ECC bytes" % self.step
        self.engine()
        return "BCH t=%d, %d bytes/step, %d ECC bytes" % (self.strength, self.step, self.ecc_bytes)


PRESETS: Dict[str, EccLayout] = {
    "hamming256": EccLayout("hamming", 256, 1, 3, name="hamming256"),
    "hamming512": EccLayout("hamming", 512, 1, 3, name="hamming512"),
    "bch4": EccLayout("bch", 512, 4, name="bch4"),
    "bch8": EccLayout("bch", 512, 8, name="bch8"),
    "bch16": EccLayout("bch", 1024, 16, name="bch16"),
}


def layout(spec: str, ecc_offset: Optional[int] = None) -> EccLayout:
    """``hamming256`` | ``hamming512`` | ``bch4`` | ``bch8`` | ``bch16`` |
    ``bch:<t>:<step>`` | ``hamming:<step>``."""
    spec = spec.lower()
    if spec in PRESETS:
        p = PRESETS[spec]
        lay = EccLayout(p.scheme, p.step, p.strength, p.ecc_bytes, ecc_offset, p.name)
    elif spec.startswith("bch:"):
        _, t, step = spec.split(":")
        lay = EccLayout("bch", int(step), int(t), ecc_offset=ecc_offset, name=spec)
    elif spec.startswith("hamming:"):
        lay = EccLayout("hamming", int(spec.split(":")[1]), 1, 3, ecc_offset, spec)
    else:
        raise ValueError("unknown ECC layout %r" % spec)
    lay.engine()
    return lay
