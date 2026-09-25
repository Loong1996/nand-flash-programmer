"""Sync FIFO clock phase: register 10 watchdog in the emulator, sweep, save / apply, CLI."""

import dataclasses
import time

import pytest

from nsprog import ft232h as F
from nsprog import protocol as P
from nsprog.cli import main
from nsprog.device import Device
from nsprog.emulator import Engine, EmulatorLink, NandModel


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(Engine, "PH_WD_S", 0.05)
    monkeypatch.setattr(F, "REVERT_S", 0.08)
    monkeypatch.setattr(F, "PROBE_S", 0.05)
    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))


def dev_with(good=None):
    link = EmulatorLink(NandModel(blocks=8), None)
    if good is not None:
        link.engine.good_phases = set(good)
    d = Device(link)
    d.open(negotiate=False)
    return d, link.engine


def test_longest_run_wraps():
    ok = [False] * 16
    for i in (14, 15, 0, 1, 2, 5, 6):
        ok[i] = True
    assert F._longest_run(ok) == [14, 15, 0, 1, 2]
    assert F._longest_run([True] * 16) == list(range(16))
    assert F._longest_run([False] * 16) == []


def test_bad_phase_reverts_and_link_recovers():
    dev, eng = dev_with(good=range(0, 6))
    assert F.set_phase(dev, 3) is None and eng.ft_phase == 3 and eng.ph_since is None
    err = F.set_phase(dev, 9)
    assert err and "bytes back" in err
    assert eng.ft_phase == 3                       # the watchdog went back to the confirmed phase
    assert dev.query_info().flags == 0             # resync already read the revert flag
    assert dev.ft_phase == 3


def test_watchdog_flag():
    dev, eng = dev_with()
    dev.link.write(bytes([P.SET_REG, P.REG_FT_PHASE, 7, 0]))
    assert eng.ft_phase == 7
    time.sleep(0.06)
    eng.idle()
    assert eng.ft_phase == 0 and eng.flags & P.FLAG_PHASE_REVERT


def test_tune_picks_middle_of_window():
    dev, eng = dev_with(good=[13, 14, 15, 0, 1, 2, 3, 8])
    seen = []
    rep = F.tune(dev, rounds=2, progress=lambda ph, err: seen.append(ph))
    assert seen == list(range(16))
    assert rep.passing == [0, 1, 2, 3, 8, 13, 14, 15]
    assert rep.window == [13, 14, 15, 0, 1, 2, 3] and rep.best == 0
    assert eng.ft_phase == 0 and eng.ph_since is None
    assert "chosen phase 0" in rep.summary()


def test_tune_all_good_keeps_default_and_none_good():
    dev, _ = dev_with()
    assert F.tune(dev, rounds=1).best == F.DEFAULT_PHASE
    dev, _ = dev_with(good=[F.DEFAULT_PHASE])
    rep = F.tune(dev, rounds=1)
    assert rep.best == F.DEFAULT_PHASE and rep.window == [F.DEFAULT_PHASE]


def test_old_gateware_is_refused():
    dev, _ = dev_with()
    dev.info = dataclasses.replace(dev.info, gw_version="1.2")
    with pytest.raises(ValueError):
        F.set_phase(dev, 1)
    F.save_phase(dev.link.name, 1)
    assert F.apply_saved_phase(dev) is None


def test_save_and_apply():
    dev, eng = dev_with(good=range(0, 9))
    assert F.saved_phase(dev.link.name) is None
    F.save_phase(dev.link.name, 5)
    assert F.saved_phase(dev.link.name) == 5 and F.saved_phase("other") == 5
    assert F.apply_saved_phase(dev) == 5 and eng.ft_phase == 5
    F.save_phase(dev.link.name, 12)                # no longer works: warn, keep the link
    assert F.apply_saved_phase(dev) is None and eng.ft_phase == 5
    assert dev.query_info().gw_version == "1.3"


def test_cli(capsys):
    assert main(["-p", "emu:nand", "ft232h-tune", "--rounds", "1"]) == 0
    out = capsys.readouterr().out
    assert "phase 15" in out and "chosen phase 0" in out and "saved" in out
    assert F.saved_phase("emulator") == 0
    assert main(["-p", "emu:nand", "ft232h-tune", "--phase", "6", "--no-save"]) == 0
    assert "phase 6 works" in capsys.readouterr().out
    assert F.saved_phase("emulator") == 0


def test_web(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app

    c = TestClient(create_app())
    assert c.post("/api/ft232h/tune", json={}).status_code == 400          # not connected
    c.post("/api/connect", json={"port": "emu:nand"})
    r = c.post("/api/ft232h/tune", json={}).json()
    assert len(r["results"]) == 16 and all(x["ok"] for x in r["results"]) and r["best"] == 0
    assert F.saved_phase("emulator") == 0
    assert "扫描相位" in c.get("/").text
