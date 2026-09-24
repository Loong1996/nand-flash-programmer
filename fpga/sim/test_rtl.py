"""End-to-end RTL tests: real host drivers -> simulated FPGA -> flash models.

Build variants (see run_sim.py): default = SPI NOR model on the SPI bus,
``SPI_NAND`` define = SPI NAND model.
"""

import io
import os
import random

import cocotb
from cocotb.triggers import Timer
from cocotb.utils import get_sim_time

from cocotb._bridge import resume
from simhost import (FtModel, FtSyncModel, SimDevice, SimFtLink, SimUartLink, UartModel,
                     bridge, start)

from nsprog import jobs  # noqa: E402  (path set up by simhost)
from nsprog import protocol as P  # noqa: E402
from nsprog.flash import SpiNand, SpiNor, detect, set_spi_clock  # noqa: E402

SPI_NAND = os.environ.get("NSPROG_SIM_SPI") == "nand"
W29N02KV = os.environ.get("NSPROG_SIM_NAND") == "w29n02kv"


def rnd(n, seed):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(n))


def raw_image(drv, nblocks, seed):
    """Random raw image whose bad-block marker bytes stay FF."""
    img = bytearray(rnd(nblocks * drv.pages_per_block * drv.raw_page, seed))
    bb_off = getattr(drv.chip, "bb_mark_off", 0)
    for blk in range(nblocks):
        for pg in (0, 1):
            img[(blk * drv.pages_per_block + pg) * drv.raw_page + drv.page_size + bb_off] = 0xFF
    return bytes(img)


def check_models(dut):
    assert int(dut.u_nand.violations.value) == 0, "NAND timing violations"
    assert int(dut.u_spi.violations.value) == 0, "SPI timing/protocol violations"


def ft_device(dut, **kw):
    ft = FtModel(dut, **kw)
    return ft, SimDevice(SimFtLink(ft))


def ft_sync_device(dut, **kw):
    ft = FtSyncModel(dut, **kw)
    return ft, SimDevice(SimFtLink(ft))


def check_ft(dut, ft):
    assert ft.errors == 0
    assert int(dut.ft_contention.value) == 0, "FT232H data bus contention"


# ----------------------------------------------------------------------------
@cocotb.test(skip=SPI_NAND)
async def ft_info_echo(dut):
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        info = dev.open(negotiate=False)
        assert info.proto == 1 and info.rx_fifo == 4096 and info.clk_hz == 27_000_000
        assert info.port == 1
        b = P.Batch()
        rs = [b.echo(i) for i in range(64)]
        pins = b.get_pins()
        dev.run(b)
        assert [r.value for r in rs] == list(range(64))
        assert pins.value.rb_ready
        assert pins.value.ft_oe_n and pins.value.ft_siwu_n and not pins.value.ft_clkout_active
    await bridge(host)()
    assert ft.errors == 0


