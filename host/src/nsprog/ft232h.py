"""FT232H: one-time EEPROM configuration for 245 FIFO mode, sync FIFO clock phase tuning."""

from __future__ import annotations

import logging
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import config
from . import protocol as P
from .link import CUSTOM_PID, find_ft232h_url, register_custom_ids

log = logging.getLogger(__name__)


def setup_eeprom(url=None, custom_pid=False, dry_run=False) -> None:
    from pyftdi.eeprom import FtdiEeprom

    register_custom_ids()
    url = url or find_ft232h_url()
    if url is None:
        raise SystemExit(
            "no FT232H found.\n"
            "  macOS: if the device is listed in 'system_profiler SPUSBDataType' but not found here,\n"
            "         the built-in FTDI serial driver owns it; run this command once with sudo.\n"
            "  Linux: add a udev rule (see docs/quickstart.md) or run with sudo.\n"
            "  Windows: install the WinUSB driver for the FT232H with Zadig.")
    ee = FtdiEeprom()
    ee.open(url)
    try:
        if ee.device_version != 0x0900:
            raise SystemExit("device at %s is not an FT232H" % url)
        blank = all(b == 0xFF for b in ee.data[:0x20])
        if blank:
            print("EEPROM is blank; writing a default configuration first")
            ee.initialize()
        ee.set_property("channel_a_type", "FIFO")
        ee.set_property("channel_a_driver", "D2XX")
        # AC5 (CLKOUT) and AC6 (OE#) must not carry a CBUS clock/LED function in
        # async mode: the FPGA would take a toggling AC5 for sync FIFO mode.
        for pin in (5, 6):
            try:
                ee.set_property("cbus_func_%d" % pin, "TRISTATE")
            except (ValueError, KeyError) as e:   # pragma: no cover - old pyftdi
                print("note: could not set CBUS%d: %s" % (pin, e))
        if custom_pid:
            ee.set_property("product_id", CUSTOM_PID)
        ee.set_product_name("nsprog FT232H")
        ee.dump_config(sys.stdout)
        changed = ee.commit(dry_run=dry_run)
        if dry_run:
            print("dry run: nothing written")
        elif changed is False:
            print("EEPROM updated. Unplug and replug the FT232H module.")
    finally:
        ee.close()


# ---------------------------------------------------------------------------
# Sync FIFO clock phase (gateware >= 1.3): the bridge runs on FT232H CLKOUT
# passed through a PLL whose phase the host sets in 16 steps of 22.5 degrees
# (1.04 ns at 60 MHz). A new phase must be confirmed with INFO within 1 s or
# the engine goes back to the previous one, so a phase that breaks the link
# costs a short wait, never the connection.

PHASES = 16
STEP_NS = 1e9 / 60e6 / PHASES
#: host wait for the engine's 1 s phase watchdog
REVERT_S = 1.3
#: time allowed for one probe round (1 KB of echoes) to come back
PROBE_S = 0.5
SAVE_FILE = "ft232h.json"


@dataclass
class TuneReport:
    results: List[Tuple[int, Optional[str]]] = field(default_factory=list)   # (phase, error)
    window: List[int] = field(default_factory=list)                           # longest passing run
    best: Optional[int] = None

    @property
    def passing(self) -> List[int]:
        return [p for p, err in self.results if err is None]

    def summary(self) -> str:
        cells = "".join("#" if err is None else "." for _, err in self.results)
        lines = ["phase  0 ... 15   %s   (# = link OK, . = errors)" % cells]
        if self.best is None:
            lines.append("no working phase")
        else:
            lines.append("window %d..%d (%d steps = %.1f ns), chosen phase %d (%.1f degrees)"
                         % (self.window[0], self.window[-1], len(self.window),
                            len(self.window) * STEP_NS, self.best, self.best * 22.5))
        return "\n".join(lines)


def phase_supported(dev) -> bool:
    return tuple(int(x) for x in dev.info.gw_version.split(".")) >= (1, 3)


