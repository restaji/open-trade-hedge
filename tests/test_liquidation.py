"""Tests for the liquidation risk module.

Covers the per-venue LIQUIDATION_SPECS table, the liquidation price calculator,
the penalty calculator, and the integration with the engine's scan output.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner.liquidation import (
    LIQUIDATION_SPECS,
    LiquidationRisk,
    compute_liquidation_risk,
    liquidation_cost_bps,
    liquidation_cost_usd,
    liquidation_distance_pct,
    liquidation_price,
)
from hedge_scanner.models import LiquidationSpec

D = Decimal


# --------------------------------------------------------------------------------------
# Spec table completeness
# --------------------------------------------------------------------------------------


class TestSpecTable:
    def test_every_major_venue_has_a_spec(self):
        expected = {"avantis", "hyperliquid", "grvt", "pacifica", "ondo", "jupiter"}
        assert expected <= set(LIQUIDATION_SPECS)

    def test_all_specs_are_frozen_dataclasses(self):
        for venue, spec in LIQUIDATION_SPECS.items():
            assert isinstance(spec, LiquidationSpec), f"{venue} is not a LiquidationSpec"
            assert spec.venue == venue

    def test_maintenance_margin_is_positive(self):
        for venue, spec in LIQUIDATION_SPECS.items():
            assert spec.maintenance_margin_pct > D(0), f"{venue} has non-positive MM"

    def test_fee_type_is_valid(self):
        valid = {"pct_of_notional", "pct_of_collateral", "full_margin_forfeit", "residual_forfeit"}
        for venue, spec in LIQUIDATION_SPECS.items():
            assert spec.liquidation_fee_type in valid, f"{venue}: {spec.liquidation_fee_type}"

    def test_cross_margin_risk_is_valid(self):
        valid = {"position_only", "full_account"}
        for venue, spec in LIQUIDATION_SPECS.items():
            assert spec.cross_margin_risk in valid, f"{venue}: {spec.cross_margin_risk}"

    def test_grvt_is_full_margin_forfeit(self):
        spec = LIQUIDATION_SPECS["grvt"]
        assert spec.liquidation_fee_type == "full_margin_forfeit"
        assert spec.cross_margin_risk == "full_account"

    def test_hyperliquid_is_full_account_cross_margin(self):
        spec = LIQUIDATION_SPECS["hyperliquid"]
        assert spec.cross_margin_risk == "full_account"


# --------------------------------------------------------------------------------------
# Liquidation price calculator
# --------------------------------------------------------------------------------------


class TestLiquidationPrice:
    def test_long_10x_1pct_mm(self):
        """10x long, 1% MM: margin = 10%, distance = 10% - 1% = 9%."""
        liq = liquidation_price(D("100"), "long", D("10"), D("1"))
        assert liq == D("100") * (1 - D("0.09"))  # 91.00
        assert liq == D("91")

    def test_short_10x_1pct_mm(self):
        liq = liquidation_price(D("100"), "short", D("10"), D("1"))
        assert liq == D("100") * (1 + D("0.09"))  # 109.00
        assert liq == D("109")

    def test_higher_mm_means_closer_liquidation(self):
        low_mm = liquidation_price(D("1000"), "long", D("10"), D("1"))
        high_mm = liquidation_price(D("1000"), "long", D("10"), D("3"))
        assert high_mm > low_mm

    def test_higher_leverage_means_closer_liquidation(self):
        low_lev = liquidation_price(D("1000"), "long", D("5"), D("1"))
        high_lev = liquidation_price(D("1000"), "long", D("20"), D("1"))
        assert abs(D("1000") - high_lev) < abs(D("1000") - low_lev)

    def test_fees_eat_into_margin(self):
        no_fees = liquidation_price(D("1000"), "long", D("10"), D("1"))
        with_fees = liquidation_price(D("1000"), "long", D("10"), D("1"), fees_pct=D("0.5"))
        assert with_fees > no_fees

    def test_rejects_zero_leverage(self):
        with pytest.raises(ValueError, match="leverage"):
            liquidation_price(D("100"), "long", D("0"), D("1"))

    def test_rejects_zero_entry(self):
        with pytest.raises(ValueError, match="entry_price"):
            liquidation_price(D("0"), "long", D("10"), D("1"))

    def test_rejects_invalid_side(self):
        with pytest.raises(ValueError, match="side"):
            liquidation_price(D("100"), "flat", D("10"), D("1"))

    def test_health_ratio_model_short(self):
        """Avantis: 15% of collateral can be lost. At 10x, distance = 1.5%."""
        liq = liquidation_price(
            D("100"), "short", D("10"), D("15"), liquidation_model="health_ratio"
        )
        # distance = (15/100)/10 = 0.015. liq = 100 * (1 + 0.015) = 101.5
        assert liq == D("101.5")

    def test_health_ratio_model_long(self):
        liq = liquidation_price(
            D("100"), "long", D("10"), D("15"), liquidation_model="health_ratio"
        )
        assert liq == D("98.5")

    def test_health_ratio_tighter_at_higher_leverage(self):
        liq_10x = liquidation_price(
            D("1000"), "short", D("10"), D("15"), liquidation_model="health_ratio"
        )
        liq_50x = liquidation_price(
            D("1000"), "short", D("50"), D("15"), liquidation_model="health_ratio"
        )
        # Higher leverage -> less distance
        assert abs(D("1000") - liq_50x) < abs(D("1000") - liq_10x)

    def test_all_decimal_no_float(self):
        liq = liquidation_price(D("65000"), "short", D("10"), D("1"))
        assert isinstance(liq, Decimal)


# --------------------------------------------------------------------------------------
# Liquidation distance
# --------------------------------------------------------------------------------------


class TestLiquidationDistance:
    def test_long_distance(self):
        dist = liquidation_distance_pct(D("100"), D("91"), "long")
        assert dist == D("9")

    def test_short_distance(self):
        dist = liquidation_distance_pct(D("100"), D("109"), "short")
        assert dist == D("9")

    def test_distance_is_always_positive(self):
        dist = liquidation_distance_pct(D("65000"), D("59150"), "long")
        assert dist > D(0)


# --------------------------------------------------------------------------------------
# Liquidation cost
# --------------------------------------------------------------------------------------


class TestLiquidationCostUsd:
    def test_pct_of_notional(self):
        spec = LiquidationSpec(
            venue="test", maintenance_margin_pct=D("1"), liquidation_fee_pct=D("1.5"),
            liquidation_fee_type="pct_of_notional", partial_liquidation=False,
            cross_margin_risk="position_only", notes="", source="",
        )
        cost = liquidation_cost_usd(D("10000"), D("1000"), spec)
        assert cost == D("150")  # 1.5% of 10000

    def test_pct_of_collateral(self):
        spec = LiquidationSpec(
            venue="test", maintenance_margin_pct=D("15"), liquidation_fee_pct=D("15"),
            liquidation_fee_type="pct_of_collateral", partial_liquidation=False,
            cross_margin_risk="position_only", notes="", source="",
        )
        cost = liquidation_cost_usd(D("10000"), D("1000"), spec)
        assert cost == D("150")  # 15% of 1000

    def test_full_margin_forfeit(self):
        spec = LiquidationSpec(
            venue="test", maintenance_margin_pct=D("1"), liquidation_fee_pct=D("0"),
            liquidation_fee_type="full_margin_forfeit", partial_liquidation=False,
            cross_margin_risk="full_account", notes="", source="",
        )
        cost = liquidation_cost_usd(D("10000"), D("1000"), spec)
        assert cost == D("1000")  # entire collateral

    def test_residual_forfeit(self):
        spec = LiquidationSpec(
            venue="test", maintenance_margin_pct=D("3.33"), liquidation_fee_pct=D("0"),
            liquidation_fee_type="residual_forfeit", partial_liquidation=True,
            cross_margin_risk="position_only", notes="", source="",
        )
        cost = liquidation_cost_usd(D("10000"), D("1000"), spec)
        assert cost == D("333")  # 3.33% of 10000


class TestLiquidationCostBps:
    def test_bps_of_notional(self):
        spec = LiquidationSpec(
            venue="test", maintenance_margin_pct=D("1"), liquidation_fee_pct=D("1.5"),
            liquidation_fee_type="pct_of_notional", partial_liquidation=False,
            cross_margin_risk="position_only", notes="", source="",
        )
        bps = liquidation_cost_bps(D("10000"), D("1000"), spec)
        assert bps == D("150")  # 1.5% = 150 bps


# --------------------------------------------------------------------------------------
# compute_liquidation_risk
# --------------------------------------------------------------------------------------


class TestComputeLiquidationRisk:
    def test_returns_none_for_unknown_venue(self):
        assert compute_liquidation_risk(
            "unknown_venue", "short", D("65000"), D("10"), D("10000")
        ) is None

    def test_returns_none_for_zero_leverage(self):
        assert compute_liquidation_risk(
            "grvt", "short", D("65000"), D("0"), D("10000")
        ) is None

    def test_grvt_short_10x(self):
        risk = compute_liquidation_risk(
            "grvt", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        assert risk.venue == "grvt"
        assert risk.leverage == D("10")
        assert risk.collateral_usd == D("1000")
        # 10x, 1% MM: distance = 10% - 1% = 9%
        assert risk.distance_pct == D("9")
        # liq price for short: entry * (1 + 0.09) = 65000 * 1.09 = 70850
        assert risk.liq_price == D("70850")
        # GRVT: full margin forfeit => penalty = entire collateral
        assert risk.penalty_usd == D("1000")
        assert risk.spec.liquidation_fee_type == "full_margin_forfeit"

    def test_pacifica_short_10x(self):
        risk = compute_liquidation_risk(
            "pacifica", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        # 10x, 1% MM: distance = 9%
        assert risk.distance_pct == D("9")
        # Penalty: 0.75% of notional = 0.0075 * 10000 = 75
        assert risk.penalty_usd == D("75")

    def test_ondo_short_10x(self):
        risk = compute_liquidation_risk(
            "ondo", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        # 10x, 2% MM: distance = 10% - 2% = 8%
        assert risk.distance_pct == D("8")
        # Penalty: 1.5% of notional = 150
        assert risk.penalty_usd == D("150")

    def test_jupiter_short_10x(self):
        risk = compute_liquidation_risk(
            "jupiter", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        # 10x, 0.2% MM: distance = 10% - 0.2% = 9.8%
        assert risk.distance_pct == D("9.8")
        # Penalty: residual_forfeit => MM% of notional = 0.2% * 10000 = 20
        assert risk.penalty_usd == D("20")

    def test_avantis_short_10x(self):
        risk = compute_liquidation_risk(
            "avantis", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        # Avantis uses health_ratio model: 15% of collateral can be lost.
        # At 10x: distance = 15%/10 = 1.5%. liq_price = 65000 * 1.015 = 65975.
        assert risk.distance_pct == D("1.5")
        assert risk.liq_price == D("65975")
        # Penalty: pct_of_collateral 15%. Collateral = 1000. 15% × 1000 = 150.
        assert risk.penalty_usd == D("150")

    def test_hyperliquid_short_10x(self):
        risk = compute_liquidation_risk(
            "hyperliquid", "short", D("65000"), D("10"), D("10000")
        )
        assert risk is not None
        # 10x, 3.33% MM: distance = 10% - 3.33% = 6.67%
        assert risk.distance_pct == D("6.67")
        # Penalty: residual_forfeit => 3.33% * 10000 = 333
        assert risk.penalty_usd == D("333")


# --------------------------------------------------------------------------------------
# Integration: liquidation risk appears in scan output
# --------------------------------------------------------------------------------------


class TestScanIntegration:
    def test_delta_hedges_carry_liquidation_risks(self):
        from hedge_scanner.engine import ScanConfig, scan

        positions = [_make_position("grvt", "BTC", "long", "50000")]
        quotes = [
            _make_quote("pacifica", "BTC", "short"),
            _make_quote("ondo", "BTC", "short"),
        ]
        result = scan(positions, quotes)

        btc_hedge = next(o for o in result.delta_hedges if o.base_asset == "BTC")
        assert len(btc_hedge.liquidation_risks) > 0

        venues_with_risk = {r.venue for r in btc_hedge.liquidation_risks}
        assert "pacifica" in venues_with_risk
        assert "ondo" in venues_with_risk

    def test_json_projection_includes_liquidation_risks(self):
        import json
        from hedge_scanner.engine import ScanConfig, scan, scan_result_to_dict

        positions = [_make_position("grvt", "BTC", "long", "50000")]
        quotes = [_make_quote("pacifica", "BTC", "short")]
        result = scan(positions, quotes)
        payload = scan_result_to_dict(result)
        encoded = json.dumps(payload)
        reloaded = json.loads(encoded)

        btc_hedge = reloaded["delta_hedges"][0]
        assert "liquidation_risks" in btc_hedge
        assert len(btc_hedge["liquidation_risks"]) > 0
        risk = btc_hedge["liquidation_risks"][0]
        assert "liquidation_price" in risk
        assert "distance_pct" in risk
        assert "penalty_usd" in risk
        assert "cross_margin_risk" in risk

    def test_json_projection_includes_liquidation_specs(self):
        import json
        from hedge_scanner.engine import scan, scan_result_to_dict

        result = scan([], [])
        payload = scan_result_to_dict(result)
        assert "liquidation_specs" in payload
        assert "grvt" in payload["liquidation_specs"]
        assert payload["liquidation_specs"]["grvt"]["liquidation_fee_type"] == "full_margin_forfeit"

    def test_no_floats_in_liquidation_output(self):
        import json
        from hedge_scanner.engine import scan, scan_result_to_dict

        positions = [_make_position("grvt", "BTC", "long", "50000")]
        quotes = [_make_quote("pacifica", "BTC", "short")]
        result = scan(positions, quotes)
        payload = scan_result_to_dict(result)

        def assert_no_floats(node, path=""):
            assert not isinstance(node, float), f"float at {path}: {node}"
            if isinstance(node, dict):
                for k, v in node.items():
                    assert_no_floats(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    assert_no_floats(v, f"{path}[{i}]")

        assert_no_floats(payload)


# --------------------------------------------------------------------------------------
# Test helpers (local factories, same pattern as test_engine.py)
# --------------------------------------------------------------------------------------


def _make_position(venue, base_asset, side, notional_usd, **extra):
    from hedge_scanner.models import Position
    notional = D(str(notional_usd))
    signed = -abs(notional) if side == "short" else abs(notional)
    return Position(
        venue=venue,
        address="0x" + "ab" * 20,
        market=f"{base_asset}_USDT_Perp",
        base_asset=base_asset,
        quote_asset="USDC",
        side=side,
        size_base=abs(notional) / D("60000"),
        notional_usd=signed,
        entry_price=D("60000"),
        mark_price=D("60000"),
        **extra,
    )


def _make_quote(venue, base_asset, side, **extra):
    from hedge_scanner.models import Quote
    return Quote(
        venue=venue,
        market=f"{base_asset}-PERP",
        side=side,
        notional_usd=D("10000"),
        taker_fee_bps=D("4"),
        close_fee_bps=D("4"),
        price_impact_bps=D("0"),
        funding_rate_8h_bps=D("0"),
        borrow_rate_8h_bps=D("0"),
        est_slippage_bps=D("0"),
        available=True,
        notes="",
        base_asset=base_asset,
        **extra,
    )
