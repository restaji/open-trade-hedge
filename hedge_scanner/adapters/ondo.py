"""Ondo Perps adapter — quotes only. Position reads are impossible for third parties.

Ondo Perps is a live product (public beta) at ondoperps.xyz: an off-chain
matching engine with on-chain custody on Ethereum, listing equity, index,
commodity and crypto perps. It is not vapourware and not testnet-only.

It is, however, a closed account system. `GET /v1/perps/positions` returns
positions "for the authenticated account" and takes **no account or address
parameter at all** — there is no request shape in which you could ask about
someone else. Auth is a JWT from Sign-In-With-Ethereum or an HMAC API key, both
of which require the account's own wallet or secret.

Verified 2026-08-19:

    GET https://api.ondoperps.xyz/v1/perps/positions      -> HTTP 401
    GET https://api.ondoperps.xyz/v1/perps/contracts      -> HTTP 200 (public)
    GET https://api.ondoperps.xyz/v1/perps/funding_rates  -> HTTP 200 (public)

Docs: https://docs.ondoperps.xyz/api-reference/positions/get-positions
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx

from ..assets import normalize_base_asset
from ..models import Position, Quote
from .base import (
    VenueRequiresAuthError,
    VenueUnavailableError,
    make_http_client,
    record_mark,
    walk_book,
)

DEFAULT_BASE_URL = "https://api.ondoperps.xyz/v1"
BOOK_DEPTH = 100

AUTH_MESSAGE = (
    "Ondo Perps exposes no third-party position read. "
    "GET /v1/perps/positions returns HTTP 401 unauthenticated and its schema "
    "accepts no account or address parameter -- it always means 'the "
    "authenticated account'. Auth is a SIWE-derived JWT or an HMAC API key, "
    "both bound to the account's own wallet or secret. A pasted address can "
    "never be resolved to an Ondo account. The user must supply their own Ondo "
    "API key for this venue."
)

BPS = Decimal(10) ** 4
# Funding is applied every hour, on the UTC hour boundary.
# https://docs.ondoperps.xyz/funding-rates
FUNDING_INTERVAL_HOURS = Decimal(1)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


class OndoAdapter:
    venue = "ondo"
    namespace = "evm"
    # Positions endpoint takes no address parameter — the API always means
    # "the authenticated account", so a third-party scanner cannot read
    # anyone's positions. `portfolio.scan(only_public=True)` uses this flag
    # to skip the adapter; public market data still works via ``get_quote``.
    public_positions = False

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("ONDO_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def __aenter__(self) -> OndoAdapter:
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
            raise VenueUnavailableError(self.venue, f"{path} returned success=false")
        return body["result"]

    async def get_positions(self, address: str) -> list[Position]:
        raise VenueRequiresAuthError(self.venue, AUTH_MESSAGE)

    async def get_marks(self) -> dict[str, Decimal]:
        """Ondo index (fallback last), keyed by base currency and market."""
        contracts = await self._get("/perps/contracts")
        out: dict[str, Decimal] = {}
        for row in contracts:
            if row.get("disabled"):
                continue
            price = _dec(row.get("indexPrice")) or _dec(row.get("lastPrice"))
            base = row.get("baseCurrency") or ""
            market = row.get("market") or ""
            if base:
                record_mark(out, base, price)
            if market:
                record_mark(out, market, price)
        return out

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = normalize_base_asset(base_asset)
        contracts = await self._get("/perps/contracts")
        contract = next(
            (
                row
                for row in contracts
                if normalize_base_asset(row.get("baseCurrency", "")) == asset
                and not row.get("disabled")
            ),
            None,
        )
        if contract is None:
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
                notes=f"Ondo Perps lists no enabled {asset} market.",
                base_asset=asset,
            )

        taker_bps = Decimal(str(contract.get("takerFee") or "0")) * BPS
        # `nextFundingRate` is the estimate for the interval about to be charged,
        # which is what a hedge opened now would actually pay or receive.
        hourly = Decimal(str(contract.get("nextFundingRate") or "0"))
        funding_8h_bps = hourly * Decimal(8) / FUNDING_INTERVAL_HOURS * BPS
        # Positive funding = longs pay shorts, so a short hedge receives it.
        hedge_sign = Decimal(-1) if side == "long" else Decimal(1)

        slippage_bps, book_note = await self._book_slippage_bps(
            contract["market"], side, notional_usd
        )

        return Quote(
            venue=self.venue,
            market=contract["market"],
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=taker_bps,
            close_fee_bps=taker_bps,
            price_impact_bps=Decimal(0),
            funding_rate_8h_bps=hedge_sign * funding_8h_bps,
            borrow_rate_8h_bps=Decimal(0),
            est_slippage_bps=slippage_bps or Decimal(0),
            available=slippage_bps is not None,
            notes=(
                "Fees are the live promotional schedule from /v1/perps/contracts "
                "and are explicitly time-limited. Funding is hourly, shown as 8h. "
                f"{book_note} Positions cannot be read without the user's API key."
            ),
            base_asset=asset,
        )

    async def _book_slippage_bps(
        self, market: str, side: str, notional_usd: Decimal
    ) -> tuple[Decimal | None, str]:
        try:
            depth = await self._get(
                "/perps/depth", {"market": market, "depth": BOOK_DEPTH}
            )
        except VenueUnavailableError as exc:
            return None, f"Orderbook unavailable: {exc.message}"

        # Levels arrive as [[price, size], ...]; a long hedge lifts asks.
        raw_levels = depth.get("asks" if side == "long" else "bids") or []
        levels = [
            (Decimal(str(price)), Decimal(str(size))) for price, size in raw_levels
        ]
        return walk_book(levels, notional_usd)

    async def health(self) -> bool:
        try:
            await self._get("/perps/contracts")
        except VenueUnavailableError:
            return False
        return True
