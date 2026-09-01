"""Tests for the Avantis hedge-pricing module.

The pins that matter most are the ones that could invert a recommendation:
maker-vs-taker resolution per hedge direction, and the funding sign. Getting
either backwards would tell a user their hedge is cheap when it is expensive, so
both are tested against a known live skew in both directions.

Fixtures under ``tests/fixtures/avantis/`` are real recorded responses; see
``capture_meta.json`` for the URLs and capture timestamp.

CONTRACT RECONCILIATION
-----------------------
``hedge_scanner.models.Quote`` exists and is imported directly, so no local
fallback is needed. Two notes for the model owner:
  * ``Quote`` already carries the section-9 ``base_asset`` addition, which this
    module populates.
  * Avantis needs fields the canonical ``Quote`` has no home for (which side of
    the fee schedule the leg landed on, whether a 0 bps rate is promotional, the
    Upside profit-share schedule). Rather than edit ``models.py``, this module
    returns ``AvantisQuote``, a subclass, so ``isinstance(q, Quote)`` holds and
    the engine consumes it unchanged. If ``Quote`` later absorbs these fields,
    delete the subclass.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from hedge_scanner.hedge_venues import avantis
from hedge_scanner.models import Quote

FIXTURES = Path(__file__).parent / "fixtures" / "avantis"


# --------------------------------------------------------------------------
# Fixture loading
# --------------------------------------------------------------------------


def _load(name: str):
    return avantis._loads_exact((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def snapshot():
    return _load("trading_v2.json")


@pytest.fixture(scope="module")
def prices():
    raw = _load("last_price.json")
    return {int(r["pairIndex"]): Decimal(str(r["c"])) for r in raw}


@pytest.fixture(scope="module")
def spread_quotes():
    return json.loads((FIXTURES / "spread_quotes.json").read_text())


@pytest.fixture
def offline(monkeypatch, snapshot, prices, spread_quotes):
    """Serve the recorded fixtures instead of the network.

    Spread lookups are keyed on the exact ``(pairIndex, coinSize10, side, open)``
    tuple that was recorded, so a size the fixture never captured returns
    ``None`` -- the same "refuse to quote" path production takes, and never a
    silent zero.
    """
    avantis.clear_caches()

    async def fake_snapshot(client=None):
        return snapshot

    async def fake_prices(client=None):
        return prices

    async def fake_spread(pair_index, coin_size, is_long, is_open, client=None):
        key = "|".join([
            str(int(pair_index)),
            str(int(coin_size * avantis.PRECISION_10)),
            "long" if is_long else "short",
            "open" if is_open else "close",
        ])
        entry = spread_quotes.get(key)
        if entry is None:
            return None
        return avantis.parse_spread_response(
            avantis._loads_exact(json.dumps(entry["response"]))
        )

    monkeypatch.setattr(avantis, "fetch_trading_snapshot", fake_snapshot)
    monkeypatch.setattr(avantis, "fetch_prices", fake_prices)
    monkeypatch.setattr(avantis, "fetch_spread_bps", fake_spread)
    yield
    avantis.clear_caches()


def _pair(snapshot, symbol: str) -> dict:
    for record in snapshot["pairInfos"].values():
        if f"{record.get('from')}/{record.get('to')}" == symbol:
            return record
    raise AssertionError(f"{symbol} missing from fixture")


def _notional_10k_coin(prices, pair_index: int) -> Decimal:
    return Decimal("10000") / prices[pair_index]


# --------------------------------------------------------------------------
# THE CENTRAL MECHANIC: maker vs taker from OI skew, per hedge direction
# --------------------------------------------------------------------------


def test_fixture_btc_is_long_heavy(snapshot):
    """Anchor for the skew tests below: BTC's recorded book leans long."""
    coin_oi = _pair(snapshot, "BTC/USD")["coinOI"]
    long_share = Decimal(str(coin_oi["long"])) / (
        Decimal(str(coin_oi["long"])) + Decimal(str(coin_oi["short"]))
    )
    assert long_share > Decimal("0.5")
    assert Decimal(str(coin_oi["long"])) == Decimal("27.2756499444")
    assert Decimal(str(coin_oi["short"])) == Decimal("25.6229739754")


def test_short_hedge_into_long_heavy_book_is_maker(snapshot, prices):
    """A user long BTC elsewhere hedges SHORT here; that improves skew -> maker.

    This is the product's central claim. If it ever flips, the Avantis-first
    pitch is wrong.
    """
    record = _pair(snapshot, "BTC/USD")
    fees = record["additionalPairParams2"]
    result = avantis.classify_skew_fee(
        Decimal(str(record["coinOI"]["long"])),
        Decimal(str(record["coinOI"]["short"])),
        _notional_10k_coin(prices, 1),
        "short",
        Decimal(str(fees["openMakerFeeP"])),
        Decimal(str(fees["openTakerFeeP"])),
    )
    assert result.tier == "maker"
    assert result.fee_pct * Decimal(100) == Decimal("1.00")
    # The long share moved toward balance but did not cross it.
    assert result.long_share_after < result.long_share_before
    assert result.long_share_after > Decimal("0.5")


