"""Shared types for the read-only file-system readers."""

from __future__ import annotations

import lzma
import os
import stat
import zlib
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

from . import lzo


class FsError(ValueError):
    pass


@dataclass
class Entry:
    path: str                       # "/etc/banner"
    kind: str                       # file | dir | symlink | chardev | blockdev | fifo | socket
    size: int = 0
    mode: int = 0o644
    mtime: int = 0
    target: str = ""                # symlink target
    read: Optional[Callable[[], bytes]] = None     # file contents (lazy)

    @property
    def mode_str(self) -> str:
        return stat.filemode({"file": stat.S_IFREG, "dir": stat.S_IFDIR, "symlink": stat.S_IFLNK,
                              "chardev": stat.S_IFCHR, "blockdev": stat.S_IFBLK, "fifo": stat.S_IFIFO,
                              "socket": stat.S_IFSOCK}.get(self.kind, 0) | (self.mode & 0o7777))


class FileSystem:
    """Base class: ``entries()`` yields every node (directories before their contents)."""

    kind = "?"

    def entries(self) -> Iterator[Entry]:
        raise NotImplementedError

    def info(self) -> Dict[str, object]:
        return {}

    def listing(self, limit: int = 100000) -> List[Entry]:
        out = []
        for e in self.entries():
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def extract(self, outdir: str) -> Dict[str, int]:
        """Write every file, directory and symlink below ``outdir``; returns counts.

        Device nodes, FIFOs and sockets are skipped (they need root and mean nothing
        off the device); files whose data cannot be decoded are counted as errors."""
        counts = {"files": 0, "dirs": 0, "symlinks": 0, "skipped": 0, "errors": 0}
        root = os.path.realpath(outdir)
        os.makedirs(root, exist_ok=True)
        for e in self.entries():
            rel = e.path.lstrip("/")
            dst = os.path.realpath(os.path.join(root, rel))
            if dst != root and not dst.startswith(root + os.sep):
                counts["skipped"] += 1                  # "../" tricks in names
                continue
            try:
                if e.kind == "dir":
                    os.makedirs(dst, exist_ok=True)
                    counts["dirs"] += 1
                elif e.kind == "file":
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(e.read() if e.read else b"")
                    counts["files"] += 1
                elif e.kind == "symlink":
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.lexists(dst):
                        os.unlink(dst)
                    try:
                        os.symlink(e.target, dst)
                    except (OSError, NotImplementedError):     # e.g. Windows without privilege
                        with open(dst + ".symlink.txt", "w") as f:
                            f.write(e.target)
                    counts["symlinks"] += 1
                else:
                    counts["skipped"] += 1
            except (FsError, lzo.LzoError, zlib.error, lzma.LZMAError, OSError):
                counts["errors"] += 1
        return counts


# --------------------------------------------------------------------------- decompressors

def _zstd(data: bytes, size: int) -> bytes:
    try:
        import zstandard
    except ImportError:
        raise FsError("zstd data: install the 'zstandard' package (pip install zstandard)") from None
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=max(size, 1 << 20))


def _lz4(data: bytes, size: int) -> bytes:
    try:
        import lz4.block
    except ImportError:
        raise FsError("lz4 data: install the 'lz4' package (pip install lz4)") from None
    return lz4.block.decompress(data, uncompressed_size=size)


def decompress(method: str, data: bytes, size: int) -> bytes:
    """``size``: expected (maximum) output size."""
    if method == "none":
        return data
    if method == "zlib":
        return zlib.decompress(data)
    if method == "deflate":
        return zlib.decompress(data, -15)
    if method == "xz":
        return lzma.decompress(data, format=lzma.FORMAT_XZ)
    if method == "lzma":
        return lzma.decompress(data, format=lzma.FORMAT_ALONE)
    if method == "lzo":
        return lzo.decompress(data, size)
    if method == "zstd":
        return _zstd(data, size)
    if method == "lz4":
        return _lz4(data, size)
    raise FsError("unsupported compression %r" % method)
