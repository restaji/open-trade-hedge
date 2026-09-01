"""Liquidation risk modelling for hedge legs.

Each venue has a different liquidation model — trigger threshold, penalty
structure, and what gets forfeited. This module:

  1. Holds a per-venue `LIQUIDATION_SPECS` table (static parameters from the
     fee-research files; maintenance margin is upgraded to live-fetched when the
     adapter supports it).
  2. Computes the liquidation price for a given entry/leverage/side.
  3. Computes the USD cost of being liquidated.
  4. Wraps the results into a `LiquidationRisk` that the engine and CLI consume.

All arithmetic is `Decimal`. There is no `float` in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hedge_scanner.models import LiquidationSpec

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
BPS_DENOM = Decimal(10_000)


# ======================================================================================
# LIQUIDATION SPECS — one entry per venue, transcribed from the fee-research files.
#
# `maintenance_margin_source` indicates whether the number comes from a live API
# ("live_api") or is hardcoded from documentation ("static").
# ======================================================================================

LIQUIDATION_SPECS: dict[str, LiquidationSpec] = {
    "avantis": LiquidationSpec(
        venue="avantis",
        maintenance_margin_pct=Decimal("15"),
        liquidation_fee_pct=Decimal("15"),
        liquidation_fee_type="pct_of_collateral",
        partial_liquidation=False,
        cross_margin_risk="position_only",
        notes=(
            "Health ratio trigger ≤85%. Liquidation bounty ~15% of residual "
            "collateral (UNVERIFIED). No separate liquidation fee beyond the "
            "bounty. Full collateral seized on liquidation."
        ),
        source="../avantis-fees.md",
        as_of="2026-08-19",
        maintenance_margin_source="static",
        liquidation_model="health_ratio",
    ),
    "hyperliquid": LiquidationSpec(
        venue="hyperliquid",
        maintenance_margin_pct=Decimal("3.33"),
        liquidation_fee_pct=ZERO,
        liquidation_fee_type="residual_forfeit",
        partial_liquidation=True,
        cross_margin_risk="full_account",
        notes=(
            "Tiered maintenance margin by position size. BTC/ETH: 3.33% at "
            "tier 1 (≤$4M notional). No explicit liquidation fee — the backstop "
            "liquidator vault takes the position at the maintenance margin level. "
            "Forfeit = maintenance margin only. Cross-margin mode: forfeit "
            "extends to cross-account equity."
        ),
        source="../hyperliquid-fees.md -> hyperliquid.gitbook.io + POST /info {type:meta}",
        as_of="2026-08-19",
        maintenance_margin_source="static",
    ),
    "grvt": LiquidationSpec(
        venue="grvt",
        maintenance_margin_pct=Decimal("1.0"),
        liquidation_fee_pct=ZERO,
        liquidation_fee_type="full_margin_forfeit",
        partial_liquidation=False,
        cross_margin_risk="full_account",
        notes=(
            "1.0% maintenance margin (BTC, tier 1, 50x). 100% of residual "
            "margin forfeited to the Insurance Fund — no percentage penalty, but "
            "the ENTIRE remaining collateral goes. On cross margin, that is the "
            "ENTIRE cross-account equity, not just the failing position's margin. "
            "THIS IS THE SINGLE MOST DANGEROUS LIQUIDATION MODEL IN THE SET."
        ),
        source="../grvt-fees.md -> help.grvt.io/en/articles/9614699",
        as_of="2026-08-19",
        maintenance_margin_source="static",
    ),
    "pacifica": LiquidationSpec(
        venue="pacifica",
        maintenance_margin_pct=Decimal("1.0"),
        liquidation_fee_pct=Decimal("0.75"),
        liquidation_fee_type="pct_of_notional",
        partial_liquidation=True,
        cross_margin_risk="position_only",
        notes=(
            "MM = 1/(2×max_leverage) = 1.0% for BTC (50x). Penalty = "
            "max(0.75%, MM_ratio×0.4) of liquidated notional = 0.75% for BTC. "
            "Partial liquidation supported. Below ⅔ MM: backstop takes 100% of "
            "remaining collateral."
        ),
        source="../pacifica-fees.md -> docs.pacifica.fi",
        as_of="2026-08-19",
        maintenance_margin_source="static",
    ),
    "ondo": LiquidationSpec(
        venue="ondo",
        maintenance_margin_pct=Decimal("2.0"),
        liquidation_fee_pct=Decimal("1.5"),
        liquidation_fee_type="pct_of_notional",
        partial_liquidation=False,
        cross_margin_risk="position_only",
        notes=(
            "2.0% maintenance margin (25x BTC). 1.5% of closed notional "
            "penalty, all to Insurance Fund. At 10x leverage, that is 15% of "
            "your margin."
        ),
        source="../ondo-perps-fees.md",
        as_of="2026-08-19",
        maintenance_margin_source="static",
    ),
    "jupiter": LiquidationSpec(
        venue="jupiter",
        maintenance_margin_pct=Decimal("0.2"),
        liquidation_fee_pct=Decimal("0.2"),
        liquidation_fee_type="residual_forfeit",
        partial_liquidation=False,
        cross_margin_risk="position_only",
        notes=(
            "Maintenance margin 0.2% (practical, from maxLeverage=500). "
            "Liquidation sweeps all remaining collateral. Structural cap: "
            "liquidation penalty ≤ 0.20% of notional. Observed: 8.4-20 bps "
            "across real liquidations. Plus normal close fee (6 bps) + accrued "
            "borrow."
        ),
        source="../jupiter-perps-fees.md -> onchain PERPHjGB",
        as_of="2026-08-19",
        maintenance_margin_source="static",
    ),
}


# ======================================================================================
# Liquidation price calculator
# ======================================================================================


def liquidation_price(
    entry_price: Decimal,
    side: str,
    leverage: Decimal,
    maintenance_margin_pct: Decimal,
    *,
    fees_pct: Decimal = ZERO,
    liquidation_model: str = "standard",
) -> Decimal:
    """Price at which a position gets liquidated.

    Standard model (most venues — MM is a fixed % of notional):

        For a long:
            liq_price = entry × (1 - (1/leverage - MM/100 - fees/100))
        For a short:
            liq_price = entry × (1 + (1/leverage - MM/100 - fees/100))

    Health-ratio model (Avantis — MM represents % of collateral that can be lost
    before liquidation fires; e.g. 15 means trigger at 85% health):

        distance = (MM_pct / 100) × (1/leverage) - fees/100
        Effective max loss = MM_pct% of initial_collateral = MM_pct% × notional/leverage

    Parameters
    ----------
    entry_price : Entry price of the position.
    side : "long" or "short".
    leverage : Position leverage (e.g. Decimal("10")).
    maintenance_margin_pct : For ``standard``: maintenance margin as % of notional.
        For ``health_ratio``: fraction of collateral that can be lost (e.g. 15 for 85% health ratio).
    fees_pct : Total fees that eat into margin at open, as percentage of notional.
    liquidation_model : "standard" or "health_ratio".
    """
    if leverage <= ZERO:
        raise ValueError(f"leverage must be positive, got {leverage}")
    if entry_price <= ZERO:
        raise ValueError(f"entry_price must be positive, got {entry_price}")

    fee_fraction = fees_pct / HUNDRED

    if liquidation_model == "health_ratio":
        # Collateral-based: you can lose MM_pct% of your collateral.
        # collateral = notional / leverage, so the distance (as fraction of
        # notional) is (MM_pct/100) / leverage.
        distance = (maintenance_margin_pct / HUNDRED) / leverage - fee_fraction
    else:
        margin_fraction = ONE / leverage
        mm_fraction = maintenance_margin_pct / HUNDRED
        distance = margin_fraction - mm_fraction - fee_fraction

    if side == "long":
        return entry_price * (ONE - distance)
    elif side == "short":
        return entry_price * (ONE + distance)
    else:
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def liquidation_distance_pct(
    entry_price: Decimal,
    liq_price: Decimal,
    side: str,
) -> Decimal:
    """How far price must move against the position before liquidation, as a %.

    Always returned as a positive number representing the adverse move the
    position can survive.
    """
    if entry_price <= ZERO:
        return ZERO
    if side == "long":
        return (entry_price - liq_price) / entry_price * HUNDRED
    elif side == "short":
        return (liq_price - entry_price) / entry_price * HUNDRED
    else:
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def liquidation_cost_usd(
    notional_usd: Decimal,
    collateral_usd: Decimal,
    spec: LiquidationSpec,
) -> Decimal:
    """Total USD lost if this position is liquidated.

    The answer depends on the venue's liquidation fee type:

    - ``pct_of_notional``: a fixed percentage of the position's notional value.
    - ``pct_of_collateral``: a percentage of the remaining collateral.
    - ``full_margin_forfeit``: 100% of whatever margin remains. GRVT.
    - ``residual_forfeit``: remaining margin at liquidation (≈ MM × notional).
    """
    notional = abs(notional_usd)
    collateral = abs(collateral_usd)

    fee_type = spec.liquidation_fee_type
    fee_pct = spec.liquidation_fee_pct

    if fee_type == "pct_of_notional":
        return fee_pct / HUNDRED * notional
    elif fee_type == "pct_of_collateral":
        return fee_pct / HUNDRED * collateral
    elif fee_type == "full_margin_forfeit":
        return collateral
    elif fee_type == "residual_forfeit":
        return spec.maintenance_margin_pct / HUNDRED * notional
    else:
        return ZERO


def liquidation_cost_bps(
    notional_usd: Decimal,
    collateral_usd: Decimal,
    spec: LiquidationSpec,
) -> Decimal:
    """Liquidation penalty expressed as bps of notional."""
    notional = abs(notional_usd)
    if notional <= ZERO:
        return ZERO
    cost = liquidation_cost_usd(notional, collateral_usd, spec)
    return cost / notional * BPS_DENOM


# ======================================================================================
# LiquidationRisk — computed per-venue result that the CLI and JSON consume.
# ======================================================================================


@dataclass(frozen=True)
class LiquidationRisk:
    """Computed liquidation risk for one hedge candidate on one venue."""

    venue: str
    side: str
    entry_price: Decimal
    leverage: Decimal
    notional_usd: Decimal
    collateral_usd: Decimal
    liq_price: Decimal
    distance_pct: Decimal
    penalty_usd: Decimal
    penalty_bps: Decimal
    spec: LiquidationSpec


def compute_liquidation_risk(
    venue: str,
    side: str,
    entry_price: Decimal,
    leverage: Decimal,
    notional_usd: Decimal,
    *,
    fees_pct: Decimal = ZERO,
    spec: LiquidationSpec | None = None,
) -> LiquidationRisk | None:
    """Compute the full liquidation risk assessment for a single hedge leg.

    Returns None if the venue has no liquidation spec.
    """
    if spec is None:
        spec = LIQUIDATION_SPECS.get(venue)
    if spec is None:
        return None
    if leverage <= ZERO or entry_price <= ZERO:
        return None

    collateral = abs(notional_usd) / leverage

    liq = liquidation_price(
        entry_price,
        side,
        leverage,
        spec.maintenance_margin_pct,
        fees_pct=fees_pct,
        liquidation_model=spec.liquidation_model,
    )
    dist = liquidation_distance_pct(entry_price, liq, side)
    penalty = liquidation_cost_usd(abs(notional_usd), collateral, spec)
    penalty_bp = liquidation_cost_bps(abs(notional_usd), collateral, spec)

    return LiquidationRisk(
        venue=venue,
        side=side,
        entry_price=entry_price,
        leverage=leverage,
        notional_usd=abs(notional_usd),
        collateral_usd=collateral,
        liq_price=liq,
        distance_pct=dist,
        penalty_usd=penalty,
        penalty_bps=penalty_bp,
        spec=spec,
    )
