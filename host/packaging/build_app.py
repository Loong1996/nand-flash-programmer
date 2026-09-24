#!/usr/bin/env python3
"""Build a single-file ``nsprog`` executable for the current OS with PyInstaller.

    pip install ./host pyinstaller
    python host/packaging/build_app.py          # -> host/dist/nsprog-<os>-<arch>[.exe]

The chip database, web UI and FPGA bitstream are bundled. USB access for the
FT232H still needs libusb (macOS: ``brew install libusb``; Windows: WinUSB
driver via Zadig); the on-board UART works without extra drivers.
"""

import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def target_name() -> str:
    osname = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    arch = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    return "nsprog-%s-%s" % (osname, arch)


def main() -> int:
    name = target_name()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--name", name,
           "--distpath", str(HERE.parent / "dist"),
           "--workpath", str(HERE.parent / "build" / "pyinstaller"),
           "--specpath", str(HERE.parent / "build"),
           "--collect-data", "nsprog",
           "--collect-submodules", "uvicorn",
           "--collect-submodules", "websockets",
           "--hidden-import", "usb.backend.libusb1",
           "--hidden-import", "serial.tools.list_ports",
           str(HERE / "entry.py")]
    print(" ".join(cmd), flush=True)
    subprocess.check_call(cmd)
    exe = HERE.parent / "dist" / (name + (".exe" if sys.platform == "win32" else ""))
    # smoke test: the bundled emulator exercises the protocol, drivers and data files
    subprocess.check_call([str(exe), "--port", "emu", "selftest"])
    subprocess.check_call([str(exe), "--port", "emu", "info"])
    print("built %s" % exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
