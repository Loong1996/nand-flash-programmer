"""Find partitions, boot headers and file systems in a flash image (offline).

Recognised:

* U-Boot environment (single and redundant, CRC checked) and ``mtdparts=``
  partition tables in it or in a kernel command line
* legacy U-Boot images (uImage, header CRC checked) and FIT / device-tree
  blobs (FIT image list, ``fixed-partitions`` partition tables, ``bootargs``)
* file systems and containers: UBI, UBIFS, SquashFS, JFFS2, CramFS, gzip,
  xz, ELF, Android boot images, zImage, CPIO

The image must be the *main area* (no OOB); :func:`scan_file` strips the OOB
of a raw image first.
"""

from __future__ import annotations

import mmap
import os
import re
import struct
import tempfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- results


@dataclass
class Finding:
    offset: int
    kind: str                    # uImage | FIT | DTB | U-Boot env | UBI | SquashFS | ...
    size: Optional[int] = None   # bytes, when known
    name: str = ""
    details: Dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        s = "0x%08X  %-12s" % (self.offset, self.kind)
        if self.size is not None:
            s += " %10s" % _fmt_size(self.size)
        else:
            s += " %10s" % ""
        if self.name:
            s += "  " + self.name
        extra = ", ".join("%s=%s" % (k, v) for k, v in self.details.items()
                          if k not in ("vars", "images", "partitions") and v not in (None, ""))
        if extra:
            s += "  (" + extra + ")"
        return s

    def as_dict(self) -> dict:
        return {"offset": self.offset, "kind": self.kind, "size": self.size, "name": self.name,
                "details": {k: v for k, v in self.details.items() if k != "vars"}}


@dataclass
class Partition:
    name: str
    offset: int
    size: Optional[int]          # None = to the end of the device
    source: str                  # "mtdparts (U-Boot env @0x...)" / "device tree @0x..."
    read_only: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "offset": self.offset, "size": self.size, "source": self.source,
                "read_only": self.read_only}


@dataclass
class ScanReport:
    size: int = 0
    findings: List[Finding] = field(default_factory=list)
    partitions: List[Partition] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        s = ["%s scanned, %d findings" % (_fmt_size(self.size), len(self.findings))]
        for f in self.findings:
            s.append("  " + f.describe())
        if self.partitions:
            src = sorted({p.source for p in self.partitions})
            s.append("partition table (%s):" % "; ".join(src))
            for p in self.partitions:
                end = self.size if p.size is None else p.offset + p.size
                inside = [f.kind for f in self.findings if p.offset <= f.offset < end]
                s.append("  %-16s 0x%08X  %10s%s%s" % (
                    p.name, p.offset, self._psize(p), "  ro" if p.read_only else "",
                    ("  -> " + ", ".join(dict.fromkeys(inside))) if inside else ""))
        for key in ("bootargs", "bootcmd"):
            if key in self.env:
                s.append("%s = %s" % (key, self.env[key][:200]))
        if not self.findings:
            s.append("nothing recognised - is this the main area (no OOB) of a raw image?")
        return "\n".join(s)

    def table(self) -> dict:
        return {"cols": ["偏移", "类型", "大小", "名称 / 说明"],
                "rows": [["0x%08X" % f.offset, f.kind, _fmt_size(f.size) if f.size else "",
                          f.name + (" · " if f.name and f.details else "") +
                          ", ".join("%s=%s" % (k, v) for k, v in f.details.items()
                                    if k not in ("vars", "images", "partitions") and v not in (None, ""))]
                         for f in self.findings]}

    def _psize(self, p: Partition) -> str:
        """Partition size; '-' partitions run to the end of the image."""
        if p.offset >= self.size:
            return "beyond image" if p.size is None else _fmt_size(p.size) + " (beyond image)"
        if p.size is None:
            return _fmt_size(self.size - p.offset) + " (rest)"
        return _fmt_size(p.size) + (" (past end)" if p.offset + p.size > self.size else "")

    def partition_table(self) -> dict:
        rows = []
        for p in self.partitions:
            end = self.size if p.size is None else p.offset + p.size
            inside = [f.kind for f in self.findings if p.offset <= f.offset < end]
            rows.append([p.name, "0x%08X" % p.offset, self._psize(p),
                         ", ".join(dict.fromkeys(inside)), p.source])
        return {"cols": ["分区", "偏移", "大小", "内容", "来源"], "rows": rows}


