"""Offline tools: ECC (checked against Linux reference output), image, UBI."""

import io
import os
import random

import pytest

from nsprog import image, ubi
from nsprog.cli import main as cli_main
from nsprog.ecc import BCH, hamming_calc, hamming_correct, layout

DATA = os.path.join(os.path.dirname(__file__), "data")


def lcg(n, seed):
    x, out = seed, bytearray()
    for _ in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((x >> 16) & 0xFF)
    return bytes(out)


# ------------------------------------------------------------------ Hamming
# Reference values produced by the Linux kernel's ecc_sw_hamming_calculate()
# (drivers/mtd/nand/ecc-sw-hamming.c, sm_order = false).
@pytest.mark.parametrize("n,seed,ref", [(256, 1, "c3ff03"), (256, 2, "f030ff"),
                                        (512, 3, "f303ff"), (512, 4, "5596aa")])
def test_hamming_matches_linux(n, seed, ref):
    assert hamming_calc(lcg(n, seed)).hex() == ref


def test_hamming_corrects_single_bit():
    r = random.Random(1)
    for n in (256, 512):
        data = lcg(n, 9)
        ecc = hamming_calc(data)
        for _ in range(50):
            pos = r.randrange(n * 8)
            buf = bytearray(data)
            buf[pos // 8] ^= 1 << (pos % 8)
            assert hamming_correct(buf, ecc) == 1 and bytes(buf) == data
        buf = bytearray(data)
        buf[0] ^= 1
        buf[5] ^= 4
        assert hamming_correct(buf, ecc) == -1


# ------------------------------------------------------------------ BCH
# Raw (unmasked) remainders produced by Linux lib/bch.c via bchlib, m=13.
def test_bch_matches_linux_lib_bch():
    data = bytes((i * 37 + 11) & 0xFF for i in range(512))
    assert BCH(13, 4, 512)._encode_raw(data).hex() == "133c4eb233b330"
    assert BCH(13, 8, 512)._encode_raw(data).hex() == "8c076650e26a1015b21c55b685"


def test_bch_erased_page_has_ff_ecc():
    for t in (4, 8):
        b = BCH(13, t, 512)
        assert b.encode(b"\xff" * 512) == b"\xff" * b.ecc_bytes


def test_bch_corrects_up_to_t():
    r = random.Random(2)
    b = BCH(13, 8, 512)
    for k in (1, 4, 8):
        data = lcg(512, k)
        ecc = b.encode(data)
        buf = bytearray(data)
        for p in r.sample(range(4096), k):
            buf[p // 8] ^= 0x80 >> (p % 8)
        assert b.correct(buf, ecc) == k and bytes(buf) == data
    buf = bytearray(lcg(512, 3))
    ecc = b.encode(bytes(buf))
    for p in r.sample(range(4096), 11):
        buf[p // 8] ^= 0x80 >> (p % 8)
    assert b.correct(buf, ecc) == -1


def test_bch_against_bchlib_if_available():
    bchlib = pytest.importorskip("bchlib")
    r = random.Random(3)
    for t in (4, 8, 16):
        ref = bchlib.BCH(t, prim_poly=0x201B)
        mine = BCH(13, t, 512)
        for _ in range(5):
            data = bytes(r.getrandbits(8) for _ in range(512))
            assert mine._encode_raw(data) == bytes(ref.encode(data))


# ------------------------------------------------------------------ image + ECC
GEO = image.Geometry(2048, 64, 4)


def build_raw(npages, ecc_spec):
    main = b"".join(lcg(2048, i + 1) for i in range(npages - 1)) + b"\xff" * 2048
    out = io.BytesIO()
    image.merge(io.BytesIO(main), None, out, GEO, layout(ecc_spec))
    return main, out.getvalue()


@pytest.mark.parametrize("spec", ["hamming256", "hamming512", "bch4", "bch8"])
def test_ecc_roundtrip_and_fix(spec):
    main, raw = build_raw(8, spec)
    rep = image.ecc_check(io.BytesIO(raw), GEO, layout(spec))
    assert rep.ok and rep.corrected_steps == 0 and rep.blank_steps == 2048 // layout(spec).step
    bad = bytearray(raw)
    bad[100] ^= 0x10                          # page 0, one flip
    bad[GEO.raw * 3 + 700] ^= 0x01            # page 3, one flip
    out = io.BytesIO()
    rep = image.ecc_check(io.BytesIO(bytes(bad)), GEO, layout(spec), out, strip=True)
    assert rep.ok and rep.corrected_bits == 2
    assert out.getvalue() == main


def test_wrong_layout_hint():
    _, raw = build_raw(4, "bch8")
    rep = image.ecc_check(io.BytesIO(raw), GEO, layout("hamming512"))
    assert not rep.ok and "does not match" in rep.summary()


def test_split_merge_strip_info():
    raw = bytearray(b"".join(lcg(2112, i) for i in range(8)))
    for pg in range(8):
        raw[pg * 2112 + 2048] = 0xFF
    raw[4 * 2112 + 2048] = 0x00               # block 1 (pages 4-7) marked bad
    main, oob = io.BytesIO(), io.BytesIO()
    assert image.split(io.BytesIO(bytes(raw)), main, oob, GEO) == 8
    again = io.BytesIO()
    main.seek(0)
    oob.seek(0)
    image.merge(main, oob, again, GEO)
    assert again.getvalue() == bytes(raw)
    inf = image.info(io.BytesIO(bytes(raw)), GEO)
    assert inf.pages == 8 and inf.blocks == 2 and inf.bad_blocks == [1]
    out = io.BytesIO()
    assert image.strip_oob(io.BytesIO(bytes(raw)), out, GEO, skip_bad=True) == 4
    assert out.getvalue() == main.getvalue()[:4 * 2048]


# ------------------------------------------------------------------ UBI (image made by ubinize)
def test_ubi_parse_and_extract(tmp_path):
    with open(os.path.join(DATA, "test.ubi"), "rb") as f:
        img = ubi.parse(f)
        assert img.peb_size == 16384 and img.leb_size == 15360
        names = {v.name: v for v in img.volumes.values()}
        assert set(names) == {"kernel", "rootfs"}
        assert names["kernel"].vol_type == "static" and names["rootfs"].vol_type == "dynamic"
        paths = ubi.extract(f, img, str(tmp_path))
    kernel = open(os.path.join(DATA, "ubi_kernel.bin"), "rb").read()
    rootfs = open(os.path.join(DATA, "ubi_rootfs.bin"), "rb").read()
    got = {os.path.basename(p): open(p, "rb").read() for p in paths}
    assert got["kernel.bin"] == kernel
    assert got["rootfs.bin"][:len(rootfs)] == rootfs
    assert ubi.detect_content(os.path.join(str(tmp_path), "rootfs.bin")) == "SquashFS"


def test_cli_tools(tmp_path, capsys):
    main, raw = build_raw(4, "bch8")
    src = tmp_path / "raw.bin"
    src.write_bytes(raw)
    assert cli_main(["ecc", "check", str(src), "--page", "2048", "--oob", "64", "--ppb", "4",
                     "--ecc", "bch8"]) == 0
    assert "0 uncorrectable" in capsys.readouterr().out
    dst = tmp_path / "main.bin"
    assert cli_main(["image", "strip", str(src), str(dst), "--page", "2048", "--oob", "64"]) == 0
    assert dst.read_bytes() == main
    assert cli_main(["ubi", "info", os.path.join(DATA, "test.ubi")]) == 0
    assert "rootfs" in capsys.readouterr().out
