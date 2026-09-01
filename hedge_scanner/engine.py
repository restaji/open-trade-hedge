"""Hedge opportunity engine.

Pure, synchronous, deterministic. Takes already-fetched `Position` and `Quote`
objects and produces netted exposure, ranked hedge candidates, funding-arb pairs
and a horizon-sensitivity surface. Performs no I/O and never invents a rate.

Methodology, formulas and limitations: see HEDGE_LOGIC.md.

Sign conventions (must match CONTRACT.md exactly):
  * `Position.notional_usd` is signed: positive long, negative short.
  * `Quote.funding_rate_8h_bps` is signed *from the hedger's perspective*:
    positive means the hedger RECEIVES funding, negative means the hedger PAYS.
  * `Quote.borrow_rate_8h_bps` is always a cost and is always non-negative.
  * Every `*_bps` cost figure produced by this module is a COST: positive means
    money out, negative means money in (positive carry).

All arithmetic is `Decimal`. There is no `float` anywhere in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from hedge_scanner.models import LiquidationSpec, Position, Quote

# --------------------------------------------------------------------------------------
# Numeric constants
# --------------------------------------------------------------------------------------

ZERO = Decimal(0)
BPS_DENOM = Decimal(10_000)
FUNDING_PERIOD_H = Decimal(8)

DEFAULT_HORIZON_H = Decimal(24)
DEFAULT_HORIZONS_H: tuple[Decimal, ...] = (
    Decimal(8),      # 8h  -- one funding period
    Decimal(24),     # 1d
    Decimal(72),     # 3d
    Decimal(168),    # 7d
    Decimal(720),    # 30d
)
DEFAULT_DUST_USD = Decimal(25)
DEFAULT_MAX_CROSSOVER_H = Decimal(720)
DEFAULT_MIN_ARB_CARRY_BPS_8H = Decimal("0.10")

# A quote sized more than this far from the notional we actually want to hedge is
# still usable, but its size-dependent legs (impact, slippage) are extrapolated.
SIZE_MISMATCH_TOLERANCE = Decimal("0.05")


# ======================================================================================
# THE FEE SCHEDULE -- the single place static venue fees are defined.
# ======================================================================================
#
# Swapping in a real number is a ONE-PLACE EDIT: change the row below and flip
# `verified=True` with a source URL and an `as_of` date.
#
# These are *static schedule* fallbacks only. When an adapter supplies live fee
# fields on a `Quote`, the Quote wins -- this table is never used to override it.
# Funding and borrow rates are NEVER in this table: they are live values and must
# be fetched (CONTRACT.md section 7).
#
#   verified=True   -> transcribed from a research file in the parent directory,
#                      with source URL and date.
#   verified=False   -> UNVERIFIED PLACEHOLDER. Not a researched number. Every
#                      output derived from it is flagged in the CLI and JSON.
#
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueFeeSchedule:
    venue: str
    display_name: str
    open_fee_bps: Decimal       # taker / skew-worsening open fee, charged on entry
    close_fee_bps: Decimal      # charged on exit
    maker_fee_bps: Decimal      # informational; negative = rebate
    hedge_destination: bool     # may we route a hedge here?
    position_readable: bool     # do we read positions from here?
    verified: bool
    source: str
    as_of: str
    # "live_api" = adapter fetches from a venue endpoint at runtime.
    # "static_fallback" = no fee API exists; rate is from docs/research.
    fee_source: str = "live_api"
    # True when the posted rate is state-dependent (per-market, or decided by live
    # OI skew) and the static number below is a REFERENCE ONLY. The adapter must
    # supply the live figure on the Quote; see CONTRACT.md 7.6 and 8.5.
    fees_state_dependent: bool = False
    # True when the rate is an explicitly temporary promotion that can be revoked.
    promotional: bool = False
    # True when the numeric fee fields in this row are placeholders and the
    # authoritative rates must be fetched live at the point of use. The `fees`
    # CLI command reads this and fans out to a live-fetch helper rather than
    # displaying the stub. Consumers that iterate FEE_SCHEDULE for pricing
    # should ignore live rows and use the venue's `hedge_venues` module.
    # See CONTRACT.md §7.6 and the §12.3 post-fix follow-up.
    live: bool = False
    min_position_usd: Decimal = ZERO
    open_fee_overrides: dict[str, Decimal] = field(default_factory=dict)
    close_fee_overrides: dict[str, Decimal] = field(default_factory=dict)
    min_position_overrides: dict[str, Decimal] = field(default_factory=dict)
    notes: str = ""

    @property
    def round_trip_fee_bps(self) -> Decimal:
        return self.open_fee_bps + self.close_fee_bps

    def open_fee_for(self, base_asset: str) -> Decimal:
        return self.open_fee_overrides.get(base_asset.upper(), self.open_fee_bps)

    def close_fee_for(self, base_asset: str) -> Decimal:
        return self.close_fee_overrides.get(base_asset.upper(), self.close_fee_bps)

    def min_position_for(self, base_asset: str) -> Decimal:
        return self.min_position_overrides.get(base_asset.upper(), self.min_position_usd)


# Ondo posts two fee pairs across its 52 markets, contradicting its own /fees page
# claim of uniform pricing. These 12 markets sit on the dearer pair.
_ONDO_DEAR_MARKETS = (
    "ARM", "AVGO", "BABA", "CRWV", "CXMT", "GLW",
    "IBM", "LITE", "TSM", "COPPER", "NATGAS", "SOXL",
)

# Avantis minimum position size is 300 USDC on all FX pairs plus gold and silver.
_AVANTIS_FX_METALS = (
    "XAU", "XAG", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "MXN", "SGD", "HKD", "CNH", "TRY", "ZAR", "PLN",
    "DKK", "KRW",
)

# Avantis Upside Perps exist on crypto majors only, as separate pair records.
AVANTIS_UPSIDE_ASSETS: frozenset[str] = frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE"})

# Modelled profit-share band for a realistic hedge (1–500% ROI). Live bands
# come from the pair record; this is the comparison-section default only.
AVANTIS_UPSIDE_BASE_SHARE = Decimal("0.25")


FEE_SCHEDULE: dict[str, VenueFeeSchedule] = {
    "grvt": VenueFeeSchedule(
        venue="grvt",
        display_name="GRVT",
        # Perp taker fee, Level 1 (base tier) = 0.0450% = 4.5 bps. Charged per
        # fill on both entry and exit; GRVT is a pure CLOB so there is no
        # separate open/close fee concept. Live ladder effective 2026-03-23.
        open_fee_bps=Decimal("4.5"),
        close_fee_bps=Decimal("4.5"),
        maker_fee_bps=Decimal("-0.01"),   # negative = rebate, at every tier
        hedge_destination=True,
        position_readable=True,
        verified=True,
        fee_source="static_fallback",  # GRVT has NO public fee API endpoint
        source="../grvt-fees.md -> help.grvt.io/en/articles/9614699 + live market-data API",
        as_of="2026-08-19",
        notes=(
            "Level 1 taker. Level 9 is 2.4 bps. Maker is a rebate at every tier, so a "
            "patient hedger can cut the round trip to roughly zero. Builder-code "
            "integrations add up to 10 bps per fill on top. Liquidation forfeits 100% of "
            "residual margin rather than charging a percentage penalty."
        ),
    ),
    "pacifica": VenueFeeSchedule(
        venue="pacifica",
        display_name="Pacifica",
        # Tier 1: 0.040% taker / 0.015% maker. Uniform across all 75 markets with
        # no per-market overrides. Verified live: GET api.pacifica.fi/api/v1/info/fees.
        open_fee_bps=Decimal("4.0"),
        close_fee_bps=Decimal("4.0"),
        maker_fee_bps=Decimal("1.5"),
        hedge_destination=True,
        position_readable=True,
        verified=True,
        source="../pacifica-fees.md -> docs.pacifica.fi/trading-on-pacifica/trading-fees + live API",
        as_of="2026-08-19",
        notes=(
            "Tier 1 taker. Best tier is 2.8 bps. Maker is positive (1.5 bps) and floors "
            "at zero -- never a rebate. Funding settles HOURLY, not 8-hourly."
        ),
    ),
    "ondo": VenueFeeSchedule(
        venue="ondo",
        display_name="Ondo Perps",
        # PER-MARKET, verified live against GET api.ondoperps.xyz/v1/markets:
        # 2.5 bps taker / 1.0 bps maker on 40 markets, 3.5 / 1.5 on the 12 below.
        # Ondo's own /fees page claim of uniform pricing is false.
        open_fee_bps=Decimal("2.5"),
        close_fee_bps=Decimal("2.5"),
        maker_fee_bps=Decimal("1.0"),
        open_fee_overrides={m: Decimal("3.5") for m in _ONDO_DEAR_MARKETS},
        close_fee_overrides={m: Decimal("3.5") for m in _ONDO_DEAR_MARKETS},
        hedge_destination=True,
        position_readable=True,
        verified=True,
        fees_state_dependent=True,   # per-market: read from /v1/markets, don't assume
        promotional=True,            # 2.5 bps is "50% off" a 5.0 bps base, no stated expiry
        source="../ondo-perps-fees.md + CONTRACT.md 8.5 -> live GET api.ondoperps.xyz/v1/markets",
        as_of="2026-08-19",
        notes=(
            "Cheapest taker commission in the venue set, but promotional: the "
            "non-promo base is 5.0 bps. Fees are per-market, so the constant here is a "
            "fallback only. Funding is HOURLY with a 0.5x dampener on everything except "
            "crypto. 52 markets: 5 crypto (BTC ETH SOL HYPE ONDO), 47 equities, "
            "commodities, ETFs and indices."
        ),
    ),
    "jupiter": VenueFeeSchedule(
        venue="jupiter",
        display_name="Jupiter Perps",
        # Onchain: increasePositionBps = decreasePositionBps = 6 for all 3 markets.
        # Plus a hard 1 bps minimum price-impact fee per side (integer ceiling
        # division on tradeImpactFeeScalar), verified against 824 real trades
        # and the official quote API. True minimum per-side cost is 7 bps.
        # Pool-based (JLP): no maker/taker distinction. 75/25 fee split JLP/Jupiter.
        open_fee_bps=Decimal("6.0"),
        close_fee_bps=Decimal("6.0"),
        maker_fee_bps=Decimal("6.0"),     # pool venue: no maker side, same rate
        hedge_destination=True,
        position_readable=True,
        verified=True,
        source="../jupiter-perps-fees.md -> onchain PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu + quote API + 824 trade sample",
        as_of="2026-08-19",
        min_position_usd=Decimal(10),     # API: collateral_size_below_minimum at <$10
        notes=(
            "Pool-based (JLP) venue: charges a one-sided hourly BORROW rate against "
            "pool utilisation instead of two-sided funding, so `borrow_rate_8h_bps` "
            "carries the carry and `funding_rate_8h_bps` is zero. A Jupiter hedge can "
            "therefore never be positive-carry. Only 3 markets: SOL, ETH, wBTC. No "
            "volume tiers, no maker rebate, no discounts. True minimum per-side cost "
            "is 7 bps (6 bps base + 1 bps price-impact floor). The docs' borrow formula "
            "is wrong by ~100x; the live rate (~12.9% APR SOL, ~6.6% short) was verified "
            "from onchain cumulativeInterestRate advances."
        ),
    ),
    "hyperliquid": VenueFeeSchedule(
        venue="hyperliquid",
        display_name="Hyperliquid",
        # Tier 0 (base): 0.045% taker / 0.015% maker. Uniform across all 177
        # perp markets. Pure CLOB: same rate on entry and exit.
        # LIVE: adapter fetches from POST /info {"type":"userFees"} at runtime.
        # The values here are fallback defaults used only if the fetch fails.
        open_fee_bps=Decimal("4.5"),
        close_fee_bps=Decimal("4.5"),
        maker_fee_bps=Decimal("1.5"),
        hedge_destination=True,
        position_readable=True,
        verified=True,
        fee_source="live_api",  # fetched from userFees endpoint at runtime
        source="../hyperliquid-fees.md -> hyperliquid.gitbook.io/Hyperliquid-docs/trading/fees + live API",
        as_of="2026-08-19",
        notes=(
            "Tier 0 taker. Tier 6 ($7B vol) is 2.4 bps. Maker is 1.5 bps at base, "
            "free at Tier 4+ ($500M vol), and a rebate (−0.1 to −0.3 bps) for top "
            "maker volume share. HYPE staking discount (5–40%) and referral discount "
            "(4%) stack multiplicatively. Funding settles HOURLY (1/8 of computed 8h "
            "rate each hour). First EVM venue with paste-an-address position reading. "
            "Fees fetched live from userFees API; the values in this table are fallback "
            "defaults."
        ),
    ),
    "ostium": VenueFeeSchedule(
        venue="ostium",
        display_name="Ostium",
        # 3–10 bps opening (varies by pair/asset class), 0 bps closing.
        # Representative crypto rate is 6 bps. Live per-pair rates fetched from
        # subgraph openFeeP field. Rollover fees (carry costs) accrue per block.
        open_fee_bps=Decimal("6"),
        close_fee_bps=Decimal("0"),
        maker_fee_bps=Decimal("6"),  # no maker/taker split; flat rate
        hedge_destination=True,
        position_readable=True,
        verified=True,
        fees_state_dependent=True,  # varies 3–10 bps by pair
        fee_source="live_api",
        source="docs.ostium.com/traders/reference/fees + subgraph openFeeP",
        as_of="2026-08-29",
        notes=(
            "Oracle-priced, no orderbook: zero slippage on execution. 0 bps close fee. "
            "Rollover (not zero-sum funding) accrues continuously per block, derived from "
            "real-world carry costs (term structure for commodities/FX, SOFR for equities). "
            "Oracle fee $0.10 flat per price request. Early-close fee (0–40 bps) applies "
            "only within 15s of open on profitable positions; irrelevant for hedges held >15s. "
            "Arbitrum L1; gas sponsored by protocol."
        ),
    ),
    "avantis": VenueFeeSchedule(
        venue="avantis",
        display_name="Avantis",
        # Stub only: live rates come from quote_hedge / the fees command.
        # Zeros here must never be shown or priced as a fallback (CONTRACT.md §7).
        open_fee_bps=Decimal(0),
        close_fee_bps=Decimal(0),
        maker_fee_bps=Decimal(0),
        hedge_destination=True,
        position_readable=False,          # CONTRACT.md section 1: hedge destination only
        verified=True,
        fee_source="live_api",
        fees_state_dependent=True,        # per-pair on the live snapshot
        live=True,                        # `fees` command must live-fetch
        min_position_usd=Decimal(100),
        min_position_overrides={m: Decimal(300) for m in _AVANTIS_FX_METALS},
        source="live: https://prod-api.avantisfi.com/data/v2/trading (fetched per invocation, see §12.3)",
        as_of="live",
        notes=(
            "Fees are LIVE-FETCHED per pair from prod-api.avantisfi.com; the numeric "
            "fields in this stub are zero placeholders and must not be shown as a "
            "static schedule. The ranker prices both legs at the pair's live maker "
            "rate (§12.8), which assumes a maker close; RWA pairs are 0/0/0/0 under a "
            "revocable growth-mode promotion (§7.6.2). The closing fee is charged on "
            "notional PLUS gross PnL, so a winning hedge pays more to close than a "
            "flat-rate model shows."
        ),
    ),
}

HEDGE_DESTINATIONS: tuple[str, ...] = tuple(
    v for v, s in FEE_SCHEDULE.items() if s.hedge_destination
)
UNVERIFIED_FEE_VENUES: frozenset[str] = frozenset(
    v for v, s in FEE_SCHEDULE.items() if not s.verified
)
STATE_DEPENDENT_FEE_VENUES: frozenset[str] = frozenset(
    v for v, s in FEE_SCHEDULE.items() if s.fees_state_dependent
)
STATIC_FALLBACK_FEE_VENUES: frozenset[str] = frozenset(
    v for v, s in FEE_SCHEDULE.items() if s.fee_source == "static_fallback"
)

# Named in every ranking whether it wins or loses (CONTRACT.md 7.5 item 1).
PRIMARY_HEDGE_VENUE = "avantis"

# CONTRACT.md §12.9: Avantis (standard + Upside) is ranked only when the
# funding it offers the hedger strictly exceeds what existing positions pay.
FUNDING_GATED_VENUES: frozenset[str] = frozenset({"avantis", "avantis_upside"})

# ======================================================================================
# End of fee schedule.
# ======================================================================================


# --------------------------------------------------------------------------------------
# Base-asset normalization
# --------------------------------------------------------------------------------------

def _load_normalizer():
    """Prefer the ingestion layer's alias map; it knows FX pairs and meme tickers."""
    try:
        from hedge_scanner import assets  # type: ignore
    except Exception:  # pragma: no cover - only before assets.py exists
        return None
    for name in ("normalize_base_asset", "normalize_base"):
        candidate = getattr(assets, name, None)
        if callable(candidate):
            return candidate
    return None


