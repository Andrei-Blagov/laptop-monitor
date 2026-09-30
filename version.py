from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def get_version() -> str:
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return text or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = get_version()
APP_NAME = "Laptop Monitor"