def _probe(dev, rng: random.Random, rounds: int) -> Optional[str]:
    """ECHO test patterns through the link; None when everything came back."""
    link = dev.link
    n = max(64, min(dev.window, 2048) // 2)
    fixed = bytes([0x00, 0xFF, 0x55, 0xAA]) + bytes(1 << i for i in range(8)) + \
        bytes(0xFF ^ (1 << i) for i in range(8))
    for r in range(rounds):
        pat = (fixed + bytes(rng.randrange(256) for _ in range(n)))[:n]
        try:
            link.write(b"".join(bytes([P.ECHO, b]) for b in pat))
        except Exception as e:                   # USB write timeout: the FPGA stopped reading
            return "round %d: write stalled (%s)" % (r + 1, e)
        got = b""
        end = time.monotonic() + PROBE_S
        while len(got) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            got += link.read(n - len(got), left)
        if got != pat:
            wrong = sum(a != b for a, b in zip(got, pat))
            return "round %d: %d of %d bytes back, %d wrong" % (r + 1, len(got), n, wrong)
    return None


def _recover(dev, fallback: int) -> None:
    """Wait for the phase watchdog and resync; if the bad phase got confirmed
    anyway (commands arrive, replies do not), switch back blindly."""
    dev.link.read(1 << 16, REVERT_S)
    try:
        dev.resync()
        return
    except Exception:
        pass
    dev.link.write(bytes([P.SET_REG, P.REG_FT_PHASE, fallback, 0, P.INFO]))
    dev.link.read(1 << 16, 0.1)
    dev.resync()


def set_phase(dev, phase: int, rounds: int = 4, seed: int = 1) -> Optional[str]:
    """Switch the sync FIFO clock to ``phase`` (0..15), test the link and confirm.

    Returns None on success; otherwise the error, after the engine has gone
    back to the previous phase and the host has resynced.
    """
    if not phase_supported(dev):
        raise ValueError("adjustable FT232H clock phase needs gateware >= 1.3 (found %s)"
                         % dev.info.gw_version)
    phase &= PHASES - 1
    prev = getattr(dev, "ft_phase", None)
    dev.link.write(bytes([P.SET_REG, P.REG_FT_PHASE, phase, 0]))
    dev.link.read(1, 0.002)                     # let the PLL output settle
    err = _probe(dev, random.Random(seed * 16 + phase), rounds)
    if err is None:
        try:
            dev.info = dev.query_info()         # confirms the new phase
            dev.ft_phase = phase
            return None
        except Exception as e:
            err = "INFO failed: %s" % e
    _recover(dev, prev if prev is not None else DEFAULT_PHASE)
    return err


#: power-up phase of the released gateware (top_tangnano9k.v FT_PHASE_DEFAULT)
DEFAULT_PHASE = 0


def _longest_run(ok: List[bool]) -> List[int]:
    """Longest run of True in a circular list, as indices."""
    n = len(ok)
    if all(ok):
        return list(range(n))
    start = ok.index(False)
    best: List[int] = []
    cur: List[int] = []
    for k in range(1, n + 1):
        i = (start + k) % n
        if ok[i]:
            cur.append(i)
            if len(cur) > len(best):
                best = list(cur)
        else:
            cur = []
    return best


def tune(dev, rounds: int = 4, progress: Optional[Callable[[int, Optional[str]], None]] = None) -> TuneReport:
    """Try all 16 phases and settle on the middle of the widest working window."""
    if getattr(dev.link, "sync", True) is False:
        raise ValueError("phase tuning is for the sync FIFO link (port 'ft232h-sync')")
    rep = TuneReport()
    for ph in range(PHASES):
        err = set_phase(dev, ph, rounds)
        rep.results.append((ph, err))
        if progress:
            progress(ph, err)
    ok = [err is None for _, err in rep.results]
    rep.window = _longest_run(ok)
    if not rep.window:
        return rep
    if len(rep.window) == PHASES:
        best = DEFAULT_PHASE
    else:
        best = rep.window[(len(rep.window) - 1) // 2]
    err = set_phase(dev, best, rounds)
    if err is not None:
        raise RuntimeError("phase %d worked during the sweep but not now: %s" % (best, err))
    rep.best = best
    return rep


def saved_phase(name: str) -> Optional[int]:
    data = config.load_json(SAVE_FILE, {})
    ph = data.get("phases", {}).get(name, data.get("phase"))
    return int(ph) if isinstance(ph, int) and 0 <= ph < PHASES else None


def save_phase(name: str, phase: int) -> None:
    data = config.load_json(SAVE_FILE, {})
    data.setdefault("phases", {})[name] = phase
    data["phase"] = phase
    config.save_json(SAVE_FILE, data)


def apply_saved_phase(dev) -> Optional[int]:
    """On a sync FIFO connect: use the phase found by ``nsprog ft232h-tune``."""
    ph = saved_phase(dev.link.name)
    if ph is None or not phase_supported(dev):
        return None
    try:
        err = set_phase(dev, ph)
    except Exception as e:
        err = str(e)
    if err is not None:
        log.warning("saved FT232H clock phase %d does not work (%s); run 'nsprog -p ft232h-sync "
                    "ft232h-tune' again", ph, err)
        return None
    return ph