_normalize_base = _load_normalizer()

_MARKET_NOISE = re.compile(
    r"(?i)[-_]?(perp(etual)?s?|swap|usdt|usdc|usd|pool)$"
)


def _fallback_normalize_base(symbol: str) -> str:
    """Last-resort base extraction. `assets.normalize_base` is authoritative."""
    token = (symbol or "").strip().upper().replace("/", "-")
    for _ in range(4):
        stripped = _MARKET_NOISE.sub("", token)
        if stripped == token:
            break
        token = stripped
    token = token.strip("-_")
    if token.startswith("W") and token[1:] in {"BTC", "ETH", "SOL"}:
        token = token[1:]
    return {"XBT": "BTC"}.get(token, token)


def quote_base_asset(quote: Quote) -> str:
    """Base asset a quote refers to, preferring the explicit field."""
    explicit = (getattr(quote, "base_asset", "") or "").strip().upper()
    if explicit:
        return explicit
    if _normalize_base is not None:
        return str(_normalize_base(quote.market)).upper()
    return _fallback_normalize_base(quote.market)


# --------------------------------------------------------------------------------------
# Horizon parsing / formatting
# --------------------------------------------------------------------------------------

_HORIZON_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hdw]?)\s*$", re.IGNORECASE)
_HORIZON_UNIT_H = {"": Decimal(1), "h": Decimal(1), "d": Decimal(24), "w": Decimal(168)}


def parse_horizon(text: str | Decimal | int) -> Decimal:
    """`"24h"` / `"3d"` / `"1w"` / `"36"` -> hours as Decimal. Bare number = hours."""
    if isinstance(text, (Decimal, int)):
        hours = Decimal(text)
    else:
        match = _HORIZON_RE.match(str(text))
        if not match:
            raise ValueError(
                f"cannot parse horizon {text!r}; use e.g. 8h, 24h, 3d, 1w, or a bare "
                f"number of hours"
            )
        hours = Decimal(match.group(1)) * _HORIZON_UNIT_H[match.group(2).lower()]
    if hours <= ZERO:
        raise ValueError(f"horizon must be positive, got {hours}")
    return hours


def parse_horizons(text: str) -> tuple[Decimal, ...]:
    """Comma-separated horizon list, de-duplicated and sorted ascending."""
    parts = [p for p in (s.strip() for s in text.split(",")) if p]
    if not parts:
        raise ValueError("horizon list is empty")
    return tuple(sorted({parse_horizon(p) for p in parts}))


def format_horizon(hours: Decimal) -> str:
    """Hours -> compact label. 720 -> '30d', 168 -> '7d', 8 -> '8h'."""
    if hours % Decimal(24) == ZERO and hours >= Decimal(24):
        return f"{(hours / Decimal(24)).normalize():f}d"
    return f"{hours.normalize():f}h"


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NetExposure:
    """Signed net notional for one base asset, aggregated across every venue."""

    base_asset: str
    net_notional_usd: Decimal        # signed: + net long, - net short
    long_notional_usd: Decimal       # magnitude of all long legs
    short_notional_usd: Decimal      # magnitude of all short legs (positive)
    position_count: int
    venues: tuple[str, ...]
    long_venues: tuple[str, ...]
    short_venues: tuple[str, ...]
    # CONTRACT.md §12.9. Absolute-notional-weighted average of every
    # contributing position's ``current_funding_rate_8h_bps``, signed from the
    # POSITION HOLDER'S perspective (positive = holder is currently receiving
    # funding, negative = paying). ``None`` when no contributing position
    # supplies a live rate. Used by ``rank_hedge_venues`` to gate Avantis
    # rows: if hedging on Avantis would leave the user's net funding negative,
    # the Avantis (and ``avantis_upside``) rows are excluded.
    weighted_current_funding_8h_bps: Decimal | None = None

    @property
    def gross_notional_usd(self) -> Decimal:
        return self.long_notional_usd + self.short_notional_usd

    @property
    def abs_net_notional_usd(self) -> Decimal:
        return abs(self.net_notional_usd)

    @property
    def offsetting_notional_usd(self) -> Decimal:
        """Notional already self-hedged: held long somewhere and short elsewhere."""
        return min(self.long_notional_usd, self.short_notional_usd)

    @property
    def gross_net_gap_usd(self) -> Decimal:
        """gross - |net|. Twice the offsetting notional: both legs are redundant."""
        return self.gross_notional_usd - self.abs_net_notional_usd

    @property
    def net_direction(self) -> str:
        if self.net_notional_usd > ZERO:
            return "long"
        if self.net_notional_usd < ZERO:
            return "short"
        return "flat"

    @property
    def hedge_side(self) -> str:
        """Side a hedge must take to flatten this exposure."""
        return "short" if self.net_notional_usd > ZERO else "long"

    @property
    def is_self_hedged(self) -> bool:
        return self.long_notional_usd > ZERO and self.short_notional_usd > ZERO


