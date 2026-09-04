"""Pacifica adapter, driven by a recorded response for a real mainnet account."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner.adapters.pacifica import PacificaAdapter

ACCOUNT = "5X8BEVZ8kQSNyRyMBNYWaBUCD3a4azTNn1vnYenML35f"


def test_recorded_positions_are_a_real_unauthenticated_success(fixture):
    body = fixture("pacifica_positions.json")
    assert body["success"] is True
    assert body["data"], "the recorded account holds open positions"
    row = body["data"][0]
    assert set(row) >= {
        "symbol",
        "side",
        "amount",
        "entry_price",
        "funding",
        "isolated",
        "liquidation_price",
        "created_at",
    }
    assert row["side"] in {"bid", "ask"}


async def test_get_positions_normalizes_recorded_positions(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    positions = await adapter.get_positions(ACCOUNT)

    assert positions
    for position in positions:
        assert position.venue == "pacifica"
        assert position.address == ACCOUNT
        assert position.quote_asset == "USDC"
        assert position.side in {"long", "short"}
        assert position.size_base > 0
        assert position.mark_price > 0
        assert position.margin_mode in {"cross", "isolated"}
        sign = 1 if position.side == "long" else -1
        assert position.notional_usd * sign > 0


async def test_bid_maps_to_long_and_ask_to_short(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    positions = await adapter.get_positions(ACCOUNT)
    by_market = {p.market: p for p in positions}

    raw_sides = {p.market: p.raw["side"] for p in positions}
    for market, raw_side in raw_sides.items():
        expected = "long" if raw_side == "bid" else "short"
        assert by_market[market].side == expected


async def test_negative_liquidation_price_is_surfaced_as_absent(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    positions = await adapter.get_positions(ACCOUNT)

    # The recorded account is cross-margined with deeply negative liquidation
    # prices, which is Pacifica's way of saying "not liquidatable at any price".
    negative = [p for p in positions if Decimal(p.raw["liquidation_price"]) < 0]
    assert negative, "fixture should contain at least one negative liquidation price"
    for position in negative:
        assert position.liquidation_price is None


async def test_funding_paid_is_passed_through_signed(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    positions = await adapter.get_positions(ACCOUNT)
    for position in positions:
        assert position.funding_paid_usd == Decimal(position.raw["funding"])


async def test_quote_signs_funding_from_the_hedge_side(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    short = await adapter.get_quote("BTC", "short", Decimal(50_000))
    long = await adapter.get_quote("BTC", "long", Decimal(50_000))

    assert short.available is True
    assert short.market == "BTC"
    assert short.base_asset == "BTC"
    # Level 0 taker rate is 0.0004 = 4 bps in the recorded fee table.
    assert short.taker_fee_bps == Decimal("4")
    assert short.close_fee_bps == Decimal("4")
    # Same market, opposite hedge side, so the carry must flip sign.
    assert short.funding_rate_8h_bps == -long.funding_rate_8h_bps


async def test_quote_is_unavailable_for_an_unlisted_asset(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    quote = await adapter.get_quote("NOTATOKEN", "short", Decimal(1_000))
    assert quote.available is False


@pytest.mark.parametrize(
    "stamp,market,base",
    [
        ("EUR", "EURUSD", "EURUSD"),
        ("EURUSD", "EURUSD", "EURUSD"),
        ("EUR/USD", "EURUSD", "EURUSD"),
        ("GOLD", "XAU", "XAU"),
        ("XAU", "XAU", "XAU"),
        ("PEPE", "kPEPE", "PEPE"),
        ("WTI", "CL", "WTI"),
        ("US500", "SP500", "US500"),
        ("USD/JPY", "USDJPY", "USDJPY"),
    ],
)
async def test_quote_resolves_aliased_stamps(replay_client, stamp, market, base):
    adapter = PacificaAdapter(client=replay_client)
    quote = await adapter.get_quote(stamp, "short", Decimal(10_000))
    assert quote.available is True
    assert quote.market == market
    assert quote.base_asset == base


async def test_health(replay_client):
    adapter = PacificaAdapter(client=replay_client)
    assert await adapter.health() is True
