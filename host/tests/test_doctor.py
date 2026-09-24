"""Wiring self-check (PIN_TEST + doctor) against the emulator's fault model."""

import threading

import pytest

from nsprog import doctor, wiring
from nsprog import protocol as P
from nsprog.cli import main as cli_main
from nsprog.device import Device
from nsprog.emulator import EmulatorLink, NandModel, SpiNorModel


def make(**faults):
    link = EmulatorLink(NandModel(), SpiNorModel())
    for k, v in faults.items():
        setattr(link.engine, k, v)
    dev = Device(link)
    dev.open(negotiate=False)
    return dev, link.engine


def by_key(checks):
    return {c.key: c for c in checks}


def test_wiring_table_is_consistent():
    assert wiring.N_TEST == 21
    assert [s.test for s in wiring.TEST_SIGNALS] == list(range(21))
    fpga = [s.fpga for s in wiring.SIGNALS if s.fpga is not None]
    assert len(fpga) == len(set(fpga)), "two signals on one FPGA pin"
    assert not any(79 <= p <= 86 for p in fpga), "bank 3 (1.8V) pins must stay unused"
    assert wiring.find("CE#").fpga == 74 and wiring.find("spi:io0").fpga == 75
    assert wiring.find("fpga49").name == "R/B#" and wiring.find("io3").test == 3
    with pytest.raises(ValueError):
        wiring.find("nope")


def test_constraints_match_wiring_table():
    import os
    import re
    cst = open(os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "constraints",
                            "tangnano9k.cst")).read()
    loc = {m.group(1): int(m.group(2)) for m in re.finditer(r'IO_LOC\s+"([^"]+)"\s+(\d+);', cst)}
    port = {"IO%d" % i: "nand_io[%d]" % i for i in range(8)}
    port.update({"CLE": "nand_cle", "ALE": "nand_ale", "WE#": "nand_we_n", "RE#": "nand_re_n",
                 "CE#": "nand_ce_n", "WP#": "nand_wp_n", "R/B#": "nand_rb_n", "CS#": "spi_cs_n",
                 "CLK": "spi_sck", "DI/IO0": "spi_io0", "DO/IO1": "spi_io1", "WP#/IO2": "spi_io2",
                 "HOLD#/IO3": "spi_io3", "RXF#": "ft_rxf_n", "TXE#": "ft_txe_n", "RD#": "ft_rd_n",
                 "WR#": "ft_wr_n", "SIWU#": "ft_siwu_n", "CLKOUT": "ft_clkout", "OE#": "ft_oe_n"})
    port.update({"D%d" % i: "ft_d[%d]" % i for i in range(8)})
    for s in wiring.SIGNALS:
        if s.fpga is not None:
            assert loc[port[s.name]] == s.fpga, s.name
    for s in wiring.TEST_SIGNALS:
        m = re.search(r'IO_PORT\s+"%s"[^;]*PULL_MODE=(\w+)' % re.escape(port[s.name]), cst)
        assert m.group(1).lower() == s.pull, s.name


def test_pin_test_levels():
    dev, eng = make()
    b = P.Batch()
    idle = b.pin_test(0, P.PT_RELEASE)
    lo = b.pin_test(12, P.PT_LOW)            # CE#
    hi = b.pin_test(8, P.PT_HIGH)            # CLE
    b.pin_test(0, P.PT_OFF)
    dev.run(b)
    assert idle.value == wiring.idle_levels()
    assert lo.value == wiring.idle_levels() & ~(1 << 12)
    assert hi.value == wiring.idle_levels() | (1 << 8)
    assert eng.pt_mode == 0


def test_doctor_clean():
    dev, _ = make()
    checks = by_key(doctor.run(dev))
    assert checks["idle"].status == "ok" and checks["shorts"].status == "ok"
    assert checks["stuck"].status == "ok" and checks["chips"].status == "ok"
    assert doctor.summary(list(checks.values())) == "全部正常"


def test_doctor_finds_shorts_and_stuck_lines():
    dev, eng = make(shorts=[(2, 3), (12, 15)], stuck={8: 1})
    checks = by_key(doctor.run(dev, detect_chips=False))
    assert checks["shorts"].status == "fail"
    assert "IO2" in checks["shorts"].detail and "IO3" in checks["shorts"].detail
    assert "CE#" in checks["shorts"].detail and "CS#" in checks["shorts"].detail
    assert checks["stuck"].status == "fail" and "CLE" in checks["stuck"].detail
    assert checks["idle"].status == "warn" and "CLE" in checks["idle"].detail
    assert eng.pt_mode == 0                          # pins handed back


def test_doctor_no_drive_and_cli(capsys):
    dev, _ = make(stuck={0: 0})
    checks = by_key(doctor.run(dev, drive=False, detect_chips=False))
    assert checks["idle"].status == "warn" and checks["shorts"].status == "skip"
    assert cli_main(["-p", "emu", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "结论：全部正常" in out
    assert cli_main(["pintest", "--list"]) == 0
    assert "HOLD#/IO3" in capsys.readouterr().out
    assert cli_main(["-p", "emu", "pintest", "--pin", "CE#", "--seconds", "0"]) == 0


def test_probe_reports_touched_pin():
    dev, eng = make()

    def touch():
        eng.probe = {12: 0}                         # someone touches CE# with a GND lead
    timer = threading.Timer(0.05, touch)
    timer.start()
    seen = None
    for i, lv in doctor.probe(dev, interval=0.01):
        seen = (i, lv)
        break
    timer.join()
    doctor.stop(dev)
    assert seen == (12, 0) and eng.pt_mode == 0


def test_old_gateware_skips_pin_test():
    dev, _ = make()
    dev.info.caps &= ~P.CAP_PIN_TEST
    checks = by_key(doctor.run(dev, detect_chips=False))
    assert checks["firmware"].status == "warn" and "idle" not in checks


def test_web_wiring_doctor_pintest(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from nsprog.web.app import create_app

    monkeypatch.setenv("NSPROG_HOME", str(tmp_path))
    c = TestClient(create_app())
    w = c.get("/api/wiring").json()
    assert set(w["packages"]) == {"tsop48", "soic8", "soic16", "ft232h"}
    assert any(s["name"] == "CE#" and s["fpga"] == 74 for s in w["signals"])
    assert c.post("/api/doctor", json={}).status_code == 400          # not connected
    c.post("/api/connect", json={"port": "emu"})
    r = c.post("/api/pintest", json={"pin": 12, "mode": P.PT_LOW}).json()
    assert r["levels"] == w["idle"] & ~(1 << 12)
    assert c.get("/api/state").json()["pintest"] == P.PT_LOW
    # detection hands the pins back to normal operation first
    s = c.post("/api/detect", json={}).json()
    assert s["pintest"] == 0 and s["nand"] is not None
    d = c.post("/api/doctor", json={"drive": True}).json()
    assert d["summary"] == "全部正常" and {x["key"] for x in d["checks"]} >= {"link", "shorts", "chips"}
