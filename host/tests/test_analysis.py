"""Offline analysis tools: image diff, partition/header scan, file-system readers."""

import gzip
import io
import os
import random
import struct
import tarfile
import zlib

import pytest

from nsprog import fs
from nsprog.cli import main
from nsprog.diff import diff
from nsprog.fs import jffs2 as J
from nsprog.fs.lzo import LzoError
from nsprog.fs.lzo import decompress as lzo_decompress
from nsprog.image import Geometry
from nsprog.scan import parse_mtdparts, scan, scan_file

DATA = os.path.join(os.path.dirname(__file__), "data")


def fixture(name):
    with open(os.path.join(DATA, name), "rb") as f:
        raw = f.read()
    return gzip.decompress(raw) if name.endswith(".gz") else raw


def reference_tree():
    """Files / symlinks / dirs of the tree every file-system fixture was built from."""
    files, links, dirs = {}, {}, set()
    with tarfile.open(fileobj=io.BytesIO(fixture("root.tar.gz"))) as t:
        for m in t.getmembers():
            p = "/" + os.path.normpath(m.name).lstrip("./")
            if p in ("/", "/."):
                continue
            if m.isfile():
                files[p] = t.extractfile(m).read()
            elif m.issym():
                links[p] = m.linkname
            elif m.isdir():
                dirs.add(p)
    return files, links, dirs


# --------------------------------------------------------------------------- diff
def rnd(n, seed=1):
    r = random.Random(seed)
    return bytearray(r.getrandbits(8) for _ in range(n))


def test_diff_classifies_pages():
    geo = Geometry(2048, 64, 4)
    raw = geo.raw
    a = rnd(raw * 12)
    a[8 * raw:] = b"\xff" * (4 * raw)                      # last block blank
    b = bytearray(a)
    b[1 * raw + 10] ^= 0x10                                # one bit flip
    b[2 * raw:3 * raw] = rnd(raw, 9)                       # changed page
    b[3 * raw:4 * raw] = b"\xff" * raw                     # erased in B
    b[5 * raw + 2048 + 3] ^= 0xFF                          # OOB only
    b[9 * raw + 5] = 0x00                                  # programmed where A is blank
    rep = diff(io.BytesIO(bytes(a)), io.BytesIO(bytes(b)), geo)
    kinds = {d.page: d.kind for d in rep.diffs}
    assert kinds == {1: "bitflip", 2: "changed", 3: "erased", 5: "changed", 9: "erased"}
    assert rep.oob_only_pages == 1 and sorted(rep.blocks) == [0, 1, 2]
    assert rep.kinds == {"bitflip": 1, "erased": 2, "changed": 2}
    assert not rep.identical and "1 bit-flip" in rep.summary()
    same = diff(io.BytesIO(bytes(a)), io.BytesIO(bytes(a)), geo)
    assert same.identical and same.diff_pages == 0


def test_diff_bitflips_only_hint_and_sizes():
    geo = Geometry(512, 16, 32)
    a = rnd(528 * 64)
    b = bytearray(a)
    for p in (3, 40):
        b[p * 528 + 7] ^= 0x01
    rep = diff(io.BytesIO(bytes(a)), io.BytesIO(bytes(b) + b"\xff" * 528), geo)
    assert rep.kinds["bitflip"] == 2 and rep.pages == 65
    assert "isolated bit flips" in rep.hint()
    assert "sizes differ" in rep.summary()


def test_image_diff_cli(tmp_path, capsys):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    data = rnd(2112 * 64)
    a.write_bytes(bytes(data))
    data[100] ^= 1
    b.write_bytes(bytes(data))
    rc = main(["image", "diff", str(a), str(b), "--page", "2048", "--oob", "64",
               "--json", str(tmp_path / "d.json")])
    assert rc == 1 and "1 bit-flip" in capsys.readouterr().out
    assert main(["image", "diff", str(a), str(a), "--page", "2048", "--oob", "64"]) == 0


# --------------------------------------------------------------------------- scan
def uimage(name, payload, typ=2, comp=1):
    hdr = bytearray(struct.pack(">IIIIIIIBBBB", 0x27051956, 0, 0, len(payload), 0x80008000, 0x80008000,
                                zlib.crc32(payload), 5, 2, typ, comp))
    hdr += name.encode().ljust(32, b"\0")
    struct.pack_into(">I", hdr, 4, zlib.crc32(bytes(hdr)))
    return bytes(hdr) + payload


