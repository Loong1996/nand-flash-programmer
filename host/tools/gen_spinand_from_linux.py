#!/usr/bin/env python3
"""Regenerate host/src/nsprog/data/spi_nand_linux.csv from the Linux kernel
SPI NAND drivers (drivers/mtd/nand/spi/*.c).

    python host/tools/gen_spinand_from_linux.py <dir with the .c files>
"""
import csv
import glob
import os
import re
import sys

OUT = os.path.join(os.path.dirname(__file__), "..", "src", "nsprog", "data", "spi_nand_linux.csv")

ENTRY = re.compile(
    r'SPINAND_INFO\(\s*"(?P<name>[^"]+)"\s*,\s*(?:/\*(?P<cmt>[^*]*)\*/)?\s*'
    r'SPINAND_ID\(\s*SPINAND_READID_METHOD_(?P<meth>\w+)\s*,(?P<ids>[^)]*)\)\s*,\s*'
    r'NAND_MEMORG\((?P<org>[^)]*)\)', re.S)


def voltage(name, cmt):
    if cmt:
        m = re.search(r"([0-9.]+)\s*V", cmt)
        if m:
            return float(m.group(1))
    n = name.upper()
    rules = [
        (r"^GD5F\d+G[QM]\d+U", 3.3), (r"^GD5F\d+G[QM]\d+R", 1.8),
        (r"^MX3[15]LF", 3.3), (r"^MX3[15]UF", 1.8),
        (r"^MT29F\d+G01AB[A]", 3.3), (r"^MT29F\d+G01AB[B]", 1.8),
        (r"^T[CH]58C[V]G", 3.3), (r"^T[CH]58C[Y]G", 1.8),
        (r"^T[CH]58N[V]G", 3.3), (r"^T[CH]58N[Y]G", 1.8),
        (r"^F50L", 3.3), (r"^F50D", 1.8),
        (r"^F35S", 3.3), (r"^F35U", 1.8),
        (r"^XT26G", 3.3), (r"^XT26Q", 1.8),
        (r"^DS35Q", 3.3), (r"^DS35M", 1.8),
    ]
    for pat, v in rules:
        if re.search(pat, n):
            return v
    return 0.0


def main(src):
    rows = []
    for path in sorted(glob.glob(os.path.join(src, "*.c"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        mfrs = dict(re.findall(r"#define\s+(SPINAND_MFR_\w+)\s+(0x[0-9a-fA-F]+)", text))
        # table name -> manufacturer ID, from "struct spinand_manufacturer { .id, .chips }"
        table_mfr = {}
        for body in re.findall(r"struct spinand_manufacturer\s+\w+\s*=\s*\{(.*?)\};", text, re.S):
            mid = re.search(r"\.id\s*=\s*(SPINAND_MFR_\w+)", body)
            tab = re.search(r"\.chips\s*=\s*(\w+)", body)
            if mid and tab and mid.group(1) in mfrs:
                table_mfr[tab.group(1)] = int(mfrs[mid.group(1)], 16)
        vendor = os.path.basename(path)[:-2]
        spans = [(m.start(), m.group(1)) for m in
                 re.finditer(r"struct spinand_info\s+(\w+)\s*\[\]\s*=", text)]
        for e in ENTRY.finditer(text):
            owner = [t for pos, t in spans if pos < e.start()]
            mfr = table_mfr.get(owner[-1]) if owner else None
            if mfr is None:
                continue
            ids = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", e.group("ids"))]
            org = [int(x) for x in e.group("org").split(",")]
            bits, page, oob, ppb, blocks, _maxbad, planes, luns, targets = org
            if bits != 1:
                continue
            full = bytes([mfr] + ids)
            note = "only the first of %d dies is used" % (targets * luns) if targets * luns > 1 else ""
            rows.append([e.group("name"), full.hex().upper(), page, oob, ppb, blocks,
                         1 if planes > 1 else 0, e.group("meth").lower(),
                         voltage(e.group("name"), e.group("cmt")), vendor, note])
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        f.write("# Generated from the Linux kernel drivers/mtd/nand/spi/*.c by host/tools/gen_spinand_from_linux.py\n")
        f.write("# name, full ID (manufacturer first), page, spare, pages/block, blocks/die, plane select, "
                "read-ID method (opcode_dummy|opcode_addr|opcode), voltage (0 = see datasheet), vendor, note\n")
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)
    print("%d entries -> %s" % (len(rows), OUT))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