def test_long_hedge_into_long_heavy_book_is_taker(snapshot, prices):
    """A user short BTC elsewhere hedges LONG here; that worsens skew -> taker."""
    record = _pair(snapshot, "BTC/USD")
    fees = record["additionalPairParams2"]
    result = avantis.classify_skew_fee(
        Decimal(str(record["coinOI"]["long"])),
        Decimal(str(record["coinOI"]["short"])),
        _notional_10k_coin(prices, 1),
        "long",
        Decimal(str(fees["openMakerFeeP"])),
        Decimal(str(fees["openTakerFeeP"])),
    )
    assert result.tier == "taker"
    assert result.fee_pct * Decimal(100) == Decimal("4.50")
    assert result.long_share_after > result.long_share_before


def test_maker_taker_flips_with_the_book_not_the_order_type(snapshot, prices):
    """Same short hedge is a TAKER once the book is short-heavy.

    Guards against the classification being accidentally hardcoded to side.
    """
    record = _pair(snapshot, "BTC/USD")
    fees = record["additionalPairParams2"]
    maker, taker = Decimal(str(fees["openMakerFeeP"])), Decimal(str(fees["openTakerFeeP"]))
    size = _notional_10k_coin(prices, 1)
    # Mirror the recorded book so shorts are now the crowded side.
    heavy_short = avantis.classify_skew_fee(
        Decimal(str(record["coinOI"]["short"])),
        Decimal(str(record["coinOI"]["long"])),
        size, "short", maker, taker,
    )
    assert heavy_short.tier == "taker"
    heavy_short_long_leg = avantis.classify_skew_fee(
        Decimal(str(record["coinOI"]["short"])),
        Decimal(str(record["coinOI"]["long"])),
        size, "long", maker, taker,
    )
    assert heavy_short_long_leg.tier == "maker"


def test_oversized_hedge_that_crosses_balance_is_mixed():
    """A leg big enough to flip the book pays the size-weighted blend."""
    result = avantis.classify_skew_fee(
        Decimal("60"), Decimal("40"), Decimal("40"), "short",
        Decimal("0.01"), Decimal("0.045"),
    )
    assert result.tier == "mixed"
    # (maker * (60-40) + taker * (40 - 60 + 40)) / 40
    expected = (Decimal("0.01") * Decimal("20") + Decimal("0.045") * Decimal("20")) / Decimal("40")
    assert result.fee_pct == expected
    assert Decimal("0.01") < result.fee_pct < Decimal("0.045")


def test_empty_book_prices_as_taker():
    """Avantis' own rule: with no OI there is no skew to improve."""
    result = avantis.classify_skew_fee(
        Decimal("0"), Decimal("0"), Decimal("1"), "short",
        Decimal("0.01"), Decimal("0.045"),
    )
    assert result.tier == "taker"
    assert result.fee_pct == Decimal("0.045")


def test_close_reverses_the_open_classification_at_unchanged_skew():
    """Unwinding a skew-improving leg worsens skew, so maker open -> taker close."""
    maker, taker = Decimal("0.01"), Decimal("0.045")
    long_oi, short_oi, size = Decimal("27.3"), Decimal("25.45"), Decimal("0.1546")
    opening = avantis.classify_skew_fee(long_oi, short_oi, size, "short", maker, taker)
    closing = avantis.classify_skew_fee(
        long_oi, short_oi + size, size, "short", maker, taker, reduce_side=True
    )
    assert opening.tier == "maker"
    assert closing.tier == "taker"


# --------------------------------------------------------------------------
# Funding sign: positive = hedger RECEIVES (CONTRACT.md section 4)
# --------------------------------------------------------------------------


def test_funding_sign_short_hedger_receives(snapshot):
    """Recorded ETH: short is negative on the wire -> the hedger RECEIVES -> positive."""
    funding = _pair(snapshot, "ETH/USD")["fundingRate"]
    assert Decimal(str(funding["short"])) < 0          # wire: negative = that side receives
    value = avantis.funding_8h_bps_for_side(funding, "short")
    assert value > 0                                    # contract: positive = hedger receives
    assert value == -Decimal(str(funding["short"])) * Decimal(8) * Decimal(100)


def test_funding_sign_long_hedger_pays(snapshot):
    """Recorded ETH: long is positive on the wire -> the hedger PAYS -> negative."""
    funding = _pair(snapshot, "ETH/USD")["fundingRate"]
    assert Decimal(str(funding["long"])) > 0
    value = avantis.funding_8h_bps_for_side(funding, "long")
    assert value < 0
    assert value == Decimal("-0.15998400")


