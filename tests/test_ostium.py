"""Ostium liquidation formula tests.

Ostium is the one adapter in the fleet whose liquidation price is computed
locally rather than read from a venue endpoint (see CONTRACT.md §12.10).
There is no `liquidationPrice` field on the subgraph's `Trade` type, no
`Position` object, and no REST API -- the frontend and the SDK both compute
liq client-side from raw Trade + Pair params. So instead of asserting that
values match a wire-canonical response, we pin the exact formula to the
worked examples published in the Ostium liquidation docs, and re-derive the
same numbers `OstiumAdapter._compute_liq_price` returns.

Docs reference: https://docs.ostium.com/traders/trading/liquidation
  Threshold (% loss) = 100% − (Leverage / MaxLevPair × 25%)
  Long  Liq = Entry × (1 − Threshold / Leverage)
  Short Liq = Entry × (1 + Threshold / Leverage)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner.adapters.ostium import (
    LEVERAGE_PRECISION,
    OstiumAdapter,
)


def _pair(max_leverage: Decimal) -> dict:
    """Build the minimal Pair payload `_compute_liq_price` reads.

    `maxLeverage` on the subgraph is stored in LEVERAGE_PRECISION (1e2), so
    a 200x cap arrives on the wire as the string "20000".
    """
    return {"maxLeverage": str(int(max_leverage * LEVERAGE_PRECISION))}


# ---------------------------------------------------------------------------
# Base formula: no fee shift, verifies the raw docs identities.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leverage,max_lev,expected_threshold_pct",
    [
        # These threshold rows are copied verbatim from the docs table at
        # https://docs.ostium.com/traders/trading/liquidation. If Ostium ever
        # changes the backstop coefficient (0.25 -> anything else), this
        # test will loudly fail on the exact rows their docs advertise.
        (Decimal(5),   Decimal(200), Decimal("99.375")),
        (Decimal(10),  Decimal(200), Decimal("98.75")),
        (Decimal(20),  Decimal(200), Decimal("97.5")),
        (Decimal(50),  Decimal(200), Decimal("93.75")),
        (Decimal(100), Decimal(200), Decimal("87.5")),
        (Decimal(200), Decimal(200), Decimal("75")),
    ],
)
def test_liq_formula_matches_docs_threshold_table(
    leverage, max_lev, expected_threshold_pct
):
    """Each row of the Ostium docs' liquidation table must round-trip exactly.

    We work backwards from the price the adapter returns to recover the
    implied threshold and compare it to the docs. Using price rather than the
    intermediate `threshold` variable exercises the actual code path the
    adapter runs at position-decode time (there is no way to short-circuit
    partway through _compute_liq_price).
    """
    entry = Decimal("100")  # arbitrary; formula is scale-invariant.
    liq = OstiumAdapter._compute_liq_price(
        _pair(max_lev),
        entry_price=entry,
        leverage=leverage,
        is_long=True,
        accrued_fees_usd=None,
        collateral_usd=Decimal(1000),
    )
    assert liq is not None
    # Long: Liq = Entry × (1 − Threshold/Leverage), so threshold =
    # Leverage × (1 − Liq/Entry). Multiply by 100 to compare in %.
    implied_pct = leverage * (1 - liq / entry) * 100
    assert implied_pct == expected_threshold_pct


def test_liq_formula_worked_example_from_docs():
    """20x long BTC/USD on max 200x → 97.5% threshold → 4.875% price move.

    This is the exact example the docs walk through step-by-step. If any part
    of the chain (threshold → price-move → liq price) drifts from the
    published number, that's a bug against Ostium's own documentation.
    """
    entry = Decimal("100000")  # dollar-neat BTC entry
    liq = OstiumAdapter._compute_liq_price(
        _pair(Decimal(200)),
        entry_price=entry,
        leverage=Decimal(20),
        is_long=True,
        accrued_fees_usd=None,
        collateral_usd=Decimal(5000),
    )
    assert liq is not None
    # 4.875% below entry, per docs.
    expected = entry * (Decimal(1) - Decimal("0.04875"))
    assert liq == expected


def test_long_and_short_are_symmetric_around_entry():
    """Long liq below entry, short liq above, same distance for same params."""
    entry = Decimal("2000")
    kwargs = dict(
        pair_data=_pair(Decimal(100)),
        entry_price=entry,
        leverage=Decimal(10),
        accrued_fees_usd=None,
        collateral_usd=Decimal(1000),
    )
    long_liq = OstiumAdapter._compute_liq_price(is_long=True, **kwargs)
    short_liq = OstiumAdapter._compute_liq_price(is_long=False, **kwargs)
    assert long_liq is not None and short_liq is not None
    assert entry - long_liq == short_liq - entry


# ---------------------------------------------------------------------------
# Fee shift: accrued rollover moves liq TOWARDS entry (docs, "Liquidation Price"
# section: "Accrued rollover fees reduce your effective collateral over time,
# which brings the actual liquidation price closer to your entry.")
# ---------------------------------------------------------------------------


def test_accrued_fees_move_long_liq_closer_to_entry():
    """Longs: accrued fees push liq UP toward entry, never past it."""
    entry = Decimal("100000")
    lev = Decimal(20)
    pair = _pair(Decimal(200))
    liq_no_fees = OstiumAdapter._compute_liq_price(
        pair, entry, lev, True, accrued_fees_usd=None, collateral_usd=Decimal(5000)
    )
    liq_with_fees = OstiumAdapter._compute_liq_price(
        pair, entry, lev, True,
        # −$100 of accrued rollover on $5000 collateral = 2% fee-fraction.
        accrued_fees_usd=Decimal("-100"),
        collateral_usd=Decimal(5000),
    )
    assert liq_no_fees is not None and liq_with_fees is not None
    assert liq_with_fees > liq_no_fees      # shifted toward entry
    assert liq_with_fees < entry            # never crosses entry


def test_accrued_fees_move_short_liq_closer_to_entry():
    """Shorts: accrued fees push liq DOWN toward entry, never past it."""
    entry = Decimal("100000")
    lev = Decimal(20)
    pair = _pair(Decimal(200))
    liq_no_fees = OstiumAdapter._compute_liq_price(
        pair, entry, lev, False, accrued_fees_usd=None, collateral_usd=Decimal(5000)
    )
    liq_with_fees = OstiumAdapter._compute_liq_price(
        pair, entry, lev, False,
        accrued_fees_usd=Decimal("-100"),
        collateral_usd=Decimal(5000),
    )
    assert liq_no_fees is not None and liq_with_fees is not None
    assert liq_with_fees < liq_no_fees
    assert liq_with_fees > entry


def test_fee_shift_scales_linearly_with_accrued_amount():
    """Doubling accrued fees doubles the liq shift (linear collateral erosion).

    Guards against a future refactor that switches to a compounding or
    nonlinear approximation without also updating the docs cross-reference.
    """
    entry = Decimal("100000")
    lev = Decimal(20)
    pair = _pair(Decimal(200))
    common = dict(
        pair_data=pair, entry_price=entry, leverage=lev, is_long=True,
        collateral_usd=Decimal(5000),
    )
    base = OstiumAdapter._compute_liq_price(accrued_fees_usd=None, **common)
    one_x = OstiumAdapter._compute_liq_price(
        accrued_fees_usd=Decimal("-50"), **common
    )
    two_x = OstiumAdapter._compute_liq_price(
        accrued_fees_usd=Decimal("-100"), **common
    )
    assert base is not None and one_x is not None and two_x is not None
    shift_1 = one_x - base
    shift_2 = two_x - base
    assert shift_2 == shift_1 * 2


# ---------------------------------------------------------------------------
# Robustness against malformed subgraph payloads (defensive nulls)
# ---------------------------------------------------------------------------


def test_returns_none_when_pair_data_is_missing():
    """No maxLeverage → no threshold → no liq price. Don't crash."""
    assert OstiumAdapter._compute_liq_price(
        {}, entry_price=Decimal(100), leverage=Decimal(10),
        is_long=True, accrued_fees_usd=None, collateral_usd=Decimal(100),
    ) is None


