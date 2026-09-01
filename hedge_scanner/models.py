"""Canonical data model, transcribed from CONTRACT.md sections 3, 4 and 9.

RECONCILIATION NOTE
-------------------
Drafted by the engine agent so the engine, CLI and tests had something to import;
reviewed and adopted unchanged by the ingestion agent (CONTRACT.md section 10.6).
There is no competing version. It is a literal transcription of the contract and
deliberately contains no logic, including the additions recorded in section 9
(`Quote.base_asset`, and the `VenueError` / `PortfolioSnapshot` pair).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class Position:
    venue: str              # "grvt" | "pacifica" | "jupiter" | "ondo"
    address: str            # the input address this came from
    market: str             # venue-native symbol, e.g. "BTC_USDT_Perp"
    base_asset: str         # normalized base, e.g. "BTC" -- used for cross-venue netting
    quote_asset: str        # e.g. "USDC"
    side: str               # "long" | "short"
    size_base: Decimal      # position size in base units, always POSITIVE
    notional_usd: Decimal   # signed: + for long, - for short. Mark-price based.
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    collateral_usd: Decimal | None = None
    unrealized_pnl_usd: Decimal | None = None
    funding_paid_usd: Decimal | None = None   # cumulative, if the venue exposes it
    # CONTRACT.md section 12.9 addition: the CURRENT funding rate this
    # position is accruing right now, signed from the POSITION HOLDER'S
    # perspective: positive = holder is currently RECEIVING funding,
    # negative = holder is currently PAYING. `None` when the adapter cannot
    # supply a live rate (e.g. Jupiter has no funding mechanism at all).
    # Distinct from `funding_paid_usd`, which is cumulative history in the
    # opposite sign convention (positive = paid).
    current_funding_rate_8h_bps: Decimal | None = None
    margin_mode: str | None = None            # "cross" | "isolated"
    opened_at: datetime | None = None
    raw: dict = field(default_factory=dict)   # untouched venue payload, for debugging


@dataclass
class Quote:
    venue: str
    market: str
    side: str                      # side of the HEDGE trade
    notional_usd: Decimal
    taker_fee_bps: Decimal         # or open fee for pool-based venues
    close_fee_bps: Decimal
    price_impact_bps: Decimal      # size-dependent; 0 if venue is orderbook w/ deep book
    funding_rate_8h_bps: Decimal   # SIGNED from the perspective of the hedge side:
                                   # positive = hedger RECEIVES, negative = hedger PAYS
    borrow_rate_8h_bps: Decimal    # Jupiter-style one-sided borrow cost, always a cost
    est_slippage_bps: Decimal
    available: bool
    notes: str = ""
    # CONTRACT.md section 9 addition: the engine nets by normalized base asset and
    # cannot reliably re-derive it from a venue-native `market` string.
    base_asset: str = ""


@dataclass
class VenueError:
    """A venue that could not be read. Surfaced to the user, never swallowed."""

    venue: str
    message: str
    kind: str = "error"          # "auth_required" | "unavailable" | "unsupported_namespace" | "error"
    address: str | None = None


@dataclass(frozen=True)
class LiquidationSpec:
    """Per-venue liquidation risk parameters.

    Used by the engine to compute liquidation distance and the cost of being
    force-closed.  Where possible the maintenance margin is fetched from a live
    API; where not, it is transcribed from the fee-research files with a dated
    source comment.
    """

    venue: str
    maintenance_margin_pct: Decimal       # e.g. Decimal("1.0") for 1%
    liquidation_fee_pct: Decimal          # % of notional charged as penalty (0 if none)
    liquidation_fee_type: str             # "pct_of_notional" | "pct_of_collateral"
                                          # | "full_margin_forfeit" | "residual_forfeit"
    partial_liquidation: bool
    cross_margin_risk: str                # "position_only" | "full_account"
    notes: str
    source: str
    as_of: str = ""
    maintenance_margin_source: str = "static"  # "live_api" | "static"
    # "standard": MM is a fixed % of notional (most venues).
    # "health_ratio": MM represents the fraction of initial collateral that can
    #   be lost before liquidation, e.g. 15 means health ratio trigger at 85%.
    #   The effective distance is then `MM_pct / (100 × leverage)`.
    liquidation_model: str = "standard"


@dataclass
class PortfolioSnapshot:
    addresses: list[str]
    positions: list[Position]
    errors: list[VenueError] = field(default_factory=list)
