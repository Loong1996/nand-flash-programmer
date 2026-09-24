#!/usr/bin/env python3
"""Build nsprog for end users with PyInstaller (no Python needed on their side).

    pip install ./host pyinstaller pillow
    python host/packaging/build_app.py              # single-file executable (any OS)
    python host/packaging/build_app.py --app        # macOS: nsprog.app + .dmg
    python host/packaging/build_app.py --installer  # Windows: Inno Setup installer (needs iscc)

Everything lands in host/dist/. The chip database, web UI and FPGA bitstream
are bundled. Double-clicking the program starts the web UI; with arguments it
is the normal command line.

macOS signing: the .app is ad-hoc signed, which is enough to run it after
right-click -> Open. With MACOS_SIGN_IDENTITY ("Developer ID Application: ...")
it is signed with hardened runtime instead, and with APPLE_ID, APPLE_TEAM_ID and
APPLE_APP_PASSWORD also notarised and stapled.

USB access for the FT232H still needs libusb (macOS: ``brew install libusb``;
Windows: WinUSB driver via Zadig); the on-board UART works without extra drivers.
"""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
DIST = HOST / "dist"
BUILD = HOST / "build"
BUNDLE_ID = "io.github.loong1996.nsprog"


def version() -> str:
    sys.path.insert(0, str(HOST / "src"))
    from nsprog import __version__
    return __version__


def arch() -> str:
    return platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")


def target_name() -> str:
    osname = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    return "nsprog-%s-%s" % (osname, arch())


def run(cmd, **kw) -> None:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call([str(c) for c in cmd], **kw)


def icons() -> dict:
    """Generate the icons (Pillow); returns {} if Pillow is missing."""
    try:
        run([sys.executable, HERE / "icon.py", BUILD / "icon"])
    except subprocess.CalledProcessError:
        print("note: Pillow not installed, building without an icon")
        return {}
    return {"png": BUILD / "icon" / "nsprog.png", "icns": BUILD / "icon" / "nsprog.icns",
            "ico": BUILD / "icon" / "nsprog.ico"}


def data_args() -> list:
    """Bundle nsprog's data files straight from this checkout."""
    pkg = HOST / "src" / "nsprog"
    out = []
    for sub in ("data", "web/static", "bitstream"):
        out += ["--add-data", "%s%s%s" % (pkg / sub, os.pathsep, "nsprog/" + sub)]
    return out


def pyinstaller(name: str, *extra) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--name", name, "--distpath", DIST, "--workpath", BUILD / "pyinstaller",
         "--specpath", BUILD,
         "--paths", HOST / "src",               # this checkout, not an older installed copy
         *data_args(),
         "--collect-submodules", "uvicorn",
         "--collect-submodules", "websockets",
         "--hidden-import", "usb.backend.libusb1",
         "--hidden-import", "serial.tools.list_ports",
         "--hidden-import", "lz4.block",                # optional: lz4 / zstd file systems
         "--hidden-import", "zstandard",
         *extra, HERE / "entry.py"])


def smoke(exe: Path) -> None:
    """The bundled emulator exercises the protocol, drivers and data files."""
    run([exe, "--port", "emu", "selftest"])
    run([exe, "--port", "emu", "info"])
    run([exe, "--port", "emu", "doctor"])


# ---------------------------------------------------------------- single file
def build_onefile(ico: dict) -> Path:
    name = target_name()
    extra = ["--onefile"]
    if ico.get("ico") and sys.platform == "win32":
        extra += ["--icon", ico["ico"]]
    pyinstaller(name, *extra)
    exe = DIST / (name + (".exe" if sys.platform == "win32" else ""))
    smoke(exe)
    print("built %s" % exe)
    return exe


# ---------------------------------------------------------------- macOS .app
def build_mac_app(ico: dict) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("--app builds a macOS .app and must run on macOS")
    extra = ["--windowed", "--onedir", "--osx-bundle-identifier", BUNDLE_ID]
    if ico.get("icns"):
        extra += ["--icon", ico["icns"]]
    pyinstaller("nsprog", *extra)
    app = DIST / "nsprog.app"
    plist = app / "Contents" / "Info.plist"
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleDisplayName": "nsprog",
        "CFBundleShortVersionString": version(),
        "CFBundleVersion": version(),
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "GPL-3.0-or-later",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    })
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    sign_mac(app)
    smoke(app / "Contents" / "MacOS" / "nsprog")
    dmg = make_dmg(app)
    print("built %s and %s" % (app, dmg))
    return dmg


def sign_mac(app: Path) -> None:
    ident = os.environ.get("MACOS_SIGN_IDENTITY")
    if ident:
        run(["codesign", "--force", "--deep", "--options", "runtime", "--timestamp",
             "--sign", ident, app])
    else:
        run(["codesign", "--force", "--deep", "--sign", "-", app])      # ad-hoc
    run(["codesign", "--verify", "--deep", "--strict", app])


def make_dmg(app: Path) -> Path:
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run(["ditto", app, stage / app.name])
    (stage / "Applications").symlink_to("/Applications")
    dmg = DIST / ("nsprog-%s-macos-%s.dmg" % (version(), arch()))
    if dmg.exists():
        dmg.unlink()
    run(["hdiutil", "create", "-volname", "nsprog", "-srcfolder", stage, "-ov",
         "-format", "UDZO", dmg])
    if os.environ.get("MACOS_SIGN_IDENTITY"):
        run(["codesign", "--force", "--sign", os.environ["MACOS_SIGN_IDENTITY"], dmg])
        if all(os.environ.get(k) for k in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD")):
            run(["xcrun", "notarytool", "submit", dmg, "--wait",
                 "--apple-id", os.environ["APPLE_ID"], "--team-id", os.environ["APPLE_TEAM_ID"],
                 "--password", os.environ["APPLE_APP_PASSWORD"]])
            run(["xcrun", "stapler", "staple", dmg])
    return dmg


# ---------------------------------------------------------------- Windows installer
def build_windows_installer(ico: dict) -> Path:
    if sys.platform != "win32":
        raise SystemExit("--installer builds a Windows installer and must run on Windows")
    exe = build_onefile(ico)
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if not Path(iscc).exists():
        raise SystemExit("Inno Setup not found (choco install innosetup)")
    run([iscc, "/DAppVersion=%s" % version(), "/DSourceExe=%s" % exe,
         "/DIconFile=%s" % ico.get("ico", ""), "/O%s" % DIST, HERE / "nsprog.iss"])
    out = DIST / ("nsprog-%s-windows-setup.exe" % version())
    print("built %s" % out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--app", action="store_true", help="macOS .app bundle + .dmg")
    g.add_argument("--installer", action="store_true", help="Windows installer (Inno Setup)")
    args = ap.parse_args()
    ico = icons()
    if args.app:
        build_mac_app(ico)
    elif args.installer:
        build_windows_installer(ico)
    else:
        build_onefile(ico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
