"""Live Avantis hedge pricing (Base L2, peer-to-pool perps).

Avantis cannot be priced with a static fee. Maker/taker is decided by OI-skew
improvement (not order type), and funding is signed per side independently.
Missing fields yield ``available=False`` rather than a fabricated zero
(CONTRACT.md §7). Units and sign conventions: ``AVANTIS_PRICING.md``.

Endpoints (fixtures in ``tests/fixtures/avantis/``):

* ``GET  /data/v2/trading`` -- commission, marginFee, fundingRate, coinOI, limits
* ``GET  /v1/price-feeds/last-price`` -- oracle price per pairIndex
* ``POST /risk/v2/spread`` -- live directional size-dependent spread (HTTP 201)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from hedge_scanner.adapters.base import make_http_client, record_mark
from hedge_scanner.assets import normalize_base_asset, pair_base_asset
from hedge_scanner.markets import canonical_base
from hedge_scanner.models import Quote

VENUE = "avantis"
UPSIDE_VENUE = "avantis_upside"

API_BASE = "https://prod-api.avantisfi.com"
FEED_BASE = "https://feed-v3.avantisfi.com"
TRADING_URL = f"{API_BASE}/data/v2/trading"
SPREAD_URL = f"{API_BASE}/risk/v2/spread"
LAST_PRICE_URL = f"{FEED_BASE}/v1/price-feeds/last-price"

# Spread engine requires a checksummed trader; zero address is the UI's anonymous fallback.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# risk-engine v2: percentages are integers at 1e10.
PRECISION_10 = Decimal(10) ** 10

# Always quote the market-order spread: a hedge is opened to be filled.
SPREAD_ORDER_TYPE_MARKET = 0

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

SNAPSHOT_TTL_S = Decimal("15")
PRICE_TTL_S = Decimal("10")
SPREAD_TTL_S = Decimal("5")

# Avantis rate fields are PERCENT; holding-cost fields are percent PER HOUR.
_PCT_TO_BPS = Decimal(100)
_HOURS_PER_8H = Decimal(8)
_HOURS_PER_YEAR = Decimal(8760)

_LONG = "long"
_SHORT = "short"

# Carry policy: include `marginFee` so Avantis 24h matches the UI
# "Net Rate (L/S) 24h" = (fundingRate + marginFee) × 24.
# CONTRACT.md §12.9 originally dropped this (on-chain funding-only); reversed
# 2026-09-02 to follow the live header on avantisfi.com.
_INCLUDE_MARGIN_FEE_IN_CARRY = True


@dataclass
class AvantisQuote(Quote):
    """Canonical ``Quote`` plus Avantis-specific fields the UI must show.

    Subclasses rather than edits ``models.py``, so ``isinstance(q, Quote)``
    holds and the ranking engine can consume it unchanged.
    """

    fee_tier: str = "n/a"  # "maker" | "taker" | "mixed" | "n/a"
    promotional_zero_fee: bool = False  # live 0 bps RWA growth-mode; revocable
    borrow_rate_annual_pct: Decimal | None = None
    funding_rate_annual_pct: Decimal | None = None
    horizon_hours: Decimal | None = None
    all_in_cost_bps: Decimal | None = None
    all_in_cost_usd: Decimal | None = None
    min_position_usd: Decimal | None = None
    max_gain_pct_of_collateral: Decimal | None = None
    profit_share_schedule: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    pair_index: int | None = None
    close_fee_base: str = "notional"


class _TTLCache:
    """Async-safe single-flight TTL cache keyed by an arbitrary hashable."""

    def __init__(self, ttl_seconds: Decimal) -> None:
        self._ttl = float(ttl_seconds)
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    async def get(self, key: Any, loader: Any) -> Any:
        hit = self._entries.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._entries.get(key)
            now = time.monotonic()
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
            value = await loader()
            self._entries[key] = (time.monotonic(), value)
            return value

    def clear(self) -> None:
        self._entries.clear()


_snapshot_cache = _TTLCache(SNAPSHOT_TTL_S)
_price_cache = _TTLCache(PRICE_TTL_S)
_spread_cache = _TTLCache(SPREAD_TTL_S)


def clear_caches() -> None:
    """Drop all cached API responses. Used by tests and by long-lived processes."""
    _snapshot_cache.clear()
    _price_cache.clear()
    _spread_cache.clear()


def _loads_exact(text: str) -> Any:
    """Parse JSON with every number as ``Decimal`` (avoids float round-trip)."""
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url)
    resp.raise_for_status()
    return _loads_exact(resp.text)


async def fetch_trading_snapshot(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """``GET /data/v2/trading``: every pair's fee, rate, OI and limit fields."""

    async def _load() -> dict[str, Any]:
        if client is not None:
            return await _get_json(client, TRADING_URL)
        async with make_http_client(timeout=HTTP_TIMEOUT) as own:
            return await _get_json(own, TRADING_URL)

    return await _snapshot_cache.get("trading", _load)


