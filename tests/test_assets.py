"""Base-asset normalization. Netting is wrong if any of these regress."""

from __future__ import annotations

import pytest

from hedge_scanner.assets import (
    normalize_base_asset,
    normalize_quote_asset,
    pair_base_asset,
)
from hedge_scanner.markets import canonical_base, same_asset


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # The contract's own example set
        ("WBTC", "BTC"),
        ("BTC", "BTC"),
        ("XBT", "BTC"),
        ("BTC-PERP", "BTC"),
        # Real venue-native symbols observed in the recorded fixtures
        ("BTC_USDT_Perp", "BTC"),  # GRVT
        ("BTC-USD.P", "BTC"),  # Ondo
        ("BTC", "BTC"),  # Pacifica
        ("BTC-PERP", "BTC"),  # Jupiter (synthesized)
        ("ETH_USDT_Perp", "ETH"),
        ("SOL-USD.P", "SOL"),
        # Wrapped and staked variants must net against the underlying
        ("wETH", "ETH"),
        ("wstETH", "ETH"),
        ("jitoSOL", "SOL"),
        ("cbBTC", "BTC"),
        # Case and whitespace
        ("  btc_usdt_perp ", "BTC"),
        # Tokens that merely start with W must survive intact
        ("WIF", "WIF"),
        ("WLD", "WLD"),
        ("WIF-USD.P", "WIF"),
        # Unknown assets pass through uppercased rather than being dropped
        ("FARTCOIN", "FARTCOIN"),
        ("", ""),
        # Hyperliquid HIP-3 sub-DEX namespacing (verified 2026-08-29).
        # `<dex>:<market>` must resolve to the underlying base so cross-venue
        # netting still works, or the tool silently double-books exposure.
        ("xyz:BRENTOIL", "BRENT"),
        ("xyz:GOLD", "XAU"),
        ("xyz:SILVER", "XAG"),
        ("xyz:AAPL", "AAPL"),
        ("xyz:CL", "WTI"),
        ("flx:SILVER", "XAG"),
        ("hyna:BTC", "BTC"),
        # Colons that are NOT HIP-3 (uppercase, unusual prefixes) must
        # continue through the old suffix-strip path unchanged.
        ("BTC:PERP", "BTC"),
    ],
)
def test_normalize_base_asset(symbol, expected):
    assert normalize_base_asset(symbol) == expected


def test_pair_base_asset_keeps_fx_legs_and_drops_crypto_quote():
    assert pair_base_asset("BTC", "USD") == "BTC"
    assert pair_base_asset("XAU", "USD") == "XAU"
    assert pair_base_asset("EUR", "USD") == "EURUSD"
    assert pair_base_asset("USD", "JPY") == "USDJPY"
    assert pair_base_asset("EUR", "GBP") == "EURGBP"
    assert pair_base_asset("EUR") == "EUR"
    assert pair_base_asset("xyz:GOLD", "USD") == "XAU"


def test_same_asset_fx_and_hip3():
    assert same_asset("EUR", "EURUSD")
    assert same_asset("EUR/USD", "EURUSD")
    assert not same_asset("EURGBP", "EURUSD")
    assert same_asset("xyz:GOLD", "XAU")
    assert same_asset("USD/JPY", "USDJPY")


def test_canonical_base_any_venue_stamp():
    """Ostium, HIP-3, and slash FX must share one book key with Avantis."""
    assert canonical_base("EUR") == "EURUSD"
    assert canonical_base("EUR/USD") == "EURUSD"
    assert canonical_base("EURUSD") == "EURUSD"
    assert canonical_base("USD/JPY") == "USDJPY"
    assert canonical_base("xyz:GOLD") == "XAU"
    assert canonical_base("GOLD") == "XAU"
    assert canonical_base("EURGBP") == "EURGBP"
    assert canonical_base("WBTC") == "BTC"


def test_hip3_fx_is_usd_quoted_not_a_cross():
    """xyz:EUR/GBP/JPY are USD books. EURGBP stays a separate Ostium-style cross."""
    assert canonical_base("xyz:EUR") == "EURUSD"
    assert canonical_base("xyz:GBP") == "GBPUSD"
    assert canonical_base("xyz:JPY") == "USDJPY"
    assert canonical_base("GBP") == "GBPUSD"
    assert canonical_base("JPY") == "USDJPY"
    assert canonical_base("GBPJPY") == "GBPJPY"
    assert canonical_base("EURGBP") == "EURGBP"
    assert same_asset("xyz:GBP", "GBPUSD")
    assert same_asset("xyz:JPY", "USDJPY")
    assert not same_asset("xyz:GBP", "GBPJPY")
    assert not same_asset("EURGBP", "EURUSD")


def test_uppercase_does_not_kill_hip3_when_canonical_base_runs_first():
    """``quote_hedge`` used to ``.upper()`` before resolve, turning xyz:GOLD into XYZ:GOLD."""
    assert canonical_base("xyz:GOLD") == "XAU"
    assert canonical_base("xyz:BRENTOIL") == "BRENT"
    # Uppercasing first is the bug; canonical_base itself must see the lowercase dex.
    assert canonical_base("XYZ:GOLD") != "XAU"


def test_usd_quote_variants():
    assert normalize_quote_asset("usdc") == "USDC"
    assert normalize_quote_asset("USDbC") == "USDC"
    assert normalize_quote_asset("USD") == "USD"


def test_every_recorded_pacifica_symbol_normalizes(fixture):
    rows = fixture("pacifica_info_prices.json")["data"]
    assert rows
    for row in rows:
        assert normalize_base_asset(row["symbol"])


def test_every_recorded_grvt_instrument_normalizes_to_its_base(fixture):
    rows = fixture("grvt_all_instruments.json")["result"]
    assert rows
    for row in rows:
        assert normalize_base_asset(row["instrument"]) == normalize_base_asset(
            row["base"]
        )
