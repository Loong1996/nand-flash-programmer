"""Byte-stream links to the programmer: UART (pyserial) and FT232H FIFO (pyftdi)."""

from __future__ import annotations

import time
from typing import List, Optional


class LinkError(IOError):
    pass


class Link:
    """A bidirectional byte stream."""

    name = "link"
    #: True when the link applies back-pressure (FT245 FIFO).
    flow_controlled = False
    #: Rough sustained throughput used for timeout estimates (bytes/s).
    bytes_per_sec = 10_000

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, n: int, timeout: float) -> bytes:
        """Return up to ``n`` bytes, waiting at most ``timeout`` seconds."""
        raise NotImplementedError

    def drain(self, quiet: float = 0.15, limit: float = 3.0) -> int:
        """Discard input until the line has been silent for ``quiet`` seconds."""
        dropped = 0
        end = time.monotonic() + limit
        last = time.monotonic()
        while time.monotonic() < end:
            got = self.read(65536, 0.02)
            if got:
                dropped += len(got)
                last = time.monotonic()
            elif time.monotonic() - last >= quiet:
                break
        return dropped

    def close(self) -> None:
        pass

    # UART only
    def set_baudrate(self, baud: int) -> None:
        raise LinkError("baud rate is not adjustable on this link")

    @property
    def baudrate(self) -> Optional[int]:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SerialLink(Link):
    """On-board BL702 USB-UART of the Tang Nano 9K (or any serial port)."""

    flow_controlled = False

    def __init__(self, port: str, baud: int = 115200):
        import serial  # pyserial

        self.name = port
        self._ser = serial.Serial(port, baud, timeout=0.05, write_timeout=10)
        self._ser.reset_input_buffer()
        self.bytes_per_sec = baud // 10

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def read(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        end = time.monotonic() + timeout
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            self._ser.timeout = min(left, 0.1)
            chunk = self._ser.read(min(n - len(buf), max(1, self._ser.in_waiting)))
            if chunk:
                buf += chunk
        return bytes(buf)

    def set_baudrate(self, baud: int) -> None:
        self._ser.baudrate = baud
        self.bytes_per_sec = baud // 10

    @property
    def baudrate(self) -> Optional[int]:
        return self._ser.baudrate

    def close(self) -> None:
        self._ser.close()


#: Custom product ID that ``nsprog ft232h-setup --custom-pid`` programs so that
#: the macOS built-in FTDI serial driver does not grab the FT232H.
CUSTOM_PID = 0x6FF0

#: USB IDs tried for FT232H (factory default first, then the custom PID).
FT232H_IDS = [(0x0403, 0x6014), (0x0403, CUSTOM_PID)]


class FtdiLink(Link):
    """FT232H FIFO link (EEPROM channel type = 245 FIFO).

    ``sync=False``: 245 asynchronous FIFO (~2 MB/s).
    ``sync=True``: 245 synchronous FIFO: the FT232H drives a 60 MHz CLKOUT
    and the gateware (>= 1.1) switches to its sync bridge (~10+ MB/s).
    """

    flow_controlled = True

    def __init__(self, url: Optional[str] = None, sync: bool = False):
        from pyftdi.ftdi import Ftdi

        self._ftdi = Ftdi()
        url = url or find_ft232h_url()
        if url is None:
            raise LinkError("no FT232H found")
        self.name = url + ("+sync" if sync else "")
        self.sync = sync
        self.bytes_per_sec = 20_000_000 if sync else 2_000_000
        self._ftdi.open_from_url(url)
        self._ftdi.set_bitmode(0, Ftdi.BitMode.RESET)   # EEPROM FIFO mode = async 245
        if sync:
            self._ftdi.set_bitmode(0xFF, Ftdi.BitMode.SYNCFF)
            self._ftdi.read_data_set_chunksize(0x10000)
            self._ftdi.write_data_set_chunksize(0x10000)
            time.sleep(0.02)                            # FPGA: detect CLKOUT, switch bridges
        self._ftdi.set_latency_timer(1 if not sync else 2)
        self._ftdi.purge_buffers()

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        step = 0x10000 if self.sync else 4096
        while view:
            n = self._ftdi.write_data(view[:step])
            view = view[n:]

    def read(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        end = time.monotonic() + timeout
        while len(buf) < n:
            chunk = self._ftdi.read_data_bytes(n - len(buf), attempt=1)
            if chunk:
                buf += chunk
            elif time.monotonic() >= end:
                break
            else:
                time.sleep(0.0005)
        return bytes(buf)

    def close(self) -> None:
        if self.sync:
            try:           # stop CLKOUT so the FPGA falls back to its async bridge
                from pyftdi.ftdi import Ftdi

                self._ftdi.set_bitmode(0, Ftdi.BitMode.RESET)
            except Exception:  # pragma: no cover - hardware path
                pass
        self._ftdi.close()


def find_ft232h_url() -> Optional[str]:
    try:
        from pyftdi.ftdi import Ftdi
        from pyftdi.usbtools import UsbTools
    except Exception:  # pragma: no cover - pyftdi missing
        return None
    register_custom_ids()
    try:
        devs = UsbTools.find_all(FT232H_IDS)
    except Exception:
        return None
    for desc, _iface in devs:
        return "ftdi://0x%04x:0x%04x:%s/1" % (desc.vid, desc.pid, desc.sn or "")
    return None


def register_custom_ids() -> None:
    try:
        from pyftdi.ftdi import Ftdi

        Ftdi.add_custom_product(0x0403, CUSTOM_PID, "nsprog")
    except Exception:
        pass


def list_serial_ports() -> List[dict]:
    try:
        from serial.tools import list_ports
    except Exception:  # pragma: no cover
        return []
    out = []
    for p in list_ports.comports():
        out.append({
            "device": p.device,
            "description": p.description or "",
            "vid": p.vid,
            "pid": p.pid,
            "likely": _likely_tang(p),
        })
    # The Tang Nano 9K exposes JTAG (interface A) and UART (interface B); the
    # UART is the higher-numbered port on every OS (ttyUSB1, ...usbserial-xxx1, COMn+1).
    out.sort(key=lambda d: d["device"], reverse=True)
    out.sort(key=lambda d: not d["likely"])
    return out


def _likely_tang(p) -> bool:
    desc = (p.description or "") + " " + (p.manufacturer or "") + " " + (p.product or "")
    if "sipeed" in desc.lower() or "bl702" in desc.lower():
        return True
    # Tang Nano 9K on-board debugger (BL702 based)
    return p.vid == 0x359F or (p.vid == 0x0403 and p.pid == 0x6010)
