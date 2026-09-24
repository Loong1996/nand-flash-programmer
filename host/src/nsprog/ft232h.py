"""One-time FT232H EEPROM configuration for 245 FIFO mode."""

from __future__ import annotations

import sys

from .link import CUSTOM_PID, find_ft232h_url, register_custom_ids


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
