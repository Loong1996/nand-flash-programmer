"""cocotb host-side models (FT232H FIFO, UART) and Link adapters so the real
``nsprog`` host drivers can run against the simulated FPGA."""

import collections
import os
import sys
import time

import cocotb
from cocotb._bridge import bridge, resume
from cocotb.triggers import FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "host", "src"))

from nsprog.device import Device  # noqa: E402
from nsprog.link import Link  # noqa: E402

__all__ = ["FtModel", "FtSyncModel", "UartModel", "SimFtLink", "SimUartLink", "SimDevice",
           "bridge", "start", "wait_quiet"]

#: 1 s of host timeout = this many ns of simulated time
TIMEOUT_SCALE_NS = 1_000_000


async def start(dut, cycles=400):
    """Idle all host-side signals, press S1 (engine reset) and wait."""
    dut.ft_rxf_n.value = 1
    dut.ft_txe_n.value = 1
    dut.ft_d_host_oe.value = 0
    dut.ft_clk_en.value = 0
    dut.uart_rx.value = 1
    dut.btn_n.value = 0b10
    for _ in range(20):
        await RisingEdge(dut.clk27)
    dut.btn_n.value = 0b11
    for _ in range(cycles):
        await RisingEdge(dut.clk27)


class FtModel:
    """FT232H in 245 asynchronous FIFO mode, seen from the FPGA pins."""

    def __init__(self, dut, stall_every=0, stall_ns=3000):
        self.dut = dut
        self.rxq = collections.deque()     # host -> FPGA
        self.txq = bytearray()             # FPGA -> host
        self.stall_every = stall_every
        self.stall_ns = stall_ns
        self.errors = 0
        dut.ft_rxf_n.value = 1
        dut.ft_txe_n.value = 0
        dut.ft_d_host_oe.value = 0
        cocotb.start_soon(self._rx())
        cocotb.start_soon(self._tx())

    async def _rx(self):
        d = self.dut
        while True:
            if not self.rxq:
                d.ft_rxf_n.value = 1
                while not self.rxq:
                    await Timer(200, "ns")
            d.ft_rxf_n.value = 0
            await FallingEdge(d.ft_rd_n)
            await Timer(10, "ns")
            d.ft_d_host.value = self.rxq.popleft()
            d.ft_d_host_oe.value = 1
            await RisingEdge(d.ft_rd_n)
            await Timer(2, "ns")
            d.ft_d_host_oe.value = 0
            d.ft_rxf_n.value = 1
            await Timer(50, "ns")

    async def _tx(self):
        d = self.dut
        n = 0
        while True:
            await FallingEdge(d.ft_wr_n)
            v1 = d.ft_d.value
            await RisingEdge(d.ft_wr_n)
            v2 = d.ft_d.value
            if not v1.is_resolvable or not v2.is_resolvable or int(v1) != int(v2):
                self.errors += 1
                d._log.error("FT write data unstable: %s -> %s", v1, v2)
            else:
                self.txq.append(int(v2))
            d.ft_txe_n.value = 1
            n += 1
            if self.stall_every and n % self.stall_every == 0:
                await Timer(self.stall_ns, "ns")   # host not reading for a while
            else:
                await Timer(50, "ns")
            d.ft_txe_n.value = 0


class FtSyncModel:
    """FT232H in 245 synchronous FIFO mode: 60 MHz CLKOUT, OE#-controlled bus.

    FPGA outputs are sampled at the falling edge of CLKOUT (they change only
    on rising edges), acted upon at the next rising edge, and the FT232H
    outputs change 4 ns after the rising edge (datasheet: 1-7.15 ns).
    ``stall_every``/``stall_cycles`` hold TXE# high now and then (host not
    reading); ``rx_gap_every`` makes RXF# go high briefly (USB packet gaps).
    """

    def __init__(self, dut, stall_every=0, stall_cycles=200, rx_gap_every=0):
        self.dut = dut
        self.rxq = collections.deque()     # host -> FPGA
        self.txq = bytearray()             # FPGA -> host
        self.stall_every = stall_every
        self.stall_cycles = stall_cycles
        self.rx_gap_every = rx_gap_every
        self.errors = 0
        self.oe_prev = 1
        dut.ft_rxf_n.value = 1
        dut.ft_txe_n.value = 0
        dut.ft_d_host_oe.value = 0
        dut.ft_clk_en.value = 1
        cocotb.start_soon(self._run())

    def _err(self, msg):
        self.errors += 1
        self.dut._log.error("FT sync: %s", msg)

    async def _run(self):
        d = self.dut
        txe_n, rxf_n, stall, n_tx, n_rx, gap, last_gap = 0, 1, 0, 0, 0, 0, 0
        await RisingEdge(d.ft_clkout)
        while True:
            await FallingEdge(d.ft_clkout)
            oe_n = int(d.ft_oe_n.value) if d.ft_oe_n.value.is_resolvable else 1
            rd_n = int(d.ft_rd_n.value)
            wr_n = int(d.ft_wr_n.value)
            dv = d.ft_d.value
            await RisingEdge(d.ft_clkout)
            # ---- transfers at this edge (FT232H outputs still hold their old values)
            if not rd_n and not rxf_n:
                if oe_n:
                    self._err("RD# low while OE# high")
                elif self.oe_prev:
                    self._err("RD# low in the first cycle of OE# low")
                else:
                    self.rxq.popleft()
                    n_rx += 1
            if not wr_n and not txe_n:
                if not oe_n:
                    self._err("WR# low while OE# low")
                elif not dv.is_resolvable:
                    self._err("WR# low with undriven data")
                else:
                    self.txq.append(int(dv))
                    n_tx += 1
                    if self.stall_every and n_tx % self.stall_every == 0:
                        stall = self.stall_cycles
            self.oe_prev = oe_n
            # ---- new FT232H output values
            await Timer(4, "ns")
            if stall:
                stall -= 1
                txe_n = 1
            else:
                txe_n = 0
            if gap:
                gap -= 1
                rxf_n = 1
            elif (self.rx_gap_every and n_rx and n_rx % self.rx_gap_every == 0
                  and n_rx != last_gap):
                gap, rxf_n, last_gap = 3, 1, n_rx
            else:
                rxf_n = 0 if self.rxq else 1
            d.ft_txe_n.value = txe_n
            d.ft_rxf_n.value = rxf_n
            if not oe_n and self.rxq:
                d.ft_d_host.value = self.rxq[0]
                d.ft_d_host_oe.value = 1
            else:
                d.ft_d_host_oe.value = 0