@dataclass(frozen=True)
class SelfHedgeFinding:
    """The user is paying to hold both sides of the same asset."""

    base_asset: str
    long_notional_usd: Decimal
    short_notional_usd: Decimal
    net_notional_usd: Decimal
    offsetting_notional_usd: Decimal
    gross_net_gap_usd: Decimal
    long_venues: tuple[str, ...]
    short_venues: tuple[str, ...]
    unwind_fee_bps: Decimal          # close fees on both redundant legs, per contract
    unwind_fee_usd: Decimal
    fee_schedule_unverified: bool
    fully_offset: bool               # net is dust: the pair is a pure round trip to nowhere


@dataclass(frozen=True)
class HedgeCost:
    """All-in cost of holding one hedge leg on one venue for one horizon.

    `total_bps` is a COST. Negative total_bps is a positive-carry hedge: the
    funding received over the horizon more than pays for the round-trip fees.
    """

    venue: str
    market: str
    side: str
    notional_usd: Decimal            # notional the USD figures are computed on
    horizon_hours: Decimal

    open_fee_bps: Decimal
    close_fee_bps: Decimal
    price_impact_bps: Decimal
    slippage_bps: Decimal

    funding_rate_8h_bps: Decimal     # raw signed quote value: + = hedger receives
    borrow_rate_8h_bps: Decimal      # always a cost

    quote_notional_usd: Decimal
    fee_provenance: str              # "live-or-adapter" | "unverified-placeholder"
    # True when this venue's posted fee is state-dependent (per-market, or set by
    # live OI skew) and must be confirmed against a live quote rather than a table.
    fees_state_dependent: bool = False
    notes: str = ""

    @property
    def round_trip_fee_bps(self) -> Decimal:
        """One-time, horizon-independent. The intercept of the cost line."""
        return (
            self.open_fee_bps
            + self.close_fee_bps
            + self.price_impact_bps
            + self.slippage_bps
        )

    @property
    def carry_cost_bps_per_8h(self) -> Decimal:
        """Carry as a COST per 8h. Funding received reduces it; borrow adds to it."""
        return self.borrow_rate_8h_bps - self.funding_rate_8h_bps

    @property
    def carry_cost_bps_per_hour(self) -> Decimal:
        """Slope of the cost line in bps per hour."""
        return self.carry_cost_bps_per_8h / FUNDING_PERIOD_H

    @property
    def carry_cost_bps(self) -> Decimal:
        """Carry cost accrued over the whole horizon."""
        return self.carry_cost_bps_per_8h * self.horizon_hours / FUNDING_PERIOD_H

    @property
    def total_bps(self) -> Decimal:
        return self.round_trip_fee_bps + self.carry_cost_bps

    @property
    def total_usd(self) -> Decimal:
        return self.total_bps * self.notional_usd / BPS_DENOM

    @property
    def round_trip_fee_usd(self) -> Decimal:
        return self.round_trip_fee_bps * self.notional_usd / BPS_DENOM

    @property
    def carry_cost_usd(self) -> Decimal:
        return self.carry_cost_bps * self.notional_usd / BPS_DENOM

    @property
    def positive_carry(self) -> bool:
        """Hedger is net paid to hold this hedge over the horizon."""
        return self.total_bps < ZERO

    @property
    def receives_funding(self) -> bool:
        return self.carry_cost_bps_per_8h < ZERO

    @property
    def fee_schedule_unverified(self) -> bool:
        return self.fee_provenance in ("unverified-placeholder", "static-fallback")

    @property
    def size_mismatch(self) -> bool:
        """Quote was priced at a materially different size than we are hedging."""
        if self.quote_notional_usd <= ZERO or self.notional_usd <= ZERO:
            return False
        gap = abs(self.quote_notional_usd - self.notional_usd) / self.notional_usd
        return gap > SIZE_MISMATCH_TOLERANCE

    def at_horizon(self, hours: Decimal) -> "HedgeCost":
        return replace(self, horizon_hours=hours)

    @property
    def breakeven_hours(self) -> Decimal | None:
        """Hours of positive carry needed to repay the round trip, or None."""
        if self.carry_cost_bps_per_8h >= ZERO:
            return None
        return (
            self.round_trip_fee_bps
            / (-self.carry_cost_bps_per_8h)
            * FUNDING_PERIOD_H
        )


@dataclass(frozen=True)
class ExcludedQuote:
    """A candidate deliberately kept out of a ranking, with the reason shown."""

    venue: str
    market: str
    side: str
    reason: str


@dataclass(frozen=True)
class DeltaHedgeOpportunity:
    kind: str
    base_asset: str
    exposure: NetExposure
    hedge_side: str
    hedge_notional_usd: Decimal
    horizon_hours: Decimal
    ranked: tuple[HedgeCost, ...]
    excluded: tuple[ExcludedQuote, ...]
    liquidation_risks: tuple["LiquidationRiskResult", ...] = ()

    @property
    def best(self) -> HedgeCost | None:
        return self.ranked[0] if self.ranked else None

    @property
    def positive_carry(self) -> tuple[HedgeCost, ...]:
        return tuple(c for c in self.ranked if c.positive_carry)


@dataclass(frozen=True)
class FundingArbOpportunity:
    """Delta-neutral pair: long on one venue, short on another, same base asset."""

    kind: str
    base_asset: str
    long_venue: str
    short_venue: str
    long_market: str
    short_market: str
    notional_usd: Decimal
    horizon_hours: Decimal

    long_funding_8h_bps: Decimal     # signed, + = receives
    short_funding_8h_bps: Decimal    # signed, + = receives
    long_borrow_8h_bps: Decimal
    short_borrow_8h_bps: Decimal
    fee_bps: Decimal                 # fees that must be earned back
    fee_basis: str                   # "round_trip" | "exit_only"
    basis: str                       # "existing" | "new"
    fee_schedule_unverified: bool
    notes: str = ""

    @property
    def net_carry_bps_per_8h(self) -> Decimal:
        """Positive = the pair is paid to exist."""
        return (
            self.long_funding_8h_bps
            + self.short_funding_8h_bps
            - self.long_borrow_8h_bps
            - self.short_borrow_8h_bps
        )

    @property
    def net_carry_usd_per_8h(self) -> Decimal:
        return self.net_carry_bps_per_8h * self.notional_usd / BPS_DENOM

    @property
    def opposite_funding_signs(self) -> bool:
        return (self.long_funding_8h_bps * self.short_funding_8h_bps) < ZERO

    @property
    def breakeven_hours(self) -> Decimal | None:
        """Hours of carry needed to repay `fee_bps`. None if carry never repays it."""
        carry = self.net_carry_bps_per_8h
        if carry <= ZERO:
            return None
        return (self.fee_bps / carry) * FUNDING_PERIOD_H

    @property
    def net_pnl_bps(self) -> Decimal:
        """Carry earned over the horizon, minus the fees that must be earned back."""
        return (
            self.net_carry_bps_per_8h * self.horizon_hours / FUNDING_PERIOD_H
            - self.fee_bps
        )

    @property
    def net_pnl_usd(self) -> Decimal:
        return self.net_pnl_bps * self.notional_usd / BPS_DENOM

    @property
    def profitable_at_horizon(self) -> bool:
        return self.net_pnl_bps > ZERO

    def at_horizon(self, hours: Decimal) -> "FundingArbOpportunity":
        return replace(self, horizon_hours=hours)


@dataclass(frozen=True)
class HorizonCrossover:
    """Holding period at which the cheapest hedge venue changes."""

    base_asset: str
    at_hours: Decimal
    from_venue: str
    to_venue: str
    cost_bps_at_crossover: Decimal


@dataclass(frozen=True)
class HorizonSensitivity:
    """Cost of every candidate venue across several horizons, plus crossovers."""

    base_asset: str
    hedge_side: str
    notional_usd: Decimal
    horizons_hours: tuple[Decimal, ...]
    venues: tuple[str, ...]
    grid: dict[str, dict[Decimal, Decimal]]   # venue -> hours -> total cost bps
    crossovers: tuple[HorizonCrossover, ...]
    max_horizon_hours: Decimal

    def cheapest_at(self, hours: Decimal) -> str | None:
        candidates = [
            (self.grid[v][hours], v) for v in self.venues if hours in self.grid[v]
        ]
        if not candidates:
            return None
        return min(candidates)[1]

    @property
    def venue_is_horizon_dependent(self) -> bool:
        return bool(self.crossovers)


@dataclass(frozen=True)
class ScanConfig:
    horizon_hours: Decimal = DEFAULT_HORIZON_H
    horizons_hours: tuple[Decimal, ...] = DEFAULT_HORIZONS_H
    dust_threshold_usd: Decimal = DEFAULT_DUST_USD
    max_crossover_horizon_h: Decimal = DEFAULT_MAX_CROSSOVER_H
    min_arb_carry_bps_8h: Decimal = DEFAULT_MIN_ARB_CARRY_BPS_8H
    funding_arb_notional_usd: Decimal | None = None   # None = use net exposure size


