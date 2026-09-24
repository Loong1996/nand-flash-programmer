"""`nsprog doctor`: one-shot check of the programmer, the wiring and the chips.

The wiring tests use the PIN_TEST operation (gateware >= 1.2): every flash-side
pin is released (only the FPGA-internal pull holds it), then each pin in turn is
driven low and high while all others are read back. That finds

* lines shorted together (a driven pin pulls another one along),
* lines shorted to GND or 3V3 (a pin cannot be driven to one level),
* lines held away from their pull level while idle (wrong wire, chip inserted,
  solder bridge).

Broken (open) wires cannot be seen from the FPGA side alone; ``probe()`` (and
the web wiring page) lets you touch each socket pin with a GND / 3V3 lead and
shows which FPGA pin reacts.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import protocol as P
from . import wiring
from .device import Device

OK, WARN, FAIL, SKIP, INFO = "ok", "warn", "fail", "skip", "info"


@dataclass
class Check:
    key: str
    title: str
    status: str
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _bits(v: int) -> List[int]:
    return [i for i in range(wiring.N_TEST) if v >> i & 1]


def _name(i: int) -> str:
    return wiring.describe(wiring.by_test_index(i))


def supports_pin_test(dev: Device) -> bool:
    return dev.opened and bool(dev.info.caps & P.CAP_PIN_TEST)


def pin_test(dev: Device, pin: int, mode: int) -> int:
    b = P.Batch()
    r = b.pin_test(pin, mode)
    dev.run(b)
    return r.value


def scan(dev: Device) -> Tuple[int, Dict[int, Tuple[int, int]]]:
    """Release everything, then drive each pin low and high.

    Returns (idle levels, {pin: (levels with pin low, levels with pin high)}).
    Always leaves the pins in normal mode.
    """
    b = P.Batch()
    idle = b.pin_test(0, P.PT_RELEASE)
    res = {}
    for i in range(wiring.N_TEST):
        lo = b.pin_test(i, P.PT_LOW)
        hi = b.pin_test(i, P.PT_HIGH)
        res[i] = (lo, hi)
    b.pin_test(0, P.PT_OFF)
    try:
        dev.run(b)
    finally:
        _off(dev)
    return idle.value, {i: (lo.value, hi.value) for i, (lo, hi) in res.items()}


def _off(dev: Device) -> None:
    try:
        pin_test(dev, 0, P.PT_OFF)
    except Exception:  # pragma: no cover - link already broken
        pass


def analyse(idle: int, drives: Dict[int, Tuple[int, int]]) -> List[Check]:
    """Turn raw scan results into findings (pure function, unit tested)."""
    out: List[Check] = []
    expect = wiring.idle_levels()
    # --- idle levels
    odd = [(i, idle >> i & 1) for i in range(wiring.N_TEST) if (idle ^ expect) >> i & 1]
    if odd:
        out.append(Check(
            "idle", "空闲电平", WARN,
            "\n".join("%s 读到%s，应为%s" % (_name(i), "高" if v else "低", "低" if v else "高")
                      for i, v in odd),
            "座子里插着芯片时个别线（如 R/B#）偏离是正常的；空座时说明这根线接到了别处、"
            "和别的线短路，或者碰到了 GND / 3V3。"))
    else:
        out.append(Check("idle", "空闲电平", OK, "21 根线都停在各自的上拉/下拉电平"))
    # --- stuck lines and shorts
    stuck, shorts = [], set()
    for i, (lo, hi) in drives.items():
        if lo >> i & 1:
            stuck.append((i, "高"))
        elif not hi >> i & 1:
            stuck.append((i, "低"))
        # other pins that followed the driven one
        for j in range(wiring.N_TEST):
            if j == i:
                continue
            base = idle >> j & 1
            if (base and not lo >> j & 1) or (not base and hi >> j & 1):
                shorts.add(tuple(sorted((i, j))))
    if stuck:
        out.append(Check(
            "stuck", "对电源短路", FAIL,
            "\n".join("%s 驱动不动，一直是%s电平" % (_name(i), lv) for i, lv in stuck),
            "一直为低：这根线碰到了 GND；一直为高：碰到了 3V3。断电后用万用表查这根线。"))
    else:
        out.append(Check("stuck", "对电源短路", OK, "每根线都能被拉高和拉低"))
    stuck_set = {i for i, _ in stuck}
    shorts = {p for p in shorts if not (p[0] in stuck_set and p[1] in stuck_set)}
    if shorts:
        out.append(Check(
            "shorts", "线间短路", FAIL,
            "\n".join("%s ↔ %s" % (_name(a), _name(b)) for a, b in sorted(shorts)),
            "两根线连在了一起：检查相邻的杜邦线、排针焊点和座子引脚之间有没有搭锡。"))
    else:
        out.append(Check("shorts", "线间短路", OK, "没有两根线连在一起"))
    return out


def run(dev: Device, *, drive: bool = True, detect_chips: bool = True,
        progress: Optional[Callable[[str], None]] = None) -> List[Check]:
    """Run every check; never raises for a failed check (only for a dead link)."""
    say = progress or (lambda _m: None)
    checks: List[Check] = []
    info = dev.info
    # ---------------------------------------------------------------- link
    say("link")
    link = "FT232H" if info.port else "UART"
    extra = ""
    if not info.port and dev.link.baudrate:
        extra = "，%d baud" % dev.link.baudrate
    checks.append(Check("link", "链路", OK, "%s%s，固件 %s" % (link, extra, info.gw_version)))
    if not supports_pin_test(dev):
        checks.append(Check("firmware", "固件版本", WARN,
                            "固件 %s 不支持引脚测试（需要 1.2 以上）" % info.gw_version,
                            "运行 nsprog fpga-flash 更新 FPGA 固件。"))
    if info.flags:
        names = {0: "未知操作码", 1: "字节间超时", 2: "串口接收溢出", 3: "波特率回退"}
        what = "、".join(v for k, v in names.items() if info.flags >> k & 1)
        checks.append(Check("flags", "错误标志", WARN, "上次连接后出现过：%s" % what,
                            "偶尔一次没关系；反复出现说明 USB 线或链路不稳定。"))
    # ---------------------------------------------------------------- FT232H
    try:
        pins = dev.pins()
        if info.port:
            mode = "同步 FIFO" if pins.ft_clkout_active else "异步 FIFO"
            checks.append(Check("ft232h", "FT232H", OK, "正在使用 %s" % mode))
        else:
            from .link import find_ft232h_url
            if find_ft232h_url():
                checks.append(Check("ft232h", "FT232H", WARN, "电脑上有 FT232H，但当前走的是串口",
                                    "用 -p ft232h 连接；如果连不上，"
                                    "检查 FT232H 到 FPGA 的接线和 EEPROM 设置。"))
        if not pins.ft_siwu_n:
            checks.append(Check("siwu", "FT232H SIWU#", WARN, "SIWU#（FPGA 35 脚）为低",
                                "SIWU# 应通过 10 kΩ 上拉到 3.3V，低电平会让 FT232H 立即发包。"))
    except Exception as e:  # pragma: no cover - old gateware
        checks.append(Check("pins", "引脚状态", WARN, str(e)))
    # ---------------------------------------------------------------- wiring
    if drive and supports_pin_test(dev):
        say("wiring")
        idle, drives = scan(dev)
        checks.extend(analyse(idle, drives))
    elif supports_pin_test(dev):
        say("wiring")
        idle = pin_test(dev, 0, P.PT_RELEASE)
        _off(dev)
        checks.extend(analyse(idle, {}))
        checks[-1].status = SKIP
        checks[-2].status = SKIP
    # ---------------------------------------------------------------- chips
    if detect_chips:
        say("chips")
        from .flash import detect
        try:
            det = detect(dev)
            found = [d.name for d in (det.nand, det.spi) if d]
            hint = "" if found else \
                "空座时这是正常的；放了芯片却识别不到，看 docs/quickstart.md 常见问题。"
            checks.append(Check("chips", "芯片", OK if found else INFO,
                                "发现：" + "、".join(found) if found else "两个座子都没有识别到芯片",
                                hint))
            for d in (det.nand, det.spi):
                if d and d.voltage and d.voltage < 2.5:
                    checks.append(Check("voltage", "芯片电压", FAIL, "%s 是 %.1fV 芯片" % (d.name, d.voltage),
                                        "Tang Nano 9K 是 3.3V 电平，直接读写会损坏芯片，需要电平转换板。"))
        except Exception as e:
            checks.append(Check("chips", "芯片", FAIL, "识别出错：%s" % e))
    return checks


def summary(checks: List[Check]) -> str:
    worst = FAIL if any(c.status == FAIL for c in checks) else \
        WARN if any(c.status == WARN for c in checks) else OK
    return {OK: "全部正常", WARN: "有需要注意的地方", FAIL: "发现问题"}[worst]


def probe(dev: Device, interval: float = 0.05) -> Iterator[Tuple[int, int]]:
    """Release all test pins and yield (test bit, new level) whenever a line changes.

    The caller stops iterating (e.g. on Ctrl+C) and should then call ``stop(dev)``.
    """
    base = pin_test(dev, 0, P.PT_RELEASE)
    while True:
        time.sleep(interval)
        lv = pin_test(dev, 0, P.PT_RELEASE)
        diff = lv ^ base
        for i in _bits(diff):
            yield i, lv >> i & 1
        base = lv


def stop(dev: Device) -> None:
    _off(dev)