def uboot_env(text, size=0x2000, redundant=False):
    body = b"\0".join(line.encode() for line in text) + b"\0\0"
    body = body.ljust(size - (5 if redundant else 4), b"\0")
    crc = struct.pack("<I", zlib.crc32(body))
    return crc + (b"\x01" if redundant else b"") + body


def test_mtdparts_parser():
    p = parse_mtdparts("console=ttyS0 mtdparts=nand0:1M(boot)ro,512k@0x200000(env),0x40000(dtb),"
                       "-(rootfs);spi0:4M(x)")
    assert [(x.name, x.offset, x.size, x.read_only) for x in p] == [
        ("boot", 0, 1 << 20, True), ("env", 0x200000, 512 << 10, False),
        ("dtb", 0x280000, 0x40000, False), ("rootfs", 0x2C0000, None, False)]
    assert parse_mtdparts("no partitions here") == []


def test_scan_env_uimage_fdt_fit_and_filesystems():
    img = bytearray(b"\xff" * (4 << 20))
    env = uboot_env(["bootdelay=3", "bootargs=console=ttyS0 root=/dev/mtdblock3",
                     "mtdparts=mtdparts=nand0:256k(u-boot)ro,128k(env),128k(dtb),1M(kernel),-(rootfs)",
                     "bootcmd=bootm"], redundant=True)
    img[0x40000:0x40000 + len(env)] = env
    dtb = fixture("board.dtb")
    img[0x60000:0x60000 + len(dtb)] = dtb
    k = uimage("Linux-6.1", os.urandom(5000))
    img[0x80000:0x80000 + len(k)] = k
    fit = fixture("fit.itb")
    img[0x100000:0x100000 + len(fit)] = fit
    sq = fixture("sq_xz.img.gz")
    img[0x180000:0x180000 + len(sq)] = sq
    rep = scan(bytes(img))
    by = {f.kind: f for f in rep.findings}
    assert by["U-Boot env"].offset == 0x40000 and "redundant" in by["U-Boot env"].describe()
    assert rep.env["bootcmd"] == "bootm"
    assert by["uImage"].name == "Linux-6.1" and by["uImage"].details["data_crc"] == "ok"
    assert by["uImage"].details["type"] == "kernel" and by["uImage"].details["arch"] == "arm"
    assert by["DTB"].name == "nsprog test board"
    assert by["FIT"].name == "fixture FIT" and "kernel: kernel arm" in by["FIT"].details["contents"]
    assert by["SquashFS"].offset == 0x180000 and by["SquashFS"].details["comp"] == "xz"
    # the env mtdparts wins over the DTB partitions
    assert [p.name for p in rep.partitions] == ["u-boot", "env", "dtb", "kernel", "rootfs"]
    s = rep.summary()
    assert "kernel" in s and "-> uImage" in s and rep.table()["rows"] and rep.partition_table()["rows"]


def test_scan_dtb_partitions_and_bad_uimage_crc():
    img = bytearray(b"\0" * 0x20000)
    dtb = fixture("board.dtb")
    img[0x1000:0x1000 + len(dtb)] = dtb
    k = bytearray(uimage("broken", b"x" * 100))
    k[40] ^= 0xFF                                          # header CRC now wrong
    img[0x8000:0x8000 + len(k)] = k
    rep = scan(bytes(img))
    assert [f.kind for f in rep.findings] == ["DTB"]
    assert [(p.name, p.offset, p.size, p.read_only) for p in rep.partitions] == [
        ("u-boot", 0, 0x100000, True), ("env", 0x100000, 0x40000, False), ("ubi", 0x800000, 0x800000, False)]


