"""GRVT and Ondo: prove the gate is real, and that quotes still work anyway.

These two tests are the product finding, not a formality. If either venue ever
opens a public address-keyed position read, the recorded 401 fixture stops
matching and this file should fail loudly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner.adapters import GrvtAdapter, OndoAdapter, VenueRequiresAuthError

EVM_ADDRESS = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"


def test_grvt_position_read_was_recorded_as_401(fixture):
    recorded = fixture("grvt_positions_unauthenticated.json")
    assert recorded["status_code"] == 401
    assert recorded["body"]["code"] == 1000
    assert "authenticate" in recorded["body"]["message"].lower()


def test_ondo_position_read_was_recorded_as_401(fixture):
    recorded = fixture("ondo_positions_unauthenticated.json")
    assert recorded["status_code"] == 401
    assert recorded["body"]["error_code"] == "auth_missing"


async def test_grvt_positions_raise_a_typed_auth_error(replay_client):
    adapter = GrvtAdapter(client=replay_client)
    with pytest.raises(VenueRequiresAuthError) as exc:
        await adapter.get_positions(EVM_ADDRESS)
    assert exc.value.kind == "auth_required"
    assert exc.value.venue == "grvt"
    assert "API key" in exc.value.message


async def test_ondo_positions_raise_a_typed_auth_error(replay_client):
    adapter = OndoAdapter(client=replay_client)
    with pytest.raises(VenueRequiresAuthError) as exc:
        await adapter.get_positions(EVM_ADDRESS)
    assert exc.value.kind == "auth_required"
    assert exc.value.venue == "ondo"
    assert "API key" in exc.value.message


async def test_grvt_quote_works_without_credentials(replay_client):
    adapter = GrvtAdapter(client=replay_client)
    quote = await adapter.get_quote("BTC", "short", Decimal(100_000))

    assert quote.available is True
    assert quote.market == "BTC_USDT_Perp"
    assert quote.base_asset == "BTC"
    assert quote.taker_fee_bps == Decimal("4.5")
    assert quote.est_slippage_bps >= 0
    assert "[fee: static fallback, no API]" in quote.notes


async def test_grvt_funding_is_converted_from_percent_to_bps(replay_client, fixture):
    ticker = fixture("grvt_ticker_btc.json")["result"]
    published_percent = Decimal(ticker["funding_rate_8h_curr"])

    adapter = GrvtAdapter(client=replay_client)
    short = await adapter.get_quote("BTC", "short", Decimal(100_000))
    long = await adapter.get_quote("BTC", "long", Decimal(100_000))

    # GRVT publishes funding in percent per interval, already 8h-normalized.
    assert short.funding_rate_8h_bps == published_percent * 100
    assert long.funding_rate_8h_bps == -short.funding_rate_8h_bps


async def test_ondo_quote_works_without_credentials(replay_client):
    adapter = OndoAdapter(client=replay_client)
    quote = await adapter.get_quote("BTC", "short", Decimal(100_000))

    assert quote.available is True
    assert quote.market == "BTC-USD.P"
    assert quote.base_asset == "BTC"
    assert quote.taker_fee_bps > 0
    assert "promotional" in quote.notes


async def test_ondo_lists_equity_perps_not_just_crypto(fixture):
    contracts = fixture("ondo_contracts.json")["result"]
    bases = {row["baseCurrency"] for row in contracts}
    assert "BTC" in bases
    # Ondo's differentiator is tokenized-equity exposure; if these vanish the
    # hedge engine's RWA routing assumptions need revisiting.
    assert bases & {"AAPL", "NVDA", "TSLA", "SPY", "QQQ"}


async def test_ondo_quote_is_unavailable_for_an_unlisted_asset(replay_client):
    adapter = OndoAdapter(client=replay_client)
    quote = await adapter.get_quote("NOTATOKEN", "short", Decimal(1_000))
    assert quote.available is False