@cocotb.test(skip=SPI_NAND)
async def ft_parallel_nand(dut):
    await start(dut)
    # Long host stalls fill the 4 KiB TX FIFO (exercises FIFO-full handling).
    ft, dev = ft_device(dut, stall_every=5000, stall_ns=1_500_000)

    def host():
        dev.open(negotiate=False)
        det = detect(dev, want="nand")
        drv = det.nand
        assert drv is not None, det.messages
        assert drv.chip.source == "onfi" and drv.blocks == 8 and drv.pages_per_block == 8
        assert drv.bad_blocks([0, 3])[3] and not drv.bad_blocks([0])[0]
        image = raw_image(drv, 2, seed=11)
        rep = jobs.write(drv, image, start=2, bb="skip")
        assert rep.ok, rep.summary()
        assert rep.bad_blocks == [3]
        out = io.BytesIO()
        jobs.read(drv, out, start=2, count=3, bb="skip")
        assert out.getvalue() == image
        assert jobs.verify(drv, image, start=2).ok
        assert jobs.erase(drv, start=2, count=3).ok
        assert jobs.blank_check(drv, start=2, count=3).ok
        # write protection is back on
        assert not dev.pin_ctrl & P.PIN_NAND_WP_HIGH
    await bridge(host)()
    assert int(dut.u_nand.programs.value) == 16 and int(dut.u_nand.erases.value) >= 2
    assert int(dut.nand_wp_n.value) == 0
    assert ft.errors == 0
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def ft_parallel_nand_no_rb(dut):
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        dev.open(negotiate=False)
        drv = detect(dev, want="nand", use_rb=False).nand
        image = raw_image(drv, 1, seed=5)
        assert jobs.write(drv, image, start=0).ok
        out = io.BytesIO()
        jobs.read(drv, out, start=0, count=1)
        assert out.getvalue() == image
    await bridge(host)()
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def ft_spi_nor(dut):
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        dev.open(negotiate=False)
        det = detect(dev, want="spi")
        drv = det.spi
        assert isinstance(drv, SpiNor) and drv.name == "W25Q16JV", det.messages
        assert set_spi_clock(dev, 13.5) == 13.5          # fastest SPI clock
        assert drv.size == 2 * 1024 * 1024
        image = rnd(9000, seed=3)
        rep = jobs.write(drv, image, start=1)
        assert rep.ok, rep.summary()
        assert drv.read(4096, len(image)) == image
        assert jobs.verify(drv, image, start=1).ok
        assert jobs.erase(drv, start=1, count=3).ok
        assert jobs.blank_check(drv, start=1, count=3).ok
    await bridge(host)()
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def uart_baud_and_nand_id(dut):
    await start(dut)
    uart = UartModel(dut)

    def host():
        dev = SimDevice(SimUartLink(uart))
        info = dev.open(negotiate=True)
        assert info.port == 0
        assert uart.baud == 3_000_000, "baud negotiation failed"
        det = detect(dev, want="nand")
        assert det.nand is not None and det.nand_id[:2] == bytes([0x2C, 0xF1])
        out = io.BytesIO()
        jobs.read(det.nand, out, start=7, count=1, oob=False)   # block no other test touches
        assert out.getvalue() == b"\xff" * det.nand.block_size
        dev.close()
    await bridge(host)()
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def ft_sync_fifo_nand_fast(dut):
    """245 sync FIFO link (60 MHz CLKOUT), fastest NAND timings, TX stalls."""
    await start(dut)
    ft, dev = ft_sync_device(dut, stall_every=3000, stall_cycles=400, rx_gap_every=700)
    await Timer(40, "us")                   # CLKOUT detection + bridge reset release

    def host():
        info = dev.open(negotiate=False)
        assert info.port == 1 and info.caps & P.CAP_QSPI and info.caps & P.CAP_SYNC245
        pins = dev.pins()
        assert pins.ft_clkout_active and pins.port_ft
        b = P.Batch()
        rs = [b.echo(i & 0xFF) for i in range(3000)]      # longer than one read burst
        dev.run(b)
        assert [r.value for r in rs] == [i & 0xFF for i in range(3000)]
        drv = detect(dev, want="nand").nand
        drv.set_timing("fast")
        image = raw_image(drv, 2, seed=21)
        assert jobs.write(drv, image, start=4, bb="skip").ok
        out = io.BytesIO()
        t0 = get_sim_time("ns")
        jobs.read(drv, out, start=4, count=2)
        dt = get_sim_time("ns") - t0
        assert out.getvalue() == image
        dut._log.info("sync FIFO NAND read (fast timing): %d bytes in %.0f us = %.2f MB/s",
                      len(image), dt / 1e3, len(image) / dt * 1e3)
        # raw bus throughput: one long NAND_READ burst from the page register
        b = P.Batch()
        b.nand_ce(True)
        r = b.nand_read(60000)
        b.nand_ce(False)
        t0 = get_sim_time("ns")
        dev.run(b)
        dt = get_sim_time("ns") - t0
        assert len(r.value) == 60000
        dut._log.info("sync FIFO NAND_READ burst: %.2f MB/s", 60000 / dt * 1e3)
    await bridge(host)()
    check_ft(dut, ft)
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def ft_spi_nor_quad(dut):
    """1-1-4 fast read (6Bh) through SPI_READ4, at the fastest SPI clock."""
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        dev.open(negotiate=False)
        drv = detect(dev, want="spi").spi
        assert drv.chip.quad_cmd == 0x6B and drv.chip.qer == 5, drv.chip
        set_spi_clock(dev, 13.5)
        image = rnd(5000, seed=8)
        assert jobs.write(drv, image, start=2).ok
        drv.quad = "auto"                       # QE is already set in the model
        assert drv.read(2 * 4096 + 3, 4000) == image[3:4003]
        assert drv.quad_active
        for quad in ("off", "auto"):
            drv.quad = quad
            t0 = get_sim_time("ns")
            assert drv.read(2 * 4096, 5000) == image
            dt = get_sim_time("ns") - t0
            dut._log.info("SPI NOR read at 13.5 MHz, quad=%s: %.2f MB/s (async FIFO link)",
                          quad, 5000 / dt * 1e3)
        out = io.BytesIO()
        jobs.read(drv, out, start=2, count=2)
        assert out.getvalue()[:len(image)] == image
    await bridge(host)()
    check_models(dut)


