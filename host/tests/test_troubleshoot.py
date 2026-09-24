"""Guided troubleshooting: symptoms, automatic checks against injected faults, CLI and web."""

import pytest

from nsprog import troubleshoot as T
from nsprog import wiring
from nsprog.cli import main
from nsprog.device import Device
from nsprog.emulator import EmulatorLink, NandModel, SpiNorModel


def dev_with(nand=True, spi=True):
    link = EmulatorLink(NandModel(blocks=8) if nand else None, SpiNorModel() if spi else None)
    d = Device(link)
    d.open(negotiate=False)
    return d, link.engine


def by_title(findings, prefix):
    return [f for f in findings if f.title.startswith(prefix)]


def test_symptoms_are_complete():
    ids = [s.id for s in T.SYMPTOMS]
    assert len(ids) == len(set(ids)) >= 6
    for s in T.SYMPTOMS:
        assert s.steps and all(c in T.CHECKS for c in s.checks)
    with pytest.raises(ValueError):
        T.symptom("nope")


def test_healthy_setup_passes():
    dev, _ = dev_with()
    for sid in ("no-chip", "bad-id", "read-unstable", "write-fail", "busy"):
        f = T.diagnose(sid, dev)
        assert T.verdict(f) == "ok", (sid, [x for x in f if x.status != "ok"])


def test_empty_socket_reports_ff_with_fix():
    dev, _ = dev_with(nand=False, spi=False)
    f = T.diagnose("no-chip", dev)
    nand = by_title(f, "并口 NAND ID")[0]
    assert nand.status == "fail" and "FF" in nand.detail and "1 脚" in nand.fix
    assert T.verdict(f) == "fail"


def test_short_is_reported_with_socket_pins():
    dev, eng = dev_with(nand=False, spi=False)
    eng.shorts = [(wiring.find("IO2").bit, wiring.find("IO3").bit)]
    f = T.diagnose("no-chip", dev)
    short = by_title(f, "接线自检 · 线间短路")[0]
    assert short.status == "fail" and "IO2" in short.detail and "IO3" in short.detail
    assert "TSOP48" in short.detail


def test_missing_wp_wire_is_found():
    dev, eng = dev_with()
    eng.wp_open = True
    f = T.diagnose("write-fail", dev)
    wp = by_title(f, "写保护")[0]
    assert wp.status == "fail" and "69" in wp.fix


def test_stuck_busy_is_found():
    dev, eng = dev_with()
    eng.nand.busy_until = 1e18
    f = T.diagnose("busy", dev)
    assert by_title(f, "R/B#")[0].status == "fail"


def test_no_device():
    f = T.diagnose("no-chip", None)
    assert f[0].status == "fail" and "没有连接" in f[0].detail
    f = T.diagnose("no-programmer", None)
    assert any(x.title == "串口" for x in f)


def test_cli(capsys):
    assert main(["troubleshoot"]) == 0
    assert "no-chip" in capsys.readouterr().out
    assert main(["-p", "emu", "troubleshoot", "write-fail"]) == 0
    out = capsys.readouterr().out
    assert "写保护" in out and "还要逐项确认" in out


def test_web(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app

    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    c = TestClient(create_app())
    syms = c.get("/api/troubleshoot").json()["symptoms"]
    assert {s["id"] for s in syms} >= {"no-chip", "no-programmer"} and all("checks" not in s for s in syms)
    r = c.post("/api/troubleshoot", json={"symptom": "no-chip"}).json()
    assert r["verdict"] == "fail" and "没有连接" in r["findings"][0]["detail"]
    assert c.post("/api/troubleshoot", json={"symptom": "x"}).status_code == 400
    c.post("/api/connect", json={"port": "emu"})
    r = c.post("/api/troubleshoot", json={"symptom": "no-chip"}).json()
    assert r["verdict"] == "ok" and r["symptom"]["id"] == "no-chip"
    assert "排障" in c.get("/").text
