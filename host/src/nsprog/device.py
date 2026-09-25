"""Connection to the programmer: batch execution, resync and baud negotiation."""

from __future__ import annotations

import collections
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

from . import protocol as P
from .link import Link, LinkError

log = logging.getLogger(__name__)

#: UART rates tried by :meth:`Device.negotiate_baud` (27 MHz / rate is exact
#: or within 1%).
BAUD_CANDIDATES = [3_000_000, 1_500_000, 1_000_000, 460_800]
DEFAULT_BAUD = 115_200
CLK_HZ = 27_000_000


class ProtocolError(IOError):
    pass


@dataclass
class _Segment:
    ops: List[P.Op]
    data: bytes
    rlen: int
    marker: int
    wait_ms: int


class Device:
    """Executes :class:`~nsprog.protocol.Batch` objects on the programmer."""

    def __init__(self, link: Link, window: Optional[int] = None):
        self.link = link
        self.window = window or 3584
        self._info: Optional[P.Info] = None
        self._marker = 0
        self.pin_ctrl = P.PIN_CTRL_DEFAULT
        self.ft_phase: Optional[int] = None      # sync FIFO clock phase set by this session
        self.cancelled: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------ setup
    @property
    def opened(self) -> bool:
        return self._info is not None

    @property
    def info(self) -> P.Info:
        """INFO of the programmer (available after :meth:`open`)."""
        if self._info is None:
            raise ProtocolError("device is not open")
        return self._info

    @info.setter
    def info(self, value: P.Info) -> None:
        self._info = value

    def open(self, negotiate: bool = True) -> P.Info:
        try:
            self.resync()
        except ProtocolError:
            # The engine may still be running at a rate negotiated by an
            # earlier session that did not close cleanly.
            if self.link.baudrate is None:
                raise
            for baud in BAUD_CANDIDATES + [DEFAULT_BAUD]:
                self.link.set_baudrate(baud)
                try:
                    self.resync(attempts=1)
                    break
                except ProtocolError:
                    continue
            else:
                raise
        self.info = self.query_info()
        self.window = max(256, self.info.rx_fifo - 512)
        if negotiate and self.link.baudrate is not None:
            self.negotiate_baud()
        return self.info

    def query_info(self) -> P.Info:
        b = P.Batch()
        r = b.info()
        self.run(b)
        return r.value

    def resync(self, attempts: int = 3) -> None:
        """Bring host and engine back into step (see protocol §5)."""
        last_err = None
        for _ in range(attempts):
            self.link.drain(quiet=0.15)
            self.link.write(bytes([P.ECHO, 0x5A, P.ECHO, 0xA5, P.INFO]))
            want = bytes([0x5A, 0xA5]) + P.INFO_MAGIC
            buf = self.link.read(2 + P.INFO_LEN, 1.0)
            pos = buf.find(want)
            if pos >= 0:
                tail = buf[pos + 2:]
                tail += self.link.read(P.INFO_LEN - len(tail), 1.0)
                if len(tail) == P.INFO_LEN:
                    self.info = P.Info.parse(tail)
                    return
            last_err = "no response (got %r)" % buf[:32]
        raise ProtocolError("programmer not responding on %s: %s" % (self.link.name, last_err))

    def _clk(self) -> int:
        """Engine clock (27 MHz before gateware 1.3, 54 MHz with the PLL)."""
        return self.info.clk_hz if self._info is not None else CLK_HZ

    def negotiate_baud(self, candidates: Optional[List[int]] = None) -> int:
        """Try faster UART rates; the engine falls back on its own if one fails."""
        env = os.environ.get("NSPROG_BAUD")
        if env:
            candidates = [int(env)]
        for baud in candidates or BAUD_CANDIDATES:
            if self._try_baud(baud):
                log.info("UART running at %d baud", baud)
                return baud
        return self.link.baudrate or DEFAULT_BAUD

    def _try_baud(self, baud: int) -> bool:
        div = round(self._clk() / baud)
        self.link.write(bytes([P.SET_BAUD]) + div.to_bytes(2, "little"))
        ack = self.link.read(1, 1.0)
        if ack != bytes([P.BAUD_ACK]):
            self.resync()
            return False
        time.sleep(0.02)
        self.link.set_baudrate(baud)
        time.sleep(0.02)
        self.link.drain(quiet=0.02, limit=0.1)
        self.link.write(bytes([P.ECHO, 0x3C, P.ECHO, 0xC3]))
        if self.link.read(2, 0.4) == bytes([0x3C, 0xC3]):
            return True
        # Engine reverts to the default rate after 1 s without confirmation.
        self.link.set_baudrate(DEFAULT_BAUD)
        time.sleep(1.2)
        self.resync()
        return False

    # ------------------------------------------------------------ execution
    def run(self, batch: P.Batch, progress: Optional[Callable[[int], None]] = None) -> None:
        """Execute ``batch``; fills in every Result. ``progress`` gets response bytes."""
        segments = self._segment(batch.ops)
        inflight: Deque[_Segment] = collections.deque()
        inflight_bytes = 0
        buf = bytearray()
        i = 0
        deadline = None
        try:
            while i < len(segments) or inflight:
                while i < len(segments) and (not inflight or
                                             inflight_bytes + len(segments[i].data) <= self.window):
                    seg = segments[i]
                    self.link.write(seg.data)
                    inflight.append(seg)
                    inflight_bytes += len(seg.data)
                    i += 1
                    if len(inflight) == 1:
                        deadline = None
                head = inflight[0]
                if deadline is None:
                    deadline = self._deadline(head)
                need = head.rlen - len(buf)
                chunk = self.link.read(need, min(0.2, max(0.0, deadline - time.monotonic())))
                if chunk:
                    buf += chunk
                    deadline = self._deadline(head)
                    if progress:
                        progress(len(chunk))
                if len(buf) >= head.rlen:
                    self._dispatch(head, bytes(buf[:head.rlen]))
                    del buf[:head.rlen]
                    inflight.popleft()
                    inflight_bytes -= len(head.data)
                    deadline = None
                elif time.monotonic() > deadline:
                    raise ProtocolError("timeout waiting for %d response bytes" % need)
                if self.cancelled() and i < len(segments):
                    segments = segments[:i]
        except (ProtocolError, LinkError, OSError):
            try:
                self.resync()
            except Exception:
                pass
            raise

    def _deadline(self, seg: _Segment) -> float:
        transfer = (seg.rlen + len(seg.data)) / max(1, self.link.bytes_per_sec)
        return time.monotonic() + 3.0 + transfer * 2 + seg.wait_ms / 1000.0

    def _segment(self, ops: List[P.Op]) -> List[_Segment]:
        out: List[_Segment] = []
        cur: List[P.Op] = []
        size = 0
        limit = self.window - 2
        for op in ops:
            if cur and size + len(op.data) > limit:
                out.append(self._close(cur))
                cur, size = [], 0
            cur.append(op)
            size += len(op.data)
        if cur:
            out.append(self._close(cur))
        return out

    def _close(self, ops: List[P.Op]) -> _Segment:
        self._marker = (self._marker + 1) & 0xFF
        data = b"".join(o.data for o in ops) + bytes([P.ECHO, self._marker])
        rlen = sum(o.rlen for o in ops) + 1
        return _Segment(ops, data, rlen, self._marker, sum(o.wait_ms for o in ops))

    @staticmethod
    def _dispatch(seg: _Segment, raw: bytes) -> None:
        if raw[-1] != seg.marker:
            raise ProtocolError("stream out of sync (marker %02x != %02x)" % (raw[-1], seg.marker))
        pos = 0
        for op in seg.ops:
            if op.rlen and op.result is not None:
                op.result.parts.append(raw[pos:pos + op.rlen])
                pos += op.rlen
            if op.result is not None and op.last_part:
                op.result._finish()

    # ------------------------------------------------------------ convenience
    def set_pin_ctrl(self, value: int, batch: Optional[P.Batch] = None) -> None:
        self.pin_ctrl = value
        b = batch if batch is not None else P.Batch()
        b.set_reg(P.REG_PIN_CTRL, value)
        if batch is None:
            self.run(b)

    def pins(self) -> P.Pins:
        b = P.Batch()
        r = b.get_pins()
        self.run(b)
        return r.value

    def close(self) -> None:
        """Leave the engine in a safe state and at the default UART rate."""
        try:
            b = P.Batch()
            b.nand_ce(False)
            b.spi_cs(False)
            b.set_reg(P.REG_PIN_CTRL, P.PIN_CTRL_DEFAULT)
            self.run(b)
            rate = self.link.baudrate
            if rate is not None and rate != DEFAULT_BAUD:
                div = round(self._clk() / DEFAULT_BAUD)
                self.link.write(bytes([P.SET_BAUD]) + div.to_bytes(2, "little"))
                self.link.read(1, 0.5)
        except Exception:
            pass
        self.link.close()


