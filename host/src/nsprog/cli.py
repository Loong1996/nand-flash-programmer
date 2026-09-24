"""Command line interface: ``nsprog <command> [options]``."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

from . import __version__, chipdb, jobs
from .device import connect
from .flash import FlashDriver, FlashError, ParallelNand, SpiNor, detect, set_spi_clock

log = logging.getLogger("nsprog")


# ----------------------------------------------------------------- progress
class Bar:
    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stderr.isatty()
        self.t0 = time.monotonic()
        self.last = 0.0
        self.phase = ""

    def __call__(self, phase, done, total):
        if phase != self.phase:
            self.phase, self.t0 = phase, time.monotonic()
        now = time.monotonic()
        if not self.enabled or (now - self.last < 0.1 and done < total):
            return
        self.last = now
        frac = min(1.0, done / total) if total else 1.0
        rate = done / max(1e-3, now - self.t0)
        bar = "#" * int(frac * 30)
        sys.stderr.write("\r%-6s [%-30s] %5.1f%%  %s/s   " % (phase, bar, frac * 100, _size(rate)))
        if done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()


def _size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return str(n)


def _int(v: str) -> int:
    v = v.strip().lower()
    mult = 1
    for suf, m in (("k", 1024), ("m", 1024 ** 2), ("g", 1024 ** 3)):
        if v.endswith(suf):
            v, mult = v[:-1], m
    return int(v, 0) * mult


# ----------------------------------------------------------------- helpers
def _open(args):
    dev = connect(args.port, negotiate=not args.no_fast_uart)
    info = dev.info
    log.info("programmer: gateware %s on %s (link %s)", info.gw_version, dev.link.name,
             "FT232H" if info.port else "UART")
    return dev


def _target(args, dev) -> FlashDriver:
    want = args.target
    det = detect(dev, nand_chip=args.chip if want == "nand" else None,
                 spi_chip=args.chip if want == "spi" else None,
                 use_rb=not args.no_rb, ecc=args.ecc,
                 want=want if want in ("nand", "spi") else "all")
    for m in det.messages:
        print(m, file=sys.stderr)
    choices = [d for d in (det.nand, det.spi) if d is not None]
    if want == "nand":
        choices = [det.nand] if det.nand else []
    elif want == "spi":
        choices = [det.spi] if det.spi else []
    if not choices:
        raise SystemExit("no flash chip found (check the socket, wiring and power)")
    if len(choices) > 1:
        raise SystemExit("chips found on both buses; choose one with --target nand|spi")
    drv = choices[0]
    if drv.kind != "nand":
        mhz = set_spi_clock(dev, args.spi_mhz)
        log.info("SPI clock %.2f MHz", mhz)
    if isinstance(drv, ParallelNand):
        prof = drv.set_timing(args.nand_timing)
        log.info("NAND bus timing: %s", prof)
    if isinstance(drv, SpiNor):
        drv.quad = args.spi_quad
    print(drv.describe(), file=sys.stderr)
    return drv


def _range(args, drv):
    """--start-block/--blocks or --offset/--length -> (start, count) in blocks."""
    start, count = args.start_block, args.blocks
    if args.offset is not None or args.length is not None:
        off = _int(args.offset or "0")
        if off % drv.block_size:
            raise SystemExit("--offset must be a multiple of the erase block (%d bytes)" % drv.block_size)
        start = off // drv.block_size
        if args.length is not None:
            ln = _int(args.length)
            count = (ln + drv.block_size - 1) // drv.block_size
    return start or 0, count


def _ctx():
    return jobs.Context(progress=Bar(), message=lambda m: print(m, file=sys.stderr))


def _finish(rep: jobs.Report) -> int:
    print(rep.summary())
    return 0 if rep.ok else 1


# ----------------------------------------------------------------- commands
def cmd_info(args):
    dev = _open(args)
    i = dev.info
    print("gateware %s, protocol %d, board %d, clock %.1f MHz, rx fifo %d, link %s, flags %02X"
          % (i.gw_version, i.proto, i.board, i.clk_hz / 1e6, i.rx_fifo,
             "FT232H" if i.port else "UART", i.flags))
    if dev.link.baudrate:
        print("UART %d baud" % dev.link.baudrate)
    det = detect(dev, use_rb=not args.no_rb, ecc=args.ecc)
    for m in det.messages:
        print(m)
    for d in (det.nand, det.spi):
        if d:
            print("  " + d.describe())
            if d.voltage and d.voltage < 2.5:
                print("  WARNING: %.1fV part - needs a level-shifting adapter" % d.voltage)
    dev.close()
    return 0


def cmd_pins(args):
    """Wiring diagnostics."""
    dev = _open(args)
    p = dev.pins()
    print("NAND R/B#      : %s" % ("high (ready)" if p.rb_ready else "LOW (busy or shorted)"))
    print("NAND IO[7:0]   : %s (%02X) - with no chip inserted all bits should read 1"
          % (format(p.nand_io, "08b"), p.nand_io))
    print("SPI DO (IO1)   : %s" % ("high" if p.spi_do else "low"))
    print("FT232H CLKOUT  : %s" % ("toggling (sync FIFO mode)" if p.ft_clkout_active else
                                   "idle (async FIFO mode or not connected - normal)"))
    print("FT232H OE#/SIWU#: %s / %s (both should be high)" % (
        "high" if p.ft_oe_n else "LOW", "high" if p.ft_siwu_n else "LOW"))
    dev.close()
    return 0


_MARK = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-", "info": "·"}


def cmd_doctor(args):
    """Check link, firmware, wiring (shorts) and chips in one go."""
    from . import doctor

    dev = _open(args)
    try:
        if args.probe:
            return _probe(dev)
        checks = doctor.run(dev, drive=not args.no_drive,
                            progress=lambda s: print("… %s" % s, file=sys.stderr))
        for c in checks:
            print("%s %s：%s" % (_MARK.get(c.status, "?"), c.title, c.detail.split("\n")[0]))
            for line in c.detail.split("\n")[1:]:
                print("    " + line)
            if c.hint and c.status in ("warn", "fail", "info"):
                print("    → " + c.hint)
        print("结论：%s" % doctor.summary(checks))
        print("（断线只能从座子一侧查：nsprog doctor --probe，用接 GND/3V3 的杜邦线逐个碰座子引脚）",
              file=sys.stderr)
        return 1 if any(c.status == "fail" for c in checks) else 0
    finally:
        dev.close()


def _probe(dev):
    from . import doctor, wiring

    if not doctor.supports_pin_test(dev):
        raise SystemExit("gateware %s has no pin test; run 'nsprog fpga-flash'" % dev.info.gw_version)
    print("探针模式：所有 Flash 引脚已释放。用一根杜邦线，一端接 GND（上拉的线）或 3V3（下拉的线），")
    print("另一端逐个碰座子引脚，这里会显示是哪根线在变化。Ctrl+C 结束。")
    for s in wiring.TEST_SIGNALS:
        print("  %-10s FPGA %-3s 空闲=%s  → 用 %s 去碰" % (
            s.name, s.fpga, "高" if s.pull == "up" else "低", "GND" if s.pull == "up" else "3V3"))
    try:
        for i, lv in doctor.probe(dev):
            print("%s  %s → %s" % (time.strftime("%H:%M:%S"), wiring.describe(wiring.by_test_index(i)),
                                  "高" if lv else "低"), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        doctor.stop(dev)
    return 0


def cmd_pintest(args):
    """Drive a single flash-side pin (multimeter / LED check at the socket)."""
    from . import doctor, wiring
    from . import protocol as P

    if args.list:
        for s in wiring.TEST_SIGNALS:
            print("%-2d %-10s FPGA %-3s %-5s %s" % (s.bit, s.name, s.fpga, s.group,
                                                  "  ".join("%s:%s" % kv for kv in s.pins.items())))
        return 0
    if not args.pin:
        raise SystemExit("give --pin NAME (see --list) or --list")
    sig = wiring.find(args.pin)
    mode = {"low": P.PT_LOW, "high": P.PT_HIGH, "toggle": P.PT_TOGGLE}[args.mode]
    dev = _open(args)
    try:
        if not doctor.supports_pin_test(dev):
            raise SystemExit("gateware %s has no pin test; run 'nsprog fpga-flash'" % dev.info.gw_version)
        lv = doctor.pin_test(dev, sig.bit, mode)
        print("%s: %s（其余引脚已释放）。读回 %s。Ctrl+C 或 %d 秒后恢复。" % (
            wiring.describe(sig), {"low": "拉低", "high": "拉高", "toggle": "每秒翻转 2 次"}[args.mode],
            "高" if lv >> sig.bit & 1 else "低", args.seconds))
        try:
            time.sleep(args.seconds)
        except KeyboardInterrupt:
            pass
    finally:
        doctor.stop(dev)
        dev.close()
    return 0


def cmd_chips(args):
    q = (args.search or "").lower()
    for c in chipdb.all_chips():
        if args.type and c["type"] != args.type:
            continue
        if q and q not in c["name"].lower() and q not in c["id"].lower():
            continue
        print("%-8s %-20s ID %-12s %s" % (c["type"], c["name"], c["id"] or "-", c["detail"]))
    return 0


def cmd_read(args):
    dev = _open(args)
    drv = _target(args, dev)
    start, count = _range(args, drv)
    with open(args.file, "wb") as f:
        rep = jobs.read(drv, f, start=start, count=count, oob=not args.no_oob,
                        bb=args.bb or "keep", ctx=_ctx(), allow_1v8=args.allow_1v8)
    dev.close()
    print("saved %s" % args.file)
    return _finish(rep)


def _load(path):
    with open(path, "rb") as f:
        return f.read()


def _oob_arg(args) -> Optional[bool]:
    if args.oob:
        return True
    if args.no_oob:
        return False
    return None


def cmd_write(args):
    data = _load(args.file)
    dev = _open(args)
    drv = _target(args, dev)
    start, _ = _range(args, drv)
    if args.bb == "force" and not args.yes:
        raise SystemExit("--bb force can destroy factory bad-block markers; add --yes to confirm")
    rep = jobs.write(drv, data, start=start, oob=_oob_arg(args), bb=args.bb or "skip",
                     erase=not args.no_erase, verify=not args.no_verify, ctx=_ctx(),
                     allow_1v8=args.allow_1v8)
    dev.close()
    return _finish(rep)


def cmd_erase(args):
    dev = _open(args)
    drv = _target(args, dev)
    start, count = _range(args, drv)
    if args.bb == "force" and not args.yes:
        raise SystemExit("--bb force erases factory bad blocks too; add --yes to confirm")
    rep = jobs.erase(drv, start=start, count=count, bb=args.bb or "skip", ctx=_ctx(),
                     allow_1v8=args.allow_1v8)
    dev.close()
    return _finish(rep)


def cmd_verify(args):
    data = _load(args.file)
    dev = _open(args)
    drv = _target(args, dev)
    start, _ = _range(args, drv)
    rep = jobs.verify(drv, data, start=start, oob=_oob_arg(args), bb=args.bb or "skip", ctx=_ctx())
    dev.close()
    return _finish(rep)


def cmd_blank(args):
    dev = _open(args)
    drv = _target(args, dev)
    start, count = _range(args, drv)
    rep = jobs.blank_check(drv, start=start, count=count, ctx=_ctx())
    dev.close()
    return _finish(rep)


def cmd_badblocks(args):
    dev = _open(args)
    drv = _target(args, dev)
    rep = jobs.bad_block_report(drv, ctx=_ctx())
    dev.close()
    print("%d bad blocks of %d" % (len(rep.bad_blocks), drv.blocks))
    return _finish(rep)


def cmd_web(args):
    from .web.app import running_instance, serve

    url = running_instance(args.web_port)
    if url:                                   # already running (e.g. the app was opened twice)
        print("nsprog web UI is already running: %s" % url)
        if not args.no_browser:
            import webbrowser
            webbrowser.open(url)
        return 0
    serve(host=args.host, port=args.web_port, open_browser=not args.no_browser,
          default_port=args.port)
    return 0


def bitstream_path() -> str:
    here = os.path.dirname(__file__)
    return os.path.join(here, "bitstream", "nsprog_tangnano9k.fs")


def cmd_fpga_flash(args):
    fs = args.file or bitstream_path()
    if not os.path.exists(fs):
        raise SystemExit("bitstream not found: %s" % fs)
    tool = shutil.which("openFPGALoader")
    if not tool:
        raise SystemExit("openFPGALoader not found. Install it first:\n"
                         "  macOS : brew install openfpgaloader\n"
                         "  Linux : apt install openfpgaloader (or build from source)\n"
                         "  Windows: https://github.com/trabucayre/openFPGALoader/releases")
    cmd = [tool, "-b", "tangnano9k"]
    if not args.sram:
        cmd.append("-f")
    cmd.append(fs)
    print(" ".join(cmd))
    return subprocess.call(cmd)


def cmd_ft232h_setup(args):
    from .ft232h import setup_eeprom

    setup_eeprom(url=args.url, custom_pid=args.custom_pid, dry_run=args.dry_run)
    return 0


# ----------------------------------------------------------------- offline tools
def _geometry(args):
    from .image import Geometry

    if getattr(args, "chip", None):
        g = Geometry.from_chip(args.chip)
        if args.ppb:
            g.ppb = args.ppb
        return g
    if not args.page:
        raise SystemExit("give --chip NAME or --page/--oob (and --ppb)")
    return Geometry(args.page, args.oob or 0, args.ppb or 64)


def _ecc_layout(args):
    from .ecc import layout

    return layout(args.ecc, args.ecc_offset)


def cmd_image(args):
    from . import image

    geo = _geometry(args)
    if args.action == "info":
        with open(args.src, "rb") as f:
            print(image.info(f, geo, raw=not args.main, bb_off=args.bb_off).summary())
        return 0
    if args.action == "strip":
        with open(args.src, "rb") as f, open(args.dst, "wb") as out:
            n = image.strip_oob(f, out, geo, skip_bad=args.skip_bad, bb_off=args.bb_off)
        print("wrote %d pages (main area only) to %s" % (n, args.dst))
        return 0
    if args.action == "split":
        with open(args.src, "rb") as f, open(args.dst, "wb") as m, open(args.oob_file, "wb") as o:
            n = image.split(f, m, o, geo)
        print("split %d pages -> %s + %s" % (n, args.dst, args.oob_file))
        return 0
    if args.action == "merge":
        lay = _ecc_layout(args) if args.ecc else None
        with open(args.src, "rb") as m, open(args.dst, "wb") as out:
            oob_in = open(args.oob_file, "rb") if args.oob_file else None
            try:
                n = image.merge(m, oob_in, out, geo, lay)
            finally:
                if oob_in:
                    oob_in.close()
        print("built %d raw pages%s -> %s" % (n, (" with %s ECC" % lay.describe()) if lay else "",
                                                args.dst))
        return 0
    if args.action == "diff":
        import json

        from .diff import diff_files

        if args.main:
            geo.oob = 0
        rep = diff_files(args.src, args.other, geo, flip_bits=args.flip_bits, keep=max(args.list, 1000))
        print(rep.summary())
        for d in rep.diffs[:args.list]:
            print("  page %7d (block %5d): %-7s main %4d B, OOB %3d B, bits 1->0 %d, 0->1 %d, first @%d"
                  % (d.page, d.page // geo.ppb, d.kind, d.main_bytes, d.oob_bytes, d.bits_10, d.bits_01,
                     d.first))
        if len(rep.diffs) > args.list:
            print("  ... (%d more; --list N shows more)" % (rep.diff_pages - args.list))
        if args.json:
            with open(args.json, "w") as f:
                json.dump({"summary": rep.summary(), "identical": rep.identical, "kinds": rep.kinds,
                           "blocks": rep.block_rows(), "pages": [d.as_dict() for d in rep.diffs]},
                          f, indent=1)
        return 0 if rep.identical else 1
    if args.action == "scan":
        import json

        from .scan import scan_file

        srep = scan_file(args.src, geo.page, 0 if args.main else geo.oob, geo.ppb)
        print(srep.summary())
        if args.env and srep.env:
            print("U-Boot environment:")
            for k, v in srep.env.items():
                print("  %s=%s" % (k, v))
        if args.json:
            with open(args.json, "w") as f:
                json.dump({"findings": [x.as_dict() for x in srep.findings],
                           "partitions": [x.as_dict() for x in srep.partitions], "env": srep.env},
                          f, indent=1)
        return 0
    raise SystemExit("unknown action")


def cmd_ecc(args):
    from . import image

    geo = _geometry(args)
    lay = _ecc_layout(args)
    print("layout: %s, ECC at OOB offset %s" % (lay.describe(),
          "end" if lay.ecc_offset is None else lay.ecc_offset))
    out = open(args.dst, "wb") if args.action == "fix" else None
    try:
        with open(args.src, "rb") as f:
            rep = image.ecc_check(f, geo, lay, out, strip=args.strip)
    finally:
        if out:
            out.close()
    print(rep.summary())
    if out:
        print("corrected image written to %s" % args.dst)
    return 0 if rep.ok else 1


def cmd_ubi(args):
    from . import ubi

    with open(args.src, "rb") as f:
        img = ubi.parse(f, args.peb)
        print(img.summary())
        if args.action == "extract":
            for path in ubi.extract(f, img, args.outdir):
                print("  %s  (%s)" % (path, ubi.detect_content(path)))
    return 0


def _main_area(args) -> bytes:
    """The image as main-area bytes (a raw image is stripped of its OOB first)."""
    import io

    if getattr(args, "oob", None) and args.page:
        from .image import Geometry, strip_oob

        out = io.BytesIO()
        with open(args.src, "rb") as f:
            strip_oob(f, out, Geometry(args.page, args.oob, args.ppb or 64))
        return out.getvalue()
    if getattr(args, "chip", None):
        from .image import Geometry, strip_oob

        g = Geometry.from_chip(args.chip)
        size = os.path.getsize(args.src)
        if size % (g.raw * g.ppb) == 0 and size % (g.page * g.ppb) != 0:
            out = io.BytesIO()
            with open(args.src, "rb") as f:
                strip_oob(f, out, g)
            return out.getvalue()
    with open(args.src, "rb") as f:
        return f.read()


def cmd_fs(args):
    from . import fs

    data = _main_area(args)
    if args.offset is not None:
        found = [fs.Found(args.offset, None, fs.open_fs(data, args.offset))]
    else:
        found = fs.find_all(data)
        if not found:
            raise SystemExit("no SquashFS / JFFS2 / UBIFS / UBI found "
                             "(raw image? give --chip or --page/--oob)")
    for f in found:
        if f.fs is None:
            print("%s: cannot open (%s)" % (f.label, f.error))
            continue
        print("%s  %s" % (f.label, ", ".join("%s=%s" % kv for kv in f.fs.info().items())))
        if args.action == "list":
            for e in f.fs.entries():
                print("  %s %10s  %s%s" % (e.mode_str, e.size if e.kind == "file" else "", e.path,
                                            " -> " + e.target if e.target else ""))
        else:
            dst = os.path.join(args.outdir, f.label) if len(found) > 1 or args.offset is None else args.outdir
            c = f.fs.extract(dst)
            print("  -> %s: %d files, %d directories, %d symlinks%s%s" % (
                dst, c["files"], c["dirs"], c["symlinks"],
                ", %d skipped (device nodes etc.)" % c["skipped"] if c["skipped"] else "",
                ", %d errors" % c["errors"] if c["errors"] else ""))
    return 0


def cmd_selftest(args):
    """Link stress test: echo patterns and a large loop of NOPs."""
    import random

    from . import protocol as P

    dev = _open(args)
    t0 = time.monotonic()
    total = 0
    rng = random.Random(1)
    for _ in range(args.rounds):
        b = P.Batch()
        vals = [rng.randrange(256) for _ in range(1000)]
        rs = [b.echo(v) for v in vals]
        x = b.spi_xfer(bytes(rng.randrange(256) for _ in range(4000)))
        dev.run(b)
        if [r.value for r in rs] != vals:
            raise SystemExit("echo mismatch - link unreliable")
        total += 2000 + 8000 + len(x.value)
    dt = time.monotonic() - t0
    print("link OK: %d rounds, %s in %.2f s (%s/s)" % (args.rounds, _size(total), dt, _size(total / dt)))
    dev.close()
    return 0


# ----------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nsprog", description="NAND / SPI flash programmer (Tang Nano 9K FPGA)")
    p.add_argument("--version", action="version", version="nsprog " + __version__)
    p.add_argument("-p", "--port", help="serial port, 'ft232h' (async FIFO), 'ft232h-sync' "
                                        "(sync FIFO), 'ftdi://...', or 'emu[:nand|spinor|spinand|w29n02kv]' "
                                        "(default: auto-detect)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--no-fast-uart", action="store_true", help="stay at 115200 baud on the UART link")
    sub = p.add_subparsers(dest="cmd", required=True)

    def chip_opts(sp, file_arg=None, bb_default=None):
        if file_arg:
            sp.add_argument("file", help=file_arg)
        sp.add_argument("-t", "--target", choices=["nand", "spi"],
                        help="bus to use (auto if only one chip found)")
        sp.add_argument("-c", "--chip", help="force a chip database entry (see 'nsprog chips')")
        sp.add_argument("--start-block", type=int, default=0, help="first erase block")
        sp.add_argument("--blocks", type=int, help="number of blocks (default: to the end / image size)")
        sp.add_argument("--offset", help="byte offset (alternative to --start-block, e.g. 0x10000, 64k)")
        sp.add_argument("--length", help="byte length (alternative to --blocks)")
        sp.add_argument("--bb", choices=jobs.BB_MODES,
                        help="bad-block handling (default: %s)" % (bb_default or "skip"))
        sp.add_argument("--no-rb", action="store_true", help="do not use R/B#; poll the status register")
        sp.add_argument("--ecc", action="store_true", help="SPI NAND: enable on-die ECC (default: raw)")
        sp.add_argument("--spi-mhz", type=float, default=6.75, help="SPI clock (default 6.75 MHz, max 13.5)")
        sp.add_argument("--spi-quad", choices=["off", "auto", "on"], default="auto",
                        help="SPI NOR 1-1-4 quad read: auto = only if QE is already set, "
                             "on = set QE for the read and restore it (default auto)")
        sp.add_argument("--nand-timing", choices=["safe", "medium", "fast", "auto"], default="safe",
                        help="parallel NAND bus timing; faster needs short wires (default safe)")
        sp.add_argument("--allow-1v8", action="store_true",
                        help="allow 1.8V parts (only with a level shifter)")
        sp.add_argument("-y", "--yes", action="store_true", help="confirm dangerous options")

    sp = sub.add_parser("info", help="show programmer and detected chips")
    sp.add_argument("--no-rb", action="store_true")
    sp.add_argument("--ecc", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("pins", help="wiring diagnostics (R/B#, NAND data bus levels)")
    sp.set_defaults(func=cmd_pins)

    sp = sub.add_parser("doctor", help="check link, firmware, wiring shorts and chips in one go")
    sp.add_argument("--no-drive", action="store_true",
                    help="only read levels; do not drive any pin (no short test)")
    sp.add_argument("--probe", action="store_true",
                    help="interactive: touch socket pins with a GND/3V3 lead, see which line reacts")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pintest", help="drive one flash-side pin to check it at the socket")
    sp.add_argument("--list", action="store_true", help="list the testable pins")
    sp.add_argument("--pin", help="pin name, e.g. CE#, IO3, spi:io0, fpga74")
    sp.add_argument("--mode", choices=["toggle", "low", "high"], default="toggle")
    sp.add_argument("--seconds", type=float, default=30.0, help="how long to hold (default 30)")
    sp.set_defaults(func=cmd_pintest)

    sp = sub.add_parser("chips", help="list the chip database")
    sp.add_argument("search", nargs="?")
    sp.add_argument("--type", choices=["nand", "spinor", "spinand"])
    sp.set_defaults(func=cmd_chips)

    sp = sub.add_parser("read", help="read the chip into a file")
    chip_opts(sp, "output file", "keep")
    sp.add_argument("--no-oob", action="store_true", help="NAND: main area only (default: page+spare)")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("write", help="erase, program and verify from a file")
    chip_opts(sp, "image file")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--oob", action="store_true", help="NAND: image contains page+spare (default: auto)")
    g.add_argument("--no-oob", action="store_true", help="NAND: image contains main area only")
    sp.add_argument("--no-erase", action="store_true")
    sp.add_argument("--no-verify", action="store_true")
    sp.set_defaults(func=cmd_write)

    sp = sub.add_parser("erase", help="erase blocks (skips factory bad blocks)")
    chip_opts(sp)
    sp.set_defaults(func=cmd_erase)

    sp = sub.add_parser("verify", help="compare the chip with a file")
    chip_opts(sp, "image file")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--oob", action="store_true")
    g.add_argument("--no-oob", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("blank", help="check that the chip is erased")
    chip_opts(sp)
    sp.set_defaults(func=cmd_blank)

    sp = sub.add_parser("badblocks", help="scan factory bad-block markers")
    chip_opts(sp)
    sp.set_defaults(func=cmd_badblocks)

    sp = sub.add_parser("web", help="start the local web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--web-port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("fpga-flash", help="program the Tang Nano 9K with the bundled bitstream")
    sp.add_argument("--file", help="bitstream (.fs) to use instead of the bundled one")
    sp.add_argument("--sram", action="store_true", help="load into SRAM only (lost on power-off)")
    sp.set_defaults(func=cmd_fpga_flash)

    sp = sub.add_parser("ft232h-setup", help="configure an FT232H EEPROM for 245 FIFO mode")
    sp.add_argument("--url", help="pyftdi URL (default: first FT232H)")
    sp.add_argument("--custom-pid", action="store_true",
                    help="also change the USB PID so the macOS FTDI serial driver leaves it alone")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_ft232h_setup)

    def geo_opts(sp):
        sp.add_argument("-c", "--chip", help="take page/OOB/pages-per-block from a chip database entry")
        sp.add_argument("--page", type=int, help="page size (main area)")
        sp.add_argument("--oob", type=int, help="spare (OOB) size per page")
        sp.add_argument("--ppb", type=int, help="pages per block (default 64)")
        sp.add_argument("--bb-off", type=int, default=0, help="bad-block marker offset in the OOB")

    def ecc_opts(sp, required=True):
        sp.add_argument("--ecc", required=required,
                        help="hamming256 | hamming512 | bch4 | bch8 | bch16 | "
                             "bch:<t>:<step> | hamming:<step>")
        sp.add_argument("--ecc-offset", type=int,
                        help="ECC start inside the OOB (default: packed at the end, Linux layout)")

    sp = sub.add_parser("image", help="offline image tools (info / strip / split / merge / diff / scan)")
    isub = sp.add_subparsers(dest="action", required=True)
    x = isub.add_parser("info", help="page / block / bad-block / content summary")
    x.add_argument("src")
    x.add_argument("--main", action="store_true", help="image has no OOB")
    geo_opts(x)
    x = isub.add_parser("strip", help="raw (page+OOB) -> main area only")
    x.add_argument("src")
    x.add_argument("dst")
    x.add_argument("--skip-bad", action="store_true", help="drop blocks that carry a bad-block marker")
    geo_opts(x)
    x = isub.add_parser("split", help="raw -> main file + OOB file")
    x.add_argument("src")
    x.add_argument("dst")
    x.add_argument("oob_file")
    geo_opts(x)
    x = isub.add_parser("merge", help="main (+ OOB file) -> raw, optionally computing ECC")
    x.add_argument("src")
    x.add_argument("dst")
    x.add_argument("--oob-file")
    geo_opts(x)
    ecc_opts(x, required=False)
    x = isub.add_parser("diff", help="compare two images page by page (bit flips vs. real changes)")
    x.add_argument("src", help="image A")
    x.add_argument("other", help="image B")
    x.add_argument("--main", action="store_true", help="images have no OOB")
    x.add_argument("--flip-bits", type=int, default=4,
                   help="at most this many differing bits per 512 B counts as a bit flip (default 4)")
    x.add_argument("--list", type=int, default=20, help="list the first N differing pages (default 20)")
    x.add_argument("--json", help="write the full report as JSON")
    geo_opts(x)
    x = isub.add_parser("scan", help="find partitions (mtdparts, device tree), U-Boot env, uImage/FIT, "
                                     "file systems")
    x.add_argument("src")
    x.add_argument("--main", action="store_true", help="image has no OOB")
    x.add_argument("--env", action="store_true", help="print every U-Boot environment variable")
    x.add_argument("--json", help="write the report as JSON")
    geo_opts(x)
    for x in isub.choices.values():
        x.set_defaults(func=cmd_image)

    sp = sub.add_parser("ecc", help="check / correct ECC in a raw image")
    esub = sp.add_subparsers(dest="action", required=True)
    x = esub.add_parser("check", help="count clean / corrected / uncorrectable ECC steps")
    x.add_argument("src")
    geo_opts(x)
    ecc_opts(x)
    x.set_defaults(func=cmd_ecc, dst=None, strip=False)
    x = esub.add_parser("fix", help="write a corrected image")
    x.add_argument("src")
    x.add_argument("dst")
    x.add_argument("--strip", action="store_true", help="write the main area only")
    geo_opts(x)
    ecc_opts(x)
    x.set_defaults(func=cmd_ecc)

    sp = sub.add_parser("ubi", help="inspect / extract UBI images (main-area images)")
    usub = sp.add_subparsers(dest="action", required=True)
    x = usub.add_parser("info")
    x.add_argument("src")
    x.add_argument("--peb", type=int, help="physical eraseblock size (default: auto)")
    x.set_defaults(func=cmd_ubi)
    x = usub.add_parser("extract")
    x.add_argument("src")
    x.add_argument("outdir")
    x.add_argument("--peb", type=int)
    x.set_defaults(func=cmd_ubi)

    sp = sub.add_parser("fs", help="list / extract SquashFS, JFFS2, UBIFS (also inside UBI) from an image")
    fsub = sp.add_subparsers(dest="action", required=True)
    for name, hlp in (("list", "list the files of every file system found"),
                      ("extract", "extract every file system found into OUTDIR/<offset>-<type>/")):
        x = fsub.add_parser(name, help=hlp)
        x.add_argument("src")
        if name == "extract":
            x.add_argument("outdir")
        x.add_argument("--offset", type=lambda v: int(v, 0),
                       help="only the file system starting at this main-area offset")
        geo_opts(x)
        x.set_defaults(func=cmd_fs)

    sp = sub.add_parser("selftest", help="link stress test")
    sp.add_argument("--rounds", type=int, default=20)
    sp.set_defaults(func=cmd_selftest)
    return p


def _safe_output() -> None:
    """Never crash on ✓ / Chinese text when the console or pipe uses a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            "✓中".encode(enc)
        except (UnicodeEncodeError, LookupError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass


def main(argv=None) -> int:
    _safe_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * args.verbose,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        logging.getLogger("nsprog").setLevel(logging.INFO if args.verbose == 1 else logging.DEBUG)
    try:
        return args.func(args) or 0
    except (FlashError, jobs.Cancelled, OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
