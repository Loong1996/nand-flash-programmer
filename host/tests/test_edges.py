"""Boundary conditions: protocol chunking, ranges, truncated images, API errors."""

import io
import os

import pytest

from nsprog import image, jobs, ubi
from nsprog import protocol as P
from nsprog.device import Device, ProtocolError
from nsprog.emulator import EmulatorLink, NandModel, SpiNorModel
from nsprog.flash import FlashError, detect


def dev_with(nand=None, spi=None, window=None):
    d = Device(EmulatorLink(nand, spi), window=window)
    d.open(negotiate=False)
    return d


# ------------------------------------------------------------------ protocol
def test_write_ops_split_at_max_chunk():
    for n, ops in ((0, 1), (1, 1), (P.MAX_CHUNK, 1), (P.MAX_CHUNK + 1, 2), (3 * P.MAX_CHUNK, 3)):
        b = P.Batch()
        b.nand_write(b"\x00" * n)
        assert len(b) == ops if n else len(b) in (0, 1), n
        assert sum(len(o.data) - 3 for o in b.ops) == n


def test_read_ops_split_at_max_read():
    b = P.Batch()
    r0 = b.spi_read(0)
    assert r0.value == b"" and len(b) == 0
    r = b.nand_read(P.MAX_READ + 1)
    assert len(b) == 2 and b.response_len == P.MAX_READ + 1
    dev = dev_with(NandModel())
    dev.run(b)
    assert len(r.value) == P.MAX_READ + 1


def test_batch_exactly_one_window():
    dev = dev_with(NandModel(), window=512)
    b = P.Batch()
    rs = [b.echo(i & 0xFF) for i in range(510)]      # + ECHO marker = one full window
    dev.run(b)
    assert [x.value for x in rs] == [i & 0xFF for i in range(510)]


def test_argument_validation():
    b = P.Batch()
    with pytest.raises(ValueError):
        b.pin_test(40, P.PT_LOW)
    with pytest.raises(ValueError):
        b.pin_test(0, 9)
    with pytest.raises(ValueError):
        b.spi_poll(b"", 1, 0, 10)
    with pytest.raises(ValueError):
        b.spi_poll(b"12345", 1, 0, 10)
    d = P.Batch()
    d.delay_us(70000)                                  # longer than one u16: split in two
    assert len(d) == 2
    with pytest.raises(ValueError):
        P.Info.parse(b"XXXX" + bytes(12))
    with pytest.raises(ValueError):
        P.Info.parse(b"NSPG")


def test_info_needs_open():
    d = Device(EmulatorLink(NandModel(), None))
    assert not d.opened
    with pytest.raises(ProtocolError):
        _ = d.info
    d.open(negotiate=False)
    assert d.opened and d.info.clk_hz == 27_000_000


# ------------------------------------------------------------------ job ranges
def test_block_ranges():
    dev = dev_with(None, SpiNorModel())
    drv = detect(dev, want="spi").spi
    last = drv.blocks - 1
    out = io.BytesIO()
    assert jobs.read(drv, out, start=last, count=1).ok and len(out.getvalue()) == drv.block_size
    for start, count in ((drv.blocks, None), (-1, 1), (last, 2), (0, 0), (0, -3)):
        with pytest.raises(FlashError):
            jobs.read(drv, io.BytesIO(), start=start, count=count)
    with pytest.raises(FlashError):                   # image larger than the chip
        jobs.write(drv, b"\x00" * (drv.size + 1), start=0)
    with pytest.raises(FlashError):
        jobs.write(drv, b"\x00" * (drv.block_size + 1), start=last)
    with pytest.raises(ValueError):
        jobs.write(drv, b"\x00", start=0, bb="bogus")


def test_empty_and_tiny_writes():
    dev = dev_with(None, SpiNorModel())
    drv = detect(dev, want="spi").spi
    rep = jobs.write(drv, b"\x42", start=3)           # one byte: rest of the page is preserved
    assert rep.ok and drv.read(3 * drv.block_size, 2) == b"\x42\xff"
    assert jobs.verify(drv, b"\x42", start=3).ok
    assert not jobs.verify(drv, b"\x43", start=3).ok