def test_funding_sign_mapping_holds_in_the_opposite_configuration(snapshot):
    """Recorded BTC has the mirror configuration: LONG receives, SHORT pays.

    Pinning both configurations from real data is what proves the sign flip is a
    mapping and not a per-side constant.
    """
    funding = _pair(snapshot, "BTC/USD")["fundingRate"]
    assert Decimal(str(funding["long"])) < 0
    assert Decimal(str(funding["short"])) > 0
    assert avantis.funding_8h_bps_for_side(funding, "long") > 0     # long receives
    assert avantis.funding_8h_bps_for_side(funding, "short") < 0     # short pays
    assert avantis.funding_8h_bps_for_side(funding, "long") == Decimal("0.08006400")


def test_both_funding_sides_are_read_not_negated(snapshot):
    """The two sides are published independently and are NOT negations."""
    for symbol in ("BTC/USD", "ETH/USD", "NVDA/USD"):
        funding = _pair(snapshot, symbol)["fundingRate"]
        long_rate = Decimal(str(funding["long"]))
        short_rate = Decimal(str(funding["short"]))
        assert long_rate != -short_rate, symbol
        long_bps = avantis.funding_8h_bps_for_side(funding, "long")
        short_bps = avantis.funding_8h_bps_for_side(funding, "short")
        assert long_bps != -short_bps, symbol
        # Exactly one side receives.
        assert (long_bps > 0) != (short_bps > 0), symbol


def test_funding_sign_is_independent_of_oi_skew(snapshot):
    """The heavier side is NOT reliably the payer, so funding must be fetched.

    Recorded BTC is long-heavy yet LONGS receive; recorded ETH is short-heavy yet
    SHORTS receive. Crypto funding is anchored to external venues (Binance /
    Hyperliquid), so it is not a function of Avantis' internal skew. Deriving the
    funding sign from skew -- or from the maker/taker result -- would be wrong.
    """
    findings = {}
    for symbol in ("BTC/USD", "ETH/USD"):
        record = _pair(snapshot, symbol)
        long_oi = Decimal(str(record["coinOI"]["long"]))
        short_oi = Decimal(str(record["coinOI"]["short"]))
        heavier = "long" if long_oi > short_oi else "short"
        funding = record["fundingRate"]
        receiver = "long" if avantis.funding_8h_bps_for_side(funding, "long") > 0 else "short"
        findings[symbol] = (heavier, receiver)
    # At least one pair has the heavy side receiving, which the naive
    # "heavier side pays" model forbids.
    assert any(heavier == receiver for heavier, receiver in findings.values()), findings


def test_missing_funding_field_is_not_treated_as_zero():
    assert avantis.funding_8h_bps_for_side({"long": Decimal("0.001")}, "short") is None


def test_zero_funding_is_distinguishable_from_missing(snapshot):
    """XAU genuinely quotes 0 funding; that must read as 0, not as unavailable."""
    funding = _pair(snapshot, "XAU/USD")["fundingRate"]
    assert avantis.funding_8h_bps_for_side(funding, "long") == Decimal(0)


# --------------------------------------------------------------------------
# Borrow-fee unit conversion: marginFee is % PER HOUR
# --------------------------------------------------------------------------


def test_borrow_unit_derivation_annualises_to_two_percent(snapshot):
    """The identity that establishes the unit: 0.00022824 x 8760 = 2.00%/yr."""
    margin = _pair(snapshot, "BTC/USD")["marginFee"]
    rate = Decimal(str(margin["long"]))
    assert rate == Decimal("0.00022824")
    annual = avantis.pct_per_hour_to_annual_pct(rate)
    assert annual == Decimal("1.9993824")
    assert abs(annual - Decimal("2.00")) < Decimal("0.001")


def test_borrow_8h_conversion_is_hours_times_pct_to_bps(snapshot):
    margin = _pair(snapshot, "BTC/USD")["marginFee"]
    assert avantis.borrow_8h_bps_for_side(margin, "long") == Decimal("0.18259200")
    assert avantis.borrow_8h_bps_for_side(margin, "short") == Decimal("0.18259200")


def test_borrow_unit_cross_checks_on_xau(snapshot):
    """Independent cross-check on a pair with a very different rate."""
    margin = _pair(snapshot, "XAU/USD")["marginFee"]
    annual_long = avantis.pct_per_hour_to_annual_pct(Decimal(str(margin["long"])))
    annual_short = avantis.pct_per_hour_to_annual_pct(Decimal(str(margin["short"])))
    assert Decimal("10.7") < annual_long < Decimal("10.8")
    assert Decimal("2.4") < annual_short < Decimal("2.6")
    # Per-side asymmetry is real and must not be collapsed to one number.
    assert annual_long != annual_short


def test_borrow_is_always_a_cost_never_negative():
    assert avantis.borrow_8h_bps_for_side({"long": Decimal("-0.0005")}, "long") > 0


