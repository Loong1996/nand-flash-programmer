import random
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from nsprog.web.app import create_app  # noqa: E402


def wait_job(c, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/state").json()
        if s["job"] and s["job"]["state"] != "running":
            return s["job"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.fixture(autouse=True)
def nsprog_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    return tmp_path


def test_web_flow():
    c = TestClient(create_app())
    assert "nsprog" in c.get("/").text
    ports = c.get("/api/ports").json()["ports"]
    assert any(p["device"] == "emu" for p in ports)
    assert c.get("/api/chips?q=w25q").json()["chips"]

    s = c.post("/api/connect", json={"port": "emu"}).json()
    assert s["connected"]
    s = c.post("/api/detect", json={}).json()
    assert s["nand"]["blocks"] == 256 and s["spi"]["name"] == "W25Q16JV"

    data = bytes(random.Random(2).getrandbits(8) for _ in range(20000))
    r = c.post("/api/job/write?target=spi&start=1", content=data)
    assert r.status_code == 200, r.text
    job = wait_job(c)
    assert job["state"] == "done", job

    c.post("/api/job/read?target=spi&start=1&count=5")
    job = wait_job(c)
    assert job["state"] == "done" and job["download"]
    got = c.get("/api/result").content
    assert got[:len(data)] == data and len(got) == 5 * 4096
    assert c.get("/api/result/bytes?offset=100&length=50").content == got[100:150]
    needle = data[7000:7006]
    def find(**kw):
        return c.get("/api/result/find", params=kw).json()["offset"]
    assert find(q=needle.hex(), hex=True) == got.find(needle)
    assert find(q=needle.hex(), hex=True, start=12000) == got.find(needle, 12000)
    assert c.get("/api/result/find", params={"q": "\u0000no-such-text\u0000"}).json()["offset"] == -1

    c.post("/api/job/verify?target=spi&start=1", content=data)
    assert wait_job(c)["report"]["ok"]

    c.post("/api/job/badblocks?target=nand")
    job = wait_job(c)
    assert job["report"]["bad_blocks"] == [3]
    assert c.get("/api/state").json()["badmap"]["nand"]["bad"] == [3]

    pins = c.get("/api/pins").json()
    assert pins["rb_ready"] and "nand_io" in pins
    assert c.post("/api/selftest").json()["ok"]

    r = c.post("/api/job/erase?target=nand&bb=force")
    assert r.status_code == 400                      # needs confirmation

    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["connected"]

    assert not c.post("/api/disconnect").json()["connected"]


def test_settings_and_history(nsprog_home):
    c = TestClient(create_app())
    assert c.get("/api/settings").json()["theme"] == "auto"
    s = c.post("/api/settings", json={"theme": "dark", "spi_mhz": 13.5, "bogus": 1}).json()
    assert s["theme"] == "dark" and "bogus" not in s
    # persisted across restarts
    assert TestClient(create_app()).get("/api/settings").json()["spi_mhz"] == 13.5

    c.post("/api/connect", json={"port": "emu"})
    c.post("/api/detect", json={})
    c.post("/api/job/blank?target=spi&count=2")
    wait_job(c)
    h = c.get("/api/history").json()["history"]
    assert h[0]["op"] == "blank" and h[0]["state"] == "done"
    assert (nsprog_home / "history.json").exists()
    assert c.delete("/api/history").json()["history"] == []


def test_tools_endpoint():
    import io
    import os

    from nsprog import image
    from nsprog.ecc import layout

    c = TestClient(create_app())
    geo = image.Geometry(2048, 64, 4)
    main = bytes(random.Random(5).getrandbits(8) for _ in range(4 * 2048))
    raw = io.BytesIO()
    image.merge(io.BytesIO(main), None, raw, geo, layout("bch8"))
    bad = bytearray(raw.getvalue())
    bad[10] ^= 1
    q = "page=2048&oob=64&ppb=4&ecc=bch8&name=dump.bin"

    c.post("/api/tools/ecc_check?" + q, content=bytes(bad))
    job = wait_job(c)
    assert job["state"] == "done" and "1 corrected" in job["report"]["summary"]

    c.post("/api/tools/ecc_fix?strip=1&" + q, content=bytes(bad))
    job = wait_job(c)
    assert job["download"] and c.get("/api/result").content == main
    assert c.get("/api/state").json()["result"]["name"] == "dump-fixed-main.bin"

    c.post("/api/tools/merge?" + q, content=main)
    wait_job(c)
    assert c.get("/api/result").content == raw.getvalue()

    ubi_img = open(os.path.join(os.path.dirname(__file__), "data", "test.ubi"), "rb").read()
    c.post("/api/tools/ubi_extract?name=test.ubi", content=ubi_img)
    job = wait_job(c)
    assert job["state"] == "done" and "rootfs" in job["report"]["summary"]
    assert c.get("/api/result").content[:2] == b"PK"

    assert c.post("/api/tools/info", content=b"").status_code == 400