@dataclass(frozen=True)
class ScanResult:
    addresses: tuple[str, ...]
    positions: tuple[Position, ...]
    exposures: tuple[NetExposure, ...]
    self_hedge_findings: tuple[SelfHedgeFinding, ...]
    delta_hedges: tuple[DeltaHedgeOpportunity, ...]
    funding_arbs: tuple[FundingArbOpportunity, ...]
    sensitivities: tuple[HorizonSensitivity, ...]
    avantis_comparisons: tuple["AvantisComparison", ...]
    upside_comparisons: tuple["UpsideHedgeComparison", ...]
    dust_exposures: tuple[NetExposure, ...]
    venue_errors: tuple[object, ...]
    config: ScanConfig
    generated_at: datetime

    @property
    def horizon_hours(self) -> Decimal:
        return self.config.horizon_hours

    @property
    def total_gross_notional_usd(self) -> Decimal:
        return sum((abs(p.notional_usd) for p in self.positions), ZERO)

    @property
    def total_abs_net_notional_usd(self) -> Decimal:
        return sum((e.abs_net_notional_usd for e in self.exposures), ZERO)


# --------------------------------------------------------------------------------------
# 1. Portfolio netting
# --------------------------------------------------------------------------------------


def _signed_notional(position: Position) -> Decimal:
    """Trust the sign of `notional_usd`, but repair it from `side` if inconsistent.

    Adapters occasionally emit an unsigned notional. Silently netting an unsigned
    short as a long is the single worst bug this tool could have, so `side` is
    treated as the authority on direction and the magnitude comes from notional.
    """
    magnitude = abs(Decimal(position.notional_usd))
    return -magnitude if position.side == "short" else magnitude


def net_exposures(
    positions: Iterable[Position],
    *,
    dust_threshold_usd: Decimal = DEFAULT_DUST_USD,
) -> tuple[tuple[NetExposure, ...], tuple[NetExposure, ...]]:
    """Aggregate positions by normalized base asset.

    Returns `(material, dust)`: exposures whose absolute net clears
    `dust_threshold_usd`, and those that do not. Dust is not discarded -- an asset
    that nets to zero across two venues is itself a finding.
    """
    buckets: dict[str, list[Position]] = {}
    for position in positions:
        buckets.setdefault((position.base_asset or "").strip().upper(), []).append(
            position
        )

    material: list[NetExposure] = []
    dust: list[NetExposure] = []

    for base_asset, group in buckets.items():
        long_notional = sum(
            (_signed_notional(p) for p in group if _signed_notional(p) > ZERO), ZERO
        )
        short_notional = -sum(
            (_signed_notional(p) for p in group if _signed_notional(p) < ZERO), ZERO
        )
        exposure = NetExposure(
            base_asset=base_asset,
            net_notional_usd=long_notional - short_notional,
            long_notional_usd=long_notional,
            short_notional_usd=short_notional,
            position_count=len(group),
            venues=_ordered_unique(p.venue for p in group),
            long_venues=_ordered_unique(
                p.venue for p in group if _signed_notional(p) > ZERO
            ),
            short_venues=_ordered_unique(
                p.venue for p in group if _signed_notional(p) < ZERO
            ),
            weighted_current_funding_8h_bps=_weighted_current_funding(group),
        )
        if exposure.abs_net_notional_usd >= dust_threshold_usd:
            material.append(exposure)
        else:
            dust.append(exposure)

    material.sort(key=lambda e: (-e.abs_net_notional_usd, e.base_asset))
    dust.sort(key=lambda e: (-e.gross_notional_usd, e.base_asset))
    return tuple(material), tuple(dust)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


def _weighted_current_funding(positions: Iterable[Position]) -> Decimal | None:
    """Absolute-notional-weighted average of live per-position funding rates.

    Returns ``None`` when no position supplies a live rate (adapter degradation)
    or when the summed weight is zero. The result is signed from the POSITION
    HOLDER'S perspective: positive = holder is currently receiving funding,
    negative = paying. This is the aggregate the Avantis filter compares its
    own quote against.

    Positions with ``current_funding_rate_8h_bps=None`` are skipped rather than
    treated as zero -- a missing rate is not the same as a zero rate, and
    pretending otherwise would silently dilute the average.
    """
    total_weight = ZERO
    total_weighted = ZERO
    for p in positions:
        rate = getattr(p, "current_funding_rate_8h_bps", None)
        if rate is None:
            continue
        weight = abs(Decimal(p.notional_usd))
        if weight <= ZERO:
            continue
        total_weight += weight
        total_weighted += weight * Decimal(rate)
    if total_weight <= ZERO:
        return None
    return total_weighted / total_weight


def self_hedge_findings(
    exposures: Iterable[NetExposure],
    *,
    dust_threshold_usd: Decimal = DEFAULT_DUST_USD,
) -> tuple[SelfHedgeFinding, ...]:
    """Flag assets held long on one venue and short on another.

    The offsetting notional is directionally inert but still pays fees and funding
    on both legs. Collapsing it is usually strictly better than holding it.
    """
    findings: list[SelfHedgeFinding] = []
    for exposure in exposures:
        if not exposure.is_self_hedged:
            continue
        offsetting = exposure.offsetting_notional_usd
        if offsetting <= ZERO:
            continue

        # Cost to unwind the redundant pair: the exit fee on both legs. Entry fees
        # are already sunk and are not a decision input.
        unverified = False
        close_bps = ZERO
        for venue in set(exposure.long_venues) | set(exposure.short_venues):
            schedule = FEE_SCHEDULE.get(venue)
            if schedule is None:
                unverified = True
                continue
            unverified = unverified or not schedule.verified
            close_bps += schedule.close_fee_for(exposure.base_asset)

        findings.append(
            SelfHedgeFinding(
                base_asset=exposure.base_asset,
                long_notional_usd=exposure.long_notional_usd,
                short_notional_usd=exposure.short_notional_usd,
                net_notional_usd=exposure.net_notional_usd,
                offsetting_notional_usd=offsetting,
                gross_net_gap_usd=exposure.gross_net_gap_usd,
                long_venues=exposure.long_venues,
                short_venues=exposure.short_venues,
                unwind_fee_bps=close_bps,
                unwind_fee_usd=close_bps * offsetting / BPS_DENOM,
                fee_schedule_unverified=unverified,
                fully_offset=exposure.abs_net_notional_usd < dust_threshold_usd,
            )
        )
    findings.sort(key=lambda f: -f.gross_net_gap_usd)
    return tuple(findings)


# --------------------------------------------------------------------------------------
# 2. Delta hedge costing and ranking
# --------------------------------------------------------------------------------------


def hedge_cost(
    quote: Quote,
    *,
    horizon_hours: Decimal,
    notional_usd: Decimal | None = None,
) -> HedgeCost:
    """Cost of holding the hedge described by `quote` for `horizon_hours`.

        round_trip_fee_bps = open + close + price_impact + slippage
        carry_cost_bps_8h  = borrow_rate_8h_bps - funding_rate_8h_bps
        total_bps          = round_trip_fee_bps + carry_cost_bps_8h * horizon_h / 8

    Note the sign of the carry term: `funding_rate_8h_bps` is positive when the
    hedger RECEIVES, so it is SUBTRACTED from cost. See CONTRACT.md section 6.
    """
    if horizon_hours <= ZERO:
        raise ValueError("horizon_hours must be positive")

    schedule = FEE_SCHEDULE.get(quote.venue)
    provenance = (
        "unverified-placeholder"
        if schedule is not None and not schedule.verified
        else (
            "static-fallback"
            if schedule is not None and schedule.fee_source == "static_fallback"
            else "live-or-adapter"
        )
    )
    target_notional = (
        Decimal(notional_usd) if notional_usd is not None else Decimal(quote.notional_usd)
    )

    return HedgeCost(
        venue=quote.venue,
        market=quote.market,
        side=quote.side,
        notional_usd=abs(target_notional),
        horizon_hours=horizon_hours,
        open_fee_bps=Decimal(quote.taker_fee_bps),
        close_fee_bps=Decimal(quote.close_fee_bps),
        price_impact_bps=Decimal(quote.price_impact_bps),
        slippage_bps=Decimal(quote.est_slippage_bps),
        funding_rate_8h_bps=Decimal(quote.funding_rate_8h_bps),
        borrow_rate_8h_bps=Decimal(quote.borrow_rate_8h_bps),
        quote_notional_usd=abs(Decimal(quote.notional_usd)),
        fee_provenance=provenance,
        fees_state_dependent=bool(schedule is not None and schedule.fees_state_dependent),
        notes=quote.notes or "",
    )


def _rank_key(cost: HedgeCost) -> tuple:
    """Cheapest total cost wins. Ties break toward less carry risk, then fee, then name.

    Positive-carry hedges naturally sort first because their `total_bps` is
    negative; no special-casing is needed, and none is used, so the ordering
    stays a single consistent economic criterion.
    """
    return (
        cost.total_bps,
        cost.carry_cost_bps_per_8h,
        cost.round_trip_fee_bps,
        cost.venue,
    )


