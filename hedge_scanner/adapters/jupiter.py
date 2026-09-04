"""Jupiter Perpetuals (Solana) adapter.

Positions are on-chain PDAs in the perps program, so *any* address is readable
by anyone with an RPC endpoint. This is the only venue of the four where the
paste-an-address UX works with no cooperation from the venue.

Everything in this module was verified against mainnet on 2026-08-19:

  program id           PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu
  Position discriminator  [170,188,143,228,122,64,247,208]  (base58 "VZMoMoKgZQb")
  Position account size   216 bytes (210 packed fields + 6 bytes trailing padding)
  custody -> mint         read from each custody account's `mint` field on-chain

Fee and rate formulas are transcribed from
https://docs.jup.ag/user-docs/trade/perps/fees and the jup-ag/docs
`perpetual-exchange/how-it-works` guide, both read 2026-08-19.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import base58
import httpx

from ..assets import normalize_base_asset
from ..markets import canonical_base
from ..models import Position, Quote
from .base import VenueUnavailableError, make_http_client, record_mark

PROGRAM_ID = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
POSITION_DISCRIMINATOR = bytes([170, 188, 143, 228, 122, 64, 247, 208])
POSITION_DISCRIMINATOR_B58 = base58.b58encode(POSITION_DISCRIMINATOR).decode()
POSITION_ACCOUNT_SIZE = 216

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
PRICE_API_URL = "https://lite-api.jup.ag/price/v3"

# Jupiter Perps public REST API. Returns positions with the exact fields
# jup.ag/portfolio displays: entryPrice, markPrice, liquidationPrice, all fee
# components, and pnlAfterFees. Documented at dev.jup.ag/docs/perp-api. This
# is the same source the frontend polls; using it here guarantees the tool's
# display values match jup.ag/portfolio to the penny. Falls back to on-chain
# decode only when this API is unreachable.
PERPS_API_URL = "https://perps-api.jup.ag/v1"

# ---------------------------------------------------------------------------
# Doves oracle (Jupiter Perps mark price)
# ---------------------------------------------------------------------------
# Jupiter Perps computes PnL, liquidation, and health checks against the Doves
# oracle (Edge by Chaos Labs primary, Pyth/Chainlink verification & fallback),
# NOT the price.jup.ag DEX aggregator. The DEX aggregator quotes a
# spot-liquidity-weighted price which drifts from the perp mark in either
# direction as pool inventories change; using it for position PnL causes
# per-asset drift versus what jup.ag/portfolio actually displays.
#
# Doves account layout (reverse-engineered against mainnet, 2026-09-02;
# confirmed against jup.ag/portfolio values on SOL and BTC to <0.05% drift):
#     [0:8]       Anchor discriminator (70f98bd9d7d0f936)
#     [168:176]   price:        u64 LE  (scale = 10^-8, i.e. USD × 1e8)
#     [177:185]   publish_time: i64 LE  (unix seconds)
# Older docs reference the account's `dovesOracle` field; the current custody
# points at `dovesAgOracle`, which is what the addresses below correspond to.
# Migration note from docs.jup.ag/user-docs/trade/perps/technical-reference.
DOVES_PROGRAM_ID = "DoVEsk76QybCEHQGzkvYPWLQu9gzNoZZZt3TPiL597e"
_DOVES_PRICE_OFF = 168
_DOVES_PUBTIME_OFF = 177
DOVES_PRICE_SCALE = Decimal(10) ** 8

# Any Doves read older than this is treated as stale and falls back to the
# DEX aggregator. 5 minutes is deliberately loose: the oracle updates every
# 10-30 seconds under normal load, so anything past a few minutes means the
# keeper has actually stopped and we should not pretend the read is fresh.
DOVES_MAX_AGE_S = 300

DOVES_ORACLES: dict[str, str] = {
    "SOL": "FYq2BWQ1V5P1WFBqr3qB2Kb5yHVvSv7upzKodgQE5zXh",
    "ETH": "AFZnHPzy4mvVCffrVwhewHbFc93uTHvDSFrVH7GtfXF1",
    "BTC": "hUqAT1KQ7eW1i6Csp9CXYtpPfSAvi835V7wKi5fRfmC",
    "USDC": "6Jp2xZUTWdDD2ZyUPRzeMdc6AFQ5K3pFgZxk2EijfjnM",
    "USDT": "Fgc93D641F8N2d1xLjQ4jmShuD3GE3BsCXA56KBQbF5u",
}

# Byte offsets into the Position account (Borsh, no padding between fields).
_P_OWNER = 8
_P_POOL = 40
_P_CUSTODY = 72
_P_COLLATERAL_CUSTODY = 104
_P_OPEN_TIME = 136
_P_UPDATE_TIME = 144
_P_SIDE = 152
_P_PRICE = 153
_P_SIZE_USD = 161
_P_COLLATERAL_USD = 169
_P_REALISED_PNL_USD = 177
_P_CUMULATIVE_INTEREST_SNAPSHOT = 185
_P_LOCKED_AMOUNT = 201
_P_BUMP = 209

# Byte offsets into the Custody account. Derived by walking the published IDL
# struct and confirmed on-chain: `mint` decodes to the documented mints and
# `fundingRateState.lastUpdate` decodes to a current unix timestamp.
_C_MINT = 40
_C_DECIMALS = 104
_C_IS_STABLE = 105
_C_TRADE_IMPACT_FEE_SCALAR = 151
_C_ASSETS_OWNED = 222
_C_ASSETS_LOCKED = 230
_C_CUMULATIVE_INTEREST_RATE = 262
_C_FUNDING_LAST_UPDATE = 278
_C_INCREASE_POSITION_BPS = 296
_C_DECREASE_POSITION_BPS = 304
_C_JUMP_MIN_RATE_BPS = 352
_C_JUMP_MAX_RATE_BPS = 360
_C_JUMP_TARGET_RATE_BPS = 368
_C_JUMP_TARGET_UTILIZATION = 376

# Fixed-point scales used by the program.
USD_SCALE = Decimal(10) ** 6  # sizeUsd, collateralUsd, price
RATE_SCALE = Decimal(10) ** 9  # cumulativeInterestRate, targetUtilizationRate
BPS_POWER = Decimal(10) ** 4
HOURS_PER_YEAR = Decimal(8760)
# jup.ag/perps and `/v1/pool-info` publish borrow as percent per hour
# (`longBorrowRatePercent: "0.0013"` = 0.0013%/hr). 8h bps = pct/hr × 8 × 100.

_SIDE_LONG = 1
_SIDE_SHORT = 2

# Custody accounts of the JLP pool 5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq.
# Enumerated from the pool account's custodies vector and each custody's `mint`
# field, mainnet 2026-08-19. The SOL/ETH/BTC/USDC/USDT entries also match the
# table published at docs.jup.ag/user-docs/trade/perps/technical-reference.
CUSTODIES: dict[str, dict] = {
    "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz": {
        "base_asset": "SOL",
        "mint": "So11111111111111111111111111111111111111112",
        "decimals": 9,
        "is_stable": False,
    },
    "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn": {
        "base_asset": "ETH",
        "mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
        "decimals": 8,
        "is_stable": False,
    },
    "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm": {
        "base_asset": "BTC",
        "mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
        "decimals": 8,
        "is_stable": False,
    },
    "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa": {
        "base_asset": "USDC",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "decimals": 6,
        "is_stable": True,
    },
    "4vkNeXiYEUizLdrpdPS1eC2mccyM4NUPRtERrk6ZETkk": {
        "base_asset": "USDT",
        "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        "decimals": 6,
        "is_stable": True,
    },
    "DdwY1ELc9rRK7xNL3hTXabSFBmVrTPpfsUZSv2Y3LL1U": {
        "base_asset": "JUPUSD",
        "mint": "JuprjznTrTSp2UFa3ZBUFgwdAmtZCq4MQCwysN55USD",
        "decimals": 6,
        "is_stable": True,
    },
}

TRADABLE_CUSTODY_BY_ASSET = {
    v["base_asset"]: k for k, v in CUSTODIES.items() if not v["is_stable"]
}
# Reverse lookup for the perps-api path, which references assets by mint rather
# than by custody pubkey.
_ASSET_BY_MINT: dict[str, dict] = {v["mint"]: v for v in CUSTODIES.values()}
# Shorts post a stable as collateral, so the borrow leg of a short is the USDC
# custody by default.
DEFAULT_STABLE_CUSTODY = "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa"


def _u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 8], "little")


def _i64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 8], "little", signed=True)


def _u128(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 16], "little")


def _pubkey(data: bytes, offset: int) -> str:
    return base58.b58encode(data[offset : offset + 32]).decode()


def decode_position_account(data: bytes) -> dict | None:
    """Decode a raw Position account.

    Returns ``None`` when the account is not a Position or when `sizeUsd == 0`.
    A zero `sizeUsd` is the venue's own definition of closed: Position accounts
    are PDAs derived from (owner, custody, collateralCustody), so they are
    reused and left behind zeroed rather than deleted. Without this check every
    wallet that ever traded would appear to hold up to nine open positions.
    """
    if len(data) < _P_BUMP + 1 or data[:8] != POSITION_DISCRIMINATOR:
        return None
    size_usd = _u64(data, _P_SIZE_USD)
    if size_usd == 0:
        return None
    return {
        "owner": _pubkey(data, _P_OWNER),
        "pool": _pubkey(data, _P_POOL),
        "custody": _pubkey(data, _P_CUSTODY),
        "collateralCustody": _pubkey(data, _P_COLLATERAL_CUSTODY),
        "openTime": _i64(data, _P_OPEN_TIME),
        "updateTime": _i64(data, _P_UPDATE_TIME),
        "side": data[_P_SIDE],
        "price": _u64(data, _P_PRICE),
        "sizeUsd": size_usd,
        "collateralUsd": _u64(data, _P_COLLATERAL_USD),
        "realisedPnlUsd": _i64(data, _P_REALISED_PNL_USD),
        "cumulativeInterestSnapshot": _u128(data, _P_CUMULATIVE_INTEREST_SNAPSHOT),
        "lockedAmount": _u64(data, _P_LOCKED_AMOUNT),
        "bump": data[_P_BUMP],
    }


def decode_doves_price(data: bytes) -> tuple[Decimal, int] | None:
    """Decode a Doves oracle account into (price_usd, publish_time_unix_s).

    Returns ``None`` when the account is too short or the price field is zero
    (Doves publishes 0 while the feed is uninitialised, never as a real price).
    Layout is documented at the top of this module; the offsets are pinned by
    tests against captured mainnet bytes so a Doves layout bump will fail
    loudly rather than silently return a garbage number.
    """
    if len(data) < _DOVES_PUBTIME_OFF + 8:
        return None
    price_raw = int.from_bytes(
        data[_DOVES_PRICE_OFF : _DOVES_PRICE_OFF + 8], "little", signed=False
    )
    if price_raw == 0:
        return None
    publish_time = int.from_bytes(
        data[_DOVES_PUBTIME_OFF : _DOVES_PUBTIME_OFF + 8], "little", signed=True
    )
    return Decimal(price_raw) / DOVES_PRICE_SCALE, publish_time


def decode_custody_account(data: bytes) -> dict:
    """Decode the fields of a Custody account this adapter needs."""
    return {
        "mint": _pubkey(data, _C_MINT),
        "decimals": data[_C_DECIMALS],
        "isStable": bool(data[_C_IS_STABLE]),
        "tradeImpactFeeScalar": _u64(data, _C_TRADE_IMPACT_FEE_SCALAR),
        "owned": _u64(data, _C_ASSETS_OWNED),
        "locked": _u64(data, _C_ASSETS_LOCKED),
        "cumulativeInterestRate": _u128(data, _C_CUMULATIVE_INTEREST_RATE),
        "fundingLastUpdate": _i64(data, _C_FUNDING_LAST_UPDATE),
        "increasePositionBps": _u64(data, _C_INCREASE_POSITION_BPS),
        "decreasePositionBps": _u64(data, _C_DECREASE_POSITION_BPS),
        "minRateBps": _u64(data, _C_JUMP_MIN_RATE_BPS),
        "maxRateBps": _u64(data, _C_JUMP_MAX_RATE_BPS),
        "targetRateBps": _u64(data, _C_JUMP_TARGET_RATE_BPS),
        "targetUtilizationRate": _u64(data, _C_JUMP_TARGET_UTILIZATION),
    }


def hourly_borrow_percent_to_8h_bps(percent_per_hour: Decimal) -> Decimal:
    """Convert Jupiter's UI/API hourly percent into bps per 8 hours.

    `0.0013` on the wire is 0.0013%/hr (the BTC long rate on jup.ag/perps),
    which is 1.04 bps / 8h.
    """
    return percent_per_hour * Decimal(8) * BPS_POWER / Decimal(100)


def borrow_apr_bps(custody: dict) -> Decimal:
    """Dual-slope borrow APR in bps for a custody's current utilization.

    Transcribed from the `Calculating Borrow Rate` pseudocode in the Jupiter
    perps docs; the model's output is an APR, hourly is APR / 8760.
    """
    owned = Decimal(custody["owned"])
    locked = Decimal(custody["locked"])
    utilization = locked / owned if owned > 0 and locked > 0 else Decimal(0)

    minimum = Decimal(custody["minRateBps"])
    maximum = Decimal(custody["maxRateBps"])
    target = Decimal(custody["targetRateBps"])
    target_utilization = Decimal(custody["targetUtilizationRate"]) / RATE_SCALE

    if target_utilization <= 0:
        return minimum
    if utilization < target_utilization:
        lower_slope = (target - minimum) / target_utilization
        return minimum + lower_slope * utilization
    upper_slope = (maximum - target) / (Decimal(1) - target_utilization)
    return target + upper_slope * (utilization - target_utilization)


def price_impact_bps(notional_usd: Decimal, trade_impact_fee_scalar: int) -> Decimal:
    """Linear price impact fee in bps.

    Docs pseudocode, with `tradeSizeUsd` in atomic USD (6 dp):
        tradeSizeUsdBps    = tradeSizeUsd * BPS_POWER
        priceImpactFeeBps  = tradeSizeUsdBps / tradeImpactFeeScalar

    The additive OI-imbalance component is NOT modeled here, so this is a floor
    on the true impact fee when the book is skewed.
    """
    if trade_impact_fee_scalar <= 0:
        return Decimal(0)
    return notional_usd * USD_SCALE * BPS_POWER / Decimal(trade_impact_fee_scalar)


class JupiterAdapter:
    venue = "jupiter"
    namespace = "solana"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        rpc_url: str | None = None,
        timeout: float = 20.0,
        now_s: Callable[[], int] | None = None,
    ) -> None:
        self._rpc_url = rpc_url or os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        # Clock hook for the Doves staleness check. Tests pin this to the
        # fixture capture time so the recorded oracle payload does not age out
        # of the freshness window; production defaults to real wall clock.
        self._now_s = now_s or (lambda: int(time.time()))

    async def __aenter__(self) -> JupiterAdapter:
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

    async def _rpc(self, method: str, params: list) -> object:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._http().post(self._rpc_url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(self.venue, f"Solana RPC error: {exc}") from exc
        if "error" in body:
            raise VenueUnavailableError(
                self.venue, f"Solana RPC returned an error: {body['error']}"
            )
        return body["result"]

    async def _fetch_custodies(self, pubkeys: list[str]) -> dict[str, dict]:
        if not pubkeys:
            return {}
        result = await self._rpc(
            "getMultipleAccounts", [pubkeys, {"encoding": "base64"}]
        )
        out: dict[str, dict] = {}
        for pubkey, account in zip(pubkeys, result["value"], strict=True):
            if account is None:
                continue
            out[pubkey] = decode_custody_account(base64.b64decode(account["data"][0]))
        return out

    async def _fetch_prices(self, mints: list[str]) -> dict[str, Decimal]:
        if not mints:
            return {}
        try:
            resp = await self._http().get(PRICE_API_URL, params={"ids": ",".join(mints)})
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise VenueUnavailableError(
                self.venue, f"Jupiter price API error: {exc}"
            ) from exc
        return {
            mint: Decimal(str(entry["usdPrice"]))
            for mint, entry in body.items()
            if entry and entry.get("usdPrice") is not None
        }

    async def _fetch_doves_prices(self) -> dict[str, tuple[Decimal, int]]:
        """Read every Doves oracle account and return ``{asset: (price, ts)}``.

        Missing accounts, undecodable payloads, and reads older than
        ``DOVES_MAX_AGE_S`` are dropped silently -- the caller falls back to the
        DEX aggregator per asset. A single ``getMultipleAccounts`` RPC covers
        all five oracles.
        """
        assets = list(DOVES_ORACLES.keys())
        pubkeys = [DOVES_ORACLES[a] for a in assets]
        result = await self._rpc(
            "getMultipleAccounts", [pubkeys, {"encoding": "base64"}]
        )
        now = self._now_s()
        out: dict[str, tuple[Decimal, int]] = {}
        for asset, account in zip(assets, result["value"], strict=True):
            if account is None:
                continue
            decoded = decode_doves_price(base64.b64decode(account["data"][0]))
            if decoded is None:
                continue
            price, publish_time = decoded
            if now - publish_time > DOVES_MAX_AGE_S:
                continue
            out[asset] = (price, publish_time)
        return out

    async def _resolve_marks(self) -> dict[str, Decimal]:
        """Build ``{base_asset: mark_price}`` using Doves first, DEX-agg fallback.

        Jupiter Perps computes PnL/liquidation against Doves. Reading Doves
        keeps this adapter's Position.mark_price aligned with what
        jup.ag/portfolio shows the trader. When a Doves account is missing or
        stale, the DEX aggregator (price.jup.ag) is used per-asset; that path
        is documented as "off vs the perp UI by whatever the pool inventory
        drift is" (see module header) and only exists so a Doves outage does
        not blank out the whole adapter.
        """
        # Fire both requests concurrently so the fallback is free when Doves
        # is fresh (which it almost always is on majors).
        doves, by_mint = await asyncio.gather(
            self._fetch_doves_prices(),
            self._fetch_prices(
                [m["mint"] for m in CUSTODIES.values() if not m["is_stable"]]
            ),
        )
        marks: dict[str, Decimal] = {}
        for meta in CUSTODIES.values():
            if meta["is_stable"]:
                continue
            asset = meta["base_asset"]
            if asset in doves:
                marks[asset] = doves[asset][0]
            else:
                fallback = by_mint.get(meta["mint"])
                if fallback is not None:
                    marks[asset] = fallback
        return marks

    async def get_marks(self) -> dict[str, Decimal]:
        """Jupiter perp mark USD, keyed by base asset and ``{ASSET}-PERP``.

        Sourced from Doves oracle when fresh, DEX aggregator otherwise. See
        ``_resolve_marks`` for the rationale.
        """
        marks = await self._resolve_marks()
        out: dict[str, Decimal] = {}
        for asset, price in marks.items():
            record_mark(out, asset, price)
            record_mark(out, f"{asset}-PERP", price)
        return out

    async def _fetch_pool_info(self, mint: str) -> dict | None:
        """Live borrow rates from ``perps-api`` — same numbers as jup.ag/perps.

        ``GET /v1/pool-info?mint=<marketMint>`` returns ``longBorrowRatePercent``
        / ``shortBorrowRatePercent`` as percent-per-hour strings. Used for
        quotes so the scanner's borrow matches the header the trader sees.
        ``None`` on any failure; the caller falls back to the on-chain jump
        rate so a Jupiter API blip does not blank the quote.
        """
        try:
            resp = await self._http().get(
                f"{PERPS_API_URL}/pool-info",
                params={"mint": mint},
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def _borrow_8h_bps_from_pool_info(
        self, pool: dict | None, side: str
    ) -> Decimal | None:
        if not pool:
            return None
        key = (
            "longBorrowRatePercent" if side == "long" else "shortBorrowRatePercent"
        )
        raw = pool.get(key)
        if raw in (None, ""):
            return None
        try:
            return hourly_borrow_percent_to_8h_bps(Decimal(str(raw)))
        except (ArithmeticError, ValueError):
            return None

    async def _fetch_positions_via_api(self, address: str) -> list[dict] | None:
        """Fetch positions from ``perps-api.jup.ag``. Returns ``None`` on failure.

        This is the same endpoint jup.ag/portfolio polls, so its
        ``entryPrice`` / ``markPrice`` / ``liquidationPrice`` / ``pnlAfterFees``
        values are guaranteed to match what the trader sees on the frontend.
        Any HTTP or decode failure falls back to the on-chain decode in
        ``get_positions``; distinguishing "API down" from "wallet has no
        positions" requires letting a successful empty list through.
        """
        try:
            resp = await self._http().get(
                f"{PERPS_API_URL}/positions",
                params={"walletAddress": address},
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        rows = body.get("dataList")
        if not isinstance(rows, list):
            return None
        return rows

    def _position_from_api(self, address: str, row: dict) -> Position | None:
        """Build a Position from a ``perps-api`` row.

        Every numeric field on the wire is a string. USD-denominated fields
        (``entryPrice``, ``markPrice``, ``liquidationPrice``, ``collateral``,
        ``leverage``, ``size``, ``*FeesUsd``, ``pnl*Usd``) are already scaled
        to human-readable USD, so no atomic-unit conversion is needed. Returns
        ``None`` for rows that reference an asset the adapter does not track.
        """
        mint = row.get("marketMint")
        meta = _ASSET_BY_MINT.get(mint) if mint else None
        base_asset = canonical_base(
            meta["base_asset"] if meta else normalize_base_asset(mint or "")
        )
        if not base_asset:
            return None

        side = row.get("side", "").lower()
        if side not in ("long", "short"):
            return None
        sign = Decimal(1) if side == "long" else Decimal(-1)

        entry_price = Decimal(row["entryPrice"])
        mark_price = Decimal(row["markPrice"])
        liq_price_raw = row.get("liquidationPrice")
        liq_price = (
            Decimal(liq_price_raw)
            if liq_price_raw not in (None, "", "0", "0.00")
            else None
        )
        collateral_usd = Decimal(row["collateral"])
        leverage = Decimal(row["leverage"])
        size_usd = Decimal(row["size"])
        size_base = size_usd / entry_price if entry_price > 0 else Decimal(0)
        notional_usd = sign * size_base * mark_price
        # `pnlAfterFeesUsd` is what jup.ag/portfolio's "PnL" column shows;
        # matches to display precision. Fall back to pre-fee if the after-fee
        # variant is absent (older API versions).
        pnl_after = row.get("pnlAfterFeesUsd")
        pnl_before = row.get("pnlBeforeFeesUsd")
        unrealized = Decimal(pnl_after) if pnl_after is not None else (
            Decimal(pnl_before) if pnl_before is not None else sign * (mark_price - entry_price) * size_base
        )
        borrow_paid = (
            Decimal(row["borrowFeesUsd"]) if row.get("borrowFeesUsd") is not None else None
        )
        # Holder-PnL sign: borrow is always a cost, so the Position field is
        # negative when the trader has paid. `borrowFeesUsd` on the wire is
        # unsigned.

        collateral_meta = _ASSET_BY_MINT.get(row.get("collateralMint", ""))
        quote_asset = (
            collateral_meta["base_asset"]
            if collateral_meta and collateral_meta["is_stable"]
            else "USD"
        )

        opened_at = None
        created = row.get("createdTime")
        if isinstance(created, (int, float)) and created > 0:
            opened_at = datetime.fromtimestamp(int(created), tz=UTC)

        return Position(
            venue=self.venue,
            address=address,
            market=f"{base_asset}-PERP",
            base_asset=base_asset,
            quote_asset=quote_asset,
            side=side,
            size_base=size_base,
            notional_usd=notional_usd,
            entry_price=entry_price,
            mark_price=mark_price,
            liquidation_price=liq_price,
            leverage=leverage,
            collateral_usd=collateral_usd,
            unrealized_pnl_usd=unrealized,
            funding_paid_usd=(-borrow_paid if borrow_paid is not None else None),
            margin_mode="isolated",
            opened_at=opened_at,
            raw=row,
        )

    async def _annotate_borrow_as_funding(self, positions: list[Position]) -> None:
        """Stamp holder-signed carry as ``current_funding_rate_8h_bps``.

        Jupiter has no two-sided funding. The holder always pays borrow, so
        this field is ``-borrow_8h_bps``. A failed pool-info fetch leaves
        ``None`` (never a silent zero).
        """
        if not positions:
            return
        mint_by_asset = {
            meta["base_asset"]: meta["mint"]
            for meta in CUSTODIES.values()
            if not meta["is_stable"]
        }
        mints = sorted({
            mint_by_asset[p.base_asset]
            for p in positions
            if p.base_asset in mint_by_asset
        })
        if not mints:
            return
        pools = await asyncio.gather(*(self._fetch_pool_info(m) for m in mints))
        pool_by_mint = dict(zip(mints, pools))
        for position in positions:
            mint = mint_by_asset.get(position.base_asset)
            if not mint:
                continue
            borrow = self._borrow_8h_bps_from_pool_info(
                pool_by_mint.get(mint), position.side
            )
            if borrow is None:
                continue
            position.current_funding_rate_8h_bps = -borrow

    async def get_positions(self, address: str) -> list[Position]:
        # Primary path: Jupiter's own perps API. Returns the exact numbers
        # jup.ag/portfolio displays. Fall through to on-chain decode only when
        # this endpoint is unavailable (returns None), so a Jupiter API outage
        # never blanks out positions the tool would otherwise see on-chain.
        api_rows = await self._fetch_positions_via_api(address)
        if api_rows is not None:
            positions: list[Position] = []
            for row in api_rows:
                pos = self._position_from_api(address, row)
                if pos is not None:
                    positions.append(pos)
            await self._annotate_borrow_as_funding(positions)
            return positions

        result = await self._rpc(
            "getProgramAccounts",
            [
                PROGRAM_ID,
                {
                    "encoding": "base64",
                    "filters": [
                        {"memcmp": {"offset": 0, "bytes": POSITION_DISCRIMINATOR_B58}},
                        {"memcmp": {"offset": _P_OWNER, "bytes": address}},
                    ],
                },
            ],
        )

        decoded = []
        for account in result:
            raw = decode_position_account(base64.b64decode(account["account"]["data"][0]))
            if raw is not None:
                raw["_pubkey"] = account["pubkey"]
                decoded.append(raw)
        if not decoded:
            return []

        needed = {d["custody"] for d in decoded} | {
            d["collateralCustody"] for d in decoded
        }
        # Fetch custodies and Doves-primary marks concurrently. Marks are
        # keyed by base asset (see ``_resolve_marks``); the caller no longer
        # cares about the mint→price mapping the DEX aggregator uses natively.
        custodies, marks = await asyncio.gather(
            self._fetch_custodies(sorted(needed)),
            self._resolve_marks(),
        )

        positions = []
        for raw in decoded:
            position = self._to_position(address, raw, custodies, marks)
            if position is not None:
                positions.append(position)
        await self._annotate_borrow_as_funding(positions)
        return positions

    def _to_position(
        self,
        address: str,
        raw: dict,
        custodies: dict[str, dict],
        marks: dict[str, Decimal],
    ) -> Position | None:
        custody_meta = CUSTODIES.get(raw["custody"])
        custody = custodies.get(raw["custody"])
        if custody is None:
            return None
        mint = custody["mint"]
        base_asset = canonical_base(
            custody_meta["base_asset"] if custody_meta else normalize_base_asset(mint)
        )

        mark_price = marks.get(base_asset)
        if mark_price is None:
            raise VenueUnavailableError(
                self.venue,
                f"no mark price available for {base_asset} "
                f"(custody {raw['custody']}, mint {mint}); both Doves and the "
                "DEX aggregator returned nothing",
            )

        entry_price = Decimal(raw["price"]) / USD_SCALE
        size_usd = Decimal(raw["sizeUsd"]) / USD_SCALE
        collateral_usd = Decimal(raw["collateralUsd"]) / USD_SCALE
        size_base = size_usd / entry_price if entry_price > 0 else Decimal(0)
        side = "long" if raw["side"] == _SIDE_LONG else "short"
        sign = Decimal(1) if side == "long" else Decimal(-1)

        notional_usd = sign * size_base * mark_price
        unrealized = sign * (mark_price - entry_price) * size_base

        # Accrued borrow fee, from the collateral custody's monotonic counter.
        # Local value is a positive cost (used in the liq formula below);
        # `funding_paid_usd` on the Position is negated to holder-PnL sign.
        collateral_custody = custodies.get(raw["collateralCustody"])
        borrow_paid = None
        if collateral_custody is not None:
            delta = (
                Decimal(collateral_custody["cumulativeInterestRate"])
                - Decimal(raw["cumulativeInterestSnapshot"])
            )
            if delta > 0:
                borrow_paid = delta / RATE_SCALE * size_usd

        quote_meta = CUSTODIES.get(raw["collateralCustody"])
        quote_asset = (
            quote_meta["base_asset"] if quote_meta and quote_meta["is_stable"] else "USD"
        )

        # Jupiter liquidates when effective collateral hits zero:
        #   collateral + unrealizedPnL - accruedBorrow - closeFee ≤ MM
        # where MM = 0.2% of notional (maxLeverage 500x).  Solving for the price
        # that triggers this gives a point-in-time liq price that accounts for
        # currently accrued borrow.  It drifts as borrow accrues (unlike venues
        # with a static MM%), but Jupiter's own UI shows the same drifting
        # number, and showing None here loses the comparison.
        liq_price = None
        close_fee_bps_val = Decimal(custody["decreasePositionBps"])
        close_fee_at_liq = close_fee_bps_val / BPS_POWER * size_usd
        maintenance_margin = Decimal("0.002") * size_usd
        net_collateral = collateral_usd - close_fee_at_liq - maintenance_margin
        if borrow_paid is not None:
            net_collateral -= borrow_paid
        if net_collateral > 0 and size_base > 0:
            if side == "long":
                liq_price = entry_price - net_collateral / size_base
            else:
                liq_price = entry_price + net_collateral / size_base
            if liq_price <= 0:
                liq_price = None

        return Position(
            venue=self.venue,
            address=address,
            market=f"{base_asset}-PERP",
            base_asset=base_asset,
            quote_asset=quote_asset,
            side=side,
            size_base=size_base,
            notional_usd=notional_usd,
            entry_price=entry_price,
            mark_price=mark_price,
            liquidation_price=liq_price,
            leverage=(size_usd / collateral_usd if collateral_usd > 0 else None),
            collateral_usd=collateral_usd,
            unrealized_pnl_usd=unrealized,
            funding_paid_usd=(-borrow_paid if borrow_paid is not None else None),
            margin_mode="isolated",
            opened_at=datetime.fromtimestamp(raw["openTime"], tz=UTC),
            raw=raw,
        )

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = canonical_base(base_asset)
        market = f"{asset}-PERP"
        custody_pk = TRADABLE_CUSTODY_BY_ASSET.get(asset)
        if custody_pk is None:
            return Quote(
                venue=self.venue,
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
                notes=f"Jupiter Perps does not list {asset}; markets are SOL, ETH, BTC.",
                base_asset=asset,
            )

        # A long borrows the token itself; a short borrows the stable it posts
        # as collateral. The borrow leg drives the carry, so it must follow side.
        borrow_custody_pk = (
            custody_pk if side == "long" else DEFAULT_STABLE_CUSTODY
        )
        mint = CUSTODIES[custody_pk]["mint"]
        pool_info, custodies = await asyncio.gather(
            self._fetch_pool_info(mint),
            self._fetch_custodies(sorted({custody_pk, borrow_custody_pk})),
        )
        custody = custodies[custody_pk]
        borrow_custody = custodies[borrow_custody_pk]

        # Prefer the rate jup.ag/perps shows (`longBorrowRatePercent` /
        # `shortBorrowRatePercent`, percent per hour). Fall back to the
        # on-chain jump-rate curve if pool-info is down.
        borrow_8h_bps = self._borrow_8h_bps_from_pool_info(pool_info, side)
        apr_bps = borrow_apr_bps(borrow_custody)
        if borrow_8h_bps is None:
            borrow_8h_bps = apr_bps / HOURS_PER_YEAR * Decimal(8)
            borrow_note = (
                f"borrow APR {apr_bps / 100:.2f}% on the "
                f"{CUSTODIES[borrow_custody_pk]['base_asset']} custody (on-chain)"
            )
        else:
            hourly = borrow_8h_bps * Decimal(100) / BPS_POWER / Decimal(8)
            borrow_note = f"borrow {hourly:.4f}%/hr from perps-api pool-info"

        return Quote(
            venue=self.venue,
            market=market,
            side=side,
            notional_usd=notional_usd,
            taker_fee_bps=Decimal(custody["increasePositionBps"]),
            close_fee_bps=Decimal(custody["decreasePositionBps"]),
            price_impact_bps=price_impact_bps(
                notional_usd, custody["tradeImpactFeeScalar"]
            ),
            # Jupiter has no funding rate at all: "Positions always pay borrow
            # fees and are never paid funding." Zero here is the real value, not
            # a missing one.
            funding_rate_8h_bps=Decimal(0),
            borrow_rate_8h_bps=borrow_8h_bps,
            # Fills execute at the oracle price regardless of size; the price
            # impact fee above is what stands in for orderbook slippage.
            est_slippage_bps=Decimal(0),
            available=True,
            notes=(
                f"No funding rate on Jupiter; {borrow_note}. "
                "Excludes the additive OI-imbalance price impact component."
            ),
            base_asset=asset,
        )

    async def health(self) -> bool:
        try:
            result = await self._rpc("getHealth", [])
        except VenueUnavailableError:
            return False
        return result == "ok"


async def _demo(address: str) -> None:  # pragma: no cover - manual probe helper
    async with JupiterAdapter() as adapter:
        positions, quote = await asyncio.gather(
            adapter.get_positions(address),
            adapter.get_quote("BTC", "short", Decimal(100_000)),
        )
        for position in positions:
            print(position)
        print(quote)
