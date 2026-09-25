"""Read-only file-system readers for flash images: SquashFS, JFFS2, UBIFS (also inside UBI).

    from nsprog import fs
    for found in fs.find_all(data):            # every file system in a flash dump
        for e in found.fs.entries(): print(e.path)
    fs.open_fs(data).extract("out/")           # one image that starts with a file system

Pure Python; LZO is built in, lz4 / zstd need the optional ``lz4`` / ``zstandard``
packages.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

from .common import Entry, FileSystem, FsError
from .jffs2 import JFFS2
from .squashfs import SquashFS
from .ubifs import UBIFS

__all__ = ["Entry", "FileSystem", "FsError", "SquashFS", "JFFS2", "UBIFS", "UbiContainer",
           "open_fs", "find_all", "Found"]


class UbiContainer(FileSystem):
    """A UBI image: every volume with a file system becomes ``/<volume name>/...``."""

    kind = "UBI"

    def __init__(self, data: bytes, peb_size: Optional[int] = None):
        from .. import ubi

        f = io.BytesIO(data)
        self.img = ubi.parse(f, peb_size)
        self.volumes: Dict[str, FileSystem] = {}
        self.skipped: Dict[str, str] = {}
        for v in sorted(self.img.volumes.values(), key=lambda v: v.vol_id):
            vdata = ubi.volume_data(f, self.img, v)
            name = v.name or "vol%d" % v.vol_id
            try:
                self.volumes[name] = open_fs(vdata)
            except FsError as e:
                self.skipped[name] = str(e)

    def info(self) -> Dict[str, object]:
        d: Dict[str, object] = {"peb": self.img.peb_size, "volumes": ", ".join(
            "%s (%s)" % (n, fs.kind) for n, fs in self.volumes.items())}
        if self.skipped:
            d["other volumes"] = ", ".join(self.skipped)
        return d

    def entries(self) -> Iterator[Entry]:
        yield Entry("/", "dir", 0, 0o755)
        for name, fs in self.volumes.items():
            prefix = "/" + name.replace("/", "_")
            for e in fs.entries():
                e.path = prefix + (e.path if e.path != "/" else "")
                yield e


def open_fs(data: bytes, offset: int = 0) -> FileSystem:
    """Open the file system that starts at ``offset``."""
    head = bytes(memoryview(data)[offset:offset + 4])
    if head == b"hsqs":
        return SquashFS(data, offset)
    if head == b"\x31\x18\x10\x06":
        return UBIFS(data, offset)
    if head == b"UBI#":
        return UbiContainer(bytes(memoryview(data)[offset:]))
    if head[:2] in (b"\x85\x19", b"\x19\x85"):
        return JFFS2(data, offset)
    if head == b"sqsh":
        raise FsError("big-endian / pre-4.0 SquashFS is not supported")
    raise FsError("no SquashFS, JFFS2, UBIFS or UBI header at offset 0x%X" % offset)


@dataclass
class Found:
    offset: int
    size: Optional[int]
    fs: Optional[FileSystem]
    error: str = ""

    @property
    def label(self) -> str:
        kind = self.fs.kind if self.fs else "?"
        return "0x%08X-%s" % (self.offset, kind)


def find_all(data: bytes, partitions: bool = True) -> List[Found]:
    """Every SquashFS / JFFS2 / UBIFS / UBI in a main-area flash dump.

    File systems are cut at the next finding or partition boundary, so a JFFS2
    partition does not swallow whatever follows it."""
    from ..scan import scan

    rep = scan(data)
    kinds = ("SquashFS", "JFFS2", "UBIFS", "UBI")
    cuts = sorted({f.offset for f in rep.findings} | {p.offset for p in rep.partitions if partitions} |
                  {p.offset + p.size for p in rep.partitions if partitions and p.size} | {len(data)})
    out: List[Found] = []
    for f in rep.findings:
        if f.kind not in kinds or f.details.get("in"):
            continue
        end = min([c for c in cuts if c > f.offset] or [len(data)])
        if f.kind in ("SquashFS", "UBI") and f.size:
            end = min(f.offset + f.size, len(data))
        chunk = bytes(memoryview(data)[f.offset:end])
        try:
            out.append(Found(f.offset, end - f.offset, open_fs(chunk)))
        except (FsError, ValueError) as e:
            out.append(Found(f.offset, end - f.offset, None, str(e)))
    return out
