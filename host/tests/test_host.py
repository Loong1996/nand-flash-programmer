import io
import random

import pytest

from nsprog import chipdb, jobs
from nsprog import protocol as P
from nsprog.device import Device
from nsprog.emulator import (EmulatorLink, NandModel, SpiNandModel, SpiNorModel,
                             build_onfi_param_page, onfi_crc16)
from nsprog.flash import ParallelNand, SpiNand, SpiNor, detect
from nsprog.onfi import parse_param_page


def make(nand=None, spi=None, window=None):
    dev = Device(EmulatorLink(nand, spi), window=window)
    dev.open(negotiate=False)
    return dev


def rnd(n, seed=1):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(n))


def raw_image(drv, nblocks, seed=1, bb_off=0):
    """Random raw (page+spare) image whose bad-block marker bytes stay FF."""
    img = bytearray(rnd(nblocks * drv.pages_per_block * drv.raw_page, seed))
    for blk in range(nblocks):
        for pg in (0, 1):
            img[(blk * drv.pages_per_block + pg) * drv.raw_page + drv.page_size + bb_off] = 0xFF
    return img


# --------------------------------------------------------------------- protocol
def test_info_and_echo():
    dev = make(NandModel())
    assert dev.info.rx_fifo == 4096
    b = P.Batch()
    rs = [b.echo(i) for i in range(300)]
    dev.run(b)
    assert [r.value for r in rs] == [i & 0xFF for i in range(300)]


def test_segmentation_small_window():
    dev = make(NandModel(), window=64)
    b = P.Batch()
    rs = [b.echo(i) for i in range(200)]
    big = b.spi_xfer(bytes(range(256)) * 10)
    dev.run(b)
    assert [r.value for r in rs] == list(range(200))
    assert big.value == b"\xff" * 2560


def test_large_read_split():
    b = P.Batch()
    r = b.nand_read(70000)
    assert len(b.ops) == 2 and b.response_len == 70000
    dev = make(NandModel())
    dev.run(b)
    assert len(r.value) == 70000


def test_resync_after_garbage():
    dev = make(NandModel())
    dev.link.write(bytes([P.NAND_WRITE, 0x10]))   # half an op
    dev.link.engine.inbuf.clear()                   # emulate the engine's inter-byte timeout
    dev.resync()
    assert dev.query_info().proto == 1


# --------------------------------------------------------------------- ONFI / DB
def test_onfi_roundtrip():
    pp = build_onfi_param_page(page=4096, spare=224, ppb=128, blocks=4096)
    assert onfi_crc16(pp[:254]) == int.from_bytes(pp[254:256], "little")
    p = parse_param_page(b"\x00" * 256 + pp)       # first copy corrupt
    assert (p.page_size, p.spare_size, p.pages_per_block, p.blocks) == (4096, 224, 128, 4096)
    assert (p.row_cycles, p.col_cycles) == (3, 2)


def test_chipdb_loads_nando():
    names = [c.name for c in chipdb.nand_chips()]
    assert "K9F2G08U0C" in names and len(names) >= 19
    assert chipdb.find_nand(bytes([0xEC, 0xDA, 0x10, 0x95, 0x44])).name == "K9F2G08U0C"
    assert chipdb.nand_by_name("W29N02GZS1BA").voltage == 1.8
    assert chipdb.find_spi_nor(bytes.fromhex("EF4015")).source == "nando"
    at45 = chipdb.find_spi_nor(bytes.fromhex("1F23"))
    assert at45.name == "AT45DB021D" and not at45.linear


# --------------------------------------------------------------------- parallel NAND
def nand_dev(**kw):
    kw.setdefault("blocks", 32)
    return make(NandModel(**kw))


def test_nand_detect_onfi_and_db():
    dev = nand_dev()
    det = detect(dev, want="nand")
    assert det.nand is not None and det.nand.chip.source == "onfi"
    assert det.nand.blocks == 32 and det.nand.page_size == 2048
    dev = nand_dev(ids=bytes([0xEC, 0xF1, 0x00, 0x95, 0x41]), onfi=False, blocks=1024)
    det = detect(dev, want="nand")
    assert det.nand.name == "K9F1G08U0E"


def test_nand_no_chip():
    dev = make(None, None)
    det = detect(dev)
    assert det.nand is None and det.spi is None
    assert any("no chip" in m for m in det.messages)