def test_missing_margin_fee_is_not_treated_as_zero():
    assert avantis.borrow_8h_bps_for_side({"long": Decimal("0.0002")}, "short") is None


# --------------------------------------------------------------------------
# RWA promotional 0 bps detection
# --------------------------------------------------------------------------


def test_rwa_pairs_read_zero_commission_from_live_data(snapshot):
    for symbol in ("XAU/USD", "EUR/USD", "NVDA/USD", "WTI/USD", "US500/USD"):
        fees = _pair(snapshot, symbol)["additionalPairParams2"]
        assert all(
            Decimal(str(fees[k])) == 0
            for k in ("openMakerFeeP", "openTakerFeeP", "closeMakerFeeP", "closeTakerFeeP")
        ), symbol


def test_crypto_pairs_are_not_zero_commission(snapshot):
    for symbol in ("BTC/USD", "ETH/USD", "SOL/USD"):
        fees = _pair(snapshot, symbol)["additionalPairParams2"]
        assert Decimal(str(fees["openMakerFeeP"])) == Decimal("0.01"), symbol
        assert Decimal(str(fees["openTakerFeeP"])) == Decimal("0.045"), symbol


def test_asset_type_alone_would_misclassify(snapshot):
    """Why the zero-fee flag is derived from fees, not from ``assetType``.

    27 live records carry a blank ``assetType``, mixing crypto pairs in with
    RWAs, so an asset-class list would misprice them.
    """
    blank = [
        record for record in snapshot["pairInfos"].values()
        if not record.get("feed", {}).get("attributes", {}).get("assetType")
    ]
    assert blank, "fixture should contain blank-assetType records"
    charged = [r for r in blank if Decimal(str(r["additionalPairParams2"]["openTakerFeeP"])) > 0]
    free = [r for r in blank if Decimal(str(r["additionalPairParams2"]["openTakerFeeP"])) == 0]
    assert charged and free


def test_rwa_quote_is_flagged_promotional_and_revocable(offline, snapshot):
    quote = asyncio.run(avantis.quote_hedge("XAU", "short", Decimal("10000"), Decimal("24")))
    assert quote.available is True
    assert quote.promotional_zero_fee is True
    assert quote.taker_fee_bps == Decimal(0)
    assert quote.close_fee_bps == Decimal(0)
    assert "PROMOTIONAL" in quote.notes
    assert "REVOCABLE" in quote.notes
    # Zero commission must not be mistaken for a free hedge.
    #
    # §12.9: the Quote's borrow_rate_8h_bps is now 0 by policy, but Avantis's
    # underlying marginFee is still non-zero (that's why we care about excluding
    # it). Both facts matter for the "not free" claim.
    assert quote.borrow_rate_8h_bps == Decimal(0)
    raw_margin = avantis.borrow_8h_bps_for_side(
        _pair(snapshot, "XAU/USD")["marginFee"], "short"
    )
    assert raw_margin > 0, "Avantis still publishes a real marginFee for XAU"
    assert quote.price_impact_bps > 0
    assert quote.all_in_cost_bps > 0