def _fmt_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    for unit, div in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= div and n % (div // 16 or 1) == 0:
            return ("%.4g %s" % (n / div, unit))
    return "%d B" % n


# --------------------------------------------------------------------------- mtdparts

_SIZE_RE = re.compile(r"(0x[0-9a-fA-F]+|\d+)([kKmMgG]?)")


def _parse_size(s: str) -> int:
    m = _SIZE_RE.fullmatch(s.strip())
    if not m:
        raise ValueError("bad size %r" % s)
    n = int(m.group(1), 0)
    return n << {"": 0, "k": 10, "m": 20, "g": 30}[m.group(2).lower()]


def parse_mtdparts(text: str, source: str = "mtdparts") -> List[Partition]:
    """``mtdparts=nand0:1M(u-boot)ro,512k@0x100000(env),-(rootfs)`` -> partitions.

    Only the first device's list is returned (the NAND chip being read)."""
    t = text.strip()
    if "mtdparts=" in t:
        t = t.split("mtdparts=", 1)[1]
    t = t.split()[0] if t else ""
    dev_part = t.split(";")[0]
    if ":" not in dev_part:
        return []
    mtd_id, spec = dev_part.split(":", 1)
    out: List[Partition] = []
    pos = 0
    for item in spec.split(","):
        item = item.strip()
        m = re.fullmatch(r"(-|[0-9a-fA-Fx]+[kKmMgG]?)(?:@([0-9a-fA-Fx]+[kKmMgG]?))?\(([^)]*)\)(ro)?(lk)?",
                         item)
        if not m:
            continue
        size = None if m.group(1) == "-" else _parse_size(m.group(1))
        if m.group(2):
            pos = _parse_size(m.group(2))
        out.append(Partition(m.group(3), pos, size, "%s (%s)" % (source, mtd_id), bool(m.group(4))))
        if size is None:
            break
        pos += size
    return out


# --------------------------------------------------------------------------- U-Boot env

ENV_SIZES = [0x1000, 0x2000, 0x4000, 0x8000, 0x10000, 0x20000, 0x40000]
_ENV_KEYS = (b"bootcmd=", b"bootargs=", b"bootdelay=", b"baudrate=", b"ethaddr=", b"mtdparts=")
_PRINTABLE = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}


def _env_starts(buf, hit: int) -> List[int]:
    """Candidate starts of the NUL-separated ``key=value`` list that contains ``hit``.

    The walk back stops at the first entry that does not look like ``key=value``;
    that entry may be the real first variable glued to printable CRC bytes, so
    every position inside it is a candidate too (the CRC check decides)."""
    start = hit
    while start > 0 and buf[start - 1] == 0:
        j = start - 2
        while j >= 0 and buf[j] in _PRINTABLE:
            j -= 1
        entry = bytes(buf[j + 1:start - 1])
        if not entry or b"=" not in entry:
            break
        if not re.match(rb"^[A-Za-z0-9_.\-:]+=", entry):
            eq = entry.index(b"=")
            return [start] + [j + 1 + k for k in range(max(0, eq - 64), eq)]
        start = j + 1
    return [start]


def find_env(buf, limit: int = 8) -> List[Tuple[Finding, Dict[str, str]]]:
    """U-Boot environments (CRC-checked); returns (finding, variables)."""
    found: List[Tuple[Finding, Dict[str, str]]] = []
    seen = set()
    size = len(buf)
    for key in _ENV_KEYS:
        pos = buf.find(key)
        while pos >= 0 and len(found) < limit:
            if pos == 0 or buf[pos - 1] == 0 or pos in (4, 5):
                for s in _env_starts(buf, pos):
                    if s in seen:
                        continue
                    seen.add(s)
                    hit = _check_env(buf, s, size)
                    if hit:
                        found.append(hit)
                        break
            pos = buf.find(key, pos + 1)
    found.sort(key=lambda x: x[0].offset)
    return found