async def fetch_prices(client: httpx.AsyncClient | None = None) -> dict[int, Decimal]:
    """``GET /v1/price-feeds/last-price``: oracle price keyed by ``pairIndex``."""

    async def _load() -> dict[int, Decimal]:
        if client is not None:
            raw = await _get_json(client, LAST_PRICE_URL)
        else:
            async with make_http_client(timeout=HTTP_TIMEOUT) as own:
                raw = await _get_json(own, LAST_PRICE_URL)
        return {
            int(row["pairIndex"]): Decimal(str(row["c"]))
            for row in raw
            if row.get("c") is not None and row.get("pairIndex") is not None
        }

    return await _price_cache.get("last_price", _load)


async def get_marks(client: httpx.AsyncClient | None = None) -> dict[str, Decimal]:
    """Oracle last-price keyed by ``from``, ``from/USD``, and canonical base.

    Standard perps are indexed first so Upside records (``BTC_UPSIDE``)
    cannot overwrite the standard BTC mark. Both share the same oracle.
    """
    snapshot, by_index = await asyncio.gather(
        fetch_trading_snapshot(client),
        fetch_prices(client),
    )
    standard: list[tuple[str, str, Decimal]] = []
    upside: list[tuple[str, str, Decimal]] = []
    for key, record in (snapshot.get("pairInfos") or {}).items():
        if not isinstance(record, dict):
            continue
        try:
            idx = int(record.get("pairIndex", key))
        except (TypeError, ValueError):
            continue
        price = by_index.get(idx)
        if price is None or price <= 0:
            continue
        from_sym = str(record.get("from") or "")
        to_sym = str(record.get("to") or "USD")
        if not from_sym:
            continue
        bucket = upside if "_UPSIDE" in from_sym.upper() else standard
        bucket.append((from_sym, to_sym, price))
    out: dict[str, Decimal] = {}
    for from_sym, to_sym, price in (*standard, *upside):
        canon = pair_base_asset(from_sym, to_sym)
        record_mark(out, f"{from_sym}/{to_sym}", price)
        if canon:
            record_mark(out, canon, price)
        # Bare ``from`` only when it is the canonical book (BTC, XAU). Never
        # stamp ``EUR`` or ``USD`` — those collide across EUR/GBP and USD/JPY.
        if canon == normalize_base_asset(from_sym):
            record_mark(out, from_sym, price)
    return out


async def fetch_spread_bps(
    pair_index: int,
    coin_size: Decimal,
    is_long: bool,
    is_open: bool,
    client: httpx.AsyncClient | None = None,
) -> Decimal | None:
    """``POST /risk/v2/spread`` for this exact size and direction.

    Spread is directional and non-monotonic in size, so it is quoted per request.
    Returns bps, or ``None`` when the engine declines (403/404 must not be read
    as a zero spread).
    """
    coin_size_10 = str(int(coin_size * PRECISION_10))
    key = (pair_index, coin_size_10, is_long, is_open)

    async def _load() -> Decimal | None:
        body = {
            "pairIndex": pair_index,
            "trader": ZERO_ADDRESS,
            "coinSize10": coin_size_10,
            "isLong": is_long,
            "isOpen": is_open,
            "orderType": SPREAD_ORDER_TYPE_MARKET,
        }
        if client is not None:
            resp = await client.post(SPREAD_URL, json=body)
        else:
            async with make_http_client(timeout=HTTP_TIMEOUT) as own:
                resp = await own.post(SPREAD_URL, json=body)
        if resp.status_code not in (200, 201):
            return None
        return parse_spread_response(_loads_exact(resp.text))

    return await _spread_cache.get(key, _load)


