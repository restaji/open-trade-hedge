"""Orderbook walk shared by the three orderbook venues.

The important property is the negative one: when the visible book cannot absorb
the requested size the walk must refuse to answer rather than return 0, because
a 0 would make the venue look free and could win a hedge ranking it should lose.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner.adapters import GrvtAdapter, OndoAdapter, PacificaAdapter
from hedge_scanner.adapters.base import walk_book


def test_fill_inside_the_touch_level_has_no_slippage():
    levels = [(Decimal(100), Decimal(50))]  # $5,000 available at the touch
    slippage, note = walk_book(levels, Decimal(1_000))
    assert slippage == 0
    assert "Walked" in note


def test_walking_deeper_levels_produces_positive_slippage():
    levels = [(Decimal(100), Decimal(10)), (Decimal(90), Decimal(10))]
    slippage, _ = walk_book(levels, Decimal(1_900))
    assert slippage > 0


def test_insufficient_depth_returns_none_not_zero():
    levels = [(Decimal(100), Decimal(1))]  # only $100 of depth
    slippage, note = walk_book(levels, Decimal(1_000_000))
    assert slippage is None
    assert "absorbs only" in note


def test_empty_book_returns_none():
    slippage, note = walk_book([], Decimal(1_000))
    assert slippage is None
    assert "empty" in note.lower()


def test_zero_and_negative_levels_are_skipped():
    levels = [(Decimal(0), Decimal(5)), (Decimal(100), Decimal(50))]
    slippage, _ = walk_book(levels, Decimal(1_000))
    assert slippage == 0


@pytest.mark.parametrize(
    ("adapter_cls", "asset"),
    [(PacificaAdapter, "BTC"), (GrvtAdapter, "BTC"), (OndoAdapter, "BTC")],
)
async def test_quote_goes_unavailable_when_the_book_is_too_thin(
    replay_client, adapter_cls, asset
):
    adapter = adapter_cls(client=replay_client)
    quote = await adapter.get_quote(asset, "short", Decimal(10_000_000_000))
    assert quote.available is False
    assert quote.est_slippage_bps == 0
    assert "absorbs only" in quote.notes


@pytest.mark.parametrize(
    "adapter_cls", [PacificaAdapter, GrvtAdapter, OndoAdapter]
)
async def test_quote_is_available_for_a_size_the_book_can_absorb(
    replay_client, adapter_cls
):
    adapter = adapter_cls(client=replay_client)
    quote = await adapter.get_quote("BTC", "short", Decimal(25_000))
    assert quote.available is True
    assert quote.est_slippage_bps >= 0