def test_returns_none_when_max_leverage_is_zero():
    assert OstiumAdapter._compute_liq_price(
        {"maxLeverage": "0"}, entry_price=Decimal(100), leverage=Decimal(10),
        is_long=True, accrued_fees_usd=None, collateral_usd=Decimal(100),
    ) is None


def test_falls_back_to_group_max_leverage_when_pair_field_missing():
    """Pair-level maxLeverage is preferred; group-level is the fallback."""
    pair = {
        "maxLeverage": "0",
        "group": {"maxLeverage": str(int(Decimal(200) * LEVERAGE_PRECISION))},
    }
    liq = OstiumAdapter._compute_liq_price(
        pair, entry_price=Decimal(100), leverage=Decimal(20),
        is_long=True, accrued_fees_usd=None, collateral_usd=Decimal(1000),
    )
    # Same as the docs' worked example ratio: 4.875% price move.
    assert liq is not None
    assert liq == Decimal("100") * (Decimal(1) - Decimal("0.04875"))


def test_resolve_pair_uses_from_and_to_not_first_from():
    """EUR/USD must not attach EUR/GBP rollover, and EURUSD must resolve."""
    adapter = OstiumAdapter.__new__(OstiumAdapter)
    pairs = {
        "9": {"id": "9", "from": "EUR", "to": "GBP"},
        "2": {"id": "2", "from": "EUR", "to": "USD"},
        "7": {"id": "7", "from": "USD", "to": "JPY"},
        "1": {"id": "1", "from": "BTC", "to": "USD"},
    }
    eur = adapter._resolve_pair("EURUSD", pairs)
    assert eur is not None and eur["id"] == "2"
    assert adapter._resolve_pair("EUR", pairs)["id"] == "2"
    assert adapter._resolve_pair("EURGBP", pairs)["id"] == "9"
    assert adapter._resolve_pair("USDJPY", pairs)["id"] == "7"
    assert adapter._resolve_pair("USD/JPY", pairs)["id"] == "7"
    assert adapter._resolve_pair("BTC", pairs)["id"] == "1"


def test_resolve_pair_commodities_and_hip3_aliases():
    adapter = OstiumAdapter.__new__(OstiumAdapter)
    pairs = {
        "x": {"id": "x", "from": "XAU", "to": "USD"},
        "w": {"id": "w", "from": "WTI", "to": "USD"},
        "b": {"id": "b", "from": "BRENT", "to": "USD"},
        "e": {"id": "e", "from": "EUR", "to": "USD"},
    }
    assert adapter._resolve_pair("GOLD", pairs)["id"] == "x"
    assert adapter._resolve_pair("xyz:GOLD", pairs)["id"] == "x"
    assert adapter._resolve_pair("XAU", pairs)["id"] == "x"
    assert adapter._resolve_pair("CL", pairs)["id"] == "w"
    assert adapter._resolve_pair("xyz:BRENTOIL", pairs)["id"] == "b"
    assert adapter._resolve_pair("EUR", pairs)["id"] == "e"
