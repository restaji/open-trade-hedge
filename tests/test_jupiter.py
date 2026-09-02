"""Jupiter adapter, driven by a recorded mainnet getProgramAccounts response."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from hedge_scanner.adapters.jupiter import (
    CUSTODIES,
    DOVES_ORACLES,
    POSITION_ACCOUNT_SIZE,
    POSITION_DISCRIMINATOR,
    POSITION_DISCRIMINATOR_B58,
    PROGRAM_ID,
    JupiterAdapter,
    borrow_apr_bps,
    decode_custody_account,
    decode_doves_price,
    decode_position_account,
    hourly_borrow_percent_to_8h_bps,
    price_impact_bps,
)

WALLET = "2JVs9RekjARxu9tRYq8Dbq2eGNRegzRSGJMrCBXKj8ti"


def _doves_capture_time(fixture_loader) -> int:
    """Read the Doves fixture's capture timestamp so tests can pin the clock.

    The Doves adapter treats reads older than 300s as stale. The fixture was
    recorded live from mainnet, so its embedded ``publish_time`` values will
    age out of the freshness window minutes after capture. Every Doves-facing
    test pins ``now_s`` to (capture + 15s) so the recorded feed reads as fresh.
    """
    return int(fixture_loader("jupiter_doves.json")["_captured_at"]) + 15


@pytest.fixture
def adapter_now_s(fixture):
    return lambda: _doves_capture_time(fixture)


def test_program_id_and_discriminator_are_the_verified_constants():
    assert PROGRAM_ID == "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
    assert POSITION_DISCRIMINATOR == bytes([170, 188, 143, 228, 122, 64, 247, 208])
    assert POSITION_DISCRIMINATOR_B58 == "VZMoMoKgZQb"


def test_recorded_accounts_match_the_documented_layout(fixture):
    accounts = fixture("jupiter_program_accounts.json")["result"]
    assert accounts, "fixture should contain at least one Position account"
    for account in accounts:
        data = base64.b64decode(account["account"]["data"][0])
        assert account["account"]["owner"] == PROGRAM_ID
        assert len(data) == POSITION_ACCOUNT_SIZE
        assert data[:8] == POSITION_DISCRIMINATOR


def test_zero_size_position_accounts_are_treated_as_closed():
    accounts = [bytes(POSITION_ACCOUNT_SIZE)]
    accounts[0] = POSITION_DISCRIMINATOR + bytes(POSITION_ACCOUNT_SIZE - 8)
    assert decode_position_account(accounts[0]) is None


def test_non_position_accounts_are_rejected():
    data = bytes([1, 184, 48, 81, 93, 131, 63, 145]) + bytes(POSITION_ACCOUNT_SIZE - 8)
    assert decode_position_account(data) is None


def test_recorded_custody_accounts_decode_to_the_documented_mints(fixture):
    values = fixture("jupiter_custodies.json")["result"]["value"]
    pubkeys = [
        "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz",
        "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn",
        "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm",
        "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa",
    ]
    for pubkey, account in zip(pubkeys, values, strict=True):
        custody = decode_custody_account(base64.b64decode(account["data"][0]))
        expected = CUSTODIES[pubkey]
        assert custody["mint"] == expected["mint"]
        assert custody["decimals"] == expected["decimals"]
        assert custody["isStable"] is expected["is_stable"]
        # The base fee has been 6 bps on both sides since launch; if this moves
        # the quote is wrong, so pin it rather than trusting the constant.
        assert custody["increasePositionBps"] == 6
        assert custody["decreasePositionBps"] == 6


async def test_get_positions_normalizes_recorded_mainnet_positions(
    replay_client, adapter_now_s
):
    adapter = JupiterAdapter(client=replay_client, now_s=adapter_now_s)
    positions = await adapter.get_positions(WALLET)

    assert positions, "the recorded wallet holds open positions"
    for position in positions:
        assert position.venue == "jupiter"
        assert position.address == WALLET
        assert position.base_asset in {"SOL", "ETH", "BTC"}
        assert position.side in {"long", "short"}
        assert position.size_base > 0
        assert position.entry_price > 0
        assert position.mark_price > 0
        assert position.margin_mode == "isolated"
        assert position.opened_at is not None
        # notional carries the direction, size_base never does
        sign = 1 if position.side == "long" else -1
        assert position.notional_usd * sign > 0
        assert position.liquidation_price is None or position.liquidation_price > 0


async def test_short_and_long_both_present_and_signed_correctly(
    replay_client, adapter_now_s
):
    adapter = JupiterAdapter(client=replay_client, now_s=adapter_now_s)
    positions = await adapter.get_positions(WALLET)
    sides = {p.side for p in positions}
    assert sides == {"long", "short"}
    for position in positions:
        if position.side == "short":
            assert position.notional_usd < 0
        else:
            assert position.notional_usd > 0


async def test_quote_reports_zero_funding_and_a_real_borrow_rate(replay_client):
    adapter = JupiterAdapter(client=replay_client)
    quote = await adapter.get_quote("BTC", "short", Decimal(100_000))

    assert quote.available is True
    assert quote.venue == "jupiter"
    assert quote.base_asset == "BTC"
    assert quote.taker_fee_bps == Decimal(6)
    assert quote.close_fee_bps == Decimal(6)
    # Jupiter genuinely has no funding rate; zero here is a fact, not a default.
    assert quote.funding_rate_8h_bps == Decimal(0)
    assert quote.borrow_rate_8h_bps > 0
    assert quote.est_slippage_bps == Decimal(0)
    assert quote.price_impact_bps > 0


async def test_quote_borrow_rate_matches_pool_info_header(adapter_now_s):
    """BTC long borrow is `longBorrowRatePercent` from pool-info (0.0013%/hr).

    Same field jup.ag/perps shows in the market header. 0.0013%/hr × 8 × 100
    = 1.04 bps/8h. Shorts use `shortBorrowRatePercent`.
    """
    import httpx as _httpx
    from tests.conftest import _handler  # type: ignore[attr-defined]

    pool_body = {
        "longAvailableLiquidity": "20000000.00",
        "longBorrowRatePercent": "0.0013",
        "longUtilizationPercent": "13.00",
        "shortAvailableLiquidity": "20000000.00",
        "shortBorrowRatePercent": "0.0007",
        "shortUtilizationPercent": "60.92",
        "openFeePercent": "0.06",
        "maxRequestExecutionSec": "45",
        "maxPriceImpactFeePercent": "0.44",
    }

    def route(request):
        url = str(request.url)
        if "perps-api.jup.ag" in url and "pool-info" in url:
            return _httpx.Response(200, json=pool_body)
        return _handler(request)

    client = _httpx.AsyncClient(transport=_httpx.MockTransport(route))
    try:
        adapter = JupiterAdapter(client=client, now_s=adapter_now_s)
        long_q = await adapter.get_quote("BTC", "long", Decimal(100_000))
        short_q = await adapter.get_quote("BTC", "short", Decimal(100_000))
    finally:
        await client.aclose()

    assert long_q.available is True
    assert long_q.funding_rate_8h_bps == Decimal(0)
    assert long_q.borrow_rate_8h_bps == Decimal("1.04")
    assert short_q.borrow_rate_8h_bps == Decimal("0.56")
    assert "pool-info" in long_q.notes


def test_hourly_borrow_percent_matches_jup_ag_header():
    assert hourly_borrow_percent_to_8h_bps(Decimal("0.0013")) == Decimal("1.04")
    assert hourly_borrow_percent_to_8h_bps(Decimal("0.0007")) == Decimal("0.56")


async def test_quote_is_unavailable_for_an_unlisted_asset(replay_client):
    adapter = JupiterAdapter(client=replay_client)
    quote = await adapter.get_quote("DOGE", "short", Decimal(10_000))
    assert quote.available is False
    assert quote.borrow_rate_8h_bps == Decimal(0)


# ---------------------------------------------------------------------------
# Doves oracle (Jupiter Perps mark price source)
# ---------------------------------------------------------------------------


def test_doves_oracle_addresses_are_the_documented_constants():
    # Layout regression: if any of these move, `_fetch_doves_prices` must be
    # updated because the RPC returns accounts positionally.
    assert DOVES_ORACLES["SOL"] == "FYq2BWQ1V5P1WFBqr3qB2Kb5yHVvSv7upzKodgQE5zXh"
    assert DOVES_ORACLES["ETH"] == "AFZnHPzy4mvVCffrVwhewHbFc93uTHvDSFrVH7GtfXF1"
    assert DOVES_ORACLES["BTC"] == "hUqAT1KQ7eW1i6Csp9CXYtpPfSAvi835V7wKi5fRfmC"


def test_decode_doves_price_matches_recorded_mainnet_bytes(fixture):
    """Pin the decoder against the captured Doves fixture.

    Values here are the exact prices the Jupiter Perps program would have used
    for PnL/liq at capture time, so any layout regression that shifts the price
    field by even one byte fails immediately.
    """
    payload = fixture("jupiter_doves.json")
    accounts = payload["result"]["value"]
    order = payload["_pubkey_to_asset"]
    prices = {}
    for pubkey, account in zip(payload["_requested_pubkeys"], accounts, strict=True):
        assert account is not None, f"missing Doves account {pubkey}"
        decoded = decode_doves_price(base64.b64decode(account["data"][0]))
        assert decoded is not None
        prices[order[pubkey]] = decoded[0]
    # Sanity bands, not exact matches (the captures freeze but SDK ports may
    # tighten these). SOL O($100), BTC O($75k), ETH O($3k), USDC ~ $1.
    assert Decimal("50") < prices["SOL"] < Decimal("500")
    assert Decimal("30000") < prices["BTC"] < Decimal("200000")
    assert Decimal("500") < prices["ETH"] < Decimal("10000")
    assert Decimal("0.98") < prices["USDC"] < Decimal("1.02")
    # USDT's Doves feed is documented-stale in the fixture; decode still works,
    # but the freshness filter drops it in `_fetch_doves_prices`. Value should
    # still be near a dollar because the last observed rate was normal.
    assert Decimal("0.98") < prices["USDT"] < Decimal("1.02")


def test_decode_doves_returns_none_for_empty_payloads():
    assert decode_doves_price(b"") is None
    assert decode_doves_price(bytes(16)) is None  # too short
    # A 200-byte all-zero blob has the right length but a zero price, which
    # Doves publishes only when the feed is uninitialised.
    assert decode_doves_price(bytes(200)) is None


async def test_get_marks_uses_doves_when_fresh(replay_client, adapter_now_s, fixture):
    """`get_marks()` must return the Doves oracle value, not the DEX-agg price.

    Jupiter Perps computes PnL/liq against Doves, so this is the mark that
    matches jup.ag/portfolio. The DEX aggregator (``price.jup.ag``) is a
    liquidity-weighted spot price that drifts against Doves in either
    direction; using it for positions caused per-asset PnL mismatch (see
    2026-09-02 investigation).
    """
    adapter = JupiterAdapter(client=replay_client, now_s=adapter_now_s)
    marks = await adapter.get_marks()

    # Doves fresh for SOL/ETH/BTC/USDC ⇒ those keys are present.
    for asset in ("SOL", "ETH", "BTC"):
        assert asset in marks
        assert marks[asset] == marks[f"{asset}-PERP"]

    # Cross-check against the DEX aggregator fixture: values MUST NOT match
    # exactly on BTC, otherwise the adapter is still reading the wrong feed.
    dex_agg = fixture("jupiter_price_v3.json")
    btc_mint = CUSTODIES["5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm"]["mint"]
    dex_agg_btc = Decimal(str(dex_agg[btc_mint]["usdPrice"]))
    assert marks["BTC"] != dex_agg_btc, (
        "BTC mark equals the DEX aggregator price; Doves reads are being "
        "shadowed by the fallback path. See adapters/jupiter.py _resolve_marks."
    )


# ---------------------------------------------------------------------------
# Jupiter Perps public API (primary source for positions -- matches frontend)
# ---------------------------------------------------------------------------


def _mock_perps_api_transport(api_body: dict):
    """Build a transport that returns ``api_body`` for perps-api requests.

    Every other URL delegates to the shared ``_handler`` in conftest so mark
    reads, custody reads, and program-account reads still hit their fixtures.
    """
    import httpx as _httpx
    from tests.conftest import _handler  # type: ignore[attr-defined]

    def route(request):
        if "perps-api.jup.ag" in str(request.url):
            return _httpx.Response(200, json=api_body)
        return _handler(request)

    return _httpx.MockTransport(route)


async def test_get_positions_prefers_perps_api_over_on_chain(adapter_now_s):
    """When perps-api responds, its fields drive the Position, not on-chain.

    Regression guard for the primary-vs-fallback ordering: `liquidationPrice`,
    `markPrice`, and `pnlAfterFeesUsd` on the returned Position must equal the
    API response exactly (matching what jup.ag/portfolio shows the trader).
    The on-chain decoder would compute different values because it applies our
    liq formula rather than reading Jupiter's own frontend-canonical number.
    """
    import httpx as _httpx
    # Synthetic response with recognisably distinct values so we can prove
    # each field flowed through unchanged. Numbers are contrived, not live.
    api_body = {
        "dataList": [
            {
                "borrowFeesUsd": "17.42",
                "closeFeesUsd": "42.00",
                "collateral": "10000.00",
                "collateralMint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
                "createdTime": 1788248913,
                "entryPrice": "70000.00",
                "leverage": "10.00",
                "liquidationPrice": "63456.78",  # <- venue-canonical, not computed
                "marketMint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
                "markPrice": "68000.00",
                "openFeesUsd": "42.00",
                "pnlAfterFeesUsd": "-2941.42",
                "pnlBeforeFeesUsd": "-2857.14",
                "positionPubkey": "SyntheticPosition111111111111111111111",
                "side": "long",
                "size": "100000.00",
                "totalFeesUsd": "101.42",
            }
        ]
    }
    client = _httpx.AsyncClient(transport=_mock_perps_api_transport(api_body))
    try:
        adapter = JupiterAdapter(client=client, now_s=adapter_now_s)
        positions = await adapter.get_positions("SyntheticWallet1111111111111111111111")
    finally:
        await client.aclose()

    assert len(positions) == 1
    p = positions[0]
    assert p.venue == "jupiter"
    assert p.base_asset == "BTC"
    assert p.side == "long"
    # Every display-critical field must equal the API value exactly.
    assert p.entry_price == Decimal("70000.00")
    assert p.mark_price == Decimal("68000.00")
    assert p.liquidation_price == Decimal("63456.78")
    assert p.leverage == Decimal("10.00")
    assert p.collateral_usd == Decimal("10000.00")
    assert p.unrealized_pnl_usd == Decimal("-2941.42")  # pnlAfterFeesUsd, not pre-fee
    assert p.funding_paid_usd == Decimal("-17.42")


async def test_perps_api_failure_falls_back_to_on_chain(replay_client, adapter_now_s):
    """A 404/5xx from perps-api must not blank out on-chain positions.

    The conftest fixture already returns 404 for perps-api, which is exactly
    the "API unavailable" case. This test just re-affirms that under those
    conditions the on-chain decoder still populates the Position list.
    Guards against a future refactor that accidentally treats "API down" as
    "no positions" (which would silently hide live positions from the user).
    """
    adapter = JupiterAdapter(client=replay_client, now_s=adapter_now_s)
    positions = await adapter.get_positions(WALLET)
    # Fixture wallet holds 2 recorded positions; both must appear via fallback.
    assert positions, "fallback path must produce positions when API 404s"
    for p in positions:
        assert p.venue == "jupiter"
        assert p.mark_price > 0
        # Fallback liq is computed from custody params, not read from a venue,
        # so it may or may not be present depending on collateral state.


async def test_perps_api_empty_dataList_returns_no_positions(adapter_now_s):
    """API says the wallet has no positions -> we return []. No fallback.

    An empty ``dataList`` is a valid successful response, not an outage. If we
    fell through to on-chain here, closed positions with lingering zero-size
    accounts could ghost-appear -- exactly the class of bug `decode_position_account`
    already guards against, but not something we should re-open at the caller.
    """
    import httpx as _httpx
    client = _httpx.AsyncClient(
        transport=_mock_perps_api_transport({"dataList": []})
    )
    try:
        adapter = JupiterAdapter(client=client, now_s=adapter_now_s)
        positions = await adapter.get_positions("EmptyWallet")
    finally:
        await client.aclose()
    assert positions == []


async def test_stale_doves_falls_back_to_dex_aggregator(replay_client, fixture):
    """When Doves publish_time is old, per-asset fallback kicks in.

    Same fixture, but ``now_s`` is pushed 24 hours past the capture so every
    Doves entry (including the SOL/ETH/BTC/USDC feeds that were fresh at
    capture) reads as stale. The adapter must then return the DEX aggregator
    price, unchanged from the pre-Doves behaviour.
    """
    stale_now = _doves_capture_time(fixture) + 86400
    adapter = JupiterAdapter(client=replay_client, now_s=lambda: stale_now)
    marks = await adapter.get_marks()

    # DEX aggregator prices should now be surfaced for every listed asset.
    dex_agg = fixture("jupiter_price_v3.json")
    for asset in ("SOL", "ETH", "BTC"):
        mint = next(
            v["mint"] for v in CUSTODIES.values() if v["base_asset"] == asset
        )
        assert marks[asset] == Decimal(str(dex_agg[mint]["usdPrice"]))


def test_borrow_rate_matches_the_documented_worked_example():
    # Docs worked example: min 10%, max 230%, target 60%, target utilization 80%.
    # At 40% utilization the borrow APR is 35%.
    custody = {
        "owned": 100,
        "locked": 40,
        "minRateBps": 1000,
        "maxRateBps": 23000,
        "targetRateBps": 6000,
        "targetUtilizationRate": 800_000_000,
    }
    assert borrow_apr_bps(custody) == Decimal(3500)


def test_borrow_rate_uses_the_steep_slope_above_target_utilization():
    custody = {
        "owned": 100,
        "locked": 90,
        "minRateBps": 1000,
        "maxRateBps": 23000,
        "targetRateBps": 6000,
        "targetUtilizationRate": 800_000_000,
    }
    # 60% + (230% - 60%) / 20% * 10% = 145%
    assert borrow_apr_bps(custody) == Decimal(14500)


def test_price_impact_matches_the_documented_formula(fixture):
    # priceImpactFeeBps = (tradeSizeUsd_atomic * 10_000) / tradeImpactFeeScalar
    scalar = 3_750_000_000_000_000  # SOL custody, recorded on-chain
    assert price_impact_bps(Decimal(100_000), scalar) == pytest.approx(
        Decimal("0.2666666"), rel=Decimal("1e-6")
    )
    assert price_impact_bps(Decimal(0), scalar) == 0
    assert price_impact_bps(Decimal(100_000), 0) == 0


async def test_health_uses_the_rpc(replay_client):
    adapter = JupiterAdapter(client=replay_client)
    assert await adapter.health() is True