def connect(port: Optional[str] = None, *, emulate: Optional[str] = None,
            negotiate: bool = True, ft_phase: bool = True) -> Device:
    """Open a programmer.

    ``port``: serial port path, ``"ft232h"``/``ftdi://...`` for the FT232H
    async FIFO, ``"ft232h-sync"`` (or a URL ending in ``+sync``) for the 245
    synchronous FIFO, or None to auto-detect (FT232H first, then likely serial
    ports).
    ``ft_phase``: on the sync FIFO link, apply the clock phase saved by
    ``nsprog ft232h-tune``.
    ``emulate``: use the built-in software emulator (e.g. ``"nand"``,
    ``"spinor"``, ``"spinand"``, ``"all"``).
    """
    from .link import FtdiLink, SerialLink, find_ft232h_url, list_serial_ports

    if emulate or (port or "").startswith("emu"):
        from .emulator import EmulatorLink

        spec_port = port or ""
        spec = emulate or (spec_port.split(":", 1)[1] if ":" in spec_port else "all")
        dev = Device(EmulatorLink.from_spec(spec))
        dev.open(negotiate=False)
        return dev

    if port and (port in ("ft232h", "ft232h-sync") or port.startswith("ftdi://")):
        sync = port == "ft232h-sync" or port.endswith("+sync")
        url = None if port.startswith("ft232h") else port[:-5] if port.endswith("+sync") else port
        dev = Device(FtdiLink(url, sync=sync))
        try:
            dev.open(negotiate=False)
        except Exception as e:
            dev.close()
            if sync:
                raise LinkError("%s. In sync FIFO mode the FPGA needs CLKOUT (FT232H AC5) on "
                                "pin 36 and gateware >= 1.1; try port 'ft232h' (async)" % e) from e
            raise
        if sync and ft_phase:
            from .ft232h import apply_saved_phase

            apply_saved_phase(dev)
        return dev

    if port:
        dev = Device(SerialLink(port))
        dev.open(negotiate=negotiate)
        return dev

    errors = []
    url = find_ft232h_url()
    if url:
        try:
            dev = Device(FtdiLink(url))
            dev.open(negotiate=False)
            return dev
        except Exception as e:  # pragma: no cover - hardware path
            errors.append("%s: %s" % (url, e))
    for p in list_serial_ports():
        if not p["likely"]:
            continue
        try:
            dev = Device(SerialLink(p["device"]))
            dev.open(negotiate=negotiate)
            return dev
        except Exception as e:  # pragma: no cover - hardware path
            errors.append("%s: %s" % (p["device"], e))
    raise LinkError("no programmer found. " + "; ".join(errors) if errors else
                    "no programmer found (is the Tang Nano 9K plugged in and flashed?)")