def rank_hedge_venues(
    base_asset: str,
    hedge_side: str,
    notional_usd: Decimal,
    quotes: Iterable[Quote],
    *,
    horizon_hours: Decimal,
    user_current_funding_8h_bps: Decimal | None = None,
) -> tuple[tuple[HedgeCost, ...], tuple[ExcludedQuote, ...]]:
    """Rank candidate hedge venues by all-in cost, cheapest first.

    Unavailable quotes are EXCLUDED with a reason, never treated as zero cost.

    ``user_current_funding_8h_bps`` is the notional-weighted average funding
    rate the user is currently accruing across their existing positions in
    this asset, signed from the position holder's perspective (positive =
    holder is receiving). When it is supplied (i.e. at least one contributing
    adapter provided a live rate), the CONTRACT.md §12.9 net-positive-funding
    gate applies to ``FUNDING_GATED_VENUES`` (Avantis and ``avantis_upside``):
    each such quote is excluded unless its ``funding_rate_8h_bps`` strictly
    exceeds ``-user_current_funding_8h_bps``, i.e. hedging on that Avantis
    instrument would leave the user's net funding strictly positive. Other
    venues rank on all-in cost regardless of the user's current funding, per
    §7's "never rig the ranking" rule. When the argument is ``None`` (no
    adapter supplied a live rate for this asset), the gate is not applied
    and every Avantis row is ranked normally.
    """
    ranked: list[HedgeCost] = []
    excluded: list[ExcludedQuote] = []
    target = abs(Decimal(notional_usd))
    user_rate = (
        Decimal(user_current_funding_8h_bps)
        if user_current_funding_8h_bps is not None
        else None
    )

    for quote in quotes:
        if quote_base_asset(quote) != base_asset:
            continue
        if quote.side != hedge_side:
            continue

        if not quote.available:
            excluded.append(
                ExcludedQuote(
                    venue=quote.venue,
                    market=quote.market,
                    side=quote.side,
                    reason=quote.notes or "quote unavailable",
                )
            )
            continue

        schedule = FEE_SCHEDULE.get(quote.venue)
        if schedule is not None and not schedule.hedge_destination:
            excluded.append(
                ExcludedQuote(
                    venue=quote.venue,
                    market=quote.market,
                    side=quote.side,
                    reason="not a permitted hedge destination",
                )
            )
            continue

        # Never recommend a hedge the venue would reject outright.
        if schedule is not None:
            minimum = schedule.min_position_for(base_asset)
            if minimum > ZERO and target < minimum:
                excluded.append(
                    ExcludedQuote(
                        venue=quote.venue,
                        market=quote.market,
                        side=quote.side,
                        reason=(
                            f"hedge size {target:,.2f} USD is below the venue minimum "
                            f"of {minimum:,.2f} USD"
                        ),
                    )
                )
                continue

        # CONTRACT.md §12.9: skip Avantis when its funding would not cover
        # what the user currently pays on the existing position.
        if user_rate is not None and quote.venue in FUNDING_GATED_VENUES:
            avantis_rate = Decimal(quote.funding_rate_8h_bps)
            required = -user_rate
            if avantis_rate <= required:
                user_cost = -user_rate  # positive = user pays, negative = user receives
                if user_cost > ZERO:
                    user_side = f"paying {user_cost} bps/8h"
                elif user_cost < ZERO:
                    user_side = f"receiving {-user_cost} bps/8h"
                else:
                    user_side = "at 0 bps/8h funding"
                if avantis_rate > ZERO:
                    hedge_side_str = f"receives {avantis_rate} bps/8h"
                elif avantis_rate < ZERO:
                    hedge_side_str = f"pays {-avantis_rate} bps/8h"
                else:
                    hedge_side_str = "pays 0 bps/8h"
                net = avantis_rate - user_cost  # >0 = net receives after hedge
                excluded.append(
                    ExcludedQuote(
                        venue=quote.venue,
                        market=quote.market,
                        side=quote.side,
                        reason=(
                            f"Avantis funding not higher than the user's current "
                            f"position funding: user is currently {user_side} on this "
                            f"asset while the Avantis hedge {hedge_side_str}. Net "
                            f"funding after hedging would be {net} bps/8h "
                            f"({'still a cost' if net <= ZERO else 'a receive'}), "
                            f"so hedging on this venue does not improve the funding "
                            f"position (§12.9)."
                        ),
                    )
                )
                continue

        ranked.append(hedge_cost(quote, horizon_hours=horizon_hours, notional_usd=target))

    ranked.sort(key=_rank_key)
    return tuple(ranked), tuple(excluded)


