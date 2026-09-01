"""Hyperliquid adapter tests, driven by recorded HLP vault responses (2026-08-19).

The fixture is the HLP vault (0x010461C14e146ac35Fe42271BDC1134Ee31C703a) which
held 175 cross-margin positions when captured. clearinghouseState is fully public
and unauthenticated — this is the first EVM venue with paste-an-address support.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from hedge_scanner.adapters.hyperliquid import HyperliquidAdapter
from hedge_scanner.assets import normalize_base_asset

HLP_VAULT = "0x010461C14e146ac35Fe42271BDC1134Ee31C703a"


# ------------------------------------------------------------------
# Fixture sanity
# ------------------------------------------------------------------


def test_clearinghouse_fixture_has_real_positions(fixture):
    body = fixture("hyperliquid/clearinghouse_state.json")
    assert "assetPositions" in body
    assert len(body["assetPositions"]) > 0
    row = body["assetPositions"][0]["position"]
    required = {"coin", "szi", "entryPx", "unrealizedPnl", "leverage", "marginUsed"}
    assert set(row) >= required


def test_all_mids_fixture_has_prices(fixture):
    body = fixture("hyperliquid/all_mids.json")
    assert isinstance(body, dict)
    assert "BTC" in body
    assert "ETH" in body


def test_funding_history_fixture_has_entries(fixture):
    body = fixture("hyperliquid/funding_history_btc.json")
    assert isinstance(body, list)
    assert len(body) > 0
    assert "fundingRate" in body[0]


# ------------------------------------------------------------------
# Position parsing
# ------------------------------------------------------------------


async def test_get_positions_returns_all_nonzero(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HLP_VAULT)

    assert len(positions) > 100
    for p in positions:
        assert p.venue == "hyperliquid"
        assert p.address == HLP_VAULT
        assert p.quote_asset == "USDC"
        assert p.side in {"long", "short"}
        assert p.size_base > 0
        assert p.mark_price > 0


# ------------------------------------------------------------------
# HIP-3 sub-DEX aggregation
# ------------------------------------------------------------------

# A real 2026-08-29 snapshot: this account holds nothing on the native DEX
# and ~$14M notional on the `xyz` sub-DEX. It exists in the suite specifically
# because a `clearinghouseState` call without a `dex` field returns [] for it,
# and the previous adapter therefore reported it as flat.
HIP3_ADDR = "0x46921f6961bdb411b756c9712f6bdb58fbd9164f"


async def test_get_positions_includes_hip3_sub_dexs(replay_client):
    """Positions from HIP-3 sub-DEXs are merged into the same result set.

    Before this fix `get_positions` only queried the native DEX, silently
    dropping every builder-deployed market. See CONTRACT.md §12.5.
    """
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HIP3_ADDR)

    assert len(positions) > 0, "HIP-3 positions must be surfaced"
    markets = {p.market for p in positions}
    assert any(m.startswith("xyz:") for m in markets), (
        f"expected at least one xyz:<market> position, got {sorted(markets)}"
    )


async def test_hip3_markets_normalise_to_underlying_base(replay_client):
    """`base_asset` on an HIP-3 position drops the `<dex>:` prefix.

    This is what makes an `xyz:BRENTOIL` position net against a `BRENT`
    hedge on Ostium or Avantis.
    """
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HIP3_ADDR)

    by_market = {p.market: p for p in positions}
    for market, expected in [
        ("xyz:BRENTOIL", "BRENT"),
        ("xyz:AAPL", "AAPL"),
        ("xyz:GOLD", "XAU"),
        ("xyz:SILVER", "XAG"),
    ]:
        pos = by_market.get(market)
        if pos is None:
            continue  # fixture may not carry this specific market
        assert pos.base_asset == expected, (
            f"{market} should normalise to {expected}, got {pos.base_asset}"
        )


async def test_hip3_positions_have_coherent_notional(replay_client):
    """Mark price must be derived from ``positionValue / size_base``.

    Regression guard for the pre-fix bug where `positionValue` was assigned
    to `mark_price` directly (a notional treated as a price), which then
    made `notional_usd` come out proportional to `size_base²`.
    """
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HIP3_ADDR)

    hip3 = [p for p in positions if p.market.startswith("xyz:")]
    assert hip3, "expected HIP-3 positions in fixture"
    for p in hip3:
        # Recover the raw positionValue from the payload and cross-check.
        pv = Decimal(str(p.raw["position"]["positionValue"]))
        expected_notional = pv if p.side == "long" else -pv
        assert p.notional_usd == expected_notional, (
            f"{p.market}: notional_usd={p.notional_usd} != {expected_notional}"
        )
        assert p.mark_price == pv / p.size_base


async def test_native_dex_unreachable_still_raises(replay_client, monkeypatch):
    """Native-DEX failure is still a venue-level error (portfolio surfaces it).

    A sub-DEX failing must not, but the primary read must — otherwise a
    down venue is indistinguishable from a flat account.
    """
    from hedge_scanner.adapters.hyperliquid import HyperliquidAdapter as HL
    from hedge_scanner.adapters.base import VenueUnavailableError

    adapter = HL(client=replay_client, api_url="https://api.hyperliquid.xyz/info")

    original = adapter._post

    async def failing_native(payload):
        if payload.get("type") == "clearinghouseState" and payload.get("dex") is None:
            raise VenueUnavailableError("hyperliquid", "simulated outage")
        return await original(payload)

    monkeypatch.setattr(adapter, "_post", failing_native)
    with pytest.raises(VenueUnavailableError):
        await adapter.get_positions(HIP3_ADDR)


async def test_sub_dex_failure_does_not_shadow_native_positions(
    replay_client, monkeypatch
):
    """A broken sub-DEX must not erase real native-DEX positions."""
    from hedge_scanner.adapters.hyperliquid import HyperliquidAdapter as HL
    from hedge_scanner.adapters.base import VenueUnavailableError

    adapter = HL(client=replay_client, api_url="https://api.hyperliquid.xyz/info")
    original = adapter._post

    async def flaky_sub(payload):
        if payload.get("type") == "clearinghouseState" and payload.get("dex") == "xyz":
            raise VenueUnavailableError("hyperliquid", "sub-DEX flaky")
        return await original(payload)

    monkeypatch.setattr(adapter, "_post", flaky_sub)
    positions = await adapter.get_positions(HLP_VAULT)
    assert len(positions) > 0  # native positions still returned


async def test_perp_dexs_cache_reused_within_ttl(replay_client):
    """Discovery is cached so a batch scan doesn't fan out N calls per address."""
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    first = await adapter._list_sub_dexs()
    # A second call while the cache is warm must not re-hit the endpoint —
    # we assert by checking the cache tuple is unchanged.
    cached_before = adapter._perp_dexs_cache
    second = await adapter._list_sub_dexs()
    assert first == second
    assert adapter._perp_dexs_cache is cached_before