def test_scan_file_strips_oob(tmp_path):
    geo = Geometry(2048, 64, 64)
    main_img = bytearray(b"\xff" * (2048 * 64 * 2))
    k = uimage("kern", b"k" * 3000)
    main_img[2048 * 64:2048 * 64 + len(k)] = k
    raw = bytearray()
    for p in range(len(main_img) // 2048):
        raw += main_img[p * 2048:(p + 1) * 2048] + b"\xff" * 64
    path = tmp_path / "raw.bin"
    path.write_bytes(bytes(raw))
    rep = scan_file(str(path), geo.page, geo.oob, geo.ppb)
    assert [(f.kind, f.offset) for f in rep.findings] == [("uImage", 2048 * 64)]
    assert main(["image", "scan", str(path), "--page", "2048", "--oob", "64"]) == 0


# --------------------------------------------------------------------------- file systems
@pytest.mark.parametrize("name", ["sq_gzip.img.gz", "sq_xz.img.gz", "sq_lzo.img.gz", "jffs2_le.img.gz",
                                  "jffs2_be_rtime.img.gz", "ubifs_lzo.img.gz", "ubifs_zlib.img.gz"])
def test_filesystem_matches_reference(name):
    files, links, dirs = reference_tree()
    f = fs.open_fs(fixture(name))
    got_files = {e.path: e.read() for e in f.entries() if e.kind == "file"}
    got_links = {e.path: e.target for e in f.entries() if e.kind == "symlink"}
    got_dirs = {e.path for e in f.entries() if e.kind == "dir"} - {"/"}
    assert got_files == files and got_links == links and got_dirs == dirs


def test_ubi_container_and_extract(tmp_path):
    files, links, _dirs = reference_tree()
    c = fs.open_fs(fixture("ubi.img.gz"))
    assert isinstance(c, fs.UbiContainer)
    assert c.info()["volumes"] == "rootfs (UBIFS)" and "blob" in c.info()["other volumes"]
    counts = c.extract(str(tmp_path / "out"))
    assert counts["files"] == len(files) and counts["errors"] == 0
    for p, data in files.items():
        assert (tmp_path / "out" / "rootfs" / p.lstrip("/")).read_bytes() == data
    link = tmp_path / "out" / "rootfs" / "etc" / "motd"
    assert os.readlink(link) == links["/etc/motd"] if hasattr(os, "readlink") and link.is_symlink() else True


def test_find_all_in_flash_dump_and_cli(tmp_path, capsys):
    img = bytearray(b"\xff" * (2 << 20))
    for off, name in ((0x20000, "sq_gzip.img.gz"), (0x60000, "jffs2_le.img.gz"), (0x100000, "ubi.img.gz")):
        d = fixture(name)
        img[off:off + len(d)] = d
    found = fs.find_all(bytes(img))
    assert [(f.offset, f.fs.kind) for f in found] == [(0x20000, "SquashFS"), (0x60000, "JFFS2"),
                                                     (0x100000, "UBI")]
    path = tmp_path / "dump.bin"
    path.write_bytes(bytes(img))
    assert main(["fs", "list", str(path)]) == 0
    out = capsys.readouterr().out
    assert "/usr/lib/deep/rand.bin" in out and "0x00060000-JFFS2" in out
    assert main(["fs", "extract", str(path), str(tmp_path / "x")]) == 0
    assert (tmp_path / "x" / "0x00100000-UBI" / "rootfs" / "etc" / "banner").exists()
    assert main(["fs", "list", str(path), "--offset", "0x20000"]) == 0


def test_open_fs_rejects_unknown():
    with pytest.raises(fs.FsError):
        fs.open_fs(b"\0" * 4096)


def test_extract_refuses_path_traversal(tmp_path):
    class Evil(fs.FileSystem):
        def entries(self):
            yield fs.Entry("/../../escape.txt", "file", 1, read=lambda: b"x")
            yield fs.Entry("/ok.txt", "file", 1, read=lambda: b"y")

    counts = Evil().extract(str(tmp_path / "out"))
    assert counts["skipped"] == 1 and counts["files"] == 1
    assert not (tmp_path / "escape.txt").exists() and (tmp_path / "out" / "ok.txt").read_bytes() == b"y"


def test_lzo_small_streams():
    assert lzo_decompress(b"\x14abc\x11\x00\x00") == b"abc"
    with pytest.raises(LzoError):
        lzo_decompress(b"\x14ab")
    with pytest.raises(LzoError):
        lzo_decompress(b"\x14abc\x11\x00\x00", 2)          # output limit


# --------------------------------------------------------------------------- JFFS2 versioning
def jffs2_inode(ino, version, data, offset=0, isize=None, mode=0o100644):
    body = struct.pack("<IIIHHIIIIIIIBBHI", ino, version, mode, 0, 0,
                       len(data) + offset if isize is None else isize, 0, 0, 0, offset, len(data), len(data),
                       0, 0, 0, J._crc(data))
    hdr = struct.pack("<HHI", 0x1985, 0xE002, 68 + len(data))
    hdr += struct.pack("<I", J._crc(hdr))
    ncrc = J._crc((hdr + body)[:60])                      # node CRC excludes data_crc / node_crc
    n = hdr + body + struct.pack("<I", ncrc) + data
    return n + b"\0" * (-len(n) % 4)


def jffs2_dirent(pino, version, ino, name, typ=8):
    nb = name.encode()
    body = struct.pack("<IIIIBBBB", pino, version, ino, 0, len(nb), typ, 0, 0)
    hdr = struct.pack("<HHI", 0x1985, 0xE001, 40 + len(nb))
    hdr += struct.pack("<I", J._crc(hdr))
    ncrc = J._crc(hdr + body)
    n = hdr + body + struct.pack("<II", ncrc, J._crc(nb)) + nb
    return n + b"\0" * (-len(n) % 4)


def test_jffs2_newest_version_wins():
    nodes = [
        jffs2_dirent(1, 1, 2, "a.txt"), jffs2_inode(2, 1, b"hello world"),
        jffs2_inode(2, 2, b"HE", offset=0, isize=11),                  # partial overwrite
        jffs2_dirent(1, 2, 3, "gone.txt"), jffs2_inode(3, 1, b"x"),
        jffs2_dirent(1, 3, 0, "gone.txt"),                              # deleted later
        jffs2_dirent(1, 4, 4, "sub", typ=4), jffs2_inode(4, 1, b"", mode=0o040755),
        jffs2_dirent(4, 5, 5, "b.txt"), jffs2_inode(5, 1, b"abc"),
        jffs2_inode(5, 2, b"", isize=1),                                # truncated to 1 byte
        b"\xde\xad\xbe\xef" * 3,                                         # junk between nodes
    ]
    f = fs.JFFS2(b"".join(nodes))
    files = {e.path: e.read() for e in f.entries() if e.kind == "file"}
    assert files == {"/a.txt": b"HEllo world", "/sub/b.txt": b"a"}


# --------------------------------------------------------------------------- web tools
def test_web_analysis_tools(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    import time
    import zipfile

    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app

    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    c = TestClient(create_app())

    def run(tool, q, body):
        assert c.post("/api/tools/%s?%s" % (tool, q), content=body).status_code == 200
        end = time.time() + 30
        while time.time() < end:
            j = c.get("/api/state").json()["job"]
            if j and j["op"] == "tool:" + tool and j["state"] != "running":
                return j
            time.sleep(0.02)
        raise AssertionError("tool did not finish")

    # file-system dump: main area, no OOB
    img = bytearray(b"\xff" * (1 << 20))
    for off, name in ((0x10000, "sq_lzo.img.gz"), (0x80000, "jffs2_le.img.gz")):
        d = fixture(name)
        img[off:off + len(d)] = d
    k = uimage("kern", b"k" * 4000)
    img[0x40000:0x40000 + len(k)] = k
    q = "page=2048&oob=0&ppb=64&name=dump.bin"

    j = run("scan", q, bytes(img))
    kinds = [r[1] for r in j["report"]["extra"]["tables"][0]["rows"]]
    assert j["state"] == "done" and kinds == ["SquashFS", "uImage", "JFFS2"]

    j = run("fs_list", q, bytes(img))
    rows = j["report"]["extra"]["tables"][0]["rows"]
    link = next(r[0]["href"] for r in rows
                if r[0]["text"] == "/usr/lib/text.txt" and r[3].startswith("SquashFS"))
    files, _links, _dirs = reference_tree()
    assert c.get(link).content == files["/usr/lib/text.txt"]
    assert c.get("/api/fs/file?i=9&path=/x").status_code == 404

    j = run("fs_extract", q, bytes(img))
    assert j["download"]
    with zipfile.ZipFile(io.BytesIO(c.get("/api/result").content)) as z:
        names = z.namelist()
        assert "0x00080000-JFFS2/usr/lib/deep/rand.bin" in names
        assert z.read("0x00010000-SquashFS/etc/banner") == files["/etc/banner"]

    # diff: A + B in one body, split = len(A)
    a = bytes(img[:0x20000])
    b = bytearray(a)
    b[100] ^= 0x01
    j = run("diff", "page=2048&oob=0&ppb=64&split=%d" % len(a), a + bytes(b))
    assert j["report"]["extra"]["kinds"]["bitflip"] == 1 and not j["report"]["ok"]
    assert c.post("/api/tools/diff?page=2048&oob=0", content=a).status_code == 400
