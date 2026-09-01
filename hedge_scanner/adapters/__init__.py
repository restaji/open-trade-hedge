"""Venue adapters.

Only Jupiter and Pacifica can serve positions for an address we do not control.
GRVT and Ondo are registered anyway so the UI can say "requires your API key"
instead of silently omitting them, and so their public quote endpoints -- which
the hedge engine needs regardless -- stay reachable.
"""

from __future__ import annotations

from .base import (
    AdapterError,
    VenueAdapter,
    VenueRequiresAuthError,
    VenueUnavailableError,
)
from .grvt import GrvtAdapter
from .hyperliquid import HyperliquidAdapter
from .jupiter import JupiterAdapter
from .ondo import OndoAdapter
from .ostium import OstiumAdapter
from .pacifica import PacificaAdapter

ADAPTER_CLASSES: tuple[type, ...] = (
    JupiterAdapter,
    PacificaAdapter,
    GrvtAdapter,
    OndoAdapter,
    HyperliquidAdapter,
    OstiumAdapter,
)

SOLANA_ADAPTERS: tuple[type, ...] = (JupiterAdapter, PacificaAdapter)
EVM_ADAPTERS: tuple[type, ...] = (GrvtAdapter, OndoAdapter, HyperliquidAdapter, OstiumAdapter)


def build_adapters(namespace: str | None = None) -> list[VenueAdapter]:
    """Instantiate adapters, optionally filtered to one address namespace."""
    if namespace == "solana":
        classes = SOLANA_ADAPTERS
    elif namespace == "evm":
        classes = EVM_ADAPTERS
    elif namespace is None:
        classes = ADAPTER_CLASSES
    else:
        raise ValueError(f"unknown namespace: {namespace!r}")
    return [cls() for cls in classes]


__all__ = [
    "ADAPTER_CLASSES",
    "EVM_ADAPTERS",
    "SOLANA_ADAPTERS",
    "AdapterError",
    "GrvtAdapter",
    "HyperliquidAdapter",
    "JupiterAdapter",
    "OndoAdapter",
    "OstiumAdapter",
    "PacificaAdapter",
    "VenueAdapter",
    "VenueRequiresAuthError",
    "VenueUnavailableError",
    "build_adapters",
]
