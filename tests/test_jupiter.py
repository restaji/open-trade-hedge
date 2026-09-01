"""Jupiter adapter, driven by a recorded mainnet getProgramAccounts response."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from hedge_scanner.adapters.jupiter import (
    CUSTODIES,
    POSITION_ACCOUNT_SIZE,
    POSITION_DISCRIMINATOR,
    POSITION_DISCRIMINATOR_B58,
    PROGRAM_ID,
    JupiterAdapter,
    borrow_apr_bps,
    decode_custody_account,
    decode_position_account,
    price_impact_bps,
)

WALLET = "2JVs9RekjARxu9tRYq8Dbq2eGNRegzRSGJMrCBXKj8ti"


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


async def test_get_positions_normalizes_recorded_mainnet_positions(replay_client):
    adapter = JupiterAdapter(client=replay_client)
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


async def test_short_and_long_both_present_and_signed_correctly(replay_client):
    adapter = JupiterAdapter(client=replay_client)
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


async def test_quote_is_unavailable_for_an_unlisted_asset(replay_client):
    adapter = JupiterAdapter(client=replay_client)
    quote = await adapter.get_quote("DOGE", "short", Decimal(10_000))
    assert quote.available is False
    assert quote.borrow_rate_8h_bps == Decimal(0)


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
