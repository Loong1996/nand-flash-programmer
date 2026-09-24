"""Guided troubleshooting: pick a symptom, run the matching automatic checks, get fixes.

Each symptom has automatic checks (run against the connected programmer, or
the computer when nothing is connected) and a manual checklist. Wiring checks
reuse :mod:`nsprog.doctor`, so a short it finds is reported here with the
socket pin numbers.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

from . import doctor
from . import protocol as P
from .device import Device

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Finding:
    status: str
    title: str
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Symptom:
    id: str
    title: str
    summary: str
    checks: List[str]
    steps: List[str]
    needs_device: bool = True

    def as_dict(self) -> dict:
        d = asdict(self)
        del d["checks"]
        return d


# --------------------------------------------------------------------------- automatic checks
Check = Callable[[Optional[Device]], List[Finding]]
CHECKS: Dict[str, Check] = {}


def _check(name: str):
    def deco(fn: Check) -> Check:
        CHECKS[name] = fn
        return fn
    return deco


@_check("computer")
def check_computer(dev: Optional[Device]) -> List[Finding]:
    from .link import find_ft232h_url, list_serial_ports

    out: List[Finding] = []
    ports = list_serial_ports()
    likely = [p["device"] for p in ports if p["likely"]]
    if likely:
        out.append(Finding(OK, "串口", "找到可能是 Tang Nano 9K 的串口：%s" % "、".join(likely),
                           "有两个口时编号大的是串口，例如 nsprog -p %s info" % likely[-1]))
    elif ports:
        names = "、".join(p["device"] for p in ports)
        out.append(Finding(WARN, "串口", "有串口，但没有像 Tang Nano 9K 的：%s" % names,
                           "手动指定试试：nsprog -p 端口 info"))
    else:
        out.append(Finding(FAIL, "串口", "电脑上没有任何串口设备",
                           "换一根能传数据的 USB 线（有的线只能充电）；"
                           "Windows 看设备管理器里有没有“USB Serial Port”；macOS 用 ls /dev/cu.* 看。"))
    try:
        url = find_ft232h_url()
    except Exception as e:                            # pyftdi / libusb missing
        url = None
        out.append(Finding(INFO, "FT232H", "无法查询 USB 设备：%s" % e,
                           "macOS：brew install libusb；Windows：用 Zadig 给 FT232H 装 WinUSB 驱动。"))
    if url:
        out.append(Finding(OK, "FT232H", "找到 FT232H：%s" % url))
    tool = shutil.which("openFPGALoader")
    out.append(Finding(OK if tool else WARN, "openFPGALoader",
                       tool or "没有安装（烧写 FPGA 固件要用）",
                       "" if tool else "macOS：brew install openfpgaloader；Linux 用发行版的包；"
                                       "Windows 可以用高云 Gowin Programmer。"))
    if dev is not None:
        out.append(Finding(OK, "编程器", "已连接：%s，固件 %s" % (dev.link.name, dev.info.gw_version)))
    return out


@_check("wiring")
def check_wiring(dev: Optional[Device]) -> List[Finding]:
    assert dev is not None
    out: List[Finding] = []
    for c in doctor.run(dev, drive=True, detect_chips=False):
        if c.key in ("idle", "stuck", "shorts", "firmware", "flags", "siwu", "ft232h"):
            st = {doctor.OK: OK, doctor.WARN: WARN, doctor.FAIL: FAIL}.get(c.status, INFO)
            out.append(Finding(st, "接线自检 · " + c.title, c.detail, c.hint))
    return out


def _ids(read: Callable[[], bytes], n: int = 6) -> List[bytes]:
    return [read() for _ in range(n)]


def _id_findings(what: str, ids: List[bytes], name: str) -> List[Finding]:
    first = ids[0]
    same = all(i == first for i in ids)
    hexid = first.hex(" ").upper()
    if all(b == 0xFF for b in first) and same:
        return [Finding(FAIL, what + " ID", "全是 FF（%s）：芯片没有应答" % hexid,
                        "芯片方向（1 脚标记对准座子 1 脚）、是否压紧；VCC 和 GND 是否接好；"
                        "NAND 查 CE#、RE#、WE#，SPI 查 CS#、CLK、DO。")]
    if all(b == 0x00 for b in first) and same:
        return [Finding(FAIL, what + " ID", "全是 00（%s）：数据线被拉低" % hexid,
                        "芯片没供电（VCC 没接）或数据线对地短路；先跑接线自检。")]
    if not same:
        diff = sorted({i.hex(" ").upper() for i in ids})
        return [Finding(FAIL, what + " ID", "连续读 %d 次，结果不一样：%s" % (len(ids), "；".join(diff[:4])),
                        "信号不稳：去耦电容（每个 VCC 脚旁 100 nF）、多接几根地线、缩短杜邦线；"
                        "NAND 用 --nand-timing safe，SPI 降到 --spi-mhz 1。")]
    return [Finding(OK, what + " ID",
                    "%s，连续 %d 次一致%s" % (hexid, len(ids), "：" + name if name else ""))]


@_check("nand_id")
def check_nand_id(dev: Optional[Device]) -> List[Finding]:
    from . import chipdb
    from .flash import ParallelNand

    assert dev is not None
    ids = _ids(lambda: ParallelNand.read_id_raw(dev, 5))
    chip = chipdb.find_nand(ids[0])
    out = _id_findings("并口 NAND", ids, chip.name if chip else "")
    if out[0].status == OK and chip is None:
        onfi = ParallelNand.read_onfi(dev)
        if onfi:
            out.append(Finding(OK, "型号", "ONFI：%s %s" % (onfi.manufacturer, onfi.model)))
        else:
            out.append(Finding(WARN, "型号", "芯片库里没有这个 ID，也不支持 ONFI",
                               "用 --chip 指定一个参数相同的型号，或者把 ID 发给我们加进芯片库。"))
    return out


@_check("spi_id")
def check_spi_id(dev: Optional[Device]) -> List[Finding]:
    from . import chipdb
    from .flash import SpiNor, set_spi_clock

    assert dev is not None
    set_spi_clock(dev, 1.0)
    ids = _ids(lambda: SpiNor.read_jedec(dev, 3))
    nor = chipdb.find_spi_nor(ids[0])
    out = _id_findings("SPI Flash", ids, nor.name if nor else "")
    if out[0].status == OK:
        set_spi_clock(dev, 13.5)
        fast = _ids(lambda: SpiNor.read_jedec(dev, 3), 4)
        if any(f != ids[0] for f in fast):
            out.append(Finding(WARN, "SPI 高速", "1 MHz 正常，13.5 MHz 下 ID 读错",
                               "线太长或在板读（测试夹）时板上负载太重：用 --spi-mhz 3 或更低。"))
        set_spi_clock(dev, 6.75)
    return out


@_check("rb")
def check_rb(dev: Optional[Device]) -> List[Finding]:
    assert dev is not None
    pins = dev.pins()
    if pins.rb_ready:
        return [Finding(OK, "R/B#", "空闲时为高（就绪）")]
    return [Finding(FAIL, "R/B#", "一直为低（忙）",
                    "R/B# 是开漏输出，要有 10 kΩ 上拉到 3V3；检查 TSOP48 第 7 脚 → FPGA 49 脚；"
                    "实在不行加 --no-rb，改用状态轮询。")]


@_check("nand_wp")
def check_nand_wp(dev: Optional[Device]) -> List[Finding]:
    from .flash import detect

    assert dev is not None
    det = detect(dev, want="nand")
    if det.nand is None:
        return [Finding(INFO, "写保护", "没有识别到并口 NAND，跳过")]
    old = dev.pin_ctrl
    try:
        dev.set_pin_ctrl(old | P.PIN_NAND_WP_HIGH)
        b = P.Batch()
        b.nand_ce(True)
        b.nand_cmd(0x70)
        r = b.nand_read(1)
        b.nand_ce(False)
        dev.run(b)
        status = r.value[0]
    finally:
        dev.set_pin_ctrl(old)
    if status & 0x80:
        return [Finding(OK, "写保护", "释放 WP# 后状态寄存器显示“未保护”（0x%02X）" % status)]
    return [Finding(FAIL, "写保护", "释放 WP# 后芯片仍处于写保护（状态 0x%02X）" % status,
                    "WP# 没接上：TSOP48 第 19 脚 → FPGA 69 脚。")]


@_check("read_stability")
def check_read_stability(dev: Optional[Device]) -> List[Finding]:
    from .flash import detect

    assert dev is not None
    det = detect(dev)
    out: List[Finding] = []
    for drv in (det.nand, det.spi):
        if drv is None:
            continue
        reads = [b"".join(drv.read_pages(0, 1, oob=True))[:4096] for _ in range(4)]
        size = len(reads[0])
        bits = sum(bin(int.from_bytes(a, "little") ^ int.from_bytes(reads[0], "little")).count("1")
                   for a in reads[1:])
        if bits == 0:
            out.append(Finding(OK, drv.name + " 读取", "同一位置读 4 次，结果一致（%d 字节）" % size))
        else:
            out.append(Finding(FAIL, drv.name + " 读取", "同一位置读 4 次，有 %d 个位不一样" % bits,
                               "总线不稳：NAND 用 --nand-timing safe、缩短数据线、加地线；"
                               "SPI 降低 --spi-mhz。MLC 芯片本身会有少量位翻转，需要 ECC。"))
    if not out:
        out.append(Finding(INFO, "读取稳定性", "没有识别到芯片，跳过"))
    return out


@_check("ft232h")
def check_ft232h(dev: Optional[Device]) -> List[Finding]:
    from .link import find_ft232h_url

    out: List[Finding] = []
    try:
        url = find_ft232h_url()
    except Exception as e:
        return [Finding(FAIL, "FT232H", "无法访问 USB：%s" % e,
                        "macOS：brew install libusb；Windows：用 Zadig 给 FT232H 装 WinUSB 驱动；"
                        "Linux：检查 udev 权限。")]
    if not url:
        return [Finding(FAIL, "FT232H", "电脑上没有找到 FT232H",
                        "检查 USB 线；EEPROM 要先设成 245 FIFO 模式：nsprog ft232h-setup。")]
    out.append(Finding(OK, "FT232H", "找到 %s" % url))
    if dev is not None:
        pins = dev.pins()
        if dev.info.port:
            out.append(Finding(OK, "链路", "正在通过 FT232H 通信（%s）" % (
                "同步 FIFO" if pins.ft_clkout_active else "异步 FIFO")))
        else:
            out.append(Finding(WARN, "链路", "现在走的是板载串口，没有用 FT232H",
                               "用 -p ft232h 重新连接；连不上时查 D0–D7、RXF#、TXE#、RD#、WR# 接线"
                               "（接线页有 FT232H 图）。"))
        if not pins.ft_siwu_n:
            out.append(Finding(WARN, "SIWU#", "为低", "SIWU#（FPGA 35 脚）需要 10 kΩ 上拉到 3.3V。"))
    return out


# --------------------------------------------------------------------------- symptoms
SYMPTOMS: List[Symptom] = [
    Symptom("no-programmer", "找不到编程器", "programmer not responding、没有串口、LED 不闪",
            ["computer"], [
                "LED0 在闪吗？不闪：nsprog fpga-flash 重新烧固件，然后按一下复位键。",
                "换一根确定能传数据的 USB 线，直接插电脑（不经过 USB 集线器）。",
                "Tang Nano 9K 会出现两个口，编号大的是串口：nsprog -p 端口 info。",
                "串口不稳时先用 115200：nsprog --no-fast-uart info。",
                "Linux：把用户加入 dialout 组；macOS 首次运行要允许。"], needs_device=False),
    Symptom("no-chip", "识别不到芯片（ID 全 FF）", "info 显示 no chip、ID FFFFFFFF",
            ["wiring", "nand_id", "spi_id", "rb"], [
                "芯片方向：芯片上的圆点 / 缺角对准座子的 1 脚。",
                "翻盖座压紧；芯片引脚没有弯。",
                "VCC 接 Tang Nano 9K 的 3V3，GND 至少接两根；每个 VCC 脚旁有 100 nF 电容。",
                "对照“接线”页逐根打勾，特别是 CE#、RE#、WE#（NAND）和 CS#、CLK、DO（SPI）。",
                "用 nsprog doctor --probe 在座子上逐脚碰一遍，找断线。"]),
    Symptom("bad-id", "ID 全 00 或每次不一样", "识别出奇怪的型号、每次读到的 ID 不同",
            ["wiring", "nand_id", "spi_id"], [
                "每个 VCC 脚旁加 100 nF 电容，3V3 上再加 10 µF。",
                "地线多接几根，杜邦线越短越好（10 cm 以内）。",
                "NAND 先用 --nand-timing safe，SPI 用 --spi-mhz 1。",
                "确认芯片是 3.3V 的（型号里 1.8V 版本不能直接接）。"]),
    Symptom("read-unstable", "读两遍不一致 / 校验失败", "verify 报差异、备份两次不同",
            ["read_stability", "nand_id", "spi_id"], [
                "先备份两次，用 nsprog image diff 对比：零星位翻转和整页不同是两种问题。",
                "NAND 用 --nand-timing safe；SPI 降低 --spi-mhz。",
                "MLC 芯片原始数据本来就会有位翻转，要用 ECC 检查（nsprog ecc check）。",
                "测试夹在板读时，板上的主控可能在抢总线：让主控保持复位或断开它的供电。"]),
    Symptom("write-fail", "写入 / 擦除失败", "write protected、program failed、erase failed",
            ["nand_wp", "rb", "nand_id", "spi_id"], [
                "NAND：WP# 要接（TSOP48 第 19 脚 → FPGA 69 脚），nsprog 写入时会自动释放写保护。",
                "SPI NOR：状态寄存器的块保护位（BP）要先清除；WP#/IO2 脚要为高。",
                "坏块：写入默认跳过出厂坏块；新坏块会在报告里列出。",
                "确认芯片不是 1.8V 版本。"]),
    Symptom("busy", "NAND 一直忙（R/B#）", "NAND stayed busy、超时",
            ["rb", "wiring", "nand_id"], [
                "R/B# 是开漏输出：要有 10 kΩ 上拉到 3V3。",
                "检查 TSOP48 第 7 脚 → FPGA 49 脚。",
                "临时绕过：加 --no-rb，改用状态寄存器轮询（稍慢）。"]),
    Symptom("ft232h", "FT232H 连不上 / 速度慢", "link 显示 UART、ft232h 报错",
            ["ft232h"], [
                "FT232H 的 EEPROM 要先设成 245 FIFO：nsprog ft232h-setup，然后重新插拔。",
                "macOS 需要 brew install libusb；Windows 用 Zadig 装 WinUSB 驱动。",
                "FT232H 和 Tang Nano 9K 必须共地；不要把 FT232H 的 3.3V/5V 接过来。",
                "同步 FIFO 模式要接 CLKOUT（AC5 → FPGA 36 脚），线尽量短；"
                "先跑 nsprog -p ft232h-sync ft232h-tune 调时钟相位（固件 1.3），"
                "可用窗口少于 4 档或仍不稳定就用异步模式 -p ft232h。"],
            needs_device=False),
]


def symptom(sid: str) -> Symptom:
    for s in SYMPTOMS:
        if s.id == sid:
            return s
    raise ValueError("unknown symptom %r (%s)" % (sid, ", ".join(s.id for s in SYMPTOMS)))


def diagnose(sid: str, dev: Optional[Device]) -> List[Finding]:
    """Run the automatic checks of a symptom; never raises for a failing check."""
    s = symptom(sid)
    if s.needs_device and dev is None:
        return [Finding(FAIL, "编程器", "没有连接编程器，先解决“找不到编程器”")] + CHECKS["computer"](None)
    out: List[Finding] = []
    for name in s.checks:
        try:
            out.extend(CHECKS[name](dev))
        except Exception as e:                          # a dead link etc.: report, keep going
            out.append(Finding(FAIL, name, "检查出错：%s" % e))
    if dev is not None:
        try:
            if doctor.supports_pin_test(dev):
                doctor.stop(dev)
        except Exception:
            pass
    return out


def verdict(findings: List[Finding]) -> str:
    if any(f.status == FAIL for f in findings):
        return FAIL
    return WARN if any(f.status == WARN for f in findings) else OK


def as_dict(sid: str, findings: List[Finding]) -> dict:
    return {"symptom": symptom(sid).as_dict(), "verdict": verdict(findings),
            "findings": [f.as_dict() for f in findings]}


__all__ = ["Finding", "Symptom", "SYMPTOMS", "CHECKS", "symptom", "diagnose", "verdict", "as_dict"]
