"""Pacifica (Solana perp DEX) adapter.

Pacifica exposes `GET /api/v1/positions?account=<solana pubkey>` with no
authentication of any kind, so third-party reads work for arbitrary addresses.
Verified against mainnet on 2026-08-19: HTTP 200 with real position data for
accounts we do not control, and HTTP 400 "Wrong address size" for a malformed
address (i.e. the parameter is genuinely a lookup key, not an identity check).

Docs: https://docs.pacifica.fi/api-documentation/api/rest-api
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ..assets import normalize_base_asset
from ..models import Position, Quote
from .base import VenueUnavailableError, make_http_client, walk_book

DEFAULT_BASE_URL = "https://api.pacifica.fi/api/v1"

# Pacifica settles perps in USDC.
QUOTE_ASSET = "USDC"

# Funding is published as an hourly rate; the docs' formula divides the 8-hour
# figure by 8 before publishing it. https://docs.pacifica.fi/trading-on-pacifica/funding-rates
FUNDING_INTERVAL_HOURS = Decimal(1)
BPS = Decimal(10) ** 4


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


class PacificaAdapter:
    venue = "pacifica"
    namespace = "solana"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("PACIFICA_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def __aenter__(self) -> PacificaAdapter:
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

    async def _get(self, path: str, params: dict | None = None) -> object:
        try:
            resp = await self._http().get(f"{self._base_url}{path}", params=params)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(self.venue, f"{path} failed: {exc}") from exc
        if not body.get("success"):
            raise VenueUnavailableError(
                self.venue, f"{path} returned success=false: {body.get('error')}"
            )
        return body["data"]

    async def get_positions(self, address: str) -> list[Position]:
        rows = await self._get("/positions", {"account": address})
        if not rows:
            return []

        prices = await self._prices_by_symbol()
        positions = []
        for row in rows:
            position = self._to_position(address, row, prices)
            if position is not None:
                positions.append(position)
        return positions

    async def _prices_by_symbol(self) -> dict[str, dict]:
        rows = await self._get("/info/prices")
        return {row["symbol"]: row for row in rows}

    def _to_position(
        self, address: str, row: dict, prices: dict[str, dict]
    ) -> Position | None:
        symbol = row["symbol"]
        size_base = _dec(row.get("amount")) or Decimal(0)
        if size_base == 0:
            return None

        price_row = prices.get(symbol) or {}
        mark_price = _dec(price_row.get("mark")) or _dec(price_row.get("oracle"))
        if mark_price is None:
            raise VenueUnavailableError(
                self.venue, f"no mark price published for symbol {symbol}"
            )

        side = "long" if row.get("side") == "bid" else "short"
        sign = Decimal(1) if side == "long" else Decimal(-1)
        entry_price = _dec(row.get("entry_price")) or Decimal(0)
        margin = _dec(row.get("margin"))
        isolated = bool(row.get("isolated"))

        # CONTRACT.md §12.9. Current live funding rate for this position,
        # signed from the POSITION HOLDER'S perspective (positive = holder
        # receives). Pacifica publishes hourly funding where positive means
        # longs pay shorts, so a long holder pays when the rate is positive
        # and a short holder receives -- flip the sign against side.
        hourly_funding = _dec(price_row.get("funding"))
        if hourly_funding is not None:
            holder_sign = Decimal(-1) if side == "long" else Decimal(1)
            current_funding_8h_bps = (
                holder_sign * hourly_funding * Decimal(8) / FUNDING_INTERVAL_HOURS * BPS
            )
        else:
            current_funding_8h_bps = None

        # Pacifica returns a negative "liquidation price" for cross positions
        # that the rest of the account collateralizes away. A negative price is
        # not a price, so surface it as absent and keep the raw value in `raw`.
        liquidation_price = _dec(row.get("liquidation_price"))
        if liquidation_price is not None and liquidation_price <= 0:
            liquidation_price = None

        notional_usd = sign * size_base * mark_price
        unrealized = sign * (mark_price - entry_price) * size_base

        leverage = None
        collateral_usd = None
        if isolated and margin and margin > 0:
            collateral_usd = margin
            leverage = abs(notional_usd) / margin

        opened_at = None
        if row.get("created_at"):
            opened_at = datetime.fromtimestamp(row["created_at"] / 1000, tz=UTC)

        return Position(
            venue=self.venue,
            address=address,
            market=symbol,
            base_asset=normalize_base_asset(symbol),
            quote_asset=QUOTE_ASSET,
            side=side,
            size_base=size_base,
            notional_usd=notional_usd,
            entry_price=entry_price,
            mark_price=mark_price,
            liquidation_price=liquidation_price,
            leverage=leverage,
            collateral_usd=collateral_usd,
            unrealized_pnl_usd=unrealized,
            # Docs: "Funding paid by this position since open". Positive = paid.
            funding_paid_usd=_dec(row.get("funding")),
            current_funding_rate_8h_bps=current_funding_8h_bps,
            margin_mode="isolated" if isolated else "cross",
            opened_at=opened_at,
            raw=row,
        )

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = normalize_base_asset(base_asset)
        prices = await self._prices_by_symbol()
        symbol = next(
            (s for s in prices if normalize_base_asset(s) == asset),
            None,
        )
        if symbol is None:
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
                notes=f"Pacifica does not list a {asset} perp.",
                base_asset=asset,
            )

        fee_levels = await self._get("/info/fees")
        # Level 0 is the rate a brand-new account pays. Quoting a better tier
        # would require knowing the user's 14-day volume, which we do not.
        base_tier = min(fee_levels, key=lambda row: row["level"])
        taker_bps = (_dec(base_tier["taker_fee_rate"]) or Decimal(0)) * BPS

        hourly_funding = _dec(prices[symbol].get("funding")) or Decimal(0)
        funding_8h_bps = hourly_funding * Decimal(8) / FUNDING_INTERVAL_HOURS * BPS
        # Positive published funding = longs pay shorts, so a short hedge
        # receives it and a long hedge pays it.
        hedge_sign = Decimal(-1) if side == "long" else Decimal(1)

        slippage_bps, book_note = await self._book_slippage_bps(
            symbol, side, notional_usd
        )

        return Quote(
            venue=self.venue,
            market=symbol,
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=taker_bps,
            close_fee_bps=taker_bps,
            # Orderbook venue: cost of size shows up as book walk, not as a
            # separate protocol impact fee.
            price_impact_bps=Decimal(0),
            funding_rate_8h_bps=hedge_sign * funding_8h_bps,
            borrow_rate_8h_bps=Decimal(0),
            est_slippage_bps=slippage_bps or Decimal(0),
            available=slippage_bps is not None,
            notes=(
                f"Fee tier 0 (taker {taker_bps:.2f} bps); funding published hourly, "
                f"shown as 8h. {book_note}"
            ),
            base_asset=asset,
        )

    async def _book_slippage_bps(
        self, symbol: str, side: str, notional_usd: Decimal
    ) -> tuple[Decimal | None, str]:
        """Walk the live book to estimate slippage versus the touch price."""
        try:
            book = await self._get("/book", {"symbol": symbol})
        except VenueUnavailableError as exc:
            return None, f"Orderbook unavailable: {exc.message}"

        # `l` is [bids, asks]; each level is {p: price, a: amount, n: orders}.
        # A long hedge lifts asks, a short hedge hits bids.
        sides = book.get("l") or []
        if len(sides) < 2:
            return None, "Orderbook response had no bid/ask sides."
        raw_levels = sides[1] if side == "long" else sides[0]

        levels = [
            (_dec(level.get("p")) or Decimal(0), _dec(level.get("a")) or Decimal(0))
            for level in raw_levels
        ]
        return walk_book(levels, notional_usd)

    async def health(self) -> bool:
        try:
            await self._get("/info/prices")
        except VenueUnavailableError:
            return False
        return True
