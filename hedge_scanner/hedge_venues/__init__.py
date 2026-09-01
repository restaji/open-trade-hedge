"""Hedge destination venues.

Position *reading* venues live in ``hedge_scanner.adapters``. This package holds
venues we only ever price a hedge against. Avantis is the primary one; it is
excluded from position reading per CONTRACT.md section 1.
"""

from __future__ import annotations

from . import avantis

__all__ = ["avantis"]
