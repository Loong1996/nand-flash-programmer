"""Pure-Python LZO1X decompressor (the format used by UBIFS, SquashFS and JFFS2).

A straight port of the Linux kernel's ``lzo1x_decompress_safe()`` (bitstream
version 0; the lzo-rle extension used only by zram is rejected).
"""

from __future__ import annotations


class LzoError(ValueError):
    pass


M2_MAX_OFFSET = 0x0800


def decompress(src: bytes, dst_len: int = 0) -> bytes:
    """Decompress a raw LZO1X stream. ``dst_len`` (if known) bounds the output."""
    ip = 0
    n = len(src)
    out = bytearray()
    limit = dst_len or (1 << 30)
    if n < 3:
        raise LzoError("input too short")
    if n >= 5 and src[0] == 17:
        if src[1]:
            raise LzoError("lzo-rle streams are not supported")
        ip = 2
    state = 0
    nxt = 0

    def need(k: int) -> None:
        if ip + k > n:
            raise LzoError("input overrun")

    def copy_match(pos: int, length: int) -> None:
        if pos < 0:
            raise LzoError("lookbehind overrun")
        if len(out) + length > limit:
            raise LzoError("output overrun")
        if pos + length <= len(out):
            out.extend(out[pos:pos + length])
        else:
            for k in range(length):
                out.append(out[pos + k])

    def copy_lit(length: int) -> None:
        nonlocal ip
        need(length)
        if len(out) + length > limit:
            raise LzoError("output overrun")
        out.extend(src[ip:ip + length])
        ip += length

    first = True
    while True:
        if first and src[ip] > 17:
            first = False
            t = src[ip] - 17
            ip += 1
            if t < 4:
                nxt = t
                state = nxt
                copy_lit(nxt)
                continue
            copy_lit(t)
            state = 4
            continue
        first = False
        need(1)
        t = src[ip]
        ip += 1
        if t < 16:
            if state == 0:
                if t == 0:
                    ip_last = ip
                    while True:
                        need(1)
                        if src[ip] != 0:
                            break
                        ip += 1
                    t += (ip - ip_last) * 255 + 15 + src[ip]
                    ip += 1
                t += 3
                copy_lit(t)
                state = 4
                continue
            if state != 4:
                nxt = t & 3
                need(1)
                pos = len(out) - 1 - (t >> 2) - (src[ip] << 2)
                ip += 1
                copy_match(pos, 2)
            else:
                nxt = t & 3
                need(1)
                pos = len(out) - (1 + M2_MAX_OFFSET) - (t >> 2) - (src[ip] << 2)
                ip += 1
                copy_match(pos, 3)
        else:
            if t >= 64:
                nxt = t & 3
                need(1)
                pos = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                length = (t >> 5) - 1 + 2
            elif t >= 32:
                length = (t & 31) + 2
                if length == 2:
                    ip_last = ip
                    while True:
                        need(1)
                        if src[ip] != 0:
                            break
                        ip += 1
                    length += (ip - ip_last) * 255 + 31 + src[ip]
                    ip += 1
                need(2)
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                pos = len(out) - 1 - (v >> 2)
                nxt = v & 3
            else:                                          # 16..31
                need(2)
                length = (t & 7) + 2
                far = (t & 8) << 11
                if length == 2:
                    ip_last = ip
                    while True:
                        need(1)
                        if src[ip] != 0:
                            break
                        ip += 1
                    length += (ip - ip_last) * 255 + 7 + src[ip]
                    ip += 1
                need(2)
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                pos = len(out) - far - (v >> 2)
                nxt = v & 3
                if pos == len(out):                        # end-of-stream marker
                    if length != 3:
                        raise LzoError("bad end marker")
                    return bytes(out)
                pos -= 0x4000
            copy_match(pos, length)
        state = nxt
        if nxt:
            copy_lit(nxt)
