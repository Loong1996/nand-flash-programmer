#!/usr/bin/env python3
"""Build the Tang Nano 9K bitstream with the open-source toolchain.

Works on macOS / Linux / Windows. Tools are taken from PATH (oss-cad-suite)
or, if missing, from the pip packages:

    pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula

    python fpga/build.py            # -> fpga/build/nsprog_tangnano9k.fs
    python fpga/build.py --install  # also copy into host/src/nsprog/bitstream/
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RTL = ["common.v", "uart.v", "ft245.v", "spi_master.v", "nand_bus.v", "engine.v", "top_tangnano9k.v"]
DEVICE = "GW1NR-LV9QN88PC6/I5"
FAMILY = "GW1N-9C"
#: required fmax per clock net: 27 MHz crystal, 60 MHz FT232H CLKOUT (sync FIFO)
TARGETS = {"clk": 54.0, "fclk": 60.0}
#: placement seeds tried in order; the 60 MHz domain has little slack, so an
#: unrelated change can move one seed just below target
SEEDS = [1, 2, 3, 4, 5, 6, 7, 8]


def tool(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise SystemExit("none of %s found; pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula"
                     % ", ".join(names))


def run(cmd):
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call([str(c) for c in cmd])


def place_and_route(nextpnr, build, seed):
    """Run nextpnr with ``seed``; return the list of missed timing targets."""
    try:
        run([nextpnr, "--json", build / "top.json", "--write", build / "pnr.json",
             "--device", DEVICE, "--vopt", "family=%s" % FAMILY,
             "--vopt", "cst=%s" % (HERE / "constraints" / "tangnano9k.cst"),
             "--sdc", HERE / "constraints" / "tangnano9k.sdc",
             "--seed", str(seed), "--log", build / "pnr.log"])
    except subprocess.CalledProcessError:
        pass                                    # nextpnr fails on missed timing; the log says why
    # The last report (after routing) wins for every clock.
    log = (build / "pnr.log").read_text()
    fmax = {}
    for name, mhz in re.findall(r"Max frequency for clock +'([^']+)': ([0-9.]+) MHz", log):
        fmax[name] = float(mhz)
    bad = []
    for clk, need in TARGETS.items():
        got = [v for k, v in fmax.items() if k.strip() == clk]
        if not got:
            bad.append("%s: no timing report" % clk)
        elif min(got) < need:
            bad.append("%s: %.1f MHz < %.0f MHz" % (clk, min(got), need))
        else:
            print("timing: %s %.1f MHz (target %.0f MHz, seed %d)" % (clk, min(got), need, seed))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true", help="copy the result into the host package")
    ap.add_argument("--seed", type=int, default=None,
                    help="placement seed (default: try seeds %s until timing is met)" % SEEDS)
    args = ap.parse_args()

    build = HERE / "build"
    build.mkdir(exist_ok=True)
    yosys = tool("yosys", "yowasp-yosys")
    nextpnr = tool("nextpnr-himbaechel", "yowasp-nextpnr-himbaechel-gowin")
    pack = tool("gowin_pack")

    srcs = " ".join(str(HERE / "rtl" / f) for f in RTL)
    run([yosys, "-q", "-l", build / "yosys.log", "-p",
         "read_verilog %s; synth_gowin -top top -json %s" % (srcs, build / "top.json")])
    for seed in [args.seed] if args.seed is not None else SEEDS:
        bad = place_and_route(nextpnr, build, seed)
        if not bad:
            break
        print("seed %d: timing not met (%s)" % (seed, "; ".join(bad)), flush=True)
    else:
        raise SystemExit("timing not met: " + "; ".join(bad))

    out = build / "nsprog_tangnano9k.fs"
    run([pack, "-d", FAMILY, "--sspi_as_gpio", "--mspi_as_gpio", "--cpu_as_gpio",
         "-o", out, build / "pnr.json"])
    print("bitstream: %s" % out)
    if args.install:
        dst = ROOT / "host" / "src" / "nsprog" / "bitstream" / out.name
        shutil.copyfile(out, dst)
        print("installed: %s" % dst)


if __name__ == "__main__":
    sys.exit(main())