def test_crypto_quote_is_not_flagged_promotional(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.promotional_zero_fee is False
    # CONTRACT.md §12.8: both legs read the pair's live maker rate --
    # openMakerFeeP (1.0 bps on crypto) and closeMakerFeeP (1.0 bps).
    assert quote.taker_fee_bps == Decimal("1.000")
    assert quote.close_fee_bps == Decimal("1.000")


# --------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------


def test_below_pair_minimum_is_rejected_with_a_reason(offline):
    """XAU's live minimum is 300 USDC; a $150 hedge must be refused, not priced."""
    quote = asyncio.run(avantis.quote_hedge("XAU", "short", Decimal("150"), Decimal("24")))
    assert quote.available is False
    assert "below the Avantis minimum" in quote.notes
    assert "300" in quote.notes
    assert quote.min_position_usd == Decimal("300")


def test_at_pair_minimum_is_accepted(offline, snapshot):
    """Boundary: exactly the minimum is allowed, so the check is not off by one."""
    record = _pair(snapshot, "XAU/USD")
    reason = avantis._tradability_reason(record, Decimal("300"), "XAU/USD")
    assert reason is None
    assert avantis._tradability_reason(record, Decimal("299.99"), "XAU/USD") is not None


def test_crypto_minimum_is_100_not_300(offline, snapshot):
    record = _pair(snapshot, "BTC/USD")
    assert Decimal(str(record["minLevPosUSDC"])) == Decimal("100")
    assert avantis._tradability_reason(record, Decimal("100"), "BTC/USD") is None
    assert avantis._tradability_reason(record, Decimal("99"), "BTC/USD") is not None


def test_unlisted_asset_returns_none(offline):
    assert asyncio.run(
        avantis.quote_hedge("DOGECOINDOESNOTEXIST", "short", Decimal("10000"), Decimal("24"))
    ) is None


def test_close_only_mode_blocks_a_new_hedge(snapshot):
    record = dict(_pair(snapshot, "BTC/USD"))
    record["additionalPairParams2"] = dict(record["additionalPairParams2"], closeOnlyMode=True)
    reason = avantis._tradability_reason(record, Decimal("10000"), "BTC/USD")
    assert reason is not None and "close-only" in reason


def test_missing_minimum_refuses_rather_than_guessing(snapshot):
    record = {k: v for k, v in _pair(snapshot, "BTC/USD").items()
              if k not in ("minLevPosUSDC", "pairMinLevPosUSDC")}
    reason = avantis._tradability_reason(record, Decimal("10000"), "BTC/USD")
    assert reason is not None and "refusing to guess" in reason


def test_profit_cap_is_surfaced(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.max_gain_pct_of_collateral == Decimal("2500")


def test_invalid_side_is_rejected(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "buy", Decimal("10000"), Decimal("24")))
    assert quote.available is False
    assert "Invalid hedge side" in quote.notes


# --------------------------------------------------------------------------
# Never fabricate a rate
# --------------------------------------------------------------------------


def test_missing_fee_field_yields_unavailable_not_zero(monkeypatch, snapshot, prices, offline):
    """Both legs read the maker fields (§12.8), so a missing openMakerFeeP -- or a
    missing closeMakerFeeP -- must refuse to quote rather than default to zero or
    fall back to the taker rate.
    """
    stripped = json.loads(json.dumps(snapshot, default=str))
    for record in stripped["pairInfos"].values():
        if record.get("from") == "BTC" and record.get("to") == "USD":
            record["additionalPairParams2"].pop("openMakerFeeP")
    reparsed = avantis._loads_exact(json.dumps(stripped))

    async def fake_snapshot(client=None):
        return reparsed

    monkeypatch.setattr(avantis, "fetch_trading_snapshot", fake_snapshot)
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.available is False
    assert "openMakerFeeP/closeMakerFeeP" in quote.notes


def test_unquotable_spread_yields_unavailable_not_zero(monkeypatch, offline):
    async def no_spread(pair_index, coin_size, is_long, is_open, client=None):
        return None

    monkeypatch.setattr(avantis, "fetch_spread_bps", no_spread)
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.available is False
    assert "rather than assuming zero spread" in quote.notes


def test_spread_engine_refusal_codes_are_not_zero():
    """403/404 from the spread engine mean 'do not execute', never 'free'."""
    assert avantis.parse_spread_response({}) is None
    assert avantis.parse_spread_response(None) is None
    assert avantis.parse_spread_response({"spreadPctWithoutFlow10": "0"}) is None


def test_spread_descaling_is_exact():
    payload = {"estimatedSpreadPctWithFlow10": "119519610"}
    # 119519610 / 1e10 = 0.011951961 percent = 1.1951961 bps
    assert avantis.parse_spread_response(payload) == Decimal("1.1951961000")


# --------------------------------------------------------------------------
# Spread must be quoted, not modelled
# --------------------------------------------------------------------------


def test_spread_is_directional_at_equal_size(spread_quotes):
    long_open = [v for k, v in spread_quotes.items()
                 if v["pair"] == "BTC/USD" and k.endswith("|long|open")][0]
    short_open = [v for k, v in spread_quotes.items()
                  if v["pair"] == "BTC/USD" and k.endswith("|short|open")][0]
    long_bps = avantis.parse_spread_response(long_open["response"])
    short_bps = avantis.parse_spread_response(short_open["response"])
    assert long_bps != short_bps


def test_spread_is_non_monotonic_in_size(spread_quotes):
    """Recorded ETH open-long: 1 unit 2.12 bps, ~5.24 units 1.00 bps, 10 units 2.37 bps.

    A single fitted curve cannot reproduce this, which is why every quote is a
    live request.
    """
    eth = {
        Decimal(k.split("|")[1]) / avantis.PRECISION_10: avantis.parse_spread_response(v["response"])
        for k, v in spread_quotes.items()
        if v["pair"] == "ETH/USD" and k.endswith("|long|open")
    }
    assert len(eth) >= 3
    sizes = sorted(eth)
    values = [eth[s] for s in sizes]
    assert values != sorted(values), f"expected non-monotonic spread, got {list(zip(sizes, values))}"


# --------------------------------------------------------------------------
# End-to-end quotes and the contract's cost formula
# --------------------------------------------------------------------------


def test_both_directions_price_both_legs_maker(offline):
    """Both long and short read openMakerFeeP + closeMakerFeeP (2.0 bps round trip).

    Product decision 2026-08-30 (CONTRACT.md §12.8): both legs price at the
    pair's live maker commission, so the round trip is direction-independent.
    Neither rate is hardcoded -- both come off `additionalPairParams2`.
    """
    short = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    long = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("24")))
    assert short.fee_tier == "maker"
    assert long.fee_tier == "maker"
    assert short.taker_fee_bps == Decimal("1.000")
    assert long.taker_fee_bps == Decimal("1.000")
    assert short.close_fee_bps == Decimal("1.000")
    assert long.close_fee_bps == Decimal("1.000")
    assert short.taker_fee_bps + short.close_fee_bps == Decimal("2.000")
    assert long.taker_fee_bps + long.close_fee_bps == Decimal("2.000")