def parse_spread_response(payload: Any) -> Decimal | None:
    """Descale a ``/risk/v2/spread`` payload to bps.

    Percent at 1e10. Prefers the with-flow estimate (what the UI shows), falls
    back to without-flow. A literal zero is the engine declining, not a free fill.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("estimatedSpreadPctWithFlow10", "spreadPctWithoutFlow10"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            continue
        if value <= 0:
            continue
        return value / PRECISION_10 * _PCT_TO_BPS
    return None


def _record_book_key(record: dict[str, Any], *, upside: bool) -> str | None:
    """Canonical book key for one Avantis pairInfos row, or None if the other book.

    Standard perps: ``BTC/USD`` → ``BTC``, ``EUR/USD`` → ``EURUSD``,
    ``USD/JPY`` → ``USDJPY``. Upside rows (``BTC_UPSIDE``) only when
    ``upside=True``.
    """
    frm = str(record.get("from") or "").strip()
    to = str(record.get("to") or "").strip()
    if not frm:
        return None
    is_upside = frm.upper().endswith("_UPSIDE")
    if upside != is_upside:
        return None
    if is_upside:
        return normalize_base_asset(frm)
    return pair_base_asset(frm, to or None)


def resolve_pair(snapshot: dict[str, Any], base_asset: str, upside: bool = False) -> dict[str, Any] | None:
    """Find the pair record for a normalized base asset, or ``None``.

    Accepts ``EUR``, ``EURUSD``, ``EUR/USD``, ``USDJPY``, ``USD/JPY``, and
    crypto tickers. Matching is on the canonical book key, not ``from == want
    and to == USD`` — that miss would drop every inverted FX pair.
    """
    want = canonical_base((base_asset or "").strip())
    if not want:
        return None
    aliased: dict[str, Any] | None = None
    for record in (snapshot.get("pairInfos") or {}).values():
        if not isinstance(record, dict):
            continue
        key = _record_book_key(record, upside=upside)
        if key is None:
            continue
        if key == want:
            return record
        if aliased is None and canonical_base(key) == want:
            aliased = record
    return aliased


@dataclass(frozen=True)
class SkewClassification:
    """OI-skew maker/taker decision for one hedge leg."""

    tier: str  # "maker" | "taker" | "mixed"
    fee_pct: Decimal  # applicable commission, PERCENT of notional
    long_share_before: Decimal | None
    long_share_after: Decimal | None


def classify_skew_fee(
    coin_oi_long: Decimal,
    coin_oi_short: Decimal,
    size_coin: Decimal,
    side: str,
    maker_fee_pct: Decimal,
    taker_fee_pct: Decimal,
    reduce_side: bool = False,
) -> SkewClassification:
    """Decide maker vs taker from coin-denominated OI before/after.

    Decimal port of ``maker_or_taker_fee_p`` in ``avantis_trader_sdk/compute/fees.py``.
    A leg that moves the long share toward 0.5 is maker; away is taker; crossing
    0.5 is a size-weighted blend. ``reduce_side=True`` prices a close (size is
    removed from that side). Empty-book and exactly-balanced both fall through
    to taker, matching the SDK. ``quote_hedge`` classifies the open against live
    ``coinOI`` and applies the same tier to the close (CONTRACT.md §12.11).
    """
    before_total = coin_oi_long + coin_oi_short
    if before_total <= 0:
        return SkewClassification("taker", taker_fee_pct, None, None)

    signed = -size_coin if reduce_side else size_coin
    if side == _LONG:
        after_long, after_short = coin_oi_long + signed, coin_oi_short
    else:
        after_long, after_short = coin_oi_long, coin_oi_short + signed
    after_long = max(after_long, Decimal(0))
    after_short = max(after_short, Decimal(0))

    after_total = after_long + after_short
    if after_total <= 0:
        return SkewClassification("taker", taker_fee_pct, None, None)

    pct_before = coin_oi_long / before_total
    pct_after = after_long / after_total
    half = Decimal("0.5")

    def _mixed(heavy: Decimal, light: Decimal) -> SkewClassification:
        blended = (
            maker_fee_pct * (heavy - light) + taker_fee_pct * (size_coin - heavy + light)
        ) / size_coin
        return SkewClassification("mixed", blended, pct_before, pct_after)

    if pct_before > half:
        if pct_after > pct_before:
            return SkewClassification("taker", taker_fee_pct, pct_before, pct_after)
        if pct_after >= half:
            return SkewClassification("maker", maker_fee_pct, pct_before, pct_after)
        return _mixed(coin_oi_long, coin_oi_short)

    if pct_before < half:
        if pct_after < pct_before:
            return SkewClassification("taker", taker_fee_pct, pct_before, pct_after)
        if pct_after <= half:
            return SkewClassification("maker", maker_fee_pct, pct_before, pct_after)
        return _mixed(coin_oi_short, coin_oi_long)

    return SkewClassification("taker", taker_fee_pct, pct_before, pct_after)


def pct_per_hour_to_8h_bps(pct_per_hour: Decimal) -> Decimal:
    """Convert an Avantis %/hour rate to the ``Quote``'s bps-per-8h basis.

    ``marginFee = 0.00022824`` × 8760 h = 1.9994 %/yr (the 2.00% protocol default).
    """
    return pct_per_hour * _HOURS_PER_8H * _PCT_TO_BPS


def pct_per_hour_to_annual_pct(pct_per_hour: Decimal) -> Decimal:
    """Convert an Avantis %/hour rate to a simple annualised percent."""
    return pct_per_hour * _HOURS_PER_YEAR


def funding_8h_bps_for_side(funding_rate: dict[str, Any], side: str) -> Decimal | None:
    """Signed funding for a hedge leg on ``side``, in bps per 8h.

    Avantis publishes ``fundingRate.long`` / ``.short`` in %/hour where a
    POSITIVE value means that side pays. CONTRACT.md §4 requires the opposite
    (positive = hedger receives), so the sign is flipped here once. Sides are
    read independently and never derived by negating the other.
    """
    raw = funding_rate.get(side)
    if raw is None:
        return None
    return -pct_per_hour_to_8h_bps(Decimal(str(raw)))


def borrow_8h_bps_for_side(margin_fee: dict[str, Any], side: str) -> Decimal | None:
    """Per-side borrow fee in bps per 8h. Always a cost, so >= 0.

    Uses ``marginFee``, not ``minLongBorrowFee`` / ``maxLongBorrowFee``: those
    do not reconcile with observed rates, so their units are unverified.
    """
    raw = margin_fee.get(side)
    if raw is None:
        return None
    return pct_per_hour_to_8h_bps(abs(Decimal(str(raw))))


def profit_share_schedule(pnl_fees: dict[str, Any]) -> list[tuple[Decimal, Decimal]]:
    """Parse ``pnlFees`` into ``[(roi_lower_bound_pct, protocol_share_pct), ...]``.

    Collapses repeated shares so ``feesP=[25,25,25,25,25,25,20,10,5,5]`` reads
    as the documented four bands.
    """
    tiers = pnl_fees.get("tierP") or []
    fees = pnl_fees.get("feesP") or []
    out: list[tuple[Decimal, Decimal]] = []
    for tier, fee in zip(tiers, fees):
        share = Decimal(str(fee))
        if out and out[-1][1] == share:
            continue
        out.append((Decimal(str(tier)), share))
    return out


def profit_share_pct_for_roi(schedule: list[tuple[Decimal, Decimal]], roi_pct: Decimal) -> Decimal | None:
    """Protocol profit share applicable at a given ROI (% of collateral)."""
    if not schedule or roi_pct <= 0:
        return None
    share = schedule[0][1]
    for lower, tier_share in schedule:
        if roi_pct >= lower:
            share = tier_share
    return share


def close_fee_usd(close_fee_bps: Decimal, notional_usd: Decimal, gross_pnl_usd: Decimal) -> Decimal:
    """Close fee in USD on ``notional + grossPnL`` (not fixed notional)."""
    return (notional_usd + gross_pnl_usd) * close_fee_bps / Decimal(10_000)


def _unavailable(
    base_asset: str,
    side: str,
    notional_usd: Decimal,
    market: str,
    reason: str,
    *,
    venue: str = VENUE,
) -> AvantisQuote:
    return AvantisQuote(
        venue=venue,
        market=market,
        side=side,
        notional_usd=notional_usd,
        taker_fee_bps=Decimal(0),
        close_fee_bps=Decimal(0),
        price_impact_bps=Decimal(0),
        funding_rate_8h_bps=Decimal(0),
        borrow_rate_8h_bps=Decimal(0),
        est_slippage_bps=Decimal(0),
        available=False,
        notes=reason,
        base_asset=base_asset.strip().upper(),
    )


def _validate_side(hedge_side: str) -> str | None:
    side = hedge_side.strip().lower()
    return side if side in (_LONG, _SHORT) else None


def _tradability_reason(record: dict[str, Any], notional_usd: Decimal, symbol: str) -> str | None:
    """Hard gates that make a hedge unexecutable. ``None`` means tradable."""
    if record.get("isPairListed") is False:
        return f"{symbol} is not listed on Avantis."
    if (record.get("additionalPairParams2") or {}).get("closeOnlyMode"):
        return f"{symbol} is in close-only mode on Avantis; no new hedge can be opened."

    minimum = record.get("minLevPosUSDC", record.get("pairMinLevPosUSDC"))
    if minimum is None:
        return f"Avantis did not return a minimum position size for {symbol}; refusing to guess."
    if notional_usd < Decimal(str(minimum)):
        return (
            f"Requested notional ${notional_usd:,.2f} is below the Avantis minimum "
            f"of {Decimal(str(minimum)):,.0f} USDC for {symbol}."
        )
    return None


def _market_hours_note(record: dict[str, Any], symbol: str) -> str:
    if (record.get("feed") or {}).get("attributes", {}).get("isOpen") is False:
        return (
            f" {symbol} market is currently CLOSED; the hedge cannot be executed until "
            "reopen, and net rate keeps accruing while it is shut."
        )
    return ""


async def quote_hedge(
    base_asset: str,
    hedge_side: str,
    notional_usd: Decimal,
    horizon_hours: Decimal,
    client: httpx.AsyncClient | None = None,
) -> Quote | None:
    """Price one Avantis fixed-fee perp hedge leg from live data.

    ``hedge_side`` is the side of the HEDGE, not the side being hedged: a user
    long BTC elsewhere needs ``hedge_side="short"`` here.

    Returns ``None`` only when Avantis does not list the asset. Every other
    refusal is ``available=False`` with a reason in ``notes``.
    """
    base = canonical_base(base_asset) or (base_asset or "").strip().upper()
    side = _validate_side(hedge_side)
    if side is None:
        return _unavailable(base, hedge_side, notional_usd, f"{base}/USD",
                            f"Invalid hedge side {hedge_side!r}; expected 'long' or 'short'.")
    if notional_usd <= 0:
        return _unavailable(base, side, notional_usd, f"{base}/USD",
                            "Notional must be positive.")

    try:
        snapshot = await fetch_trading_snapshot(client)
        prices = await fetch_prices(client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return _unavailable(base, side, notional_usd, f"{base}/USD",
                            f"Avantis API unavailable: {type(exc).__name__}: {exc}")

    record = resolve_pair(snapshot, base)
    if record is None:
        return None

    symbol = f"{record.get('from')}/{record.get('to')}"
    pair_index = int(record.get("index"))

    reason = _tradability_reason(record, notional_usd, symbol)
    if reason is not None:
        quote = _unavailable(base, side, notional_usd, symbol, reason)
        quote.pair_index = pair_index
        minimum = record.get("minLevPosUSDC", record.get("pairMinLevPosUSDC"))
        if minimum is not None:
            quote.min_position_usd = Decimal(str(minimum))
        return quote

    # Maker/taker is OI-skew, not order type (docs.avantisfi.com maker-and-taker;
    # CONTRACT.md §12.11). Adding to the heavier side is taker; joining the
    # lighter side is maker. Both legs take that tier. Read live
    # `additionalPairParams2`, not docs or legacy `openFeeP`.
    fees = record.get("additionalPairParams2") or {}
    required = ("openMakerFeeP", "openTakerFeeP", "closeMakerFeeP", "closeTakerFeeP")
    if any(fees.get(k) is None for k in required):
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return open/close maker and taker fees for {symbol}; "
                            "refusing to default a missing fee to zero.")
    open_maker = Decimal(str(fees["openMakerFeeP"]))
    open_taker = Decimal(str(fees["openTakerFeeP"]))
    close_maker = Decimal(str(fees["closeMakerFeeP"]))
    close_taker = Decimal(str(fees["closeTakerFeeP"]))

    coin_oi = record.get("coinOI") or {}
    if coin_oi.get("long") is None or coin_oi.get("short") is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return coinOI for {symbol}; "
                            "cannot classify maker vs taker without live skew.")
    long_oi = Decimal(str(coin_oi["long"]))
    short_oi = Decimal(str(coin_oi["short"]))

    price = prices.get(pair_index)
    if price is None or price <= 0:
        return _unavailable(base, side, notional_usd, symbol,
                            f"No live oracle price for {symbol}; cannot size the hedge.")
    size_coin = notional_usd / price

    # Same live book and hedge side for both legs: the close is classified as
    # another trade on that side of the book, not as an unwind against our own
    # fill. A $10k short into a long-heavy book is maker open and maker close.
    opening = classify_skew_fee(long_oi, short_oi, size_coin, side, open_maker, open_taker)
    closing = classify_skew_fee(long_oi, short_oi, size_coin, side, close_maker, close_taker)
    open_fee_bps = opening.fee_pct * _PCT_TO_BPS
    close_fee_bps = closing.fee_pct * _PCT_TO_BPS

    margin_fee = record.get("marginFee") or {}
    borrow_8h = borrow_8h_bps_for_side(margin_fee, side)
    if borrow_8h is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return marginFee.{side} for {symbol}; "
                            "refusing to treat borrow cost as zero.")

    funding_rate = record.get("fundingRate") or {}
    funding_8h = funding_8h_bps_for_side(funding_rate, side)
    if funding_8h is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return fundingRate.{side} for {symbol}; "
                            "refusing to treat funding as zero.")

    is_long = side == _LONG
    open_spread, close_spread = await asyncio.gather(
        fetch_spread_bps(pair_index, size_coin, is_long, True, client),
        fetch_spread_bps(pair_index, size_coin, is_long, False, client),
    )
    if open_spread is None or close_spread is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis spread engine would not quote {symbol} {side} at "
                            f"${notional_usd:,.0f}; treating as not executable rather than "
                            "assuming zero spread.")

    # Detected from the rates this quote actually uses, not assetType (blank
    # on 27 records). Growth-mode RWA pairs sit at 0 on all four fields.
    promotional = (open_fee_bps == 0 and close_fee_bps == 0)

    borrow_8h_effective = borrow_8h if _INCLUDE_MARGIN_FEE_IN_CARRY else Decimal(0)

    horizon = Decimal(str(horizon_hours))
    carry_periods = horizon / _HOURS_PER_8H
    all_in_bps = (
        open_fee_bps + close_fee_bps + open_spread + close_spread
        + (borrow_8h_effective - funding_8h) * carry_periods
    )

    notes: list[str] = []
    if promotional:
        notes.append(
            "PROMOTIONAL 0 bps: commission is zero under Avantis' temporary RWA "
            '"growth mode", which the team has said explicitly ends at unstated RWA '
            "OI milestones. This rate is REVOCABLE and must not be presented as "
            "durable. Spread, borrow and funding still apply."
        )
    else:
        open_field = {
            "maker": "openMakerFeeP",
            "taker": "openTakerFeeP",
            "mixed": "openMakerFeeP/openTakerFeeP blend",
        }[opening.tier]
        close_field = {
            "maker": "closeMakerFeeP",
            "taker": "closeTakerFeeP",
            "mixed": "closeMakerFeeP/closeTakerFeeP blend",
        }[closing.tier]
        note = (
            f"Open fee {open_fee_bps} bps ({open_field}), close fee {close_fee_bps} bps "
            f"({close_field}): a {open_fee_bps + close_fee_bps} bps {opening.tier} round "
            f"trip. Avantis maker/taker is OI-skew, not order type: adding to the "
            f"heavier (dominant) side is taker, joining the lighter side is maker; both "
            f"legs of this {side} hedge take that tier"
        )
        if opening.long_share_before is not None and opening.long_share_after is not None:
            note += (
                f". Live long share {opening.long_share_before} → {opening.long_share_after}"
            )
        notes.append(note + ".")
    notes.append(
        f"Close fee applies to (notional + gross PnL), not fixed notional, so a winning "
        f"hedge pays more than {close_fee_bps} bps of notional and a losing one pays less."
    )
    funding_annual = pct_per_hour_to_annual_pct(Decimal(str(funding_rate[side])))
    borrow_annual = pct_per_hour_to_annual_pct(Decimal(str(margin_fee[side])))
    notes.append(
        f"Funding {'RECEIVED' if funding_8h > 0 else 'PAID'} "
        f"{abs(funding_8h)} bps/8h ({funding_annual}%/yr "
        f"{'received' if funding_8h > 0 else 'paid'})."
    )
    if _INCLUDE_MARGIN_FEE_IN_CARRY:
        notes.append(f"Borrow {borrow_8h} bps/8h cost ({borrow_annual}%/yr).")
    else:
        notes.append(
            f"Borrow (marginFee) EXCLUDED per §12.9: API still reports "
            f"{borrow_8h} bps/8h ({borrow_annual}%/yr) but Avantis is not charging "
            f"this on-chain; carry uses funding only."
        )
    notes.append(
        f"Spread quoted live per direction and size (open {open_spread} bps, close "
        f"{close_spread} bps); it is non-monotonic in size and never interpolated."
    )
    notes.append(f"All-in {all_in_bps} bps over {horizon}h. Gas, keeper and oracle fees are genuinely zero.")
    hours_note = _market_hours_note(record, symbol)
    if hours_note:
        notes.append(hours_note.strip())

    return AvantisQuote(
        venue=VENUE,
        market=symbol,
        side=side,
        notional_usd=notional_usd,
        taker_fee_bps=open_fee_bps,
        close_fee_bps=close_fee_bps,
        price_impact_bps=open_spread,
        funding_rate_8h_bps=funding_8h,
        borrow_rate_8h_bps=borrow_8h_effective,
        est_slippage_bps=close_spread,
        available=True,
        notes=" ".join(notes),
        base_asset=base,
        fee_tier=("n/a" if promotional else opening.tier),
        promotional_zero_fee=promotional,
        borrow_rate_annual_pct=pct_per_hour_to_annual_pct(Decimal(str(margin_fee[side]))),
        funding_rate_annual_pct=-pct_per_hour_to_annual_pct(Decimal(str(funding_rate[side]))),
        horizon_hours=horizon,
        all_in_cost_bps=all_in_bps,
        all_in_cost_usd=all_in_bps * notional_usd / Decimal(10_000),
        min_position_usd=Decimal(str(record.get("minLevPosUSDC"))),
        max_gain_pct_of_collateral=(
            Decimal(str((record.get("values") or {}).get("maxGainP")))
            if (record.get("values") or {}).get("maxGainP") is not None else None
        ),
        pair_index=pair_index,
        close_fee_base="notional + grossPnL",
    )


async def quote_upside_hedge(
    base_asset: str,
    hedge_side: str,
    notional_usd: Decimal,
    client: httpx.AsyncClient | None = None,
) -> Quote | None:
    """Price an Avantis Upside Perps hedge leg.

    Upside is a different cost shape, not a cheaper rate: no borrow, a share of
    gross profit on a winning close only. There is no "expected cost in bps"
    without a price-move assumption; the schedule is in ``profit_share_schedule``.
    ``taker_fee_bps`` / ``close_fee_bps`` carry the live pair-record values.
    """
    base = canonical_base(base_asset) or (base_asset or "").strip().upper()
    side = _validate_side(hedge_side)
    if side is None:
        return _unavailable(base, hedge_side, notional_usd, f"{base}_UPSIDE/USD",
                            f"Invalid hedge side {hedge_side!r}; expected 'long' or 'short'.",
                            venue=UPSIDE_VENUE)
    if notional_usd <= 0:
        return _unavailable(base, side, notional_usd, f"{base}_UPSIDE/USD",
                            "Notional must be positive.",
                            venue=UPSIDE_VENUE)

    try:
        snapshot = await fetch_trading_snapshot(client)
        prices = await fetch_prices(client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return _unavailable(base, side, notional_usd, f"{base}_UPSIDE/USD",
                            f"Avantis API unavailable: {type(exc).__name__}: {exc}",
                            venue=UPSIDE_VENUE)

    record = resolve_pair(snapshot, base, upside=True)
    if record is None:
        return None

    symbol = f"{record.get('from')}/{record.get('to')}"
    pair_index = int(record.get("index"))

    if not (record.get("storagePairParams") or {}).get("isPnlTypeAllowed"):
        return _unavailable(base, side, notional_usd, symbol,
                            f"Upside Perps are gated off for {symbol} (isPnlTypeAllowed=0).",
                            venue=UPSIDE_VENUE)

    reason = _tradability_reason(record, notional_usd, symbol)
    if reason is not None:
        quote = _unavailable(base, side, notional_usd, symbol, reason, venue=UPSIDE_VENUE)
        quote.pair_index = pair_index
        return quote

    schedule = profit_share_schedule(record.get("pnlFees") or {})
    if not schedule:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return a pnlFees profit-share schedule for {symbol}.",
                            venue=UPSIDE_VENUE)

    margin_fee = record.get("marginFee") or {}
    borrow_8h = borrow_8h_bps_for_side(margin_fee, side)
    if borrow_8h is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return marginFee.{side} for {symbol}.",
                            venue=UPSIDE_VENUE)

    funding_rate = record.get("fundingRate") or {}
    funding_8h = funding_8h_bps_for_side(funding_rate, side)
    if funding_8h is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return fundingRate.{side} for {symbol}.",
                            venue=UPSIDE_VENUE)

    price = prices.get(pair_index)
    if price is None or price <= 0:
        return _unavailable(base, side, notional_usd, symbol,
                            f"No live oracle price for {symbol}; cannot size the hedge.",
                            venue=UPSIDE_VENUE)
    size_coin = notional_usd / price

    is_long = side == _LONG
    open_spread, close_spread = await asyncio.gather(
        fetch_spread_bps(pair_index, size_coin, is_long, True, client),
        fetch_spread_bps(pair_index, size_coin, is_long, False, client),
    )
    if open_spread is None or close_spread is None:
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis spread engine would not quote {symbol} {side} at "
                            f"${notional_usd:,.0f}.",
                            venue=UPSIDE_VENUE)

    fees = record.get("additionalPairParams2") or {}
    required = ("openTakerFeeP", "closeTakerFeeP")
    if any(fees.get(k) is None for k in required):
        return _unavailable(base, side, notional_usd, symbol,
                            f"Avantis did not return openTakerFeeP/closeTakerFeeP for {symbol}.",
                            venue=UPSIDE_VENUE)
    open_taker = Decimal(str(fees["openTakerFeeP"]))
    close_taker = Decimal(str(fees["closeTakerFeeP"]))

    open_fee_bps = open_taker * _PCT_TO_BPS
    close_fee_bps = close_taker * _PCT_TO_BPS

    bands = ", ".join(
        f"ROI >={lower}%: {share}% of gross profit" for lower, share in schedule
    )
    notes = [
        "UPSIDE PERPS: cost is PnL-CONTINGENT, not bps of notional, and is NOT "
        "comparable to the other venues' fee fields without an assumed price move.",
        f"Profit share (live pnlFees): {bands}. Zero cost if the hedge closes at a loss.",
        f"Borrow fee is genuinely zero here (live marginFee.{side} = "
        f"{Decimal(str(margin_fee[side]))}); funding and spread are the only "
        "unconditional holding costs.",
        f"Funding {'RECEIVED' if funding_8h > 0 else 'PAID'} {abs(funding_8h)} bps/8h.",
        f"Spread quoted live: open {open_spread} bps, close {close_spread} bps.",
        f"Open fee {open_fee_bps} bps (openTakerFeeP), close fee {close_fee_bps} bps (closeTakerFeeP).",
        "As a hedge this is cheaper when the hedge turns out unnecessary and more "
        "expensive when it works: it surrenders 5-25% of the gross profit that "
        "offsets the user's loss elsewhere.",
        f"Max gain capped at {(record.get('values') or {}).get('maxGainP')}% of collateral. "
        "Market orders only -- no limit or TWAP on Upside.",
    ]
    hours_note = _market_hours_note(record, symbol)
    if hours_note:
        notes.append(hours_note.strip())

    borrow_8h_effective = borrow_8h if _INCLUDE_MARGIN_FEE_IN_CARRY else Decimal(0)

    return AvantisQuote(
        venue=UPSIDE_VENUE,
        market=symbol,
        side=side,
        notional_usd=notional_usd,
        taker_fee_bps=open_fee_bps,
        close_fee_bps=close_fee_bps,
        price_impact_bps=open_spread,
        funding_rate_8h_bps=funding_8h,
        borrow_rate_8h_bps=borrow_8h_effective,
        est_slippage_bps=close_spread,
        available=True,
        notes=" ".join(notes),
        base_asset=base,
        fee_tier="taker",
        promotional_zero_fee=False,
        borrow_rate_annual_pct=pct_per_hour_to_annual_pct(Decimal(str(margin_fee[side]))),
        funding_rate_annual_pct=-pct_per_hour_to_annual_pct(Decimal(str(funding_rate[side]))),
        profit_share_schedule=schedule,
        min_position_usd=Decimal(str(record.get("minLevPosUSDC"))),
        max_gain_pct_of_collateral=(
            Decimal(str((record.get("values") or {}).get("maxGainP")))
            if (record.get("values") or {}).get("maxGainP") is not None else None
        ),
        pair_index=pair_index,
        close_fee_base="gross profit (profit share), not notional",
    )