def delta_hedge_opportunities(
    exposures: Iterable[NetExposure],
    quotes: Iterable[Quote],
    *,
    config: ScanConfig | None = None,
) -> tuple[DeltaHedgeOpportunity, ...]:
    """One `delta_hedge` opportunity per asset with material net exposure."""
    config = config or ScanConfig()
    quote_list = list(quotes)
    opportunities: list[DeltaHedgeOpportunity] = []

    for exposure in exposures:
        if exposure.abs_net_notional_usd < config.dust_threshold_usd:
            continue
        notional = exposure.abs_net_notional_usd
        ranked, excluded = rank_hedge_venues(
            exposure.base_asset,
            exposure.hedge_side,
            notional,
            quote_list,
            horizon_hours=config.horizon_hours,
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        opportunities.append(
            DeltaHedgeOpportunity(
                kind="delta_hedge",
                base_asset=exposure.base_asset,
                exposure=exposure,
                hedge_side=exposure.hedge_side,
                hedge_notional_usd=notional,
                horizon_hours=config.horizon_hours,
                ranked=ranked,
                excluded=excluded,
            )
        )

    # Positive-carry opportunities first, then by size of the exposure at stake.
    opportunities.sort(
        key=lambda o: (
            ZERO if (o.best and o.best.positive_carry) else Decimal(1),
            -o.hedge_notional_usd,
        )
    )
    return tuple(opportunities)


# --------------------------------------------------------------------------------------
# 2b. Avantis comparison line (CONTRACT.md 7.5 item 1)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AvantisComparison:
    """Where Avantis lands versus the cheapest alternative, win or lose.

    Avantis is the product's intended hedge destination, so it is named in every
    ranking rather than quietly dropping off the bottom. The ranking itself is not
    weighted toward it: this type only reports the gap the ranking already produced.
    """

    base_asset: str
    horizon_hours: Decimal
    notional_usd: Decimal
    avantis: HedgeCost | None
    avantis_rank: int | None
    best_alternative: HedgeCost | None
    excluded_reason: str | None

    @property
    def verdict(self) -> str:
        if self.avantis is None:
            return "no_quote"
        if self.best_alternative is None:
            return "only_candidate"
        if self.avantis.total_bps < self.best_alternative.total_bps:
            return "wins"
        if self.avantis.total_bps == self.best_alternative.total_bps:
            return "ties"
        return "loses"

    @property
    def delta_bps(self) -> Decimal | None:
        """Avantis cost minus the best alternative. Positive means Avantis is dearer."""
        if self.avantis is None or self.best_alternative is None:
            return None
        return self.avantis.total_bps - self.best_alternative.total_bps

    @property
    def delta_usd(self) -> Decimal | None:
        if self.delta_bps is None:
            return None
        return self.delta_bps * self.notional_usd / BPS_DENOM


def avantis_comparison(
    opportunity: DeltaHedgeOpportunity, *, venue: str = PRIMARY_HEDGE_VENUE
) -> AvantisComparison:
    """Extract the Avantis line from a ranking without reordering it."""
    avantis: HedgeCost | None = None
    avantis_rank: int | None = None
    for index, cost in enumerate(opportunity.ranked, start=1):
        if cost.venue == venue:
            avantis, avantis_rank = cost, index
            break

    best_alternative = next(
        (c for c in opportunity.ranked if c.venue != venue), None
    )
    excluded_reason = next(
        (x.reason for x in opportunity.excluded if x.venue == venue), None
    )
    if avantis is None and excluded_reason is None:
        excluded_reason = "no quote returned for this asset and side"

    return AvantisComparison(
        base_asset=opportunity.base_asset,
        horizon_hours=opportunity.horizon_hours,
        notional_usd=opportunity.hedge_notional_usd,
        avantis=avantis,
        avantis_rank=avantis_rank,
        best_alternative=best_alternative,
        excluded_reason=excluded_reason if avantis is None else None,
    )


# --------------------------------------------------------------------------------------
# 2c. Avantis Upside Perps as a distinct hedge instrument (CONTRACT.md 7.6)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsideHedgeComparison:
    """Upside Perps charge no commission and no borrow, but take a share of profit.

    As a hedge leg the risk shape inverts: you pay nothing when the hedge turns out
    to have been unnecessary, and you pay a share of the payoff exactly when the
    hedge works. So the comparison is not a single number, it is a threshold on how
    far the underlying moves against the position being hedged.
    """

    base_asset: str
    horizon_hours: Decimal
    notional_usd: Decimal
    standard_venue: str
    standard_cost_bps: Decimal        # cheapest conventional hedge over the horizon
    upside_fixed_cost_bps: Decimal    # spread plus carry; no commission, no borrow
    profit_share_fraction: Decimal
    derived_from_venue: str | None    # None when quoted directly by an adapter
    notes: str = ""

    @property
    def standard_cost_usd(self) -> Decimal:
        return self.standard_cost_bps * self.notional_usd / BPS_DENOM

    @property
    def upside_fixed_cost_usd(self) -> Decimal:
        return self.upside_fixed_cost_bps * self.notional_usd / BPS_DENOM

    @property
    def breakeven_adverse_move_bps(self) -> Decimal | None:
        """Adverse move at which Upside stops being the cheaper hedge.

        The hedge's gross profit is roughly the adverse move times the notional, so
        the profit share costs `share * move_bps`. Upside is cheaper while

            upside_fixed_bps + share * move_bps < standard_bps

        which gives a threshold of `(standard_bps - upside_fixed_bps) / share`.
        Below that move the hedge was barely needed and Upside is cheaper; above it
        the profit share exceeds the commission it saved.
        """
        if self.profit_share_fraction <= ZERO:
            return None
        headroom = self.standard_cost_bps - self.upside_fixed_cost_bps
        if headroom <= ZERO:
            return ZERO   # Upside is already the dearer option before any payoff
        return headroom / self.profit_share_fraction

    @property
    def cheaper_when_hedge_unused(self) -> bool:
        return self.upside_fixed_cost_bps < self.standard_cost_bps


def upside_hedge_comparison(
    opportunity: DeltaHedgeOpportunity,
    quotes: Iterable[Quote] = (),
    *,
    profit_share_fraction: Decimal = AVANTIS_UPSIDE_BASE_SHARE,
) -> UpsideHedgeComparison | None:
    """Evaluate an Avantis Upside Perp against the cheapest conventional hedge.

    Uses an `avantis_upside` quote if an adapter supplies one. Otherwise derives the
    fixed leg from the standard Avantis quote by dropping commission and borrow,
    both documented as zero on Upside, and keeping spread and funding, both of which
    still apply. That derivation is recorded in `derived_from_venue` so the output
    can say where the number came from.
    """
    if opportunity.base_asset not in AVANTIS_UPSIDE_ASSETS or not opportunity.ranked:
        return None

    # Compare against a conventional perp, not against the Upside row itself.
    cheapest = next(
        (c for c in opportunity.ranked if c.venue != "avantis_upside"), None
    )
    if cheapest is None:
        return None
    horizon = opportunity.horizon_hours

    quoted = next(
        (
            q
            for q in quotes
            if q.venue == "avantis_upside"
            and q.available
            and q.side == opportunity.hedge_side
            and quote_base_asset(q) == opportunity.base_asset
        ),
        None,
    )
    if quoted is not None:
        fixed = hedge_cost(
            quoted, horizon_hours=horizon, notional_usd=opportunity.hedge_notional_usd
        )
        fixed_bps = fixed.total_bps
        derived_from = None
    else:
        source = next(
            (c for c in opportunity.ranked if c.venue == PRIMARY_HEDGE_VENUE), None
        )
        if source is None:
            return None
        # Commission and borrow are zero on Upside; spread and funding still apply.
        fixed_bps = (
            source.price_impact_bps
            + source.slippage_bps
            - source.funding_rate_8h_bps * horizon / FUNDING_PERIOD_H
        )
        derived_from = source.venue

    return UpsideHedgeComparison(
        base_asset=opportunity.base_asset,
        horizon_hours=horizon,
        notional_usd=opportunity.hedge_notional_usd,
        standard_venue=cheapest.venue,
        standard_cost_bps=cheapest.total_bps,
        upside_fixed_cost_bps=fixed_bps,
        profit_share_fraction=profit_share_fraction,
        derived_from_venue=derived_from,
        notes=(
            "Profit share is keyed to ROI, so the modelled "
            f"{(profit_share_fraction * Decimal(100)).normalize():f}% band applies from "
            "1% to 500% ROI; higher-ROI closes keep more. Market orders only, crypto "
            "majors only, and the hedge cannot be closed as a limit."
        ),
    )


# --------------------------------------------------------------------------------------
# 3. Funding arbitrage
# --------------------------------------------------------------------------------------


def funding_arb_opportunities(
    exposures: Iterable[NetExposure],
    quotes: Iterable[Quote],
    positions: Iterable[Position] = (),
    *,
    config: ScanConfig | None = None,
) -> tuple[FundingArbOpportunity, ...]:
    """Same-asset opposite-funding-sign pairs across two venues.

    Two flavours, both tagged:

    `existing` -- the user already holds the long leg on one venue and the short
    leg on another. Entry fees are sunk, so only the exit fees have to be earned
    back and `fee_basis` is "exit_only".

    `new` -- a delta-neutral pair the user could open. The full round trip on both
    legs has to be earned back; `fee_basis` is "round_trip".

    Only pairs with net carry above `min_arb_carry_bps_8h` are returned: a pair
    that bleeds carry is not an arbitrage, it is a loss with extra steps.
    """
    config = config or ScanConfig()
    quote_list = list(quotes)
    position_list = list(positions)

    long_quotes: dict[tuple[str, str], Quote] = {}
    short_quotes: dict[tuple[str, str], Quote] = {}
    for quote in quote_list:
        if not quote.available:
            continue
        key = (quote_base_asset(quote), quote.venue)
        if quote.side == "long":
            long_quotes.setdefault(key, quote)
        elif quote.side == "short":
            short_quotes.setdefault(key, quote)

    held: dict[tuple[str, str, str], Decimal] = {}
    for position in position_list:
        key = (
            (position.base_asset or "").strip().upper(),
            position.venue,
            position.side,
        )
        held[key] = held.get(key, ZERO) + abs(Decimal(position.notional_usd))

    results: list[FundingArbOpportunity] = []
    exposure_by_asset = {e.base_asset: e for e in exposures}

    assets = {a for (a, _v) in long_quotes} | {a for (a, _v) in short_quotes}
    for base_asset in sorted(assets):
        long_venues = sorted(v for (a, v) in long_quotes if a == base_asset)
        short_venues = sorted(v for (a, v) in short_quotes if a == base_asset)

        for long_venue in long_venues:
            for short_venue in short_venues:
                if long_venue == short_venue:
                    continue
                long_q = long_quotes[(base_asset, long_venue)]
                short_q = short_quotes[(base_asset, short_venue)]

                is_existing = (
                    (base_asset, long_venue, "long") in held
                    and (base_asset, short_venue, "short") in held
                )

                if is_existing:
                    notional = min(
                        held[(base_asset, long_venue, "long")],
                        held[(base_asset, short_venue, "short")],
                    )
                    fee_bps = (
                        Decimal(long_q.close_fee_bps) + Decimal(short_q.close_fee_bps)
                    )
                    fee_basis = "exit_only"
                    basis = "existing"
                else:
                    exposure = exposure_by_asset.get(base_asset)
                    notional = (
                        config.funding_arb_notional_usd
                        if config.funding_arb_notional_usd is not None
                        else (
                            exposure.abs_net_notional_usd
                            if exposure is not None
                            and exposure.abs_net_notional_usd > ZERO
                            else min(
                                abs(Decimal(long_q.notional_usd)),
                                abs(Decimal(short_q.notional_usd)),
                            )
                        )
                    )
                    fee_bps = (
                        Decimal(long_q.taker_fee_bps)
                        + Decimal(long_q.close_fee_bps)
                        + Decimal(long_q.price_impact_bps)
                        + Decimal(long_q.est_slippage_bps)
                        + Decimal(short_q.taker_fee_bps)
                        + Decimal(short_q.close_fee_bps)
                        + Decimal(short_q.price_impact_bps)
                        + Decimal(short_q.est_slippage_bps)
                    )
                    fee_basis = "round_trip"
                    basis = "new"

                if notional is None or notional <= ZERO:
                    continue

                opportunity = FundingArbOpportunity(
                    kind="funding_arb",
                    base_asset=base_asset,
                    long_venue=long_venue,
                    short_venue=short_venue,
                    long_market=long_q.market,
                    short_market=short_q.market,
                    notional_usd=notional,
                    horizon_hours=config.horizon_hours,
                    long_funding_8h_bps=Decimal(long_q.funding_rate_8h_bps),
                    short_funding_8h_bps=Decimal(short_q.funding_rate_8h_bps),
                    long_borrow_8h_bps=Decimal(long_q.borrow_rate_8h_bps),
                    short_borrow_8h_bps=Decimal(short_q.borrow_rate_8h_bps),
                    fee_bps=fee_bps,
                    fee_basis=fee_basis,
                    basis=basis,
                    fee_schedule_unverified=bool(
                        {long_venue, short_venue} & UNVERIFIED_FEE_VENUES
                    ),
                )
                if opportunity.net_carry_bps_per_8h < config.min_arb_carry_bps_8h:
                    continue
                results.append(opportunity)

    results.sort(
        key=lambda o: (
            ZERO if o.basis == "existing" else Decimal(1),
            -o.net_carry_bps_per_8h,
        )
    )
    return tuple(results)


# --------------------------------------------------------------------------------------
# 4. Horizon sensitivity and crossover detection
# --------------------------------------------------------------------------------------


def _lower_envelope_crossovers(
    lines: Sequence[tuple[str, Decimal, Decimal]],
    max_hours: Decimal,
) -> tuple[list[tuple[Decimal, str, str, Decimal]], str | None]:
    """Exact breakpoints of the lower envelope of cost lines over (0, max_hours].

    Each line is `(venue, intercept_bps, slope_bps_per_hour)`. Cost is affine in
    the holding period -- fees are one-time, carry accrues linearly -- so the
    cheapest-venue frontier is a piecewise-linear lower envelope and its
    breakpoints are solved algebraically rather than found by sampling a grid.
    A grid search would miss any crossover that falls between sample points, and
    those are exactly the ones a trader needs to know about.

    Returns `(breakpoints, leader_at_zero)` where each breakpoint is
    `(hours, from_venue, to_venue, cost_bps_at_crossover)`.
    """
    if len(lines) < 2:
        return [], (lines[0][0] if lines else None)

    # Leader as the horizon approaches zero: lowest fixed fee, then flattest carry.
    leader = min(lines, key=lambda ln: (ln[1], ln[2], ln[0]))
    leader_at_zero = leader[0]

    breakpoints: list[tuple[Decimal, str, str, Decimal]] = []
    current_h = ZERO

    for _ in range(len(lines)):
        _, leader_intercept, leader_slope = leader
        best: tuple[Decimal, tuple[str, Decimal, Decimal]] | None = None

        for candidate in lines:
            if candidate[0] == leader[0]:
                continue
            _, intercept, slope = candidate
            denominator = slope - leader_slope
            if denominator >= ZERO:
                continue  # never becomes cheaper: same or steeper carry
            crossing = (leader_intercept - intercept) / denominator
            if crossing <= current_h or crossing > max_hours:
                continue
            if best is None or (crossing, candidate[2]) < (best[0], best[1][2]):
                best = (crossing, candidate)

        if best is None:
            break

        crossing, candidate = best
        cost_at = leader_intercept + leader_slope * crossing
        breakpoints.append((crossing, leader[0], candidate[0], cost_at))
        leader = candidate
        current_h = crossing

    return breakpoints, leader_at_zero


def horizon_sensitivity(
    opportunity: DeltaHedgeOpportunity,
    *,
    config: ScanConfig | None = None,
) -> HorizonSensitivity:
    """Cost of every ranked venue at each configured horizon, plus crossovers.

    This is the analytically load-bearing output. Fees are one-time and carry is
    time-proportional, so the cheapest venue is a function of how long the hedge
    is held: a high-fee venue that pays funding overtakes a cheap venue that
    charges it, and the hour at which that happens is the decision.
    """
    config = config or ScanConfig()
    horizons = tuple(sorted(set(config.horizons_hours)))

    grid: dict[str, dict[Decimal, Decimal]] = {}
    lines: list[tuple[str, Decimal, Decimal]] = []
    for cost in opportunity.ranked:
        grid[cost.venue] = {
            hours: cost.at_horizon(hours).total_bps for hours in horizons
        }
        lines.append(
            (cost.venue, cost.round_trip_fee_bps, cost.carry_cost_bps_per_hour)
        )

    breakpoints, _ = _lower_envelope_crossovers(lines, config.max_crossover_horizon_h)
    crossovers = tuple(
        HorizonCrossover(
            base_asset=opportunity.base_asset,
            at_hours=hours,
            from_venue=from_venue,
            to_venue=to_venue,
            cost_bps_at_crossover=cost_bps,
        )
        for hours, from_venue, to_venue, cost_bps in breakpoints
    )

    return HorizonSensitivity(
        base_asset=opportunity.base_asset,
        hedge_side=opportunity.hedge_side,
        notional_usd=opportunity.hedge_notional_usd,
        horizons_hours=horizons,
        venues=tuple(c.venue for c in opportunity.ranked),
        grid=grid,
        crossovers=crossovers,
        max_horizon_hours=config.max_crossover_horizon_h,
    )


# --------------------------------------------------------------------------------------
# 5. Liquidation risk per hedge candidate
# --------------------------------------------------------------------------------------

# Late import to avoid a circular dependency (liquidation.py imports from models only).
from hedge_scanner.liquidation import (  # noqa: E402
    LIQUIDATION_SPECS,
    LiquidationRisk as LiquidationRiskResult,
    compute_liquidation_risk,
)

# Default leverage for hedge sizing when no explicit leverage is provided.
DEFAULT_HEDGE_LEVERAGE = Decimal(10)
# BTC reference price — used ONLY in the example output. The engine always needs
# a real entry price from a Quote or Position, but the CLI `fees` table and the
# test helper need a plausible reference price.
_REFERENCE_ENTRY_PRICE: dict[str, Decimal] = {
    "BTC": Decimal("65000"),
    "ETH": Decimal("3000"),
    "SOL": Decimal("150"),
}


def compute_hedge_liquidation_risks(
    opportunity: DeltaHedgeOpportunity,
    *,
    leverage: Decimal = DEFAULT_HEDGE_LEVERAGE,
    entry_price: Decimal | None = None,
) -> tuple[LiquidationRiskResult, ...]:
    """Compute liquidation risk for every ranked venue in a delta hedge opportunity.

    When `entry_price` is None, falls back to a reference price for the asset.
    The reference price is intentionally conservative and should only be used for
    illustrative output; a live scan should supply the actual mark price.
    """
    price = entry_price or _REFERENCE_ENTRY_PRICE.get(
        opportunity.base_asset, Decimal("100")
    )

    risks: list[LiquidationRiskResult] = []
    for cost in opportunity.ranked:
        risk = compute_liquidation_risk(
            venue=cost.venue,
            side=cost.side,
            entry_price=price,
            leverage=leverage,
            notional_usd=cost.notional_usd,
        )
        if risk is not None:
            risks.append(risk)
    return tuple(risks)


# --------------------------------------------------------------------------------------
# Top-level scan
# --------------------------------------------------------------------------------------


def scan(
    positions: Iterable[Position],
    quotes: Iterable[Quote] = (),
    *,
    addresses: Sequence[str] = (),
    venue_errors: Sequence[object] = (),
    config: ScanConfig | None = None,
    generated_at: datetime | None = None,
) -> ScanResult:
    """Run the full analysis over an already-fetched portfolio."""
    config = config or ScanConfig()
    position_list = list(positions)
    quote_list = list(quotes)

    exposures, dust = net_exposures(
        position_list, dust_threshold_usd=config.dust_threshold_usd
    )
    findings = self_hedge_findings(
        list(exposures) + list(dust), dust_threshold_usd=config.dust_threshold_usd
    )
    delta_hedges_raw = delta_hedge_opportunities(exposures, quote_list, config=config)

    # Attach liquidation risk to every delta hedge. If positions have a mark price,
    # use that; otherwise fall back to the reference table.
    mark_prices: dict[str, Decimal] = {}
    for p in position_list:
        asset = (p.base_asset or "").strip().upper()
        if asset and p.mark_price and Decimal(p.mark_price) > ZERO:
            mark_prices.setdefault(asset, Decimal(p.mark_price))

    delta_hedges_list: list[DeltaHedgeOpportunity] = []
    for opp in delta_hedges_raw:
        entry_price = mark_prices.get(opp.base_asset)
        liq_risks = compute_hedge_liquidation_risks(
            opp, entry_price=entry_price
        )
        delta_hedges_list.append(replace(opp, liquidation_risks=liq_risks))
    delta_hedges = tuple(delta_hedges_list)

    arbs = funding_arb_opportunities(
        exposures, quote_list, position_list, config=config
    )
    sensitivities = tuple(
        horizon_sensitivity(o, config=config) for o in delta_hedges if o.ranked
    )
    comparisons = tuple(avantis_comparison(o) for o in delta_hedges)
    upside = tuple(
        c
        for c in (upside_hedge_comparison(o, quote_list) for o in delta_hedges)
        if c is not None
    )

    return ScanResult(
        addresses=tuple(addresses),
        positions=tuple(position_list),
        exposures=exposures,
        self_hedge_findings=findings,
        delta_hedges=delta_hedges,
        funding_arbs=arbs,
        sensitivities=sensitivities,
        avantis_comparisons=comparisons,
        upside_comparisons=upside,
        dust_exposures=dust,
        venue_errors=tuple(venue_errors),
        config=config,
        generated_at=generated_at or datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------------------
# JSON projection
# --------------------------------------------------------------------------------------


def _d(value: Decimal | None, places: str = "0.0001") -> str | None:
    """Decimals serialise as STRINGS to avoid a float round trip. See HEDGE_LOGIC.md.

    `:f` rather than `str()` because `normalize()` renders trailing-zero integers in
    exponent form -- Decimal('720').normalize() is '7.2E+2', which reads as corrupt.
    """
    if value is None:
        return None
    return f"{Decimal(value).quantize(Decimal(places)).normalize():f}"


def _hedge_cost_dict(cost: HedgeCost) -> dict:
    return {
        "venue": cost.venue,
        "market": cost.market,
        "side": cost.side,
        "notional_usd": _d(cost.notional_usd, "0.01"),
        "horizon_hours": _d(cost.horizon_hours, "0.01"),
        "open_fee_bps": _d(cost.open_fee_bps),
        "close_fee_bps": _d(cost.close_fee_bps),
        "price_impact_bps": _d(cost.price_impact_bps),
        "slippage_bps": _d(cost.slippage_bps),
        "round_trip_fee_bps": _d(cost.round_trip_fee_bps),
        "funding_rate_8h_bps": _d(cost.funding_rate_8h_bps),
        "borrow_rate_8h_bps": _d(cost.borrow_rate_8h_bps),
        "carry_cost_bps_per_8h": _d(cost.carry_cost_bps_per_8h),
        "carry_cost_bps": _d(cost.carry_cost_bps),
        "carry_cost_usd": _d(cost.carry_cost_usd, "0.01"),
        "total_cost_bps": _d(cost.total_bps),
        "total_cost_usd": _d(cost.total_usd, "0.01"),
        "positive_carry": cost.positive_carry,
        "breakeven_hours": _d(cost.breakeven_hours, "0.01"),
        "fee_provenance": cost.fee_provenance,
        "fee_schedule_unverified": cost.fee_schedule_unverified,
        "fees_state_dependent": cost.fees_state_dependent,
        "size_mismatch": cost.size_mismatch,
        "quote_notional_usd": _d(cost.quote_notional_usd, "0.01"),
        "notes": cost.notes,
    }


def scan_result_to_dict(result: ScanResult) -> dict:
    """Structured projection for `--json`. Money and bps are strings, not floats."""
    return {
        "schema_version": 1,
        "generated_at": result.generated_at.isoformat(),
        "addresses": list(result.addresses),
        "assumptions": {
            "horizon_hours": _d(result.config.horizon_hours, "0.01"),
            "horizon_label": format_horizon(result.config.horizon_hours),
            "sensitivity_horizons_hours": [
                _d(h, "0.01") for h in result.config.horizons_hours
            ],
            "dust_threshold_usd": _d(result.config.dust_threshold_usd, "0.01"),
            "funding_convention": (
                "funding_rate_8h_bps positive = hedger receives; all *_cost_bps "
                "positive = money out; Position.current_funding_rate_8h_bps "
                "positive = position holder receives"
            ),
            "decimal_encoding": "strings, to avoid float rounding",
            "unverified_fee_venues": sorted(UNVERIFIED_FEE_VENUES),
            "avantis_funding_gate": (
                "when the user's live position funding is known for an asset, "
                "Avantis and avantis_upside rows are excluded from the ranking "
                "unless the offered funding rate strictly exceeds the user's "
                "current position funding rate for that asset (§12.9)"
            ),
        },
        "venue_errors": [
            {
                "venue": getattr(e, "venue", None),
                "kind": getattr(e, "kind", "error"),
                "address": getattr(e, "address", None),
                "message": getattr(e, "message", str(e)),
            }
            for e in result.venue_errors
        ],
        "positions": [
            {
                "venue": p.venue,
                "address": p.address,
                "market": p.market,
                "base_asset": p.base_asset,
                "side": p.side,
                "size_base": _d(p.size_base, "0.00000001"),
                "notional_usd": _d(_signed_notional(p), "0.01"),
                "entry_price": _d(p.entry_price, "0.00000001"),
                "mark_price": _d(p.mark_price, "0.00000001"),
                "liquidation_price": _d(p.liquidation_price, "0.00000001"),
                "leverage": _d(p.leverage, "0.01"),
                "collateral_usd": _d(p.collateral_usd, "0.01"),
                "unrealized_pnl_usd": _d(p.unrealized_pnl_usd, "0.01"),
                "current_funding_rate_8h_bps": _d(
                    getattr(p, "current_funding_rate_8h_bps", None)
                ),
                "margin_mode": p.margin_mode,
            }
            for p in result.positions
        ],
        "net_exposures": [
            {
                "base_asset": e.base_asset,
                "net_notional_usd": _d(e.net_notional_usd, "0.01"),
                "gross_notional_usd": _d(e.gross_notional_usd, "0.01"),
                "long_notional_usd": _d(e.long_notional_usd, "0.01"),
                "short_notional_usd": _d(e.short_notional_usd, "0.01"),
                "gross_net_gap_usd": _d(e.gross_net_gap_usd, "0.01"),
                "net_direction": e.net_direction,
                "hedge_side": e.hedge_side,
                "venues": list(e.venues),
                "position_count": e.position_count,
                "weighted_current_funding_8h_bps": _d(
                    e.weighted_current_funding_8h_bps
                ),
                "material": e.abs_net_notional_usd
                >= result.config.dust_threshold_usd,
            }
            for e in list(result.exposures) + list(result.dust_exposures)
        ],
        "self_hedge_findings": [
            {
                "base_asset": f.base_asset,
                "long_notional_usd": _d(f.long_notional_usd, "0.01"),
                "short_notional_usd": _d(f.short_notional_usd, "0.01"),
                "net_notional_usd": _d(f.net_notional_usd, "0.01"),
                "offsetting_notional_usd": _d(f.offsetting_notional_usd, "0.01"),
                "gross_net_gap_usd": _d(f.gross_net_gap_usd, "0.01"),
                "long_venues": list(f.long_venues),
                "short_venues": list(f.short_venues),
                "unwind_fee_bps": _d(f.unwind_fee_bps),
                "unwind_fee_usd": _d(f.unwind_fee_usd, "0.01"),
                "fully_offset": f.fully_offset,
                "fee_schedule_unverified": f.fee_schedule_unverified,
            }
            for f in result.self_hedge_findings
        ],
        "delta_hedges": [
            {
                "kind": o.kind,
                "base_asset": o.base_asset,
                "hedge_side": o.hedge_side,
                "hedge_notional_usd": _d(o.hedge_notional_usd, "0.01"),
                "horizon_hours": _d(o.horizon_hours, "0.01"),
                "exposure_net_notional_usd": _d(
                    o.exposure.net_notional_usd, "0.01"
                ),
                "ranked": [_hedge_cost_dict(c) for c in o.ranked],
                "excluded": [
                    {
                        "venue": x.venue,
                        "market": x.market,
                        "side": x.side,
                        "reason": x.reason,
                    }
                    for x in o.excluded
                ],
                "liquidation_risks": [
                    {
                        "venue": r.venue,
                        "side": r.side,
                        "leverage": _d(r.leverage, "0.01"),
                        "entry_price": _d(r.entry_price, "0.01"),
                        "liquidation_price": _d(r.liq_price, "0.01"),
                        "distance_pct": _d(r.distance_pct),
                        "penalty_usd": _d(r.penalty_usd, "0.01"),
                        "penalty_bps": _d(r.penalty_bps),
                        "collateral_usd": _d(r.collateral_usd, "0.01"),
                        "liquidation_fee_type": r.spec.liquidation_fee_type,
                        "cross_margin_risk": r.spec.cross_margin_risk,
                        "partial_liquidation": r.spec.partial_liquidation,
                    }
                    for r in o.liquidation_risks
                ],
            }
            for o in result.delta_hedges
        ],
        "funding_arbs": [
            {
                "kind": a.kind,
                "basis": a.basis,
                "base_asset": a.base_asset,
                "long_venue": a.long_venue,
                "short_venue": a.short_venue,
                "long_market": a.long_market,
                "short_market": a.short_market,
                "notional_usd": _d(a.notional_usd, "0.01"),
                "long_funding_8h_bps": _d(a.long_funding_8h_bps),
                "short_funding_8h_bps": _d(a.short_funding_8h_bps),
                "opposite_funding_signs": a.opposite_funding_signs,
                "net_carry_bps_per_8h": _d(a.net_carry_bps_per_8h),
                "net_carry_usd_per_8h": _d(a.net_carry_usd_per_8h, "0.01"),
                "fee_bps": _d(a.fee_bps),
                "fee_basis": a.fee_basis,
                "breakeven_hours": _d(a.breakeven_hours, "0.01"),
                "horizon_hours": _d(a.horizon_hours, "0.01"),
                "net_pnl_bps": _d(a.net_pnl_bps),
                "net_pnl_usd": _d(a.net_pnl_usd, "0.01"),
                "profitable_at_horizon": a.profitable_at_horizon,
                "fee_schedule_unverified": a.fee_schedule_unverified,
            }
            for a in result.funding_arbs
        ],
        "avantis_comparison": [
            {
                "base_asset": c.base_asset,
                "verdict": c.verdict,
                "notional_usd": _d(c.notional_usd, "0.01"),
                "horizon_hours": _d(c.horizon_hours, "0.01"),
                "avantis_rank": c.avantis_rank,
                "avantis_cost_bps": _d(c.avantis.total_bps) if c.avantis else None,
                "avantis_cost_usd": _d(c.avantis.total_usd, "0.01") if c.avantis else None,
                "best_alternative_venue": (
                    c.best_alternative.venue if c.best_alternative else None
                ),
                "best_alternative_cost_bps": (
                    _d(c.best_alternative.total_bps) if c.best_alternative else None
                ),
                "delta_bps": _d(c.delta_bps),
                "delta_usd": _d(c.delta_usd, "0.01"),
                "excluded_reason": c.excluded_reason,
            }
            for c in result.avantis_comparisons
        ],
        "upside_perp_comparison": [
            {
                "base_asset": u.base_asset,
                "horizon_hours": _d(u.horizon_hours, "0.01"),
                "notional_usd": _d(u.notional_usd, "0.01"),
                "standard_venue": u.standard_venue,
                "standard_cost_bps": _d(u.standard_cost_bps),
                "standard_cost_usd": _d(u.standard_cost_usd, "0.01"),
                "upside_fixed_cost_bps": _d(u.upside_fixed_cost_bps),
                "upside_fixed_cost_usd": _d(u.upside_fixed_cost_usd, "0.01"),
                "profit_share_fraction": _d(u.profit_share_fraction),
                "breakeven_adverse_move_bps": _d(u.breakeven_adverse_move_bps),
                "cheaper_when_hedge_unused": u.cheaper_when_hedge_unused,
                "derived_from_venue": u.derived_from_venue,
                "notes": u.notes,
            }
            for u in result.upside_comparisons
        ],
        "horizon_sensitivity": [
            {
                "base_asset": s.base_asset,
                "hedge_side": s.hedge_side,
                "notional_usd": _d(s.notional_usd, "0.01"),
                "horizons": [
                    {
                        "hours": _d(h, "0.01"),
                        "label": format_horizon(h),
                        "cheapest_venue": s.cheapest_at(h),
                        "cost_bps_by_venue": {
                            v: _d(s.grid[v][h]) for v in s.venues if h in s.grid[v]
                        },
                    }
                    for h in s.horizons_hours
                ],
                "crossovers": [
                    {
                        "at_hours": _d(c.at_hours, "0.01"),
                        "at_label": format_horizon(c.at_hours),
                        "from_venue": c.from_venue,
                        "to_venue": c.to_venue,
                        "cost_bps_at_crossover": _d(c.cost_bps_at_crossover),
                    }
                    for c in s.crossovers
                ],
                "venue_is_horizon_dependent": s.venue_is_horizon_dependent,
            }
            for s in result.sensitivities
        ],
        "liquidation_specs": {
            venue: {
                "maintenance_margin_pct": _d(spec.maintenance_margin_pct),
                "liquidation_fee_pct": _d(spec.liquidation_fee_pct),
                "liquidation_fee_type": spec.liquidation_fee_type,
                "liquidation_model": spec.liquidation_model,
                "partial_liquidation": spec.partial_liquidation,
                "cross_margin_risk": spec.cross_margin_risk,
                "maintenance_margin_source": spec.maintenance_margin_source,
                "notes": spec.notes,
                "source": spec.source,
                "as_of": spec.as_of,
            }
            for venue, spec in LIQUIDATION_SPECS.items()
        },
        "fee_schedule": {
            venue: {
                "display_name": s.display_name,
                "open_fee_bps": _d(s.open_fee_bps),
                "close_fee_bps": _d(s.close_fee_bps),
                "maker_fee_bps": _d(s.maker_fee_bps),
                "verified": s.verified,
                "fee_source": s.fee_source,
                "fees_state_dependent": s.fees_state_dependent,
                "promotional": s.promotional,
                # `live` = True marks a row whose numeric fields are a stub;
                # downstream tooling must fetch live rather than trust the
                # numbers here. See §12.3 post-fix follow-up.
                "live": s.live,
                "min_position_usd": _d(s.min_position_usd, "0.01"),
                "source": s.source,
                "as_of": s.as_of,
                "hedge_destination": s.hedge_destination,
                "position_readable": s.position_readable,
                "notes": s.notes,
            }
            for venue, s in FEE_SCHEDULE.items()
        },
    }


# --------------------------------------------------------------------------------------
# Quote construction helper (used by the CLI's fixture loader and by adapters)
# --------------------------------------------------------------------------------------


def quote_from_schedule(
    venue: str,
    base_asset: str,
    side: str,
    notional_usd: Decimal,
    *,
    funding_rate_8h_bps: Decimal | None,
    borrow_rate_8h_bps: Decimal | None = None,
    price_impact_bps: Decimal = ZERO,
    est_slippage_bps: Decimal = ZERO,
    market: str | None = None,
    extra_notes: str = "",
) -> Quote:
    """Build a `Quote` from the static fee schedule plus an EXPLICIT live carry rate.

    `funding_rate_8h_bps` has no default on purpose. If both carry inputs are
    None the quote comes back `available=False` -- a missing rate is never
    silently treated as zero (CONTRACT.md section 7).
    """
    schedule = FEE_SCHEDULE.get(venue)
    if schedule is None:
        return Quote(
            venue=venue,
            market=market or f"{base_asset}-PERP",
            side=side,
            notional_usd=Decimal(notional_usd),
            taker_fee_bps=ZERO,
            close_fee_bps=ZERO,
            price_impact_bps=ZERO,
            funding_rate_8h_bps=ZERO,
            borrow_rate_8h_bps=ZERO,
            est_slippage_bps=ZERO,
            available=False,
            notes=f"unknown venue {venue!r}: no fee schedule",
            base_asset=base_asset,
        )

    open_bps = schedule.open_fee_for(base_asset)
    close_bps = schedule.close_fee_for(base_asset)

    if funding_rate_8h_bps is None and borrow_rate_8h_bps is None:
        return Quote(
            venue=venue,
            market=market or f"{base_asset}-PERP",
            side=side,
            notional_usd=Decimal(notional_usd),
            taker_fee_bps=open_bps,
            close_fee_bps=close_bps,
            price_impact_bps=Decimal(price_impact_bps),
            funding_rate_8h_bps=ZERO,
            borrow_rate_8h_bps=ZERO,
            est_slippage_bps=Decimal(est_slippage_bps),
            available=False,
            notes=(
                f"no live funding or borrow rate for {base_asset} on {venue}"
                + (f"; {extra_notes}" if extra_notes else "")
            ),
            base_asset=base_asset,
        )

    provenance = [
        "UNVERIFIED PLACEHOLDER fee schedule"
        if not schedule.verified
        else (
            f"[fee: static fallback, no API] source: {schedule.source} ({schedule.as_of})"
            if schedule.fee_source == "static_fallback"
            else f"fees: {schedule.source} ({schedule.as_of})"
        )
    ]
    if schedule.fees_state_dependent:
        provenance.append(
            "static reference rate: this venue's fee is state-dependent and must be "
            "confirmed against a live quote"
        )
    if schedule.promotional:
        provenance.append("rate is promotional and revocable")
    if extra_notes:
        provenance.append(extra_notes)

    return Quote(
        venue=venue,
        market=market or f"{base_asset}-PERP",
        side=side,
        notional_usd=Decimal(notional_usd),
        taker_fee_bps=open_bps,
        close_fee_bps=close_bps,
        price_impact_bps=Decimal(price_impact_bps),
        funding_rate_8h_bps=Decimal(funding_rate_8h_bps or ZERO),
        borrow_rate_8h_bps=Decimal(borrow_rate_8h_bps or ZERO),
        est_slippage_bps=Decimal(est_slippage_bps),
        available=True,
        notes="; ".join(provenance),
        base_asset=base_asset,
    )