def test_maker_round_trip_is_read_from_the_pair_record_not_hardcoded(snapshot, prices, monkeypatch, offline):
    """The 2.0 bps round trip must track the live record, not a constant.

    Doubling both maker fields on the snapshot must double the quoted round
    trip. A hardcoded 1.0/1.0 would leave it unchanged and fail here.
    """
    bumped = json.loads(json.dumps(snapshot, default=str))
    for record in bumped["pairInfos"].values():
        if record.get("from") == "BTC" and record.get("to") == "USD":
            fees = record["additionalPairParams2"]
            fees["openMakerFeeP"] = "0.02"
            fees["closeMakerFeeP"] = "0.03"
    reparsed = avantis._loads_exact(json.dumps(bumped))

    async def fake_snapshot(client=None):
        return reparsed

    monkeypatch.setattr(avantis, "fetch_trading_snapshot", fake_snapshot)
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.taker_fee_bps == Decimal("2.000")
    assert quote.close_fee_bps == Decimal("3.000")


def test_positive_carry_is_reported_only_when_funding_actually_pays(offline):
    """Fee tier is always maker; funding direction is an independent input.

    The maker-hedge decision fixes commission; it does not imply positive carry.
    CONTRACT.md §7.6(a) is explicit that funding is anchored to external
    venues on crypto and is NOT a consequence of the maker/taker classification.
    """
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.fee_tier == "maker"
    assert quote.funding_rate_8h_bps < 0
    other = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("24")))
    assert other.fee_tier == "maker"
    assert other.funding_rate_8h_bps > 0


def test_all_in_cost_matches_the_contract_formula(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    expected = (
        quote.taker_fee_bps
        + quote.close_fee_bps
        + quote.price_impact_bps
        + quote.est_slippage_bps
        + (quote.borrow_rate_8h_bps - quote.funding_rate_8h_bps) * Decimal("24") / Decimal("8")
    )
    assert quote.all_in_cost_bps == expected
    assert quote.all_in_cost_usd == expected * Decimal("10000") / Decimal("10000")


def test_carry_excludes_margin_fee_by_policy(offline, snapshot):
    """§12.9: Avantis hedge quotes must compute carry from funding alone.

    Regression guard for the product decision to zero out `marginFee` in the
    Quote (Avantis stopped applying it on-chain but the API still reports it).
    Three invariants:
      1. `Quote.borrow_rate_8h_bps == 0` (what the engine actually reads).
      2. Avantis's raw `marginFee.<side>` is still non-zero in the API fixture
         (so we know the exclusion isn't hiding a data-missing bug).
      3. The excluded rate is surfaced in the notes text so the transition-period
         reconciliation is transparent.
    Flipping `_INCLUDE_MARGIN_FEE_IN_CARRY` back to True would fail this test.
    """
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.borrow_rate_8h_bps == Decimal(0)
    raw = avantis.borrow_8h_bps_for_side(_pair(snapshot, "BTC/USD")["marginFee"], "short")
    assert raw > 0, "Avantis's fixture still publishes non-zero marginFee.short"
    assert "EXCLUDED per §12.9" in quote.notes
    assert str(raw) in quote.notes  # the excluded rate must be visible to the user

    # And the engine's carry math on the returned Quote must reduce to -funding:
    carry_8h = quote.borrow_rate_8h_bps - quote.funding_rate_8h_bps
    assert carry_8h == -quote.funding_rate_8h_bps


def test_quote_conforms_to_the_canonical_schema(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert isinstance(quote, Quote)
    assert quote.venue == "avantis"
    assert quote.market == "BTC/USD"
    assert quote.base_asset == "BTC"
    assert quote.side == "short"
    for name in ("taker_fee_bps", "close_fee_bps", "price_impact_bps",
                 "funding_rate_8h_bps", "borrow_rate_8h_bps", "est_slippage_bps",
                 "notional_usd"):
        assert isinstance(getattr(quote, name), Decimal), name


def test_carry_scales_with_horizon(offline):
    """Carry component must scale linearly with hold time (both signs).

    Historically this test asserted "cost grows with hold" on BTC long, which
    happened to be true under Avantis's borrow+funding model because borrow rent
    dominated the tiny received funding. Under §12.9 (funding-only), the same
    BTC long leg is a net receiver, so total cost DROPS with hold. The
    invariant the test actually cares about -- carry is linear in horizon --
    is direction-agnostic and holds under both regimes.
    """
    short_hold = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("8")))
    long_hold = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("720")))
    # Same fixed portion (fees + spread), so the delta is 100% carry.
    delta = long_hold.all_in_cost_bps - short_hold.all_in_cost_bps
    per_period = short_hold.borrow_rate_8h_bps - short_hold.funding_rate_8h_bps
    # 720 h - 8 h = 712 h = 89 eight-hour periods.
    assert delta == per_period * Decimal("89")

    # And the paying-side test that the old assertion meant to make: on a leg
    # that genuinely pays net (BTC short under this fixture), longer horizon
    # really does cost more.
    short_hold_short = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("8")))
    long_hold_short = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("720")))
    assert long_hold_short.all_in_cost_bps > short_hold_short.all_in_cost_bps