def _check_env(buf, s: int, total: int) -> Optional[Tuple[Finding, Dict[str, str]]]:
    for hdr in (4, 5):                               # 5: redundant env (CRC + flag byte)
        base = s - hdr
        if base < 0:
            continue
        crc = struct.unpack_from("<I", buf, base)[0]
        for env_size in ENV_SIZES:
            if base + env_size > total:
                break
            if zlib.crc32(buf[base + hdr:base + env_size]) & 0xFFFFFFFF == crc:
                data = bytes(buf[s:base + env_size])
                env = _parse_env(data)
                f = Finding(base, "U-Boot env", env_size, "%d variables" % len(env),
                            {"redundant": "yes, flags=%d" % buf[base + 4] if hdr == 5 else "",
                             "vars": env})
                return f, env
    return None


def _parse_env(data: bytes) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for entry in data.split(b"\0"):
        if not entry:
            break
        if b"=" in entry:
            k, v = entry.split(b"=", 1)
            env[k.decode("latin-1")] = v.decode("latin-1")
    return env


# --------------------------------------------------------------------------- uImage

UIMAGE_MAGIC = b"\x27\x05\x19\x56"
_UI_TYPES = {1: "standalone", 2: "kernel", 3: "ramdisk", 4: "multi", 5: "firmware", 6: "script",
             7: "filesystem", 8: "flat_dt", 17: "kernel_noload"}
_UI_OS = {0: "invalid", 1: "openbsd", 2: "netbsd", 3: "freebsd", 5: "linux", 17: "u-boot",
          20: "arm-trusted-firmware"}
_UI_ARCH = {2: "arm", 3: "x86", 5: "mips", 7: "ppc", 8: "s390", 15: "sparc64", 16: "m68k", 21: "nds32",
            22: "arm64", 26: "riscv", 27: "x86_64"}
_UI_COMP = {0: "none", 1: "gzip", 2: "bzip2", 3: "lzma", 4: "lzo", 5: "lz4", 6: "zstd"}


def parse_uimage(buf, off: int) -> Optional[Finding]:
    if off + 64 > len(buf):
        return None
    hdr = bytearray(buf[off:off + 64])
    (magic, hcrc, tstamp, size, load, ep, dcrc, os_, arch, typ, comp) = struct.unpack_from(
        ">IIIIIIIBBBB", hdr)
    hdr[4:8] = b"\0\0\0\0"
    if zlib.crc32(bytes(hdr)) & 0xFFFFFFFF != hcrc:
        return None
    name = bytes(hdr[32:64]).split(b"\0")[0].decode("latin-1")
    d: Dict[str, object] = {"type": _UI_TYPES.get(typ, typ), "os": _UI_OS.get(os_, os_),
                            "arch": _UI_ARCH.get(arch, arch), "comp": _UI_COMP.get(comp, comp),
                            "load": "0x%X" % load, "entry": "0x%X" % ep}
    if off + 64 + size <= len(buf):
        d["data_crc"] = "ok" if zlib.crc32(buf[off + 64:off + 64 + size]) & 0xFFFFFFFF == dcrc else "BAD"
    else:
        d["data_crc"] = "truncated"
    return Finding(off, "uImage", 64 + size, name, d)


# --------------------------------------------------------------------------- FDT / FIT

FDT_MAGIC = b"\xd0\x0d\xfe\xed"


@dataclass
class FdtNode:
    name: str
    props: Dict[str, bytes] = field(default_factory=dict)
    children: List["FdtNode"] = field(default_factory=list)

    def child(self, name: str) -> Optional["FdtNode"]:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def str_prop(self, name: str) -> str:
        v = self.props.get(name)
        return v.split(b"\0")[0].decode("latin-1", "replace") if v else ""

    def walk(self, path: str = ""):
        p = path + "/" + self.name if self.name else path or "/"
        yield p, self
        for c in self.children:
            yield from c.walk(p if p != "/" else "")


