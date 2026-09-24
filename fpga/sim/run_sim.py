#!/usr/bin/env python3
"""Build and run the RTL simulation (Icarus Verilog + cocotb).

    python fpga/sim/run_sim.py                 # SPI NOR and SPI NAND variants, all tests
    python fpga/sim/run_sim.py nor             # SPI NOR model on the SPI bus
    python fpga/sim/run_sim.py nand            # SPI NAND model on the SPI bus
    python fpga/sim/run_sim.py nor ft_spi_nor  # selected test(s)
    python fpga/sim/run_sim.py gate            # gate-level: the yosys netlist instead of the RTL
    python fpga/sim/run_sim.py gate ft_spi_nor # (default gate tests: GATE_TESTS below)

The gate-level variant synthesises fpga/rtl with yosys (synth_gowin, the same
flow as fpga/build.py), writes the netlist as Verilog and simulates it with the
Gowin cell library, so synthesis/simulation mismatches (e.g. a tri-state that
does not become an IOBUF) show up before the bitstream is ever loaded.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
FPGA = HERE.parent
RTL = FPGA / "rtl"
VARIANTS = ("nor", "nand", "gate")

#: gate-level simulation is ~10x slower; these cover every block of the design
GATE_TESTS = ["ft_info_echo", "ft_pin_test_doctor", "ft_spi_nor_quad",
              "ft_parallel_nand_no_rb", "ft_sync_fifo_nand_fast", "uart_baud_and_nand_id"]


def yosys_share() -> Path:
    exe = shutil.which("yosys")
    if exe:
        out = subprocess.run(["yosys-config", "--datdir"], capture_output=True, text=True)
        if out.returncode == 0:
            return Path(out.stdout.strip())
    import yowasp_yosys
    return Path(yowasp_yosys.__file__).parent / "share"


def netlist() -> Path:
    """Synthesise the design (as fpga/build.py does) and return the Verilog netlist."""
    out = FPGA / "build" / "gate"
    out.mkdir(parents=True, exist_ok=True)
    yosys = shutil.which("yosys") or shutil.which("yowasp-yosys")
    if not yosys:
        raise SystemExit("yosys not found: pip install yowasp-yosys")
    srcs = " ".join("rtl/%s" % p.name for p in sorted(RTL.glob("*.v")))
    # yowasp tools only see the current directory tree, so run from fpga/
    subprocess.check_call([yosys, "-q", "-l", "build/gate/yosys.log", "-p",
                           "read_verilog %s; synth_gowin -top top; "
                           "write_verilog -noattr build/gate/netlist.v" % srcs], cwd=FPGA)
    return out / "netlist.v"


def cell_library() -> Path:
    """yosys' Gowin cells_sim.v, with its IOBUF bug fixed.

    The upstream model reads ``assign I = IO;`` (it drives its own input and
    never its O output), so every bidirectional pin would read Z in a
    gate-level simulation.
    """
    src = (yosys_share() / "gowin" / "cells_sim.v").read_text()
    bad = "  assign IO = OEN ? 1'bz : I;\n  assign I = IO;"
    if bad in src:
        src = src.replace(bad, "  assign IO = OEN ? 1'bz : I;\n  assign O = IO;")
    dst = FPGA / "build" / "gate" / "cells_sim_fixed.v"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src)
    return dst


def run(variant: str, tests=None) -> None:
    models = sorted((HERE / "models").glob("*.v"))
    if variant == "gate":
        sources = [netlist(), cell_library(), HERE / "gowin_bram_sim.v"]
        defines = {"GATE_SIM": 1}
        tests = tests or GATE_TESTS
    else:
        sources = sorted(RTL.glob("*.v"))
        defines = {"SPI_NAND": 1} if variant == "nand" else {}
    sources += models + [HERE / "tb.v"]
    build_dir = HERE / "sim_build" / variant
    env = {"NSPROG_SIM_SPI": "nand" if variant == "nand" else "nor", "PYTHONPATH": str(HERE),
           "NSPROG_SIM_GATE": "1" if variant == "gate" else ""}
    if tests:
        env["COCOTB_TEST_FILTER"] = "|".join(r"\.%s$" % t for t in tests)
    runner = get_runner("icarus")
    runner.build(sources=sources, hdl_toplevel="tb", defines=defines, build_dir=build_dir,
                 always=True, timescale=("1ns", "1ps"))
    xml = runner.test(hdl_toplevel="tb", test_module="test_rtl", build_dir=build_dir,
                      test_dir=HERE, extra_env=env, results_xml=str(build_dir / "results.xml"))
    text = Path(xml).read_text()
    if "<failure" in text or "<error" in text:
        raise SystemExit("simulation tests failed (%s)" % variant)


if __name__ == "__main__":
    variants = [a for a in sys.argv[1:] if a in VARIANTS] or ["nor", "nand"]
    tests = [a for a in sys.argv[1:] if a not in VARIANTS] or None
    for v in variants:
        run(v, tests)
    print("simulation passed:", ", ".join(variants))