def test_close_fee_base_is_notional_plus_pnl(offline):
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote.close_fee_base == "notional + grossPnL"
    flat = avantis.close_fee_usd(quote.close_fee_bps, Decimal("10000"), Decimal("0"))
    winner = avantis.close_fee_usd(quote.close_fee_bps, Decimal("10000"), Decimal("1000"))
    loser = avantis.close_fee_usd(quote.close_fee_bps, Decimal("10000"), Decimal("-1000"))
    assert winner > flat > loser
    assert flat == quote.close_fee_bps * Decimal("10000") / Decimal("10000")


def test_a_hedge_can_be_expensive_and_the_quote_says_so(offline):
    """No Avantis bias: a 2.0 bps commission does not make the hedge cheap.

    Commission is direction-independent under §12.8, so spread + carry is what
    makes any given hedge expensive or cheap -- and the all-in cost must exceed
    the commission by enough that the quote cannot be read as near-free.
    """
    quote = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("24")))
    assert quote.available is True
    assert quote.fee_tier == "maker"
    # 2.0 bps commission, plus positive spread and (for this direction)
    # positive borrow.
    assert quote.all_in_cost_bps > quote.taker_fee_bps + quote.close_fee_bps
    assert "openMakerFeeP" in quote.notes
    assert "closeMakerFeeP" in quote.notes
    # §12.8 point 2: the taker-close alternative must be disclosed, so the
    # 2.0 bps figure is never presented as guaranteed.
    assert "ASSUMPTION" in quote.notes
    assert "closeTakerFeeP" in quote.notes
    assert quote.funding_rate_8h_bps > 0
    assert quote.all_in_cost_bps > quote.taker_fee_bps


# --------------------------------------------------------------------------
# Upside Perps
# --------------------------------------------------------------------------


def test_upside_profit_share_schedule_matches_documented_bands(snapshot):
    """Live pnlFees collapses to the documented 25 / 20 / 10 / 5 bands."""
    record = _pair(snapshot, "BTC_UPSIDE/USD")
    schedule = avantis.profit_share_schedule(record["pnlFees"])
    assert schedule == [
        (Decimal("1"), Decimal("25")),
        (Decimal("500"), Decimal("20")),
        (Decimal("1500"), Decimal("10")),
        (Decimal("2500"), Decimal("5")),
    ]


def test_upside_profit_share_lookup_by_roi(snapshot):
    schedule = avantis.profit_share_schedule(_pair(snapshot, "BTC_UPSIDE/USD")["pnlFees"])
    assert avantis.profit_share_pct_for_roi(schedule, Decimal("100")) == Decimal("25")
    assert avantis.profit_share_pct_for_roi(schedule, Decimal("500")) == Decimal("20")
    assert avantis.profit_share_pct_for_roi(schedule, Decimal("1500")) == Decimal("10")
    assert avantis.profit_share_pct_for_roi(schedule, Decimal("3000")) == Decimal("5")
    # A losing hedge costs nothing at all.
    assert avantis.profit_share_pct_for_roi(schedule, Decimal("-50")) is None


def test_upside_borrow_fee_is_genuinely_zero(snapshot):
    margin = _pair(snapshot, "BTC_UPSIDE/USD")["marginFee"]
    assert avantis.borrow_8h_bps_for_side(margin, "long") == Decimal(0)
    assert avantis.borrow_8h_bps_for_side(margin, "short") == Decimal(0)


def test_upside_quote_states_cost_is_pnl_contingent(offline):
    quote = asyncio.run(avantis.quote_upside_hedge("BTC", "short", Decimal("10000")))
    assert quote.available is True
    assert quote.market == "BTC_UPSIDE/USD"
    assert quote.borrow_rate_8h_bps == Decimal(0)
    assert quote.profit_share_schedule
    assert "PnL-CONTINGENT" in quote.notes
    assert "Zero cost if the hedge closes at a loss" in quote.notes
    # No invented expected-cost-in-bps.
    assert quote.all_in_cost_bps is None