def parse_fdt(buf, off: int) -> Optional[Tuple[FdtNode, int]]:
    if off + 40 > len(buf):
        return None
    (magic, total, o_struct, o_strings, o_rsv, version, last_comp, _cpu, sz_strings,
     sz_struct) = struct.unpack_from(">IIIIIIIIII", buf, off)
    if version < 16 or version > 17 or last_comp > 17 or total < 40 or total > 64 << 20:
        return None
    if o_struct >= total or o_strings >= total or off + total > len(buf):
        return None
    strings = bytes(buf[off + o_strings:off + o_strings + sz_strings])
    p = off + o_struct
    end = off + total
    stack: List[FdtNode] = []
    root: Optional[FdtNode] = None
    try:
        while p < end:
            tok = struct.unpack_from(">I", buf, p)[0]
            p += 4
            if tok == 1:                                   # BEGIN_NODE
                e = buf.find(b"\0", p, end)
                name = bytes(buf[p:e]).decode("latin-1")
                p = (e + 4) & ~3
                node = FdtNode(name)
                if stack:
                    stack[-1].children.append(node)
                else:
                    root = node
                stack.append(node)
            elif tok == 2:                                 # END_NODE
                stack.pop()
            elif tok == 3:                                 # PROP
                ln, nameoff = struct.unpack_from(">II", buf, p)
                p += 8
                pname = strings[nameoff:strings.index(b"\0", nameoff)].decode("latin-1")
                stack[-1].props[pname] = bytes(buf[p:p + ln])
                p = (p + ln + 3) & ~3
            elif tok == 4:                                 # NOP
                continue
            elif tok == 9:                                 # END
                break
            else:
                return None
    except (IndexError, ValueError, struct.error):
        return None
    if root is None:
        return None
    return root, total


def _cells(v: bytes) -> List[int]:
    return [struct.unpack_from(">I", v, i)[0] for i in range(0, len(v) - 3, 4)]


def fdt_partitions(root: FdtNode, source: str) -> List[Partition]:
    out: List[Partition] = []
    for _path, node in root.walk():
        compat = node.props.get("compatible", b"")
        if b"fixed-partitions" not in compat and not any(c.name.startswith("partition@")
                                                        for c in node.children):
            continue
        acells = _cells(node.props.get("#address-cells", b"\0\0\0\1"))[0]
        scells = _cells(node.props.get("#size-cells", b"\0\0\0\1"))[0]
        for c in node.children:
            reg = c.props.get("reg")
            if not reg or not c.name.startswith("partition"):
                continue
            v = _cells(reg)
            if len(v) < acells + scells:
                continue
            addr = 0
            for x in v[:acells]:
                addr = (addr << 32) | x
            size = 0
            for x in v[acells:acells + scells]:
                size = (size << 32) | x
            out.append(Partition(c.str_prop("label") or c.name, addr, size, source,
                                 "read-only" in c.props))
    return out


def describe_fdt(buf, off: int) -> Optional[Tuple[Finding, List[Partition], str]]:
    r = parse_fdt(buf, off)
    if r is None:
        return None
    root, total = r
    images = root.child("images")
    bootargs = ""
    chosen = root.child("chosen")
    if chosen is not None:
        bootargs = chosen.str_prop("bootargs")
    if images is not None and images.children:
        imgs = []
        for c in images.children:
            size = len(c.props["data"]) if "data" in c.props else (
                _cells(c.props["data-size"])[0] if "data-size" in c.props else None)
            imgs.append("%s: %s%s%s" % (c.name, c.str_prop("type") or "?",
                                         (" " + c.str_prop("arch")) if c.str_prop("arch") else "",
                                         (" %s" % _fmt_size(size)) if size else ""))
        f = Finding(off, "FIT", total, root.str_prop("description"),
                    {"images": imgs, "contents": "; ".join(imgs)})
        return f, [], bootargs
    model = root.str_prop("model") or root.str_prop("compatible")
    parts = fdt_partitions(root, "device tree @0x%X" % off)
    f = Finding(off, "DTB", total, model, {"partitions": len(parts) or ""})
    return f, parts, bootargs


# --------------------------------------------------------------------------- magics

def _u32le(buf, o):
    return struct.unpack_from("<I", buf, o)[0]


def _squashfs(buf, o) -> Optional[Finding]:
    if o + 96 > len(buf):
        return None
    le = bytes(buf[o:o + 4]) == b"hsqs"
    fmt = "<" if le else ">"
    inodes, _mtime, block_size, frags, comp, blog, _flags, _ids, major, minor = struct.unpack_from(
        fmt + "IIIIHHHHHH", buf, o + 4)
    if major != 4 or block_size != 1 << blog or not 4096 <= block_size <= 1 << 20:
        return None
    used = struct.unpack_from(fmt + "Q", buf, o + 40)[0]
    comps = {1: "gzip", 2: "lzma", 3: "lzo", 4: "xz", 5: "lz4", 6: "zstd"}
    return Finding(o, "SquashFS", used, "", {"version": "%d.%d" % (major, minor),
                                            "comp": comps.get(comp, comp), "inodes": inodes,
                                            "block": _fmt_size(block_size)})