class UartModel:
    def __init__(self, dut, baud=115200):
        self.dut = dut
        self.baud = baud
        self.txq = collections.deque()    # host -> FPGA
        self.rxq = bytearray()            # FPGA -> host
        dut.uart_rx.value = 1
        cocotb.start_soon(self._send())
        cocotb.start_soon(self._recv())

    @property
    def bit_ps(self):
        return round(1e12 / self.baud)

    async def _send(self):
        """Queue items: a byte, ("badstop", byte) = frame with a 0 stop bit, ("break", us)."""
        d = self.dut
        while True:
            while not self.txq:
                await Timer(1, "us")
            b = self.txq.popleft()
            bit = self.bit_ps
            if isinstance(b, tuple) and b[0] == "break":
                d.uart_rx.value = 0
                await Timer(b[1], "us")
                d.uart_rx.value = 1
                await Timer(bit * 2, "ps")
                continue
            stop = 1
            if isinstance(b, tuple):
                b, stop = b[1], 0
            for v in [0] + [(b >> i) & 1 for i in range(8)] + [stop]:
                d.uart_rx.value = v
                await Timer(bit, "ps")
            if not stop:
                d.uart_rx.value = 1
                await Timer(bit, "ps")

    async def _recv(self):
        d = self.dut
        while True:
            await FallingEdge(d.uart_tx)
            bit = self.bit_ps
            await Timer(bit + bit // 2, "ps")
            v = 0
            for i in range(8):
                v |= int(d.uart_tx.value) << i
                await Timer(bit, "ps")
            self.rxq.append(v)


class _SimLinkBase(Link):
    bytes_per_sec = 1_000_000
    timeout_scale_ns = TIMEOUT_SCALE_NS

    def _buf(self):
        raise NotImplementedError

    def read(self, n, timeout):
        return resume(self._read)(n, timeout)

    async def _read(self, n, timeout):
        buf = self._buf()
        end = get_sim_time("ns") + max(20_000, timeout * self.timeout_scale_ns)
        while len(buf) < n and get_sim_time("ns") < end:
            await Timer(2, "us")
        got = bytes(buf[:n])
        del buf[:n]
        return got


class SimFtLink(_SimLinkBase):
    name = "sim-ft245"
    flow_controlled = True

    def __init__(self, ft: FtModel):
        self.ft = ft

    def _buf(self):
        return self.ft.txq

    def write(self, data):
        resume(self._write)(bytes(data))

    async def _write(self, data):
        self.ft.rxq.extend(data)


class SimUartLink(_SimLinkBase):
    name = "sim-uart"
    flow_controlled = False
    timeout_scale_ns = 20_000_000       # 1 s -> 20 ms: bytes take 87 us each at 115200

    def __init__(self, uart: UartModel):
        self.uart = uart

    def _buf(self):
        return self.uart.rxq

    def write(self, data):
        resume(self._write)(bytes(data))

    async def _write(self, data):
        self.uart.txq.extend(data)

    def set_baudrate(self, baud):
        self.uart.baud = baud

    @property
    def baudrate(self):
        return self.uart.baud


async def wait_quiet(buf, rxq, quiet_ns=150_000_000, step_ns=1_000_000):
    """Model a host drain: wait until nothing is pending towards the FPGA and nothing
    arrived from it for ``quiet_ns`` of simulated time (the engine aborts a partial
    operation after 100 ms), then discard what arrived."""
    last = get_sim_time("ns")
    seen = len(buf)
    while True:
        await Timer(step_ns, "ns")
        now = get_sim_time("ns")
        if len(buf) != seen or rxq:
            seen, last = len(buf), now
        elif now - last >= quiet_ns:
            break
    del buf[:]


class SimDevice(Device):
    """Device with wall-clock timeouts relaxed for (slow) simulation."""

    def _deadline(self, seg):
        return time.monotonic() + 900.0
