"""UBI image inspection and volume extraction (offline).

Input is a *main-area* image (no OOB; use ``nsprog image strip`` first).
Only static structure is parsed: EC headers, VID headers and the volume table.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional, Tuple

EC_MAGIC = b"UBI#"
VID_MAGIC = b"UBI!"
LAYOUT_VOL_ID = 0x7FFFEFFF
VTBL_RECORD = 172
MAX_VOLUMES = 128


def _crc(data: bytes) -> int:
    # UBI: crc32_le with seed 0xFFFFFFFF and no final inversion (= inverted zlib crc32).
    return zlib.crc32(data) ^ 0xFFFFFFFF


@dataclass
class Volume:
    vol_id: int
    name: str
    vol_type: str
    reserved_pebs: int
    alignment: int
    data_pad: int
    lebs: Dict[int, Tuple[int, int, int]] = field(default_factory=dict)   # lnum -> (peb, sqnum, data_size)

    @property
    def used_lebs(self) -> int:
        return len(self.lebs)


@dataclass
class UbiImage:
    peb_size: int
    vid_offset: int
    data_offset: int
    pebs: int
    image_seq: int
    volumes: Dict[int, Volume] = field(default_factory=dict)
    bad_crc: int = 0

    @property
    def leb_size(self) -> int:
        return self.peb_size - self.data_offset

    def summary(self) -> str:
        s = ["UBI: %d PEBs of %d KiB, LEB %d bytes, VID header @%d, data @%d, image seq 0x%08X" % (
            self.pebs, self.peb_size // 1024, self.leb_size, self.vid_offset, self.data_offset,
            self.image_seq)]
        for v in sorted(self.volumes.values(), key=lambda v: v.vol_id):
            s.append("  volume %d %-16s %-7s %4d/%d LEBs used" % (
                v.vol_id, repr(v.name), v.vol_type, v.used_lebs, v.reserved_pebs))
        if self.bad_crc:
            s.append("  %d headers with bad CRC were ignored" % self.bad_crc)
        return "\n".join(s)


def _guess_peb(f: BinaryIO, size: int) -> int:
    # Smallest first: with 16 KiB PEBs there is also an EC header at 128 KiB.
    for peb in (16 * 1024, 32 * 1024, 64 * 1024, 128 * 1024, 256 * 1024, 512 * 1024,
                1024 * 1024, 2048 * 1024):
        if size < 2 * peb:
            continue
        f.seek(peb)
        if f.read(4) == EC_MAGIC:
            return peb
    raise ValueError("cannot determine the PEB size; pass --peb")


def parse(f: BinaryIO, peb_size: Optional[int] = None) -> UbiImage:
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if f.read(4) != EC_MAGIC:
        raise ValueError("not a UBI image (no 'UBI#' at offset 0)")
    peb = peb_size or _guess_peb(f, size)
    f.seek(0)
    ec = f.read(64)
    vid_off, data_off, image_seq = struct.unpack(">IIi", ec[16:28])
    img = UbiImage(peb, vid_off, data_off, size // peb, image_seq & 0xFFFFFFFF)
    layout: Dict[int, Tuple[int, bytes]] = {}
    for p in range(img.pebs):
        f.seek(p * peb)
        hdr = f.read(64)
        if hdr[:4] != EC_MAGIC:
            continue
        if _crc(hdr[:60]) != struct.unpack(">I", hdr[60:64])[0]:
            img.bad_crc += 1
            continue
        f.seek(p * peb + vid_off)
        vid = f.read(64)
        if vid[:4] != VID_MAGIC:
            continue
        if _crc(vid[:60]) != struct.unpack(">I", vid[60:64])[0]:
            img.bad_crc += 1
            continue
        vol_type = vid[5]
        vol_id, lnum = struct.unpack(">II", vid[8:16])
        data_size, used_ebs, data_pad = struct.unpack(">III", vid[20:32])
        sqnum = struct.unpack(">Q", vid[40:48])[0]
        if vol_id == LAYOUT_VOL_ID:
            f.seek(p * peb + data_off)
            if lnum not in layout or layout[lnum][0] < sqnum:
                layout[lnum] = (sqnum, f.read(img.leb_size))
            continue
        v = img.volumes.get(vol_id)
        if v is None:
            v = img.volumes[vol_id] = Volume(vol_id, "vol%d" % vol_id,
                                             "static" if vol_type == 2 else "dynamic", 0, 1, data_pad)
        prev = v.lebs.get(lnum)
        if prev is None or prev[1] < sqnum:
            v.lebs[lnum] = (p, sqnum, data_size)
    if layout:
        vtbl = layout[min(layout)][1]
        for i in range(min(MAX_VOLUMES, len(vtbl) // VTBL_RECORD)):
            rec = vtbl[i * VTBL_RECORD:(i + 1) * VTBL_RECORD]
            reserved, align, pad = struct.unpack(">III", rec[0:12])
            if reserved == 0 or _crc(rec[:168]) != struct.unpack(">I", rec[168:172])[0]:
                continue
            vtype = "dynamic" if rec[12] == 1 else "static"
            name_len = struct.unpack(">H", rec[14:16])[0]
            name = rec[16:16 + min(name_len, 127)].decode("utf-8", "replace")
            v = img.volumes.get(i)
            if v is None:
                v = img.volumes[i] = Volume(i, name, vtype, reserved, align, pad)
            v.name, v.vol_type, v.reserved_pebs, v.alignment, v.data_pad = name, vtype, reserved, align, pad
    return img


def volume_data(f: BinaryIO, img: UbiImage, v: Volume) -> bytes:
    """Contents of a volume: used LEBs in order, unmapped LEBs (up to the last used one) as 0xFF."""
    leb = img.leb_size - v.data_pad
    out = bytearray()
    if v.lebs:
        for lnum in range(max(v.lebs) + 1):
            if lnum not in v.lebs:
                out += b"\xff" * leb
                continue
            peb, _sq, dsize = v.lebs[lnum]
            f.seek(peb * img.peb_size + img.data_offset)
            n = dsize if v.vol_type == "static" and dsize else leb
            out += f.read(n)
    return bytes(out)


def extract(f: BinaryIO, img: UbiImage, outdir: str, vol_ids: Optional[List[int]] = None) -> List[str]:
    """Write each volume to ``outdir/<name>.bin`` (see :func:`volume_data`)."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    for v in sorted(img.volumes.values(), key=lambda v: v.vol_id):
        if vol_ids and v.vol_id not in vol_ids:
            continue
        path = os.path.join(outdir, "%s.bin" % (v.name.replace("/", "_") or "vol%d" % v.vol_id))
        with open(path, "wb") as out:
            out.write(volume_data(f, img, v))
        written.append(path)
    return written


def detect_content(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(4)
    if head == b"hsqs":
        return "SquashFS"
    if head == b"\x31\x18\x10\x06":
        return "UBIFS"
    if head == b"\x27\x05\x19\x56":
        return "U-Boot uImage"
    if head == b"\xd0\x0d\xfe\xed":
        return "FDT/FIT"
    if head in (b"\x85\x19\x01\xe0", b"\x85\x19\x02\xe0", b"\x85\x19\x03\x20", b"\x19\x85\xe0\x01",
                b"\x19\x85\xe0\x02", b"\x19\x85\x20\x03"):
        return "JFFS2"
    return "data"
