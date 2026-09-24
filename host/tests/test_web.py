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

    c.post("/api/job/verify?target=spi&start=1", content=data)
    assert wait_job(c)["report"]["ok"]

    c.post("/api/job/badblocks?target=nand")
    job = wait_job(c)
    assert job["report"]["bad_blocks"] == [3]

    r = c.post("/api/job/erase?target=nand&bb=force")
    assert r.status_code == 400                      # needs confirmation

    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["connected"]

    assert not c.post("/api/disconnect").json()["connected"]
