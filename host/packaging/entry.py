"""Entry point of the standalone app (PyInstaller).

Without arguments (e.g. double-clicked) it starts the web UI and opens the
browser; with arguments it is the normal ``nsprog`` command line.
"""

import os
import sys


def _no_console_fix() -> None:
    """Windowed builds have no stdout/stderr; send them to a log file instead."""
    if sys.stdout is None or sys.stderr is None:
        home = os.environ.get("NSPROG_HOME") or os.path.join(os.path.expanduser("~"), ".nsprog")
        os.makedirs(home, exist_ok=True)
        log = open(os.path.join(home, "nsprog.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


if __name__ == "__main__":
    _no_console_fix()
    from nsprog.cli import main

    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1].startswith("-psn_")):
        # double-clicked (macOS adds -psn_... on old systems): desktop-app mode
        sys.argv[1:] = ["web"]
        os.environ["NSPROG_APP"] = "1"
    sys.exit(main())
