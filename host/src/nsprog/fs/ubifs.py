"""UBIFS reader.

Scans every node of every LEB (like a recovery tool, without walking the
index B-tree): the newest node (highest sequence number) wins for each inode,
directory entry and data block, deleted entries and truncations are applied.
Input is the UBIFS volume (LEBs back to back), e.g. from ``nsprog ubi`` or
``mkfs.ubifs``; :func:`open_fs` also accepts a whole UBI image.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Tuple

from .common import Entry, FileSystem, FsError, decompress

MAGIC = b"\x31\x18\x10\x06"
BLOCK = 4096
T_INO, T_DATA, T_DENT, T_XENT, T_TRUN, T_PAD, T_SB = 0, 1, 2, 3, 4, 5, 6
COMPR = {0: "none", 1: "lzo", 2: "deflate", 3: "zstd"}
DT_KIND = {0: "file", 1: "dir", 2: "symlink", 3: "blockdev", 4: "chardev", 5: "fifo", 6: "socket"}
ROOT_INO = 1


def _crc(data: bytes) -> int:
    """UBIFS CRC: crc32_le seeded with 0xFFFFFFFF, no final inversion."""
    return zlib.crc32(data) ^ 0xFFFFFFFF


@dataclass
class _Ino:
    sqnum: int
    size: int
    mtime: int
    nlink: int
    mode: int
    data: bytes


class UBIFS(FileSystem):
    kind = "UBIFS"

    def __init__(self, data: bytes, offset: int = 0):
        buf = bytes(memoryview(data)[offset:])
        if buf[:4] != MAGIC or len(buf) < 4096 or buf[20] != T_SB:
            raise FsError("not a UBIFS volume (no superblock node at the start)")
        self.min_io, self.leb_size, self.leb_cnt = struct.unpack_from("<III", buf, 32)
        self.default_compr = COMPR.get(struct.unpack_from("<H", buf, 84)[0], "?")
        self.fmt_version = struct.unpack_from("<I", buf, 80)[0]
        if not 4096 <= self.leb_size <= 4 << 20:
            raise FsError("implausible LEB size %d" % self.leb_size)
        self.nodes = 0
        self.bad = 0
        self.inos: Dict[int, _Ino] = {}
        self.dents: Dict[Tuple[int, str], Tuple[int, int, int]] = {}     # (parent, name) -> sqnum, inum, type
        self.blocks: Dict[Tuple[int, int], Tuple[int, int, int, bytes]] = {}   # sqnum, size, compr, raw
        self.truns: List[Tuple[int, int, int]] = []                       # inum, sqnum, new size
        for leb in range(len(buf) // self.leb_size):
            self._scan_leb(buf, leb * self.leb_size)

    def info(self) -> Dict[str, object]:
        return {"leb_size": self.leb_size, "leb_count": self.leb_cnt, "compression": self.default_compr,
                "nodes": self.nodes, "bad_crc": self.bad, "inodes": len(self.inos)}

    def _scan_leb(self, buf: bytes, base: int) -> None:
        end = base + self.leb_size
        pos = base
        while pos + 24 <= end:
            if buf[pos:pos + 4] != MAGIC:
                nxt = buf.find(MAGIC, pos + 8, end)
                if nxt < 0:
                    return
                pos = nxt
                continue
            crc, sqnum, ln, ntype = struct.unpack_from("<IQIB", buf, pos + 4)
            if ln < 24 or pos + ln > end or _crc(buf[pos + 8:pos + ln]) != crc:
                self.bad += buf[pos + 20] != T_PAD
                pos += 8
                continue
            self.nodes += 1
            if ntype == T_INO:
                self._ino(buf, pos, sqnum)
            elif ntype == T_DENT:
                self._dent(buf, pos, sqnum)
            elif ntype == T_DATA:
                self._data(buf, pos, sqnum, ln)
            elif ntype == T_TRUN:
                inum, = struct.unpack_from("<I", buf, pos + 24)
                new_size, = struct.unpack_from("<Q", buf, pos + 48)
                self.truns.append((inum, sqnum, new_size))
            pos += (ln + 7) & ~7

    @staticmethod
    def _key(buf: bytes, pos: int) -> Tuple[int, int, int]:
        inum, word = struct.unpack_from("<II", buf, pos + 24)
        return inum, word >> 29, word & 0x1FFFFFFF

    def _ino(self, buf: bytes, pos: int, sqnum: int) -> None:
        inum, _t, _ = self._key(buf, pos)
        size, = struct.unpack_from("<Q", buf, pos + 48)
        mtime, = struct.unpack_from("<Q", buf, pos + 72)
        nlink, _uid, _gid, mode, _flags, dlen = struct.unpack_from("<IIIIII", buf, pos + 92)
        prev = self.inos.get(inum)
        if prev is None or sqnum > prev.sqnum:
            self.inos[inum] = _Ino(sqnum, size, mtime, nlink, mode, bytes(buf[pos + 160:pos + 160 + dlen]))

    def _dent(self, buf: bytes, pos: int, sqnum: int) -> None:
        parent, _t, _h = self._key(buf, pos)
        inum, = struct.unpack_from("<Q", buf, pos + 40)
        typ = buf[pos + 49]
        nlen, = struct.unpack_from("<H", buf, pos + 50)
        name = bytes(buf[pos + 56:pos + 56 + nlen]).decode("utf-8", "replace")
        key = (parent, name)
        prev = self.dents.get(key)
        if prev is None or sqnum > prev[0]:
            self.dents[key] = (sqnum, inum, typ)

    def _data(self, buf: bytes, pos: int, sqnum: int, ln: int) -> None:
        inum, _t, block = self._key(buf, pos)
        size, compr = struct.unpack_from("<IH", buf, pos + 40)
        key = (inum, block)
        prev = self.blocks.get(key)
        if prev is None or sqnum > prev[0]:
            self.blocks[key] = (sqnum, size, compr, bytes(buf[pos + 48:pos + ln]))

    def _file(self, inum: int) -> bytes:
        ino = self.inos[inum]
        out = bytearray(ino.size)
        cut = [(sq, sz) for i, sq, sz in self.truns if i == inum]
        for (i, block), (sqnum, size, compr, raw) in self.blocks.items():
            if i != inum or block * BLOCK >= ino.size:
                continue
            if any(sqnum < sq and block * BLOCK >= sz for sq, sz in cut):
                continue                                    # truncated away later
            chunk = decompress(COMPR.get(compr, str(compr)), raw, size)[:size]
            end = min(block * BLOCK + len(chunk), ino.size)
            out[block * BLOCK:end] = chunk[:end - block * BLOCK]
        return bytes(out)

    def _reader(self, inum: int) -> Callable[[], bytes]:
        return lambda: self._file(inum)

    def entries(self) -> Iterator[Entry]:
        children: Dict[int, List[Tuple[str, int, int]]] = {}
        for (parent, name), (_sq, inum, typ) in self.dents.items():
            if inum and inum in self.inos and self.inos[inum].nlink:
                children.setdefault(parent, []).append((name, inum, typ))
        root = self.inos.get(ROOT_INO)
        yield Entry("/", "dir", 0, (root.mode if root else 0o755) & 0o7777, root.mtime if root else 0)
        stack = [("", ROOT_INO)]
        seen = {ROOT_INO}
        while stack:
            path, parent = stack.pop()
            for name, inum, typ in sorted(children.get(parent, [])):
                p = path + "/" + name
                ino = self.inos[inum]
                kind = DT_KIND.get(typ, "file")
                mode = ino.mode & 0o7777
                if kind == "dir":
                    yield Entry(p, "dir", 0, mode, ino.mtime)
                    if inum not in seen:
                        seen.add(inum)
                        stack.append((p, inum))
                elif kind == "file":
                    yield Entry(p, "file", ino.size, mode, ino.mtime, read=self._reader(inum))
                elif kind == "symlink":
                    target = ino.data.decode("utf-8", "replace")
                    yield Entry(p, "symlink", len(target), mode, ino.mtime, target=target)
                else:
                    yield Entry(p, kind, 0, mode, ino.mtime)
