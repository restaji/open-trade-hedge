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
from datetime import UTC, datetime
from decimal import Decimal

import base58
import httpx

from ..assets import normalize_base_asset
from ..models import Position, Quote
from .base import VenueUnavailableError, make_http_client

PROGRAM_ID = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
POSITION_DISCRIMINATOR = bytes([170, 188, 143, 228, 122, 64, 247, 208])
POSITION_DISCRIMINATOR_B58 = base58.b58encode(POSITION_DISCRIMINATOR).decode()
POSITION_ACCOUNT_SIZE = 216

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
PRICE_API_URL = "https://lite-api.jup.ag/price/v3"

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
    ) -> None:
        self._rpc_url = rpc_url or os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

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

    async def get_positions(self, address: str) -> list[Position]:
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
        custodies = await self._fetch_custodies(sorted(needed))
        mints = sorted(
            {c["mint"] for pk, c in custodies.items() if not c["isStable"]}
        )
        prices = await self._fetch_prices(mints)

        positions = []
        for raw in decoded:
            position = self._to_position(address, raw, custodies, prices)
            if position is not None:
                positions.append(position)
        return positions

    def _to_position(
        self,
        address: str,
        raw: dict,
        custodies: dict[str, dict],
        prices: dict[str, Decimal],
    ) -> Position | None:
        custody_meta = CUSTODIES.get(raw["custody"])
        custody = custodies.get(raw["custody"])
        if custody is None:
            return None
        mint = custody["mint"]
        base_asset = (
            custody_meta["base_asset"] if custody_meta else normalize_base_asset(mint)
        )

        mark_price = prices.get(mint)
        if mark_price is None:
            raise VenueUnavailableError(
                self.venue,
                f"no mark price available for custody {raw['custody']} (mint {mint})",
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
        # Positive means the position has paid this much.
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
            funding_paid_usd=borrow_paid,
            margin_mode="isolated",
            opened_at=datetime.fromtimestamp(raw["openTime"], tz=UTC),
            raw=raw,
        )

    async def get_quote(
        self, base_asset: str, side: str, notional_usd: Decimal
    ) -> Quote:
        asset = normalize_base_asset(base_asset)
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
        custodies = await self._fetch_custodies(
            sorted({custody_pk, borrow_custody_pk})
        )
        custody = custodies[custody_pk]
        borrow_custody = custodies[borrow_custody_pk]

        apr_bps = borrow_apr_bps(borrow_custody)
        borrow_8h_bps = apr_bps / HOURS_PER_YEAR * Decimal(8)

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
                f"No funding rate on Jupiter; borrow APR {apr_bps / 100:.2f}% on the "
                f"{CUSTODIES[borrow_custody_pk]['base_asset']} custody. "
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
