"""SquashFS 4.0 reader (little-endian, as written by mksquashfs 4.x)."""

from __future__ import annotations

import struct
from typing import Callable, Dict, Iterator, List, Tuple

from .common import Entry, FileSystem, FsError, decompress

MAGIC = b"hsqs"
COMPRESSORS = {1: "zlib", 2: "lzma", 3: "lzo", 4: "xz", 5: "lz4", 6: "zstd"}
META = 8192
NO_FRAGMENT = 0xFFFFFFFF


class SquashFS(FileSystem):
    kind = "SquashFS"

    def __init__(self, data: bytes, offset: int = 0):
        self.d = memoryview(data)[offset:]
        if bytes(self.d[:4]) != MAGIC:
            raise FsError("not a SquashFS 4 image (little-endian 'hsqs' magic missing)")
        (self.inodes, self.mtime, self.block_size, self.frag_count, comp, self.block_log, self.flags,
         self.id_count, major, minor) = struct.unpack_from("<IIIIHHHHHH", self.d, 4)
        if major != 4:
            raise FsError("SquashFS %d.%d is not supported (only 4.x)" % (major, minor))
        (self.root_ref, self.bytes_used, _id_tab, _xattr_tab, self.inode_tab, self.dir_tab,
         self.frag_tab, _export_tab) = struct.unpack_from("<QQQQQQQQ", self.d, 32)
        if comp not in COMPRESSORS:
            raise FsError("unknown SquashFS compressor %d" % comp)
        self.comp: str = COMPRESSORS[comp]
        self._meta: Dict[int, Tuple[bytes, int]] = {}
        self._frags: List[Tuple[int, int]] = []

    def info(self) -> Dict[str, object]:
        return {"compression": self.comp, "block_size": self.block_size, "inodes": self.inodes,
                "bytes_used": self.bytes_used}

    # ------------------------------------------------------------------ low level
    def _block(self, pos: int) -> Tuple[bytes, int]:
        """Metadata block at absolute ``pos``: (data, position of the next block)."""
        if pos in self._meta:
            return self._meta[pos]
        hdr = struct.unpack_from("<H", self.d, pos)[0]
        size = hdr & 0x7FFF
        raw = bytes(self.d[pos + 2:pos + 2 + size])
        data = raw if hdr & 0x8000 else decompress(self.comp, raw, META)
        r = (data, pos + 2 + size)
        self._meta[pos] = r
        return r

    def _read_meta(self, table: int, block: int, offset: int, length: int) -> Tuple[bytes, int, int]:
        """Read ``length`` bytes of metadata; returns (bytes, next block, next offset)."""
        out = bytearray()
        pos = table + block
        while True:
            data, nxt = self._block(pos)
            take = data[offset:offset + length - len(out)]
            out += take
            offset += len(take)
            if len(out) >= length:
                return bytes(out), pos - table, offset
            if offset >= len(data):
                pos, offset = nxt, 0
                if pos >= len(self.d):
                    raise FsError("metadata runs past the end of the image")

    class _Cursor:
        def __init__(self, fs: "SquashFS", table: int, block: int, offset: int):
            self.fs, self.table, self.block, self.offset = fs, table, block, offset

        def read(self, n: int) -> bytes:
            data, self.block, self.offset = self.fs._read_meta(self.table, self.block, self.offset, n)
            return data

    def _fragment(self, idx: int) -> Tuple[int, int]:
        if not self._frags:
            n = self.frag_count
            ptrs = struct.unpack_from("<%dQ" % ((n + 511) // 512), self.d, self.frag_tab)
            for p in ptrs:
                data, _ = self._block(p)
                for k in range(0, len(data), 16):
                    start, size, _unused = struct.unpack_from("<QII", data, k)
                    self._frags.append((start, size))
        return self._frags[idx]

    def _data_block(self, start: int, size_word: int, out_size: int) -> bytes:
        size = size_word & 0xFFFFFF
        if size == 0:
            return b"\0" * out_size                     # sparse block
        raw = bytes(self.d[start:start + size])
        return raw if size_word & (1 << 24) else decompress(self.comp, raw, self.block_size)

    # ------------------------------------------------------------------ inodes
    def _inode(self, ref: int) -> dict:
        c = self._Cursor(self, self.inode_tab, ref >> 16, ref & 0xFFFF)
        typ, perm, _uid, _gid, mtime, _ino = struct.unpack("<HHHHII", c.read(16))
        node = {"type": typ, "mode": perm, "mtime": mtime}
        if typ == 1:                                            # basic dir
            blk, _nl, fsize, off, _parent = struct.unpack("<IIHHI", c.read(16))
            node.update(kind="dir", dir=(blk, off, fsize))
        elif typ == 8:                                          # extended dir
            _nl, fsize, blk, _parent, _icount, off, _x = struct.unpack("<IIIIHHI", c.read(24))
            node.update(kind="dir", dir=(blk, off, fsize))
        elif typ in (2, 9):                                     # file
            if typ == 2:
                start, frag, foff, size = struct.unpack("<IIII", c.read(16))
            else:
                start, size, _sparse, _nl, frag, foff, _x = struct.unpack("<QQQIIII", c.read(40))
            nblocks = size // self.block_size if frag != NO_FRAGMENT else \
                (size + self.block_size - 1) // self.block_size
            sizes = struct.unpack("<%dI" % nblocks, c.read(4 * nblocks)) if nblocks else ()
            node.update(kind="file", size=size, start=start, frag=frag, foff=foff, sizes=sizes)
        elif typ in (3, 10):                                    # symlink
            _nl, tsize = struct.unpack("<II", c.read(8))
            node.update(kind="symlink", target=c.read(tsize).decode("utf-8", "replace"))
        elif typ in (4, 5, 11, 12):
            node.update(kind="blockdev" if typ in (4, 11) else "chardev")
        elif typ in (6, 7, 13, 14):
            node.update(kind="fifo" if typ in (6, 13) else "socket")
        else:
            raise FsError("unknown inode type %d" % typ)
        return node

    def _file_data(self, n: dict) -> bytes:
        out = bytearray()
        pos = n["start"]
        remaining = n["size"]
        for w in n["sizes"]:
            chunk = self._data_block(pos, w, min(self.block_size, remaining))
            out += chunk[:min(self.block_size, remaining)]
            remaining -= min(self.block_size, remaining)
            pos += w & 0xFFFFFF
        if n["frag"] != NO_FRAGMENT and remaining > 0:
            start, size = self._fragment(n["frag"])
            block = self._data_block(start, size, self.block_size)
            out += block[n["foff"]:n["foff"] + remaining]
        return bytes(out)

    def _dir(self, blk: int, off: int, fsize: int) -> Iterator[Tuple[str, int]]:
        c = self._Cursor(self, self.dir_tab, blk, off)
        left = fsize - 3
        while left > 0:
            count, start, _base = struct.unpack("<III", c.read(12))
            left -= 12
            for _ in range(count + 1):
                ioff, _delta, _typ, nsize = struct.unpack("<HhHH", c.read(8))
                name = c.read(nsize + 1).decode("utf-8", "replace")
                left -= 8 + nsize + 1
                yield name, (start << 16) | ioff

    def _reader(self, n: dict) -> Callable[[], bytes]:
        return lambda: self._file_data(n)

    def entries(self) -> Iterator[Entry]:
        root = self._inode(self.root_ref)
        yield Entry("/", "dir", 0, root["mode"], root["mtime"])
        stack = [("", root)]
        while stack:
            path, node = stack.pop()
            for name, ref in sorted(self._dir(*node["dir"])):
                n = self._inode(ref)
                p = path + "/" + name
                if n["kind"] == "dir":
                    yield Entry(p, "dir", 0, n["mode"], n["mtime"])
                    stack.append((p, n))
                elif n["kind"] == "file":
                    yield Entry(p, "file", n["size"], n["mode"], n["mtime"], read=self._reader(n))
                elif n["kind"] == "symlink":
                    yield Entry(p, "symlink", len(n["target"]), n["mode"], n["mtime"], target=n["target"])
                else:
                    yield Entry(p, n["kind"], 0, n["mode"], n["mtime"])