def _ubi(buf, o) -> Optional[Finding]:
    if o + 64 > len(buf) or buf[o + 4] != 1:
        return None
    # count consecutive erase blocks: find the PEB size from the next EC header
    nxt = buf.find(b"UBI#", o + 512)
    if nxt < 0:
        return Finding(o, "UBI", None, "", {"pebs": 1})
    peb = nxt - o
    n = 1
    p = o
    while p + peb < len(buf) and bytes(buf[p + peb:p + peb + 4]) == b"UBI#":
        p += peb
        n += 1
    return Finding(o, "UBI", n * peb, "", {"peb": _fmt_size(peb), "pebs": n})


def _ubifs(buf, o) -> Optional[Finding]:
    if o + 24 > len(buf) or buf[o + 20] != 6:          # superblock node
        return None
    leb = _u32le(buf, o + 36) if o + 44 <= len(buf) else 0
    lebs = _u32le(buf, o + 40) if o + 44 <= len(buf) else 0
    return Finding(o, "UBIFS", leb * lebs if leb and lebs < 1 << 20 else None, "",
                   {"leb": _fmt_size(leb) if leb else ""})


def _jffs2(buf, o) -> Optional[Finding]:
    if o + 12 > len(buf):
        return None
    le = buf[o] == 0x85
    fmt = "<" if le else ">"
    _magic, ntype, _ln = struct.unpack_from(fmt + "HHI", buf, o)
    if ntype not in (0xE001, 0xE002, 0x2003, 0x2004):
        return None
    return Finding(o, "JFFS2", None, "", {"endian": "little" if le else "big"})


def _cramfs(buf, o) -> Optional[Finding]:
    if o + 64 > len(buf) or bytes(buf[o + 16:o + 32]) != b"Compressed ROMFS":
        return None
    return Finding(o, "CramFS", _u32le(buf, o + 4), bytes(buf[o + 48:o + 64]).split(b"\0")[0].decode(
        "latin-1"))


def _android(buf, o) -> Optional[Finding]:
    if o + 64 > len(buf):
        return None
    ksize, _kaddr, rsize = struct.unpack_from("<III", buf, o + 8)
    return Finding(o, "Android boot", None, "", {"kernel": _fmt_size(ksize), "ramdisk": _fmt_size(rsize)})


def _elf(buf, o) -> Optional[Finding]:
    if o + 20 > len(buf) or buf[o + 4] not in (1, 2) or buf[o + 5] not in (1, 2):
        return None
    le = buf[o + 5] == 1
    mach = struct.unpack_from("<H" if le else ">H", buf, o + 18)[0]
    names = {3: "x86", 8: "mips", 40: "arm", 62: "x86_64", 183: "arm64", 243: "riscv"}
    return Finding(o, "ELF", None, "", {"bits": 32 if buf[o + 4] == 1 else 64,
                                        "machine": names.get(mach, mach)})


def _zimage(buf, o) -> Optional[Finding]:
    # ARM zImage: magic 0x016F2818 at +0x24, start/end at +0x28/+0x2C
    base = o - 0x24
    if base < 0:
        return None
    start, end = struct.unpack_from("<II", buf, o + 4)
    if end <= start or end - start > 64 << 20:
        return None
    return Finding(base, "zImage", end - start, "ARM Linux kernel")


def _gzip(buf, o) -> Optional[Finding]:
    if o + 10 > len(buf) or buf[o + 3] & 0xE0:
        return None
    name = ""
    if buf[o + 3] & 0x08:                             # FNAME
        e = buf.find(b"\0", o + 10, o + 266)
        if e > 0:
            name = bytes(buf[o + 10:e]).decode("latin-1")
    return Finding(o, "gzip", None, name)


def _simple(kind):
    return lambda buf, o: Finding(o, kind)


#: magic -> (parser, required alignment)
MAGICS = [
    (b"UBI#", _ubi, 512),
    (b"\x31\x18\x10\x06", _ubifs, 8),
    (b"hsqs", _squashfs, 4),
    (b"sqsh", _squashfs, 4),
    (b"\x85\x19", _jffs2, 4),
    (b"\x19\x85", _jffs2, 4),
    (b"\x45\x3d\xcd\x28", _cramfs, 4),
    (b"ANDROID!", _android, 512),
    (b"\x7fELF", _elf, 4),
    (b"\x18\x28\x6f\x01", _zimage, 4),
    (b"\x1f\x8b\x08", _gzip, 4),
    (b"\xfd7zXZ\x00", _simple("xz"), 4),
    (b"070701", _simple("cpio"), 4),
]


