"""Vercel FastAPI entrypoint.

Vercel looks for a FastAPI instance named `app` in `app.py` at the project
root. The real routes live in `hedge_scanner.web`; this file only re-exports
that instance so a drag-and-drop / Git import deploy works without extra
dashboard config.

`sys.path` is pinned to this directory because Vercel does not always install
the local wheel — it installs the dependencies listed in `pyproject.toml` and
expects the project root to be importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hedge_scanner.web import app

__all__ = ["app"]
