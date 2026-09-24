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

__all__ = ["FtModel", "UartModel", "SimFtLink", "SimUartLink", "SimDevice", "bridge",
           "start"]

#: 1 s of host timeout = this many ns of simulated time
TIMEOUT_SCALE_NS = 1_000_000


async def start(dut, cycles=400):
    """Idle all host-side signals, press S1 (engine reset) and wait."""
    dut.ft_rxf_n.value = 1
    dut.ft_txe_n.value = 1
    dut.ft_d_host_oe.value = 0
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
        d = self.dut
        while True:
            while not self.txq:
                await Timer(1, "us")
            b = self.txq.popleft()
            bit = self.bit_ps
            for v in [0] + [(b >> i) & 1 for i in range(8)] + [1]:
                d.uart_rx.value = v
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


class SimDevice(Device):
    """Device with wall-clock timeouts relaxed for (slow) simulation."""

    def _deadline(self, seg):
        return time.monotonic() + 900.0
