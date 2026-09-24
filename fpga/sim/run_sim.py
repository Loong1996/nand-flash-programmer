#!/usr/bin/env python3
"""Build and run the RTL simulation (Icarus Verilog + cocotb).

    python fpga/sim/run_sim.py                 # both variants, all tests
    python fpga/sim/run_sim.py nor             # SPI NOR model on the SPI bus
    python fpga/sim/run_sim.py nand            # SPI NAND model on the SPI bus
    python fpga/sim/run_sim.py nor ft_spi_nor  # selected test(s)
"""

import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
RTL = HERE.parent / "rtl"


def run(variant: str, tests=None) -> None:
    sources = sorted(RTL.glob("*.v")) + sorted((HERE / "models").glob("*.v")) + [HERE / "tb.v"]
    defines = {"SPI_NAND": 1} if variant == "nand" else {}
    build_dir = HERE / "sim_build" / variant
    env = {"NSPROG_SIM_SPI": variant, "PYTHONPATH": str(HERE)}
    if tests:
        env["COCOTB_TEST_FILTER"] = "|".join(tests)
    runner = get_runner("icarus")
    runner.build(sources=sources, hdl_toplevel="tb", defines=defines, build_dir=build_dir,
                 always=True, timescale=("1ns", "1ps"))
    xml = runner.test(hdl_toplevel="tb", test_module="test_rtl", build_dir=build_dir,
                      test_dir=HERE, extra_env=env, results_xml=str(build_dir / "results.xml"))
    text = Path(xml).read_text()
    if "<failure" in text or "<error" in text:
        raise SystemExit("simulation tests failed (%s)" % variant)


if __name__ == "__main__":
    variants = [a for a in sys.argv[1:] if a in ("nor", "nand")] or ["nor", "nand"]
    tests = [a for a in sys.argv[1:] if a not in ("nor", "nand")] or None
    for v in variants:
        run(v, tests)
    print("RTL simulation passed:", ", ".join(variants))
