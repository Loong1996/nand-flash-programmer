"""Entry point of the standalone app (PyInstaller).

Without arguments (e.g. double-clicked) it starts the web UI and opens the
browser; with arguments it is the normal ``nsprog`` command line.
"""

import sys

from nsprog.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("web")
    sys.exit(main())