# ------------------------------------------------------------------ offline tools
GEO = image.Geometry(2048, 64, 4)


def test_image_info_truncated_and_empty():
    inf = image.info(io.BytesIO(b""), GEO)
    assert inf.pages == 0 and inf.blocks == 0
    raw = b"\xff" * (GEO.raw * 3 + 100)
    inf = image.info(io.BytesIO(raw), GEO)
    assert inf.pages == 4 and inf.trailing_bytes == 100 and "trailing" in inf.summary()


def test_strip_partial_block_and_unknown_chip():
    raw = b"\xff" * (GEO.raw * 5)                      # 1 full block + 1 page
    out = io.BytesIO()
    assert image.strip_oob(io.BytesIO(raw), out, GEO) == 5
    assert len(out.getvalue()) == 5 * GEO.page
    with pytest.raises(ValueError):
        image.Geometry.from_chip("NO-SUCH-CHIP")
    assert image.Geometry.from_chip("W25N01GV").page == 2048


def test_ecc_check_empty_and_blank():
    from nsprog.ecc import layout
    rep = image.ecc_check(io.BytesIO(b""), GEO, layout("bch8"))
    assert rep.pages == 0 and rep.ok
    rep = image.ecc_check(io.BytesIO(b"\xff" * GEO.raw * 4), GEO, layout("bch8"))
    assert rep.ok and rep.blank_steps == rep.steps
    with pytest.raises(ValueError):
        layout("crc32")


def test_ubi_rejects_non_ubi():
    with pytest.raises(ValueError):
        ubi.parse(io.BytesIO(b"\x00" * 4096))
    with pytest.raises(ValueError):
        ubi.parse(io.BytesIO(b""))


# ------------------------------------------------------------------ web API errors
def _wait_idle(c, timeout=30.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if c.get("/api/state").json()["job"]["state"] != "running":
            return
        time.sleep(0.02)
    raise AssertionError("web job did not finish")


def test_web_api_errors(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app

    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    c = TestClient(create_app())
    assert c.get("/api/result").status_code == 404
    assert c.get("/api/result/bytes").status_code == 404
    assert c.post("/api/detect", json={}).status_code == 400
    assert c.get("/api/pins").status_code == 400
    assert c.post("/api/job/read?target=nand").status_code == 400      # not connected
    assert c.post("/api/tools/info", content=b"").status_code == 400
    assert c.post("/api/connect", json={"port": "/dev/does-not-exist"}).status_code == 400
    c.post("/api/connect", json={"port": "emu"})
    c.post("/api/detect", json={})
    assert c.post("/api/job/read?target=nand&start=99999").status_code in (200, 400)
    _wait_idle(c)
    # results: clamping and bad searches
    assert c.post("/api/tools/strip?page=2048&oob=64&ppb=4", content=b"\xff" * GEO.raw * 4).status_code == 200
    _wait_idle(c)
    size = c.get("/api/state").json()["result"]["size"]
    assert len(c.get("/api/result/bytes?offset=-5&length=10").content) == 10
    assert c.get("/api/result/bytes?offset=%d&length=10" % size).content == b""
    assert c.get("/api/result/find", params={"q": ""}).status_code == 400
    assert c.get("/api/result/find", params={"q": "zz", "hex": True}).status_code == 400
    assert os.path.exists(tmp_path / "history.json")


def test_quit_only_in_app_mode_and_instance_probe(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app, running_instance

    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    monkeypatch.delenv("NSPROG_APP", raising=False)
    c = TestClient(create_app())
    assert c.get("/api/state").json()["app_mode"] is False
    assert c.post("/api/quit").status_code == 403
    assert running_instance(1) is None           # nothing listens on port 1


def test_cli_legacy_codepage_output():
    """Windows pipes/consoles may be cp1252: ✓ and Chinese must not crash the CLI."""
    import subprocess
    import sys

    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    r = subprocess.run([sys.executable, "-c", "import sys; from nsprog.cli import main; sys.exit(main())",
                        "-p", "emu", "doctor"], env=env, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
