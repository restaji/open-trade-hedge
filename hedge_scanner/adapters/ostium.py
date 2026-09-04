"""Ostium (Arbitrum perps DEX, oracle-priced) adapter.

Ostium exposes positions and market data via a GraphQL subgraph at the Builder
API. Positions are public and readable for ANY Arbitrum address without auth.

Fee model (verified 2026-08-29 from docs.ostium.com/traders/reference/fees):
  - Opening: 3–10 bps of notional (varies by asset class / pair)
  - Closing: 0 bps
  - Oracle: $0.10 USDC flat per price request (negligible on large notionals)
  - Rollover: continuous accrual per block, derived from real-world carry costs
  - Early-close: 0–40 bps decaying linearly within first 15s, capped at profit
  - Liquidation: remaining collateral forfeited

Overnight financing (rollover) formula (verified against app.ostium.com UI):
  Long  per-block rate = (lastRolloverLongPure + brokerPremium) / 1e18
  Short per-block rate = (-lastRolloverLongPure + brokerPremium) / 1e18
  Both fields are per-block rates stored in 1e18 precision.
  Arbitrum block time = 0.25s → 345,600 blocks/day, 115,200 blocks/8h.
  If isNegativeRolloverAllowed is false, rate is floored at 0 (user never receives).
  Funding fees are deprecated; all pairs use rollover only.

Subgraph precision (verified by introspection 2026-08-29):
  - Prices (openPrice, etc.): 1e18 precision (divide by 1e18 to get USD)
  - Collateral: USDC with 6 decimals (divide by 1e6)
  - Leverage: 1e2 precision (divide by 100, e.g. 7500 = 75x)
  - takerFeeP / makerFeeP: 1e10 precision (60000 = 6 bps)
  - lastRolloverLongPure / brokerPremium: per-block rate in 1e18

Subgraph: https://builder.prod.bedrock.ostium.io/v1/subgraph/gn
Docs: https://docs.ostium.com/developer/sdk/overview
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx

from ..assets import pair_base_asset
from ..markets import canonical_base
from ..models import Position, Quote
from .base import VenueUnavailableError, make_http_client, record_mark

SUBGRAPH_URL = "https://builder.prod.bedrock.ostium.io/v1/subgraph/gn"

QUOTE_ASSET = "USDC"
BPS = Decimal(10_000)

# Precision constants from the Ostium subgraph
PRICE_PRECISION = Decimal("1e18")
COLLATERAL_PRECISION = Decimal("1e6")   # USDC 6 decimals
LEVERAGE_PRECISION = Decimal("100")     # 7500 = 75x
FEE_PRECISION = Decimal("1e10")         # 60000 = 0.0006% = 6 bps
RATE_PRECISION = Decimal("1e18")        # per-block rate precision

# Arbitrum block time 0.25s
BLOCKS_PER_8H = Decimal("115200")       # 8 * 3600 / 0.25

DEFAULT_OPEN_FEE_BPS_FALLBACK = Decimal("6")
CLOSE_FEE_BPS = Decimal(0)

# ---------------------------------------------------------------------------
# GraphQL queries (field names verified by __type introspection 2026-08-29)
# ---------------------------------------------------------------------------

_OPEN_TRADES_QUERY = """
query OpenTrades($trader: Bytes!) {
  trades(
    where: { trader: $trader, isOpen: true }
    first: 1000
    orderBy: timestamp
    orderDirection: desc
  ) {
    id
    pair { id from to }
    trader
    index
    isBuy
    openPrice
    leverage
    collateral
    rollover
    funding
    timestamp
  }
}
"""

_PAIRS_QUERY = """
query Pairs {
  pairs(first: 200) {
    id
    from
    to
    takerFeeP
    makerFeeP
    maxLeverage
    spreadP
    lastRolloverLongPure
    brokerPremium
    isNegativeRolloverAllowed
    accRolloverLong
    accRolloverShort
    accFundingLong
    accFundingShort
    longOI
    shortOI
    group { id name maxLeverage }
    lastTradePrice
    fee { minLevPos oracleFee liqFeeP }
  }
}
"""


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _rollover_8h_bps(pair: dict, side: str) -> Decimal:
    """Compute the overnight financing rate in bps per 8h for a given side.

    Formula: long = (lastRolloverLongPure + brokerPremium) / 1e18 per block
             short = (-lastRolloverLongPure + brokerPremium) / 1e18 per block
    Convert to 8h bps: per_block * 115200 * 10000
    """
    pure = _dec(pair.get("lastRolloverLongPure")) or Decimal(0)
    premium = _dec(pair.get("brokerPremium")) or Decimal(0)
    neg_allowed = pair.get("isNegativeRolloverAllowed", False)

    if side == "long":
        per_block = (pure + premium) / RATE_PRECISION
    else:
        per_block = (-pure + premium) / RATE_PRECISION

    if not neg_allowed and per_block < 0:
        per_block = Decimal(0)

    return per_block * BLOCKS_PER_8H * BPS


class OstiumAdapter:
    venue = "ostium"
    namespace = "evm"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        subgraph_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._subgraph_url = (
            subgraph_url
            or os.environ.get("OSTIUM_SUBGRAPH_URL", SUBGRAPH_URL)
        )
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._pairs_cache: dict[str, dict] | None = None

    async def __aenter__(self) -> OstiumAdapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = make_http_client(timeout=self._timeout)
        return self._client

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            resp = await self._http().post(self._subgraph_url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(
                self.venue, f"subgraph query failed: {exc}"
            ) from exc
        if "errors" in body:
            raise VenueUnavailableError(
                self.venue, f"subgraph errors: {body['errors']}"
            )
        return body.get("data", {})

    # ------------------------------------------------------------------
    # Pairs cache
    # ------------------------------------------------------------------

    async def _get_pairs(self) -> dict[str, dict]:
        if self._pairs_cache is not None:
            return self._pairs_cache
        data = await self._gql(_PAIRS_QUERY)
        pairs_list = data.get("pairs", [])
        cache: dict[str, dict] = {}
        for pair in pairs_list:
            pair_id = pair.get("id", "")
            cache[pair_id] = pair
        self._pairs_cache = cache
        return cache

    def _pair_open_fee_bps(self, pair: dict) -> Decimal:
        """Extract the opening fee in bps from a pair record.

        takerFeeP uses 1e10 precision: 60000 / 1e10 = 0.000006 = 0.0006% = 6 bps.
        """
        raw = _dec(pair.get("takerFeeP"))
        if raw is not None and raw > 0:
            return raw / FEE_PRECISION * Decimal("1000000")  # to bps: raw/1e10 * 1e6 = raw/1e4
        return DEFAULT_OPEN_FEE_BPS_FALLBACK

    # ------------------------------------------------------------------
    # Positions (with accrued rollover computation)
    # ------------------------------------------------------------------

    async def get_positions(self, address: str) -> list[Position]:
        data = await self._gql(_OPEN_TRADES_QUERY, {"trader": address.lower()})
        trades = data.get("trades", [])
        if not trades:
            return []

        pairs = await self._get_pairs()

        positions: list[Position] = []
        for trade in trades:
            pos = self._to_position(address, trade, pairs)
            if pos is not None:
                positions.append(pos)
        return positions

    def _to_position(
        self, address: str, trade: dict, pairs: dict[str, dict]
    ) -> Position | None:
        pair = trade.get("pair", {})
        pair_id = pair.get("id", "")
        from_sym = pair.get("from", "")
        to_sym = pair.get("to", "")
        if not from_sym:
            return None

        market = f"{from_sym}/{to_sym}" if to_sym else from_sym

        is_long = trade.get("isBuy", True)
        side = "long" if is_long else "short"

        raw_collateral = _dec(trade.get("collateral"))
        raw_leverage = _dec(trade.get("leverage"))
        raw_open_price = _dec(trade.get("openPrice"))

        if raw_collateral is None or raw_leverage is None or raw_open_price is None:
            return None
        if raw_collateral <= 0 or raw_open_price <= 0:
            return None

        collateral = raw_collateral / COLLATERAL_PRECISION
        leverage = raw_leverage / LEVERAGE_PRECISION
        open_price = raw_open_price / PRICE_PRECISION

        # Use live mark price from pair's lastTradePrice; fall back to entry
        pair_data = pairs.get(pair_id, {})
        raw_mark = _dec(pair_data.get("lastTradePrice"))
        mark_price = raw_mark / PRICE_PRECISION if raw_mark and raw_mark > 0 else open_price

        size_base = collateral * leverage / open_price if open_price > 0 else Decimal(0)
        notional_usd = size_base * mark_price

        # Compute accrued rollover fees from accumulator delta
        funding_paid_usd = self._compute_accrued_rollover(
            trade, pairs.get(pair_id, {}), is_long, collateral, leverage
        )

        # Liquidation price (incorporates accrued fees shifting liq closer)
        liq_price = self._compute_liq_price(
            pairs.get(pair_id, {}), open_price, leverage, is_long,
            funding_paid_usd, collateral,
        )

        # Unrealized PnL = size_base × (mark − entry) for longs, flipped for shorts
        if is_long:
            unrealized_pnl = size_base * (mark_price - open_price)
        else:
            unrealized_pnl = size_base * (open_price - mark_price)

        return Position(
            venue=self.venue,
            address=address,
            market=market,
            base_asset=canonical_base(
                f"{from_sym}/{to_sym}" if to_sym else from_sym
            ),
            quote_asset=QUOTE_ASSET,
            side=side,
            size_base=size_base,
            notional_usd=notional_usd,
            entry_price=open_price,
            mark_price=mark_price,
            liquidation_price=liq_price,
            leverage=leverage,
            collateral_usd=collateral,
            unrealized_pnl_usd=unrealized_pnl,
            funding_paid_usd=funding_paid_usd,
            margin_mode="isolated",
            opened_at=trade.get("timestamp"),
            raw=trade,
        )

    @staticmethod
    def _compute_liq_price(
        pair_data: dict,
        entry_price: Decimal,
        leverage: Decimal,
        is_long: bool,
        accrued_fees_usd: Decimal | None,
        collateral_usd: Decimal,
    ) -> Decimal | None:
        """Compute the liquidation price using Ostium's threshold formula,
        adjusted for accrued rollover fees.

        Base formula:
          Threshold = 100% − (leverage / maxLevPair × 25%)
          Long  liq = entry × (1 − threshold / leverage)
          Short liq = entry × (1 + threshold / leverage)

        Fee adjustment shifts liq closer to entry:
          shift = abs(fees) / collateral × entry / leverage
        """
        if not pair_data or leverage <= 0:
            return None

        raw_max = _dec(pair_data.get("maxLeverage")) or Decimal(0)
        max_lev = raw_max / LEVERAGE_PRECISION
        if max_lev <= 0:
            group = pair_data.get("group", {})
            raw_group_max = _dec(group.get("maxLeverage")) or Decimal(0)
            max_lev = raw_group_max / LEVERAGE_PRECISION

        if max_lev <= 0:
            return None

        threshold = Decimal(1) - (leverage / max_lev * Decimal("0.25"))
        price_move = threshold / leverage

        if is_long:
            base_liq = entry_price * (1 - price_move)
        else:
            base_liq = entry_price * (1 + price_move)

        if accrued_fees_usd is not None and collateral_usd > 0:
            fee_shift = abs(accrued_fees_usd) / collateral_usd * entry_price / leverage
            if is_long:
                return base_liq + fee_shift
            else:
                return base_liq - fee_shift

        return base_liq

    @staticmethod
    def _compute_accrued_rollover(
        trade: dict,
        pair_data: dict,
        is_long: bool,
        collateral_usd: Decimal,
        leverage: Decimal,
    ) -> Decimal | None:
        """Compute accrued rollover in USD from accumulator deltas.

        The trade stores the accumulator value at open; the pair stores the
        current accumulator. Rollover applies to the full notional
        (collateral × leverage), not just collateral.
        Fee = delta × collateral × leverage / RATE_PRECISION
        """
        trade_rollover = _dec(trade.get("rollover"))
        if trade_rollover is None or not pair_data:
            return None

        if is_long:
            current_acc = _dec(pair_data.get("accRolloverLong"))
        else:
            current_acc = _dec(pair_data.get("accRolloverShort"))

        if current_acc is None:
            return None

        delta = current_acc - trade_rollover
        accrued_usd = delta * collateral_usd * leverage / RATE_PRECISION

        # Negative = user paid out
        return -abs(accrued_usd)

    # ------------------------------------------------------------------
    # Marks
    # ------------------------------------------------------------------

    async def get_marks(self) -> dict[str, Decimal]:
        """Ostium last-trade marks, keyed by ``from`` and ``from/to``."""
        pairs = await self._get_pairs()
        out: dict[str, Decimal] = {}
        for pair in pairs.values():
            from_sym = pair.get("from") or ""
            to_sym = pair.get("to") or ""
            raw_mark = _dec(pair.get("lastTradePrice"))
            mark = (
                raw_mark / PRICE_PRECISION
                if raw_mark is not None and raw_mark > 0
                else None
            )
            canon = canonical_base(f"{from_sym}/{to_sym}" if to_sym else from_sym)
            if canon:
                record_mark(out, canon, mark)
            if from_sym and to_sym:
                record_mark(out, f"{from_sym}/{to_sym}", mark)
            elif from_sym:
                record_mark(out, from_sym, mark)
        return out

    # ------------------------------------------------------------------
    # Quotes (with live overnight financing rate)
    # ------------------------------------------------------------------

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = canonical_base(base_asset)
        pairs = await self._get_pairs()

        target_pair = self._resolve_pair(asset, pairs)
        if target_pair is None:
            return Quote(
                venue=self.venue,
                market=asset,
                side=side,
                notional_usd=notional_usd,
                taker_fee_bps=Decimal(0),
                close_fee_bps=Decimal(0),
                price_impact_bps=Decimal(0),
                funding_rate_8h_bps=Decimal(0),
                borrow_rate_8h_bps=Decimal(0),
                est_slippage_bps=Decimal(0),
                available=False,
                notes=f"Ostium does not list a {asset} perp.",
                base_asset=asset,
            )

        open_fee_bps = self._pair_open_fee_bps(target_pair)
        market_name = f"{target_pair.get('from', asset)}/{target_pair.get('to', 'USD')}"

        # Live overnight financing: rollover is Ostium's equivalent of borrow cost
        rollover_bps = _rollover_8h_bps(target_pair, side)

        pure = _dec(target_pair.get("lastRolloverLongPure")) or Decimal(0)
        premium = _dec(target_pair.get("brokerPremium")) or Decimal(0)
        pure_ann = float(pure / RATE_PRECISION * Decimal("345600") * 365 * 100)
        premium_ann = float(premium / RATE_PRECISION * Decimal("345600") * 365 * 100)
        rollover_24h_pct = float(rollover_bps * 3 / BPS * 100)  # 8h→24h, bps→%

        return Quote(
            venue=self.venue,
            market=market_name,
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=open_fee_bps,
            close_fee_bps=CLOSE_FEE_BPS,
            price_impact_bps=Decimal(0),
            funding_rate_8h_bps=Decimal(0),
            borrow_rate_8h_bps=rollover_bps,
            est_slippage_bps=Decimal(0),
            available=True,
            notes=(
                f"Open fee {open_fee_bps} bps (live takerFeeP), close fee 0 bps. "
                f"Overnight financing ({side}): {rollover_24h_pct:+.4f}%/24h "
                f"(carry={pure_ann:+.2f}% + premium={premium_ann:.2f}% ann). "
                "Oracle fee $0.10 flat. Oracle-priced, no orderbook slippage."
            ),
            base_asset=asset,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        try:
            data = await self._gql("{ pairs(first: 1) { id } }")
        except VenueUnavailableError:
            return False
        return bool(data.get("pairs"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_pair(self, asset: str, pairs: dict[str, dict]) -> dict | None:
        want = canonical_base(asset)
        if not want:
            return None
        aliased: dict | None = None
        for pair in pairs.values():
            key = pair_base_asset(pair.get("from") or "", pair.get("to") or "")
            if key == want:
                return pair
            if aliased is None and canonical_base(key) == want:
                aliased = pair
        return aliased
