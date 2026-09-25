"""Per-user settings directory: ``~/.nsprog`` (or ``$NSPROG_HOME``)."""

from __future__ import annotations

import json
import os


def home_dir() -> str:
    d = os.environ.get("NSPROG_HOME") or os.path.join(os.path.expanduser("~"), ".nsprog")
    os.makedirs(d, exist_ok=True)
    return d


def load_json(name, default):
    try:
        with open(os.path.join(home_dir(), name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, data) -> None:
    path = os.path.join(home_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
