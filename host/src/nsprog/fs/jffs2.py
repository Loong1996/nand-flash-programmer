"""JFFS2 reader: scans every node, keeps the newest version of each, rebuilds the tree."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .common import Entry, FileSystem, FsError, decompress

NODE_DIRENT = 0xE001
NODE_INODE = 0xE002
COMPR = {0: "none", 1: "zero", 2: "rtime", 3: "rubinmips", 4: "copy", 5: "dynrubin", 6: "zlib",
         7: "lzo", 8: "lzma"}
DT_KIND = {1: "fifo", 2: "chardev", 4: "dir", 6: "blockdev", 8: "file", 10: "symlink", 12: "socket"}


def _crc(data: bytes) -> int:
    """JFFS2 CRC: crc32_le seeded with 0, no final inversion."""
    return (zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def rtime_decompress(src: bytes, dst_len: int) -> bytes:
    positions = [0] * 256
    out = bytearray()
    pos = 0
    while len(out) < dst_len:
        if pos + 2 > len(src):
            raise FsError("rtime: input overrun")
        value = src[pos]
        repeat = src[pos + 1]
        pos += 2
        out.append(value)
        back = positions[value]
        positions[value] = len(out)
        for _ in range(repeat):
            out.append(out[back])
            back += 1
    return bytes(out[:dst_len])


@dataclass
class _Inode:
    version: int = -1
    mode: int = 0
    mtime: int = 0
    isize: int = 0
    # (version, offset, dsize, raw data, compression)
    data: List[Tuple[int, int, int, bytes, int]] = field(default_factory=list)


class JFFS2(FileSystem):
    kind = "JFFS2"

    def __init__(self, data: bytes, offset: int = 0):
        self.d = memoryview(data)[offset:]
        if bytes(self.d[:2]) == b"\x85\x19":
            self.fmt = "<"
        elif bytes(self.d[:2]) == b"\x19\x85":
            self.fmt = ">"
        else:
            raise FsError("not a JFFS2 image (no 0x1985 node magic at the start)")
        self.nodes = 0
        self.bad = 0
        self.inodes: Dict[int, _Inode] = {}
        # (parent ino, name) -> (version, ino, type)
        self.dirents: Dict[Tuple[int, str], Tuple[int, int, int]] = {}
        self._scan()

    def info(self) -> Dict[str, object]:
        return {"endian": "little" if self.fmt == "<" else "big", "nodes": self.nodes,
                "bad_crc": self.bad, "inodes": len(self.inodes)}

    def _scan(self) -> None:
        fmt = self.fmt
        magic = b"\x85\x19" if fmt == "<" else b"\x19\x85"
        buf = bytes(self.d)
        pos = 0
        n = len(buf)
        while pos + 12 <= n:
            if buf[pos:pos + 2] != magic:
                nxt = buf.find(magic, pos + 4)
                if nxt < 0:
                    break
                pos = (nxt + 3) & ~3 if nxt % 4 else nxt
                continue
            ntype, totlen, hcrc = struct.unpack_from(fmt + "HII", buf, pos + 2)
            if _crc(buf[pos:pos + 8]) != hcrc or totlen < 12 or pos + totlen > n:
                pos += 4
                continue
            self.nodes += 1
            if ntype == NODE_INODE:
                self._inode(buf, pos, totlen)
            elif ntype == NODE_DIRENT:
                self._dirent(buf, pos, totlen)
            pos += (totlen + 3) & ~3

    def _inode(self, buf: bytes, pos: int, totlen: int) -> None:
        (ino, version, mode, _uid, _gid, isize, _atime, mtime, _ctime, offset, csize, dsize, compr,
         _ucompr, _flags, _dcrc, ncrc) = struct.unpack_from(self.fmt + "IIIHHIIIIIIIBBHII", buf, pos + 12)
        if _crc(buf[pos:pos + 60]) != ncrc:
            self.bad += 1
            return
        node = self.inodes.setdefault(ino, _Inode())
        if version > node.version:
            node.version, node.mode, node.mtime, node.isize = version, mode, mtime, isize
        if dsize or csize:
            node.data.append((version, offset, dsize, bytes(buf[pos + 68:pos + 68 + csize]), compr))

    def _dirent(self, buf: bytes, pos: int, totlen: int) -> None:
        pino, version, ino, _mctime, nsize, typ, _u1, _u2, ncrc, _namecrc = struct.unpack_from(
            self.fmt + "IIIIBBBBII", buf, pos + 12)
        if _crc(buf[pos:pos + 32]) != ncrc:
            self.bad += 1
            return
        name = bytes(buf[pos + 40:pos + 40 + nsize]).decode("utf-8", "replace")
        key = (pino, name)
        prev = self.dirents.get(key)
        if prev is None or version > prev[0]:
            self.dirents[key] = (version, ino, typ)

    def _data(self, ino: int) -> bytes:
        node = self.inodes.get(ino)
        if node is None:
            return b""
        out = bytearray(node.isize)
        for _version, off, dsize, raw, compr in sorted(node.data):
            method = COMPR.get(compr, str(compr))
            if method == "zero":
                chunk = b"\0" * dsize
            elif method == "rtime":
                chunk = rtime_decompress(raw, dsize)
            elif method == "zlib":
                chunk = zlib.decompress(raw)
            elif method in ("none", "copy"):
                chunk = raw
            elif method in ("lzo", "lzma"):
                chunk = decompress(method, raw, dsize) if method == "lzo" else _lzma(raw, dsize)
            else:
                raise FsError("JFFS2 %s compression is not supported" % method)
            end = min(off + dsize, len(out))
            if off < end:
                out[off:end] = chunk[:end - off]
        return bytes(out)

    def _reader(self, ino: int) -> Callable[[], bytes]:
        return lambda: self._data(ino)

    def entries(self) -> Iterator[Entry]:
        children: Dict[int, List[Tuple[str, int, int]]] = {}
        for (pino, name), (_v, ino, typ) in self.dirents.items():
            if ino:                                        # ino 0 = deleted
                children.setdefault(pino, []).append((name, ino, typ))
        root = self.inodes.get(1)
        yield Entry("/", "dir", 0, (root.mode if root else 0o755) & 0o7777, root.mtime if root else 0)
        stack = [("", 1)]
        seen = {1}
        while stack:
            path, pino = stack.pop()
            for name, ino, typ in sorted(children.get(pino, [])):
                p = path + "/" + name
                node = self.inodes.get(ino) or _Inode()
                kind = DT_KIND.get(typ, "file")
                mode, mtime = node.mode & 0o7777, node.mtime
                if kind == "dir":
                    yield Entry(p, "dir", 0, mode, mtime)
                    if ino not in seen:
                        seen.add(ino)
                        stack.append((p, ino))
                elif kind == "file":
                    yield Entry(p, "file", node.isize, mode, mtime, read=self._reader(ino))
                elif kind == "symlink":
                    target = self._data(ino).decode("utf-8", "replace")
                    yield Entry(p, "symlink", len(target), mode, mtime, target=target)
                else:
                    yield Entry(p, kind, 0, mode, mtime)


def _lzma(raw: bytes, dsize: int) -> bytes:
    # Linux JFFS2 LZMA: raw LZMA1 stream, lc=0 lp=0 pb=0, dictionary 0x2000
    import lzma

    filt = [{"id": lzma.FILTER_LZMA1, "lc": 0, "lp": 0, "pb": 0, "dict_size": 0x2000}]
    return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt).decompress(raw, dsize)


def find(buf: bytes) -> Optional[int]:
    """Offset of the first JFFS2 node with a valid header CRC, or None."""
    for magic, fmt in ((b"\x85\x19", "<"), (b"\x19\x85", ">")):
        pos = buf.find(magic)
        while 0 <= pos < len(buf) - 12:
            if pos % 4 == 0:
                hcrc = struct.unpack_from(fmt + "I", buf, pos + 8)[0]
                if _crc(buf[pos:pos + 8]) == hcrc:
                    return pos
            pos = buf.find(magic, pos + 1)
    return None