async def test_positions_have_correct_sign_convention(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HLP_VAULT)

    for p in positions:
        if p.side == "long":
            assert p.notional_usd > 0, f"{p.market} long should have positive notional"
        else:
            assert p.notional_usd < 0, f"{p.market} short should have negative notional"


async def test_btc_position_fields_populated(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HLP_VAULT)
    btc = next((p for p in positions if p.market == "BTC"), None)

    assert btc is not None, "HLP vault should have a BTC position"
    assert btc.base_asset == "BTC"
    assert btc.entry_price > 0
    assert btc.leverage is not None
    assert btc.margin_mode == "cross"
    assert btc.funding_paid_usd is not None
    assert btc.collateral_usd is not None


async def test_positions_include_both_longs_and_shorts(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HLP_VAULT)

    longs = [p for p in positions if p.side == "long"]
    shorts = [p for p in positions if p.side == "short"]
    assert len(longs) > 0
    assert len(shorts) > 0


# ------------------------------------------------------------------
# Symbol normalization (k-prefix coins)
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "venue_coin,expected_base",
    [
        ("kPEPE", "PEPE"),
        ("kBONK", "BONK"),
        ("kSHIB", "SHIB"),
        ("kFLOKI", "FLOKI"),
        ("kNEIRO", "NEIRO"),
        ("kLUNC", "LUNC"),
        ("BTC", "BTC"),
        ("ETH", "ETH"),
        ("SOL", "SOL"),
        ("SPX", "US500"),
    ],
)
def test_normalize_hyperliquid_symbols(venue_coin, expected_base):
    assert normalize_base_asset(venue_coin) == expected_base


async def test_k_prefix_positions_normalized(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    positions = await adapter.get_positions(HLP_VAULT)

    kpepe = [p for p in positions if p.market == "kPEPE"]
    if kpepe:
        assert kpepe[0].base_asset == "PEPE"


# ------------------------------------------------------------------
# Quote construction
# ------------------------------------------------------------------


async def test_quote_btc_short(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    q = await adapter.get_quote("BTC", "short", Decimal(10_000))

    assert q.venue == "hyperliquid"
    assert q.market == "BTC"
    assert q.side == "short"
    assert q.available is True
    # Fee fetched live from userFees endpoint (fixture: cross=0.00045 → 4.5 bps)
    assert q.taker_fee_bps == Decimal("4.5")
    assert q.close_fee_bps == Decimal("4.5")
    assert q.borrow_rate_8h_bps == Decimal(0)
    assert q.price_impact_bps == Decimal(0)
    assert q.base_asset == "BTC"
    assert "live from userFees API" in q.notes


async def test_quote_funding_sign_flips_with_side(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    short = await adapter.get_quote("BTC", "short", Decimal(10_000))
    long = await adapter.get_quote("BTC", "long", Decimal(10_000))

    assert short.funding_rate_8h_bps == -long.funding_rate_8h_bps


async def test_quote_unlisted_asset(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    q = await adapter.get_quote("NOTATOKEN", "short", Decimal(1_000))
    assert q.available is False


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------


async def test_health(replay_client):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    assert await adapter.health() is True


# ------------------------------------------------------------------
# Live integration (opt-in)
# ------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("HYPERLIQUID_LIVE_TESTS"),
    reason="Set HYPERLIQUID_LIVE_TESTS=1 to run live API tests",
)
async def test_live_position_fetch():
    async with HyperliquidAdapter() as adapter:
        positions = await adapter.get_positions(HLP_VAULT)
        assert len(positions) > 0
        for p in positions:
            assert p.venue == "hyperliquid"
            assert p.size_base > 0


@pytest.mark.skipif(
    not os.environ.get("HYPERLIQUID_LIVE_TESTS"),
    reason="Set HYPERLIQUID_LIVE_TESTS=1 to run live API tests",
)
async def test_live_quote():
    async with HyperliquidAdapter() as adapter:
        q = await adapter.get_quote("ETH", "short", Decimal(5_000))
        assert q.available is True
        # Fee fetched live from the userFees endpoint
        assert q.taker_fee_bps > 0
        assert "live from userFees API" in q.notes


@pytest.mark.skipif(
    not os.environ.get("HYPERLIQUID_LIVE_TESTS"),
    reason="Set HYPERLIQUID_LIVE_TESTS=1 to run live API tests",
)
async def test_live_health():
    async with HyperliquidAdapter() as adapter:
        assert await adapter.health() is True