def test_upside_records_the_open_close_fee_conflict(offline):
    """Live pair record has openTakerFeeP/closeTakerFeeP which we use directly."""
    quote = asyncio.run(avantis.quote_upside_hedge("BTC", "short", Decimal("10000")))
    assert "openTakerFeeP" in quote.notes
    assert quote.taker_fee_bps > 0
    assert quote.fee_tier == "taker"
    assert quote.taker_fee_bps == Decimal("4.500")


def test_upside_unavailable_for_assets_without_an_upside_pair(offline):
    assert asyncio.run(avantis.quote_upside_hedge("XAU", "short", Decimal("10000"))) is None


def test_upside_respects_the_pair_minimum(offline):
    quote = asyncio.run(avantis.quote_upside_hedge("BTC", "short", Decimal("50")))
    assert quote.available is False
    assert "below the Avantis minimum" in quote.notes


def test_upside_venue_string_is_distinct_from_the_standard_perp(offline):
    """The Upside leg MUST carry ``venue == "avantis_upside"`` (CONTRACT.md §12.4).

    Two different instruments cannot share one venue name -- if they did, the
    ranking engine would treat them as duplicate quotes on the same venue and
    silently drop one, and ``engine.upside_hedge_comparison`` (which filters on
    ``venue == "avantis_upside"``) would never find the direct quote and always
    fall back to deriving Upside from the standard Avantis quote.
    """
    upside = asyncio.run(avantis.quote_upside_hedge("BTC", "short", Decimal("10000")))
    standard = asyncio.run(
        avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24"))
    )
    assert upside.venue == avantis.UPSIDE_VENUE == "avantis_upside"
    assert standard.venue == avantis.VENUE == "avantis"
    assert upside.venue != standard.venue


def test_upside_unavailable_quote_still_carries_the_upside_venue(offline):
    """A refusal must show up as ``venue="avantis_upside"``, not ``"avantis"``.

    Otherwise a below-minimum Upside call would masquerade as a second
    standard-Avantis row in the ranking (and would sort as available=False
    against the standard perp, corrupting whatever the renderer shows for it).
    """
    quote = asyncio.run(avantis.quote_upside_hedge("BTC", "short", Decimal("50")))
    assert quote.available is False
    assert quote.venue == avantis.UPSIDE_VENUE


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_snapshot_is_cached_within_ttl(monkeypatch, snapshot):
    avantis.clear_caches()
    calls = []

    async def counting_get(client, url):
        calls.append(url)
        return snapshot

    monkeypatch.setattr(avantis, "_get_json", counting_get)

    async def scenario():
        return await asyncio.gather(*(avantis.fetch_trading_snapshot() for _ in range(8)))

    results = asyncio.run(scenario())
    assert len(results) == 8
    assert len(calls) == 1, "concurrent quotes must share one snapshot fetch"
    avantis.clear_caches()


# --------------------------------------------------------------------------
# Live smoke test (opt-in): AVANTIS_LIVE_TESTS=1
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AVANTIS_LIVE_TESTS") != "1",
    reason="set AVANTIS_LIVE_TESTS=1 to hit the production Avantis API",
)
def test_live_btc_quote_is_internally_consistent():
    """Live consistency check under the maker round trip (CONTRACT.md §12.8).

    Both directions price both legs at the pair's live maker rate, so both must
    report ``fee_tier == "maker"`` and 1.0 bps open + 1.0 bps close on crypto at
    current rates. The old assertion that the two legs classify differently
    belonged to the live-skew path that ``classify_skew_fee`` implements for its
    own tests but ``quote_hedge`` no longer invokes.
    """
    avantis.clear_caches()
    quote = asyncio.run(avantis.quote_hedge("BTC", "short", Decimal("10000"), Decimal("24")))
    assert quote is not None and quote.available is True
    assert quote.fee_tier == "maker"
    assert quote.taker_fee_bps == Decimal("1.00")
    assert quote.close_fee_bps == Decimal("1.00")
    assert quote.price_impact_bps > 0 and quote.est_slippage_bps > 0
    # §12.9: Quote's borrow is 0 by policy; the raw marginFee is still non-zero
    # (surfaced in notes only). Both invariants matter.
    assert quote.borrow_rate_8h_bps == Decimal(0)
    assert "EXCLUDED per §12.9" in quote.notes
    assert "marginFee" in quote.notes
    long_leg = asyncio.run(avantis.quote_hedge("BTC", "long", Decimal("10000"), Decimal("24")))
    assert long_leg.fee_tier == "maker"
    assert long_leg.taker_fee_bps == Decimal("1.00")
    assert long_leg.close_fee_bps == Decimal("1.00")
    # At most one side receives. Both-zero is a real live state (observed on BTC
    # 2026-08-19), so it is permitted; both-positive would be a sign bug.
    assert not (quote.funding_rate_8h_bps > 0 and long_leg.funding_rate_8h_bps > 0)