@cocotb.test(skip=SPI_NAND)
async def ft_pin_test_doctor(dut):
    """PIN_TEST + nsprog doctor on the RTL: clean wiring, then an injected IO2-IO3 short."""
    from nsprog import doctor, wiring

    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        info = dev.open(negotiate=False)
        assert info.caps & P.CAP_PIN_TEST and info.gw_version == "1.2"
        b = P.Batch()
        idle = b.pin_test(0, P.PT_RELEASE)
        ce_low = b.pin_test(12, P.PT_LOW)
        io0_low = b.pin_test(17, P.PT_LOW)
        b.pin_test(0, P.PT_OFF)
        dev.run(b)
        assert idle.value == wiring.idle_levels(), hex(idle.value)
        assert ce_low.value == wiring.idle_levels() & ~(1 << 12)
        assert io0_low.value == wiring.idle_levels() & ~(1 << 17)
        checks = {c.key: c for c in doctor.run(dev)}
        assert checks["idle"].status == "ok", checks["idle"].detail
        assert checks["shorts"].status == "ok", checks["shorts"].detail
        assert checks["stuck"].status == "ok", checks["stuck"].detail
        assert checks["chips"].status == "ok" and "W25Q16JV" in checks["chips"].detail
        resume(set_short)(1)
        checks = {c.key: c for c in doctor.run(dev, detect_chips=False)}
        resume(set_short)(0)
        assert checks["shorts"].status == "fail"
        assert "IO2" in checks["shorts"].detail and "IO3" in checks["shorts"].detail
        assert checks["stuck"].status == "ok"
        # normal operation is back afterwards
        assert detect(dev, want="nand").nand is not None

    async def set_short(v):
        dut.short_io23.value = v
        await Timer(100, "ns")

    await bridge(host)()
    check_models(dut)


# ----------------------------------------------------------------------------
@cocotb.test(skip=not SPI_NAND)
async def ft_spi_nand(dut):
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        dev.open(negotiate=False)
        det = detect(dev, want="spi")
        drv = det.spi
        assert isinstance(drv, SpiNand) and drv.name == "W25N01GV", det.messages
        set_spi_clock(dev, 13.5)
        drv.blocks = 4                         # model size
        assert drv.bad_blocks([2])[2]
        img = bytearray(rnd(2 * drv.pages_per_block * drv.raw_page, seed=9))
        for blk in range(2):
            for pg in (0, 1):
                img[(blk * drv.pages_per_block + pg) * drv.raw_page + drv.page_size] = 0xFF
        img = bytes(img)
        rep = jobs.write(drv, img, start=1)
        assert rep.ok, rep.summary()
        assert rep.bad_blocks == [2]
        out = io.BytesIO()
        jobs.read(drv, out, start=1, count=3, bb="skip")
        assert out.getvalue() == img
        assert jobs.erase(drv, start=0, count=4).ok
        assert jobs.blank_check(drv, start=0, count=4).ok
    await bridge(host)()
    check_models(dut)


# ----------------------------------------------------------------------------
@cocotb.test(skip=not W29N02KV)
async def ft_w29n02kvsiaf(dut):
    """Winbond W29N02KVSIAF (first test chip): ID, ONFI + database, 2048+128 pages, real tR/tPROG/tBERS."""
    await start(dut)
    ft, dev = ft_device(dut)

    def host():
        dev.open(negotiate=False)
        det = detect(dev, want="nand")
        drv = det.nand
        assert drv is not None, det.messages
        assert drv.name == "W29N02KVSIAF" and drv.chip.source == "nsprog+onfi", det.messages
        assert det.nand_id[:5] == bytes.fromhex("EFDA109506")
        assert (drv.page_size, drv.chip.spare_size, drv.pages_per_block, drv.blocks) == (2048, 128, 64, 2048)
        assert drv.chip.ecc_bits == 4 and drv.chip.voltage == 3.3
        assert any("4-bit ECC per 512 B" in m for m in det.messages)
        assert drv.bad_blocks([0, 3])[3] and not drv.bad_blocks([0])[0]
        image = raw_image(drv, 1, seed=29)
        rep = jobs.write(drv, image, start=4, bb="skip")
        assert rep.ok, rep.summary()
        out = io.BytesIO()
        jobs.read(drv, out, start=4, count=1, bb="skip")
        assert out.getvalue() == image
        assert jobs.erase(drv, start=4, count=1).ok
        assert jobs.blank_check(drv, start=4, count=1).ok
        assert not dev.pin_ctrl & P.PIN_NAND_WP_HIGH
    await bridge(host)()
    assert int(dut.u_nand.programs.value) == 64 and int(dut.u_nand.erases.value) >= 1
    assert ft.errors == 0
    check_models(dut)
