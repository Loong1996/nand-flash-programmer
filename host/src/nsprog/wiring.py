"""Wiring of the Tang Nano 9K programmer (single source for doctor and the web UI).

Keep in sync with fpga/constraints/tangnano9k.cst and docs/wiring.md.
``test`` is the bit index used by the PIN_TEST operation (None = not testable).
``pull`` is the FPGA-internal pull on that pin, i.e. the level a released,
unconnected line reads.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Signal:
    name: str                    # signal name as printed in the docs
    fpga: Optional[int]          # FPGA package pin (board silkscreen); None = power rail
    group: str                   # nand | spi | ft232h | power
    test: Optional[int] = None   # PIN_TEST bit
    pull: str = "up"             # up | down | none
    pins: Dict[str, str] = field(default_factory=dict)   # package -> pin on that package
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def bit(self) -> int:
        """PIN_TEST bit (only for testable signals)."""
        if self.test is None:
            raise ValueError("%s is not covered by the pin test" % self.name)
        return self.test


# Package keys used in ``Signal.pins``
PACKAGES = {
    "tsop48": "TSOP48 并口 NAND",
    "soic8": "SOP8 / WSON8 / DIP8 / USON8 / 测试夹",
    "soic16": "SOIC16（300 mil）",
    "ft232h": "FT232H 模块",
}

SIGNALS: List[Signal] = [
    # ---------------------------------------------------------------- parallel NAND
    *[Signal("IO%d" % i, fpga, "nand", test=i, pull="up", pins={"tsop48": str(tp)})
      for i, (fpga, tp) in enumerate([(25, 29), (26, 30), (27, 31), (28, 32),
                                       (29, 41), (30, 42), (31, 43), (32, 44)])],
    Signal("CLE", 70, "nand", test=8, pull="down", pins={"tsop48": "16"}),
    Signal("ALE", 71, "nand", test=9, pull="down", pins={"tsop48": "17"}),
    Signal("WE#", 72, "nand", test=10, pull="up", pins={"tsop48": "18"}),
    Signal("RE#", 73, "nand", test=11, pull="up", pins={"tsop48": "8"}),
    Signal("CE#", 74, "nand", test=12, pull="up", pins={"tsop48": "9"}),
    Signal("WP#", 69, "nand", test=13, pull="down", pins={"tsop48": "19"},
           note="FPGA 默认拉低（写保护）"),
    Signal("R/B#", 49, "nand", test=14, pull="up", pins={"tsop48": "7"},
           note="开漏；建议另加 10 kΩ 上拉到 3V3"),
    # ---------------------------------------------------------------- SPI flash
    Signal("CS#", 39, "spi", test=15, pull="up", pins={"soic8": "1", "soic16": "7"}),
    Signal("CLK", 63, "spi", test=16, pull="down", pins={"soic8": "6", "soic16": "16"}),
    Signal("DI/IO0", 75, "spi", test=17, pull="up", pins={"soic8": "5", "soic16": "15"}),
    Signal("DO/IO1", 77, "spi", test=18, pull="up", pins={"soic8": "2", "soic16": "8"}),
    Signal("WP#/IO2", 76, "spi", test=19, pull="up", pins={"soic8": "3", "soic16": "9"}),
    Signal("HOLD#/IO3", 48, "spi", test=20, pull="up", pins={"soic8": "7", "soic16": "1"}),
    # ---------------------------------------------------------------- FT232H
    *[Signal("D%d" % i, fpga, "ft232h", pins={"ft232h": "AD%d" % i})
      for i, fpga in enumerate([41, 42, 51, 53, 54, 55, 56, 57])],
    Signal("RXF#", 33, "ft232h", pins={"ft232h": "AC0"}),
    Signal("TXE#", 34, "ft232h", pins={"ft232h": "AC1"}),
    Signal("RD#", 40, "ft232h", pins={"ft232h": "AC2"}),
    Signal("WR#", 38, "ft232h", pins={"ft232h": "AC3"}),
    Signal("SIWU#", 35, "ft232h", pins={"ft232h": "AC4"}, note="另加 10 kΩ 上拉到 3.3V"),
    Signal("CLKOUT", 36, "ft232h", pins={"ft232h": "AC5"}, note="尽量短"),
    Signal("OE#", 37, "ft232h", pins={"ft232h": "AC6"}, note="建议 10 kΩ 上拉到 3.3V"),
    # ---------------------------------------------------------------- power
    Signal("VCC", None, "power", pull="none",
           pins={"tsop48": "12, 37", "soic8": "8", "soic16": "2"},
           note="接 Tang Nano 9K 的 3V3；每个座子旁加 100 nF 电容"),
    Signal("GND", None, "power", pull="none",
           pins={"tsop48": "13, 36", "soic8": "4", "soic16": "10", "ft232h": "GND"},
           note="FT232H 必须共地；它的 3.3V/5V 不要接"),
]

#: Signals covered by PIN_TEST, indexed by test bit.
TEST_SIGNALS: List[Signal] = sorted((s for s in SIGNALS if s.test is not None),
                                    key=lambda s: s.test or 0)
N_TEST = len(TEST_SIGNALS)                      # 21


def idle_levels() -> int:
    """Levels of all test pins when released and nothing is connected."""
    v = 0
    for s in TEST_SIGNALS:
        if s.pull == "up":
            v |= 1 << (s.test or 0)
    return v


def describe(sig: Signal) -> str:
    """'CE#（FPGA 74 脚 ↔ TSOP48 第 9 脚）'"""
    where = "、".join("%s 第 %s 脚" % (PACKAGES[k].split("（")[0].split(" /")[0], v)
                     for k, v in sig.pins.items())
    return "%s（FPGA %s 脚%s）" % (sig.name, sig.fpga, " ↔ " + where if where else "")


def by_test_index(i: int) -> Signal:
    return TEST_SIGNALS[i]


def find(name: str) -> Signal:
    """Look up a test signal by name ('CE#', 'nand:ce#', 'io3', 'fpga74', '74')."""
    n = name.strip().lower()
    group = None
    if ":" in n:
        group, n = n.split(":", 1)
    if n.startswith("fpga"):
        n = n[4:]
    for s in TEST_SIGNALS:
        if group and s.group != group:
            continue
        if n == s.name.lower() or n in s.name.lower().split("/") or n == str(s.fpga):
            return s
    raise ValueError("unknown test pin %r (see 'nsprog pintest --list')" % name)