def test_nand_write_read_verify_skip_bad():
    dev = nand_dev()
    drv = detect(dev, want="nand").nand
    ppb, raw = drv.pages_per_block, drv.raw_page
    image = raw_image(drv, 6)
    image[ppb * raw: 2 * ppb * raw] = b"\xff" * (ppb * raw)   # a blank block
    rep = jobs.write(drv, bytes(image), start=1, oob=True, bb="skip")
    assert rep.ok, rep.summary()
    assert rep.bad_blocks == [3]
    out = io.BytesIO()
    jobs.read(drv, out, start=1, count=7, oob=True, bb="skip")
    assert out.getvalue() == bytes(image)
    assert jobs.verify(drv, bytes(image), start=1, oob=True).ok
    # bad block kept intact (marker still there)
    assert drv.bad_blocks([3])[3]


def test_nand_keep_mode_and_main_only():
    dev = nand_dev()
    drv = detect(dev, want="nand").nand
    ppb = drv.pages_per_block
    image = rnd(5 * ppb * drv.page_size, seed=7)
    rep = jobs.write(drv, image, start=0, oob=False, bb="keep")
    assert rep.ok and rep.skipped_blocks == [3]
    out = io.BytesIO()
    jobs.read(drv, out, start=0, count=5, oob=False, bb="keep")
    got = out.getvalue()
    bs = ppb * drv.page_size
    for blk in (0, 1, 2, 4):
        assert got[blk * bs:(blk + 1) * bs] == image[blk * bs:(blk + 1) * bs]


def test_nand_erase_and_blank():
    dev = nand_dev()
    drv = detect(dev, want="nand").nand
    jobs.write(drv, bytes(raw_image(drv, 2)), start=0)
    assert not jobs.blank_check(drv).ok
    rep = jobs.erase(drv)
    assert rep.ok and rep.skipped_blocks == [3]
    assert jobs.blank_check(drv).ok
    assert jobs.bad_block_report(drv).bad_blocks == [3]


def test_nand_write_protect_is_restored():
    dev = nand_dev()
    drv = detect(dev, want="nand").nand
    jobs.write(drv, bytes(raw_image(drv, 1)), start=0)
    assert not dev.pin_ctrl & P.PIN_NAND_WP_HIGH
    # program without begin_write() must be rejected by the chip
    res = drv.program_pages([(0, b"\x00" * drv.raw_page)])
    assert not res[0].ok and "write protected" in res[0].message


def test_nand_without_rb():
    dev = nand_dev()
    drv = detect(dev, want="nand", use_rb=False).nand
    assert not drv.use_rb
    image = bytes(raw_image(drv, 2, seed=3))
    assert jobs.write(drv, image, start=0).ok
    out = io.BytesIO()
    jobs.read(drv, out, start=0, count=2)
    assert out.getvalue() == image


def test_small_page_nand():
    model = NandModel(page=512, spare=16, ppb=32, blocks=4096, onfi=False,
                      ids=bytes([0xEC, 0x76, 0xA5, 0xC0]), small_page=True,
                      row_cycles=3, col_cycles=1, bad_blocks=())
    dev = make(model)
    drv = detect(dev, want="nand").nand
    assert drv.name == "K9F1208U0B"
    image = bytes(raw_image(drv, 2, seed=9, bb_off=5))
    assert jobs.write(drv, image, start=10).ok
    out = io.BytesIO()
    jobs.read(drv, out, start=10, count=2)
    assert out.getvalue() == image


def test_1v8_guard():
    dev = nand_dev()
    drv = ParallelNand(dev, chipdb.nand_by_name("W29N02GZS1BA"))
    with pytest.raises(Exception, match="1.8V"):
        jobs.read(drv, io.BytesIO(), count=1)


# --------------------------------------------------------------------- SPI NOR
def test_spinor_detect_write_read():
    dev = make(None, SpiNorModel(protected=True))
    drv = detect(dev, want="spi").spi
    assert isinstance(drv, SpiNor) and drv.name == "W25Q16JV"
    assert drv.size == 2 * 1024 * 1024
    image = rnd(100_000, seed=4)
    rep = jobs.write(drv, image, start=2)
    assert rep.ok, rep.summary()
    assert drv.read(2 * 4096, len(image)) == image
    assert jobs.verify(drv, image, start=2).ok
    # data after the image inside the last erase unit was preserved (blank here)
    assert jobs.erase(drv, start=0, count=drv.blocks).ok
    assert jobs.blank_check(drv).ok