def _scan_magic(buf, magic: bytes, parser, align: int, limit: int) -> List[Finding]:
    out: List[Finding] = []
    pos = buf.find(magic)
    covered_to = -1
    while pos >= 0 and len(out) < limit:
        if pos % align == 0 and pos >= covered_to:
            f = parser(buf, pos)
            if f is not None:
                out.append(f)
                # skip the inside of containers so their own nodes are not reported again
                if f.kind in ("UBI", "SquashFS", "CramFS") and f.size:
                    covered_to = f.offset + f.size
                elif f.kind == "JFFS2":
                    covered_to = pos + (1 << 20)      # one finding per MiB of JFFS2 nodes
                elif f.kind == "UBIFS":
                    covered_to = pos + (1 << 20)
        pos = buf.find(magic, pos + 1)
    return out


# --------------------------------------------------------------------------- driver

def scan(buf, limit: int = 64) -> ScanReport:
    """Scan a main-area image (bytes, bytearray or mmap)."""
    rep = ScanReport(size=len(buf))
    cand: List[Tuple[int, List[Partition]]] = []          # (priority, partitions)
    for f, env in find_env(buf):
        rep.findings.append(f)
        if not rep.env:
            rep.env = dict(env)
        bootargs = env.get("bootargs", "")
        mp = env.get("mtdparts") or (bootargs if "mtdparts=" in bootargs else "")
        if mp:
            cand.append((0, parse_mtdparts(mp, "mtdparts, U-Boot env @0x%X" % f.offset)))
    pos = buf.find(UIMAGE_MAGIC)
    while pos >= 0 and len(rep.findings) < limit * 4:
        ui = parse_uimage(buf, pos)
        if ui:
            rep.findings.append(ui)
        pos = buf.find(UIMAGE_MAGIC, pos + 1)
    pos = buf.find(FDT_MAGIC)
    while pos >= 0 and len(rep.findings) < limit * 4:
        if pos % 4 == 0:
            r = describe_fdt(buf, pos)
            if r:
                f, parts, bootargs = r
                rep.findings.append(f)
                if parts:
                    cand.append((2, parts))
                if bootargs and "mtdparts=" in bootargs:
                    cand.append((1, parse_mtdparts(bootargs, "bootargs in device tree @0x%X" % pos)))
                if bootargs and "bootargs" not in rep.env:
                    rep.env["bootargs"] = bootargs
                pos = buf.find(FDT_MAGIC, pos + max(4, f.size or 4))
                continue
        pos = buf.find(FDT_MAGIC, pos + 1)
    covered = [(f.offset, f.offset + (f.size or 0)) for f in rep.findings if f.kind in ("uImage", "FIT")]
    for magic, parser, align in MAGICS:
        for f in _scan_magic(buf, magic, parser, align, limit):
            if any(a < f.offset < b for a, b in covered):
                continue                                   # inside a uImage/FIT payload
            rep.findings.append(f)
    rep.findings.sort(key=lambda f: f.offset)
    for f in rep.findings:
        if f.kind == "UBIFS" and any(u.kind == "UBI" and u.size and u.offset <= f.offset < u.offset + u.size
                                     for u in rep.findings):
            f.details["in"] = "UBI volume"
    cand = [c for c in cand if c[1]]
    if cand:
        rep.partitions = min(cand, key=lambda c: c[0])[1]
    return rep


def scan_file(path: str, page: int = 0, oob: int = 0, ppb: int = 64, limit: int = 64) -> ScanReport:
    """Scan an image file; a raw image (``oob`` > 0) is stripped to its main area first."""
    tmp = None
    try:
        if oob:
            from .image import Geometry, strip_oob

            fd, tmp = tempfile.mkstemp(prefix="nsprog-scan-")
            with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
                strip_oob(src, dst, Geometry(page, oob, ppb))
            path = tmp
        with open(path, "rb") as f:
            if os.path.getsize(path) == 0:
                return ScanReport()
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                return scan(m, limit)
    finally:
        if tmp:
            os.unlink(tmp)
