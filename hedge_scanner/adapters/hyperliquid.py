"""Hyperliquid (L1 perps DEX, onchain orderbook) adapter.

Hyperliquid exposes a fully public, unauthenticated info API at
``POST https://api.hyperliquid.xyz/info``. Verified 2026-08-19: the
``clearinghouseState`` endpoint returns complete position data for ANY
EVM address, making this the **first EVM venue** in the tool where
paste-an-address works without credentials.

HIP-3 sub-DEXs (verified 2026-08-29). Since HIP-3 shipped, Hyperliquid
hosts **builder-deployed sub-DEXs** alongside the native perp DEX
(xyz/XYZ, flx/Felix Exchange, vntl/Ventuals, hyna/HyENA, km/mkts by
Kinetiq, cash/dreamcash, para/Paragon, io/EntropyIO, abcd/ABCDEx).
Their markets are namespaced ``<dex>:<coin>`` (e.g. ``xyz:BRENTOIL``).
A call to ``clearinghouseState`` **without** a ``dex`` field returns
positions on the native DEX only, silently omitting HIP-3 exposure —
this was previously producing false-negative "no positions" reports
for accounts that trade only on sub-DEXs. `perpDexs` is the discovery
endpoint; we fan out one `clearinghouseState` per sub-DEX and merge.

API docs: https://hyperliquid.gitbook.io/Hyperliquid-docs
Fee schedule: https://hyperliquid.gitbook.io/Hyperliquid-docs/trading/fees
Funding: https://hyperliquid.gitbook.io/Hyperliquid-docs/trading/funding
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

import httpx

from ..markets import canonical_base, same_asset
from ..models import Position, Quote
from .base import VenueUnavailableError, make_http_client, record_mark

DEFAULT_API_URL = "https://api.hyperliquid.xyz/info"

QUOTE_ASSET = "USDC"
BPS = Decimal(10_000)

# Hyperliquid settles funding hourly (1/8 of the 8h computed rate each hour).
# The Quote schema uses 8h bps, so we convert: hourly_rate × 8.
FUNDING_HOURLY_TO_8H = Decimal(8)

# Static fallback: Level 0 (base tier) perp rates.
# Source: https://hyperliquid.gitbook.io/Hyperliquid-docs/trading/fees
# Date: 2026-08-19. Used ONLY when the live userFees endpoint fails.
DEFAULT_PERP_TAKER_FEE_BPS = Decimal("4.5")   # 0.045%
DEFAULT_PERP_MAKER_FEE_BPS = Decimal("1.5")   # 0.015%

# TTL for fee schedule cache (seconds). Fees don't change intra-session.
_FEE_CACHE_TTL_S = 300.0

# TTL for the perpDexs discovery cache. Deployers rarely register or halt a
# sub-DEX; a 10-minute window is short enough to pick up a new listing between
# scans while sparing every scan a discovery round-trip.
_PERP_DEXS_CACHE_TTL_S = 600.0

# Predicted hourly funding from metaAndAssetCtxs. Avantis (and a hedge
# opened now) track the current rate, not the last hourly settlement.
_FUNDING_CTX_TTL_S = 10.0

# The zero address is used to query the base fee schedule without a real account.
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _hip3_dex(market: str) -> str | None:
    """HIP-3 dex prefix from a namespaced market, or None for native coins."""
    if ":" not in market:
        return None
    head, _, tail = market.partition(":")
    if not tail:
        return None
    if 1 <= len(head) <= 8 and head.islower() and head.isalnum():
        return head
    return None


class HyperliquidAdapter:
    venue = "hyperliquid"
    namespace = "evm"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        api_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_url = (
            api_url or os.environ.get("HYPERLIQUID_API_URL", DEFAULT_API_URL)
        ).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._fee_cache: tuple[float, Decimal, Decimal, bool] | None = None
        # (fetched_at, sub_dex_names, HIP-3 market keys like xyz:GOLD).
        self._perp_dexs_cache: (
            tuple[float, tuple[str, ...], tuple[str, ...]] | None
        ) = None
        # dex-or-native -> (fetched_at, coin -> hourly funding fraction).
        self._funding_fracs_by_dex: dict[str, tuple[float, dict[str, Decimal]]] = {}

    async def __aenter__(self) -> HyperliquidAdapter:
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

    async def _post(self, payload: dict) -> object:
        try:
            resp = await self._http().post(self._api_url, json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(
                self.venue, f"info API failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(self, address: str) -> list[Position]:
        """Return every non-zero position across the native DEX and every HIP-3 sub-DEX.

        A ``clearinghouseState`` call carries positions for exactly one DEX
        (the native one when no ``dex`` field is present). Sub-DEX accounts
        are separate clearinghouses on the same address, so a single call
        misses them entirely — hence the fan-out here.

        The mark price is read from each position's own ``positionValue``
        first, then falls back to ``allMids`` for the native DEX only.
        ``allMids`` does not carry HIP-3 quotes and is not queried per
        sub-DEX to avoid a per-DEX round trip we rarely need.
        """
        native_task = self._fetch_positions_for_dex(address, dex=None)
        sub_dex_names = await self._list_sub_dexs()
        sub_tasks = [self._fetch_positions_for_dex(address, dex=d) for d in sub_dex_names]

        # allMids only covers the native DEX. HIP-3 positions carry
        # `positionValue` so mark_price is derivable without it.
        mids_task = self._all_mids_safe()

        results = await asyncio.gather(native_task, *sub_tasks, mids_task)
        mids: dict[str, Decimal] = results[-1]
        dex_payloads = results[:-1]

        positions: list[Position] = []
        dex_labels = [None, *sub_dex_names]
        for dex_name, entries in zip(dex_labels, dex_payloads, strict=True):
            for entry in entries:
                position = self._to_position(address, entry, mids, dex=dex_name)
                if position is not None:
                    positions.append(position)

        # CONTRACT.md §12.9. Attach the CURRENT live funding rate to each
        # position so the engine can gate Avantis rows on "hedging on Avantis
        # would strictly improve funding". Fetched per unique coin in
        # parallel; a per-coin failure leaves that position's rate as None
        # rather than falling back to zero (§7 non-negotiable).
        await self._annotate_current_funding(positions)
        return positions

    async def _fetch_positions_for_dex(
        self, address: str, dex: str | None
    ) -> list[dict]:
        """Return ``assetPositions`` for one DEX. Returns [] on any failure.

        A single sub-DEX failing must not shadow real positions from other
        DEXs; ``get_positions`` is documented to raise ``VenueUnavailableError``
        only when the whole venue is unreachable, and the native-DEX call is
        what still enforces that.
        """
        payload: dict[str, object] = {"type": "clearinghouseState", "user": address}
        if dex is not None:
            payload["dex"] = dex
        try:
            data = await self._post(payload)
        except VenueUnavailableError:
            if dex is None:
                # Native DEX unreachable = venue unreachable; propagate so the
                # portfolio layer can record a proper VenueError.
                raise
            return []
        if not isinstance(data, dict):
            return []
        return data.get("assetPositions") or []

    async def _all_mids_safe(self, dex: str | None = None) -> dict[str, Decimal]:
        try:
            return await self._all_mids(dex)
        except VenueUnavailableError:
            return {}

    def _to_position(
        self,
        address: str,
        entry: dict,
        mids: dict[str, Decimal],
        dex: str | None = None,
    ) -> Position | None:
        pos = entry.get("position", {})
        coin = pos.get("coin", "")
        if not coin:
            return None

        szi = _dec(pos.get("szi"))
        if szi is None or szi == 0:
            return None

        side = "long" if szi > 0 else "short"
        size_base = abs(szi)
        sign = Decimal(1) if side == "long" else Decimal(-1)

        entry_price = _dec(pos.get("entryPx")) or Decimal(0)

        # Priority for mark price: derive from positionValue (always coherent
        # with szi for the payload we have), then allMids (native DEX only),
        # then entry price as a last resort. The previous version used
        # `positionValue` as a *price* fallback, which is a notional — off by
        # size_base. That silently corrupted mark and notional_usd whenever
        # allMids missed a coin (HIP-3 markets, newly listed, or stale cache).
        position_value = _dec(pos.get("positionValue"))
        if position_value is not None and position_value > 0 and size_base > 0:
            mark_price = position_value / size_base
        else:
            mark_price = mids.get(coin) or entry_price

        notional_usd = sign * size_base * mark_price

        unrealized_pnl = _dec(pos.get("unrealizedPnl"))
        liq_px = _dec(pos.get("liquidationPx"))
        margin_used = _dec(pos.get("marginUsed"))

        leverage_info = pos.get("leverage", {})
        if isinstance(leverage_info, dict):
            margin_mode = leverage_info.get("type")  # "cross" | "isolated"
            leverage_val = _dec(leverage_info.get("value"))
        else:
            margin_mode = None
            leverage_val = _dec(leverage_info)

        cum_funding = pos.get("cumFunding", {})
        # `sinceOpen` is already holder-PnL signed (negative = paid).
        funding_since_open = _dec(cum_funding.get("sinceOpen"))

        # Preserve the native <dex>:<coin> namespace when the position came
        # from a sub-DEX. This keeps the CLI's market column self-explaining
        # (e.g. `xyz:BRENTOIL`) and disambiguates a same-name market that
        # could exist on multiple HIP-3 DEXs.
        market = f"{dex}:{coin}" if dex is not None and ":" not in coin else coin

        raw_with_dex = dict(entry)
        if dex is not None:
            raw_with_dex["_dex"] = dex

        return Position(
            venue=self.venue,
            address=address,
            market=market,
            base_asset=canonical_base(market),
            quote_asset=QUOTE_ASSET,
            side=side,
            size_base=size_base,
            notional_usd=notional_usd,
            entry_price=entry_price,
            mark_price=mark_price,
            liquidation_price=liq_px,
            leverage=leverage_val,
            collateral_usd=margin_used,
            unrealized_pnl_usd=unrealized_pnl,
            funding_paid_usd=funding_since_open,
            margin_mode=margin_mode,
            opened_at=None,
            raw=raw_with_dex,
        )

    async def _list_sub_dexs(self) -> tuple[str, ...]:
        """Discover HIP-3 sub-DEX names via the ``perpDexs`` endpoint.

        Response shape (verified 2026-08-29):
            [null, {name: "xyz", ...}, {name: "flx", ...}, ...]
        The leading ``null`` represents the native DEX; every other entry is
        a deployed sub-DEX with a lowercase short name that acts as the
        ``dex`` parameter for ``clearinghouseState``.
        """
        names, _markets = await self._perp_dexs()
        return names

    async def _hip3_markets(self) -> tuple[str, ...]:
        """Namespacing ``xyz:GOLD`` / ``xyz:BRENTOIL`` from ``perpDexs``."""
        _names, markets = await self._perp_dexs()
        return markets

    async def _perp_dexs(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if self._perp_dexs_cache is not None:
            fetched_at, names, markets = self._perp_dexs_cache
            if time.monotonic() - fetched_at < _PERP_DEXS_CACHE_TTL_S:
                return names, markets
        try:
            data = await self._post({"type": "perpDexs"})
        except VenueUnavailableError:
            # If discovery fails we still return native-DEX positions, so
            # cache an empty list briefly rather than retrying on every call.
            empty = (time.monotonic(), (), ())
            self._perp_dexs_cache = empty
            return (), ()
        names: list[str] = []
        markets: list[str] = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
                for row in entry.get("assetToStreamingOiCap") or []:
                    market = row[0] if row else None
                    if isinstance(market, str) and market:
                        markets.append(market)
        packed = (tuple(names), tuple(markets))
        self._perp_dexs_cache = (time.monotonic(), packed[0], packed[1])
        return packed

    # ------------------------------------------------------------------
    # Fee schedule (live fetch with fallback)
    # ------------------------------------------------------------------

    async def _fetch_fee_schedule(self) -> tuple[Decimal, Decimal, bool]:
        """Fetch the base fee schedule from the userFees endpoint.

        Returns (taker_bps, maker_bps, is_live). When the fetch fails, returns
        the static fallback and is_live=False.
        """
        if self._fee_cache is not None:
            ts, taker, maker, is_live = self._fee_cache
            if time.monotonic() - ts < _FEE_CACHE_TTL_S:
                return taker, maker, is_live

        try:
            data = await self._post({"type": "userFees", "user": _ZERO_ADDRESS})
            schedule = data.get("feeSchedule", {})
            cross_rate = _dec(schedule.get("cross"))
            add_rate = _dec(schedule.get("add"))
            if cross_rate is not None and add_rate is not None:
                taker_bps = cross_rate * BPS
                maker_bps = add_rate * BPS
                self._fee_cache = (time.monotonic(), taker_bps, maker_bps, True)
                return taker_bps, maker_bps, True
        except (VenueUnavailableError, KeyError, TypeError):
            pass

        # Fallback to static defaults
        self._fee_cache = (
            time.monotonic(),
            DEFAULT_PERP_TAKER_FEE_BPS,
            DEFAULT_PERP_MAKER_FEE_BPS,
            False,
        )
        return DEFAULT_PERP_TAKER_FEE_BPS, DEFAULT_PERP_MAKER_FEE_BPS, False

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = canonical_base(base_asset)
        native = await self._all_mids()
        coin = self._resolve_coin(asset, native)
        if coin is None:
            coin = self._resolve_coin(asset, await self._hip3_universe())
        if coin is None:
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
                notes=f"Hyperliquid does not list a {asset} perp.",
                base_asset=asset,
            )

        funding_8h_bps = await self._latest_funding_8h_bps(coin)

        # Funding sign convention: positive published rate = longs pay shorts.
        # Quote convention: positive = hedger RECEIVES.
        # So: short hedge receives when rate > 0, long hedge pays.
        hedge_sign = Decimal(1) if side == "short" else Decimal(-1)

        taker_bps, _maker_bps, fees_live = await self._fetch_fee_schedule()

        available = funding_8h_bps is not None
        funding = (hedge_sign * funding_8h_bps) if funding_8h_bps is not None else Decimal(0)

        fee_note = (
            f"fee: live from userFees API ({taker_bps} bps taker)"
            if fees_live
            else (
                f"[fee: static fallback ({taker_bps} bps taker), userFees fetch failed; "
                f"source: hyperliquid.gitbook.io/docs/trading/fees, 2026-08-19]"
            )
        )

        return Quote(
            venue=self.venue,
            market=coin,
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=taker_bps,
            close_fee_bps=taker_bps,
            price_impact_bps=Decimal(0),
            funding_rate_8h_bps=funding,
            borrow_rate_8h_bps=Decimal(0),
            est_slippage_bps=Decimal(0),
            available=available,
            notes=(
                f"{fee_note}; funding hourly (predicted), shown as 8h equivalent. "
                "Orderbook venue with deep book; price impact not modeled."
                + ("" if available else " No live funding rate available.")
            ),
            base_asset=asset,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        try:
            result = await self._post({"type": "allMids"})
        except VenueUnavailableError:
            return False
        return isinstance(result, dict) and len(result) > 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def get_marks(self) -> dict[str, Decimal]:
        """Hyperliquid mids, native DEX first then HIP-3 ``<dex>:<coin>``.

        Spot/oracle internals (``@`` / ``#`` keys) are dropped. HIP-3
        marks are indexed under the namespaced market so they cannot
        overwrite a native-DEX coin of the same underlying.
        """
        out: dict[str, Decimal] = {}
        native = await self._all_mids_safe()
        for coin, mid in native.items():
            if not coin or coin[0] in "@#":
                continue
            record_mark(out, coin, mid)

        dexs = await self._list_sub_dexs()
        if not dexs:
            return out
        results = await asyncio.gather(
            *(self._all_mids_safe(dex) for dex in dexs),
            return_exceptions=True,
        )
        for dex, mids in zip(dexs, results):
            if not isinstance(mids, dict):
                continue
            for coin, mid in mids.items():
                if not coin or coin[0] in "@#":
                    continue
                record_mark(out, f"{dex}:{coin}", mid)
        return out

    async def _all_mids(self, dex: str | None = None) -> dict[str, Decimal]:
        payload: dict[str, object] = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        data = await self._post(payload)
        if not isinstance(data, dict):
            return {}
        out: dict[str, Decimal] = {}
        for coin, mid_str in data.items():
            mid = _dec(mid_str)
            if mid is not None and mid > 0:
                out[coin] = mid
        return out

    async def _hip3_universe(self) -> dict[str, Decimal]:
        """HIP-3 ``<dex>:<coin>`` mids plus ``perpDexs`` listings.

        Native ``allMids`` has no gold/FX. ``xyz:GOLD`` is only on the xyz
        book (and in ``perpDexs`` even when that book's mids are empty).
        """
        out: dict[str, Decimal] = {}
        dexs = await self._list_sub_dexs()
        if dexs:
            results = await asyncio.gather(
                *(self._all_mids_safe(d) for d in dexs),
                return_exceptions=True,
            )
            for dex, mids in zip(dexs, results):
                if not isinstance(mids, dict):
                    continue
                for coin, mid in mids.items():
                    if not coin or coin[0] in "@#":
                        continue
                    key = coin if ":" in coin else f"{dex}:{coin}"
                    out.setdefault(key, mid)
        for market in await self._hip3_markets():
            out.setdefault(market, Decimal(0))
        return out

    def _resolve_coin(self, asset: str, mids: dict[str, Decimal]) -> str | None:
        """Find the Hyperliquid coin symbol for a normalized base asset."""
        if asset in mids:
            return asset
        from ..assets import ALIASES
        for alias, canonical in ALIASES.items():
            if canonical == asset and alias in mids:
                return alias
        for coin in mids:
            if same_asset(coin, asset):
                return coin
        return None

    async def _predicted_funding_fracs(self, dex: str | None = None) -> dict[str, Decimal]:
        """Current hourly funding fraction per coin on one DEX.

        Native ``metaAndAssetCtxs`` names are bare (``BTC``). HIP-3 with
        ``dex="xyz"`` names are namespaced (``xyz:GOLD``). ``fundingHistory``
        uses the same names — ``GOLD`` 500s, ``xyz:GOLD`` returns the rate.
        """
        key = dex or ""
        now = time.monotonic()
        hit = self._funding_fracs_by_dex.get(key)
        if hit is not None and now - hit[0] < _FUNDING_CTX_TTL_S:
            return hit[1]
        payload: dict[str, object] = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        try:
            data = await self._post(payload)
        except VenueUnavailableError:
            self._funding_fracs_by_dex[key] = (now, {})
            return {}
        if not isinstance(data, list) or len(data) < 2:
            self._funding_fracs_by_dex[key] = (now, {})
            return {}
        universe = data[0].get("universe") or []
        ctxs = data[1] if isinstance(data[1], list) else []
        out: dict[str, Decimal] = {}
        for row, ctx in zip(universe, ctxs):
            name = (row or {}).get("name")
            frac = _dec((ctx or {}).get("funding"))
            if name and frac is not None:
                out[name] = frac
        self._funding_fracs_by_dex[key] = (now, out)
        return out

    async def _settled_funding_8h_bps(self, coin: str) -> Decimal | None:
        """Last hourly settlement, as 8h bps. Fallback when predicted is missing."""
        try:
            start_ms = int((time.time() - 7200) * 1000)
            data = await self._post({
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start_ms,
            })
        except VenueUnavailableError:
            return None
        if not data:
            return None
        latest = data[-1]
        hist_coin = latest.get("coin")
        if hist_coin and not same_asset(str(hist_coin), coin):
            return None
        hourly_rate = _dec(latest.get("fundingRate"))
        if hourly_rate is None:
            return None
        return hourly_rate * FUNDING_HOURLY_TO_8H * BPS

    async def _latest_funding_8h_bps(self, coin: str) -> Decimal | None:
        """Hourly funding as 8h bps. Predicted first, last settled as fallback.

        Signed per Hyperliquid convention: positive = longs pay shorts. Callers
        that need the position-holder's perspective (positive = holder
        receives) flip the sign against the holder's side.
        """
        predicted = (await self._predicted_funding_fracs(_hip3_dex(coin))).get(coin)
        if predicted is not None:
            return predicted * FUNDING_HOURLY_TO_8H * BPS
        return await self._settled_funding_8h_bps(coin)

    async def _annotate_current_funding(self, positions: list[Position]) -> None:
        """Fill ``current_funding_rate_8h_bps`` on each held position, in place.

        Native coins share one ``metaAndAssetCtxs`` fetch. HIP-3 coins use the
        namespaced market (``xyz:GOLD``) on both predicted ctxs and
        ``fundingHistory`` — the bare coin 500s. A per-coin failure leaves
        that position's rate as ``None`` (§12.9), never as zero.

        The Hyperliquid published rate is positive when longs pay, so the
        POSITION HOLDER'S perspective flips against the position side:
        ``-published`` for a long holder (they pay when the rate is positive),
        ``+published`` for a short holder.
        """
        coins = sorted({p.market for p in positions if p.market})
        if not coins:
            return
        results = await asyncio.gather(
            *(self._latest_funding_8h_bps(c) for c in coins),
            return_exceptions=True,
        )
        rates: dict[str, Decimal] = {}
        for coin, rate in zip(coins, results):
            if isinstance(rate, Decimal):
                rates[coin] = rate
        for position in positions:
            published = rates.get(position.market)
            if published is None:
                continue
            sign = Decimal(-1) if position.side == "long" else Decimal(1)
            position.current_funding_rate_8h_bps = sign * published