def test_spinor_partial_block_preserved():
    model = SpiNorModel()
    dev = make(None, model)
    drv = detect(dev, want="spi").spi
    model.mem[0:8192] = b"\x55" * 8192
    jobs.write(drv, b"\xAA" * 1000, start=0)
    assert bytes(model.mem[:1000]) == b"\xAA" * 1000
    assert bytes(model.mem[1000:8192]) == b"\x55" * 7192


def test_spinor_sfdp_generic():
    dev = make(None, SpiNorModel(jedec=b"\xAB\xCD\x17", size=8 * 1024 * 1024))
    det = detect(dev, want="spi")
    assert det.spi.size == 8 * 1024 * 1024
    assert any("SFDP" in m for m in det.messages)


def test_spinor_quad_read_modes():
    model = SpiNorModel()
    dev = make(None, model)
    drv = detect(dev, want="spi").spi
    assert drv.chip.quad_cmd == 0x6B and drv.chip.qer == 5 and drv.quad_capable
    image = rnd(20000, seed=6)
    assert jobs.write(drv, image, start=1).ok
    model.sr2 = 0x00                              # QE clear: auto must not use 6Bh
    drv.quad = "auto"
    assert drv.read(4096, len(image)) == image and not drv.quad_active
    drv.quad = "on"                               # sets QE for the read, then restores it
    out = io.BytesIO()
    jobs.read(drv, out, start=1, count=5)
    assert out.getvalue()[:len(image)] == image
    assert model.sr2 == 0x00
    model.sr2 = 0x02
    drv.quad = "auto"
    assert drv.read(4096, 100) == image[:100] and drv.quad_active
    drv.quad = "off"
    assert drv.read(4096, 100) == image[:100] and not drv.quad_active


def test_nand_timing_profiles():
    dev = nand_dev()
    drv = detect(dev, want="nand").nand
    assert drv.auto_timing() == "safe"            # emulator ONFI page: mode 0 only
    assert drv.set_timing("fast") == "fast"
    regs = dev.link.engine.regs
    assert regs[P.REG_T_RP] == 1 and regs[P.REG_T_REH] == 1 and regs[P.REG_T_WHR] == 3
    image = raw_image(drv, 1, seed=3)
    assert jobs.write(drv, image, start=0).ok
    with pytest.raises(ValueError):
        drv.set_timing("warp")


# --------------------------------------------------------------------- SPI NAND
def test_spinand_flow():
    dev = make(None, SpiNandModel(blocks=16))
    drv = detect(dev, want="spi").spi
    assert isinstance(drv, SpiNand) and drv.name == "W25N01GV"
    drv.blocks = 16
    image = bytes(raw_image(drv, 3, seed=5))
    rep = jobs.write(drv, image, start=4)
    assert rep.ok and rep.bad_blocks == [5]
    out = io.BytesIO()
    jobs.read(drv, out, start=4, count=4, bb="skip")
    assert out.getvalue() == image
    assert jobs.erase(drv).ok
    assert jobs.blank_check(drv).ok


def test_db_name_with_onfi_geometry():
    # ID of K9F2G08U0C (NANDO DB: 2048+64) but the chip's ONFI page says 2048+128.
    dev = make(NandModel(ids=bytes([0xEC, 0xDA, 0x10, 0x95, 0x44]), spare=128, blocks=16))
    det = detect(dev, want="nand")
    assert det.nand.name == "K9F2G08U0C" and det.nand.spare_size == 128
    assert any("geometry taken from ONFI" in m for m in det.messages)


def test_spinand_linux_table_matching():
    assert chipdb.find_spi_nand(bytes.fromhex("FFEFAA2100"), bytes.fromhex("EFAA21")).name == "W25N01GV"
    # GigaDevice "opcode_addr" parts answer after 9Fh 00h
    assert chipdb.find_spi_nand(b"\xff\xff\xff", bytes.fromhex("C8D1C8")).name == "GD5F1GQ4UExxG"
    # ESMT F50L1G41LB is C8 01 7F 7F 7F per the Linux table
    assert chipdb.find_spi_nand(b"\xff" * 5, bytes.fromhex("C8017F7F7F")).name == "F50L1G41LB"
    assert chipdb.guess_voltage("Winbond W29N02KVxxAF") == 3.3
