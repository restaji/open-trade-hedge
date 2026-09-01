"""Base-asset normalization. Netting is wrong if any of these regress."""

from __future__ import annotations

import pytest

from hedge_scanner.assets import normalize_base_asset, normalize_quote_asset


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
