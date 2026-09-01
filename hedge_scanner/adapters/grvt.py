"""GRVT adapter — quotes only. Position reads are impossible for third parties.

GRVT is a ZK validium with a CEX-shaped API. Every account endpoint sits behind
a `gravity=` session cookie obtained by exchanging an API key or an EIP-712
wallet signature, and there is no public read path keyed by address.

Verified 2026-08-19:

    POST https://trades.grvt.io/full/v1/positions
      -> HTTP 401 {"code":1000,"message":"You need to authenticate prior to
                    using this functionality","status":401}

Market data is a separate, fully public host, so `get_quote` works: funding,
mark price and instrument metadata all resolve without credentials.

Docs: https://api-docs.grvt.io/  |  https://api-docs.grvt.io/market_data_api/
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx

from ..assets import normalize_base_asset
from ..models import Position, Quote
from .base import VenueRequiresAuthError, VenueUnavailableError, make_http_client, walk_book

DEFAULT_MARKET_DATA_URL = "https://market-data.grvt.io/full/v1"
DEFAULT_TRADING_URL = "https://trades.grvt.io/full/v1"

AUTH_MESSAGE = (
    "GRVT requires an account-bound session to read positions. "
    "POST /full/v1/positions returns HTTP 401 (code 1000) without a `gravity=` "
    "session cookie, and that cookie is only issued in exchange for the "
    "account's own API key or an EIP-712 signature from the account's wallet. "
    "There is no public endpoint that maps an address to its positions, so a "
    "pasted address can never be read. The user must supply their own GRVT API "
    "key for this venue."
)

# Fee schedule: GRVT does NOT expose fees on any public API endpoint.
# The /all_instruments endpoint returns instrument metadata but no fee fields.
# The only authoritative source is the help center article (a web page, not an API).
# TODO: If GRVT ships a fee endpoint, fetch live here instead.
#
# Source: help.grvt.io/en/articles/9614699 via ../grvt-fees.md
# Date: 2026-08-19 (fee tier table "Active after March 23, 2026, 4:00 AM UTC")
# Level 1 (base tier) perp rates; better tiers require 30d volume we cannot see.
DEFAULT_PERP_TAKER_FEE_BPS = Decimal("4.5")  # 0.0450%
DEFAULT_PERP_MAKER_FEE_BPS = Decimal("-0.01")  # -0.0001%, a rebate at every tier

_FEE_SOURCE = "help.grvt.io/en/articles/9614699"
_FEE_AS_OF = "2026-08-19"

PERCENT_TO_BPS = Decimal(100)
BOOK_DEPTH = 100


class GrvtAdapter:
    venue = "grvt"
    namespace = "evm"
    # Positions endpoint is auth-gated (see AUTH_MESSAGE); a paste-an-address
    # scanner has no way in. `portfolio.scan(only_public=True)` uses this flag
    # to skip the adapter entirely rather than fire a call that always 401s.
    # Public market data still works via ``get_quote``.
    public_positions = False

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        market_data_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._market_data_url = (
            market_data_url
            or os.environ.get("GRVT_MARKET_DATA_URL", DEFAULT_MARKET_DATA_URL)
        ).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def __aenter__(self) -> GrvtAdapter:
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

    async def _post(self, path: str, payload: dict) -> object:
        try:
            resp = await self._http().post(
                f"{self._market_data_url}{path}", json=payload
            )
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(self.venue, f"{path} failed: {exc}") from exc
        if "result" not in body:
            raise VenueUnavailableError(
                self.venue, f"{path} returned no result: {body}"
            )
        return body["result"]

    async def get_positions(self, address: str) -> list[Position]:
        raise VenueRequiresAuthError(self.venue, AUTH_MESSAGE)

    async def _instrument_for(self, asset: str) -> str | None:
        instruments = await self._post("/all_instruments", {"is_active": True})
        candidates = [
            row["instrument"]
            for row in instruments
            if row.get("kind") == "PERPETUAL"
            and normalize_base_asset(row.get("base", "")) == asset
        ]
        if not candidates:
            return None
        # Prefer the USDT-quoted book, which is GRVT's primary perp quote unit.
        usdt = [i for i in candidates if i.endswith("_USDT_Perp")]
        return (usdt or candidates)[0]

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = normalize_base_asset(base_asset)
        instrument = await self._instrument_for(asset)
        if instrument is None:
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
                notes=f"GRVT lists no active perpetual for {asset}.",
                base_asset=asset,
            )

        ticker = await self._post("/ticker", {"instrument": instrument})
        # GRVT publishes funding rates in percent per the instrument's interval;
        # `funding_rate_8h_curr` is already normalized to 8 hours.
        funding_pct = Decimal(str(ticker.get("funding_rate_8h_curr") or "0"))
        funding_8h_bps = funding_pct * PERCENT_TO_BPS
        # Positive funding = longs pay shorts, so a short hedge receives it.
        hedge_sign = Decimal(-1) if side == "long" else Decimal(1)

        slippage_bps, book_note = await self._book_slippage_bps(
            instrument, side, notional_usd
        )

        return Quote(
            venue=self.venue,
            market=instrument,
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=DEFAULT_PERP_TAKER_FEE_BPS,
            close_fee_bps=DEFAULT_PERP_TAKER_FEE_BPS,
            price_impact_bps=Decimal(0),
            funding_rate_8h_bps=hedge_sign * funding_8h_bps,
            borrow_rate_8h_bps=Decimal(0),
            est_slippage_bps=slippage_bps or Decimal(0),
            available=slippage_bps is not None,
            notes=(
                f"[fee: static fallback, no API] Level 1 taker ({DEFAULT_PERP_TAKER_FEE_BPS} bps); "
                f"source: {_FEE_SOURCE}, as of {_FEE_AS_OF}. "
                f"Maker fills earn a {DEFAULT_PERP_MAKER_FEE_BPS} bps rebate instead. "
                f"{book_note} Positions cannot be read without the user's API key."
            ),
            base_asset=asset,
        )

    async def _book_slippage_bps(
        self, instrument: str, side: str, notional_usd: Decimal
    ) -> tuple[Decimal | None, str]:
        try:
            book = await self._post(
                "/book", {"instrument": instrument, "depth": BOOK_DEPTH}
            )
        except VenueUnavailableError as exc:
            return None, f"Orderbook unavailable: {exc.message}"

        raw_levels = book.get("asks" if side == "long" else "bids") or []
        levels = [
            (Decimal(str(level["price"])), Decimal(str(level["size"])))
            for level in raw_levels
        ]
        return walk_book(levels, notional_usd)

    async def health(self) -> bool:
        try:
            await self._post("/instrument", {"instrument": "BTC_USDT_Perp"})
        except VenueUnavailableError:
            return False
        return True
