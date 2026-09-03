"""Tests for the hedge opportunity engine.

Fixtures are constructed locally so these tests stand alone from the ingestion
layer and from `tests/fixtures/` (owned by the adapter work).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedge_scanner import engine
from hedge_scanner.engine import (
    FEE_SCHEDULE,
    ScanConfig,
    avantis_comparison,
    delta_hedge_opportunities,
    format_horizon,
    funding_arb_opportunities,
    hedge_cost,
    horizon_sensitivity,
    net_exposures,
    parse_horizon,
    parse_horizons,
    quote_from_schedule,
    rank_hedge_venues,
    scan,
    scan_result_to_dict,
    self_hedge_findings,
    upside_hedge_comparison,
)
from hedge_scanner.models import Position, Quote

D = Decimal


# --------------------------------------------------------------------------------------
# Local factories
# --------------------------------------------------------------------------------------


def make_position(
    venue: str,
    base_asset: str,
    side: str,
    notional_usd: str | Decimal,
    *,
    address: str = "0x" + "ab" * 20,
    market: str | None = None,
    mark_price: str = "100",
    entry_price: str = "100",
    **extra,
) -> Position:
    notional = D(str(notional_usd))
    signed = -abs(notional) if side == "short" else abs(notional)
    return Position(
        venue=venue,
        address=address,
        market=market or f"{base_asset}_USDT_Perp",
        base_asset=base_asset,
        quote_asset="USDC",
        side=side,
        size_base=abs(notional) / D(mark_price),
        notional_usd=signed,
        entry_price=D(entry_price),
        mark_price=D(mark_price),
        **extra,
    )


def make_quote(
    venue: str,
    base_asset: str,
    side: str,
    *,
    notional_usd: str | Decimal = "10000",
    open_fee_bps: str | Decimal = "4",
    close_fee_bps: str | Decimal = "4",
    price_impact_bps: str | Decimal = "0",
    slippage_bps: str | Decimal = "0",
    funding_8h_bps: str | Decimal = "0",
    borrow_8h_bps: str | Decimal = "0",
    available: bool = True,
    notes: str = "",
    market: str | None = None,
) -> Quote:
    return Quote(
        venue=venue,
        market=market or f"{base_asset}-PERP",
        side=side,
        notional_usd=D(str(notional_usd)),
        taker_fee_bps=D(str(open_fee_bps)),
        close_fee_bps=D(str(close_fee_bps)),
        price_impact_bps=D(str(price_impact_bps)),
        funding_rate_8h_bps=D(str(funding_8h_bps)),
        borrow_rate_8h_bps=D(str(borrow_8h_bps)),
        est_slippage_bps=D(str(slippage_bps)),
        available=available,
        notes=notes,
        base_asset=base_asset,
    )


# --------------------------------------------------------------------------------------
# Horizon parsing
# --------------------------------------------------------------------------------------


class TestHorizonParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("8h", D(8)),
            ("24h", D(24)),
            ("3d", D(72)),
            ("7d", D(168)),
            ("30d", D(720)),
            ("1w", D(168)),
            ("36", D(36)),
            ("1.5d", D(36)),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_horizon(text) == expected

    @pytest.mark.parametrize("text", ["", "soon", "0h", "-4h", "3m", "24hh"])
    def test_rejects_garbage(self, text):
        with pytest.raises(ValueError):
            parse_horizon(text)

    def test_list_is_sorted_and_deduped(self):
        assert parse_horizons("3d,8h,24h,8h") == (D(8), D(24), D(72))

    def test_labels_round_trip(self):
        assert format_horizon(D(8)) == "8h"
        assert format_horizon(D(24)) == "1d"
        assert format_horizon(D(168)) == "7d"
        assert format_horizon(D(720)) == "30d"


# --------------------------------------------------------------------------------------
# 1. Netting
# --------------------------------------------------------------------------------------


class TestNetting:
    def test_single_venue_long(self):
        material, dust = net_exposures([make_position("grvt", "BTC", "long", "50000")])
        assert dust == ()
        (exposure,) = material
        assert exposure.base_asset == "BTC"
        assert exposure.net_notional_usd == D(50000)
        assert exposure.gross_notional_usd == D(50000)
        assert exposure.gross_net_gap_usd == D(0)
        assert exposure.net_direction == "long"
        assert exposure.hedge_side == "short"

    def test_offsetting_cross_venue_positions_net_to_zero(self):
        material, dust = net_exposures(
            [
                make_position("pacifica", "SOL", "long", "40000"),
                make_position("jupiter", "SOL", "short", "40000"),
            ]
        )
        assert material == ()
        (exposure,) = dust
        assert exposure.net_notional_usd == D(0)
        assert exposure.gross_notional_usd == D(80000)
        assert exposure.offsetting_notional_usd == D(40000)
        assert exposure.gross_net_gap_usd == D(80000)
        assert exposure.net_direction == "flat"
        assert exposure.is_self_hedged

    def test_partial_cross_venue_offset_reports_net_and_gap(self):
        material, dust = net_exposures(
            [
                make_position("grvt", "ETH", "long", "100000"),
                make_position("pacifica", "ETH", "short", "30000"),
            ]
        )
        assert dust == ()
        (exposure,) = material
        assert exposure.net_notional_usd == D(70000)
        assert exposure.long_notional_usd == D(100000)
        assert exposure.short_notional_usd == D(30000)
        assert exposure.gross_notional_usd == D(130000)
        assert exposure.offsetting_notional_usd == D(30000)
        assert exposure.gross_net_gap_usd == D(60000)
        assert exposure.hedge_side == "short"
        assert exposure.long_venues == ("grvt",)
        assert exposure.short_venues == ("pacifica",)

    def test_nets_across_three_venues(self):
        material, _ = net_exposures(
            [
                make_position("grvt", "BTC", "long", "10000"),
                make_position("pacifica", "BTC", "long", "5000"),
                make_position("jupiter", "BTC", "short", "20000"),
            ]
        )
        (exposure,) = material
        assert exposure.net_notional_usd == D(-5000)
        assert exposure.net_direction == "short"
        assert exposure.hedge_side == "long"
        assert exposure.position_count == 3
        assert set(exposure.venues) == {"grvt", "pacifica", "jupiter"}

    def test_assets_do_not_cross_contaminate(self):
        material, _ = net_exposures(
            [
                make_position("grvt", "BTC", "long", "10000"),
                make_position("grvt", "ETH", "short", "10000"),
            ]
        )
        by_asset = {e.base_asset: e for e in material}
        assert by_asset["BTC"].net_notional_usd == D(10000)
        assert by_asset["ETH"].net_notional_usd == D(-10000)

    def test_side_overrides_unsigned_notional_from_a_sloppy_adapter(self):
        """An unsigned short must never be netted as a long."""
        sloppy = make_position("pacifica", "BTC", "short", "25000")
        sloppy.notional_usd = D(25000)  # positive despite side="short"
        material, _ = net_exposures([sloppy])
        (exposure,) = material
        assert exposure.net_notional_usd == D(-25000)
        assert exposure.net_direction == "short"

    def test_exposures_sorted_by_absolute_net_descending(self):
        material, _ = net_exposures(
            [
                make_position("grvt", "BTC", "long", "1000"),
                make_position("grvt", "ETH", "short", "90000"),
                make_position("grvt", "SOL", "long", "5000"),
            ]
        )
        assert [e.base_asset for e in material] == ["ETH", "SOL", "BTC"]


class TestDustAndZeroExposure:
    def test_dust_net_is_classified_as_flat_not_material(self):
        material, dust = net_exposures(
            [
                make_position("grvt", "BTC", "long", "10000"),
                make_position("pacifica", "BTC", "short", "9990"),
            ],
            dust_threshold_usd=D(25),
        )
        assert material == ()
        (exposure,) = dust
        assert exposure.net_notional_usd == D(10)

    def test_dust_threshold_boundary_is_inclusive(self):
        material, dust = net_exposures(
            [make_position("grvt", "BTC", "long", "25")], dust_threshold_usd=D(25)
        )
        assert len(material) == 1 and dust == ()

    def test_dust_gets_no_delta_hedge_proposal(self):
        positions = [
            make_position("grvt", "BTC", "long", "10000"),
            make_position("pacifica", "BTC", "short", "9990"),
        ]
        quotes = [make_quote("avantis", "BTC", "short")]
        result = scan(positions, quotes, config=ScanConfig(dust_threshold_usd=D(25)))
        assert result.delta_hedges == ()
        assert result.sensitivities == ()

    def test_empty_portfolio_is_not_an_error(self):
        result = scan([], [])
        assert result.exposures == ()
        assert result.delta_hedges == ()
        assert result.funding_arbs == ()
        assert result.total_gross_notional_usd == D(0)

    def test_zero_notional_position_does_not_create_exposure(self):
        material, dust = net_exposures([make_position("grvt", "BTC", "long", "0")])
        assert material == ()
        assert dust[0].gross_notional_usd == D(0)
        assert self_hedge_findings(dust) == ()


class TestSelfHedgeFindings:
    def test_fully_offset_pair_is_flagged_as_pure_fee_drag(self):
        material, dust = net_exposures(
            [
                make_position("grvt", "BTC", "long", "50000"),
                make_position("pacifica", "BTC", "short", "50000"),
            ]
        )
        (finding,) = self_hedge_findings(list(material) + list(dust))
        assert finding.fully_offset is True
        assert finding.offsetting_notional_usd == D(50000)
        assert finding.gross_net_gap_usd == D(100000)
        # Exit fee on both legs, from the static schedule.
        expected = (
            FEE_SCHEDULE["grvt"].close_fee_bps + FEE_SCHEDULE["pacifica"].close_fee_bps
        )
        assert finding.unwind_fee_bps == expected
        assert finding.unwind_fee_usd == expected * D(50000) / D(10000)
        assert finding.fee_schedule_unverified is False

    def test_same_venue_both_sides_pays_close_twice(self):
        material, dust = net_exposures(
            [
                make_position("jupiter", "BTC", "long", "206000"),
                make_position("jupiter", "BTC", "short", "211000"),
            ]
        )
        (finding,) = self_hedge_findings(list(material) + list(dust))
        assert finding.offsetting_notional_usd == D(206000)
        assert finding.unwind_fee_bps == FEE_SCHEDULE["jupiter"].close_fee_bps * 2
        assert finding.unwind_fee_usd == D("12") * D(206000) / D(10000)

    def test_partial_offset_is_flagged_but_not_fully_offset(self):
        material, _ = net_exposures(
            [
                make_position("grvt", "ETH", "long", "80000"),
                make_position("pacifica", "ETH", "short", "20000"),
            ]
        )
        (finding,) = self_hedge_findings(material)
        assert finding.fully_offset is False
        assert finding.offsetting_notional_usd == D(20000)

    def test_single_sided_exposure_produces_no_finding(self):
        material, _ = net_exposures([make_position("grvt", "BTC", "long", "50000")])
        assert self_hedge_findings(material) == ()

    def test_unverified_venue_taints_the_unwind_estimate(self):
        material, dust = net_exposures(
            [
                make_position("jupiter", "SOL", "long", "10000"),
                make_position("pacifica", "SOL", "short", "10000"),
            ]
        )
        (finding,) = self_hedge_findings(list(material) + list(dust))
        assert finding.fee_schedule_unverified is False


# --------------------------------------------------------------------------------------
# 2. Cost model and funding sign conventions
# --------------------------------------------------------------------------------------


class TestCostModel:
    def test_cost_components_and_horizon_scaling(self):
        quote = make_quote(
            "pacifica",
            "BTC",
            "short",
            notional_usd="100000",
            open_fee_bps="4",
            close_fee_bps="4",
            price_impact_bps="1",
            slippage_bps="2",
            funding_8h_bps="0",
        )
        cost = hedge_cost(quote, horizon_hours=D(24))
        assert cost.round_trip_fee_bps == D(11)
        assert cost.carry_cost_bps == D(0)
        assert cost.total_bps == D(11)
        assert cost.total_usd == D(11) * D(100000) / D(10000)  # 110 USD

    def test_fees_are_one_time_and_do_not_scale_with_horizon(self):
        quote = make_quote("pacifica", "BTC", "short", funding_8h_bps="0")
        short = hedge_cost(quote, horizon_hours=D(8))
        long = hedge_cost(quote, horizon_hours=D(720))
        assert short.total_bps == long.total_bps == D(8)

    def test_carry_scales_linearly_with_horizon(self):
        quote = make_quote("pacifica", "BTC", "short", funding_8h_bps="-2")
        assert hedge_cost(quote, horizon_hours=D(8)).carry_cost_bps == D(2)
        assert hedge_cost(quote, horizon_hours=D(24)).carry_cost_bps == D(6)
        assert hedge_cost(quote, horizon_hours=D(720)).carry_cost_bps == D(180)

    def test_everything_is_decimal_never_float(self):
        cost = hedge_cost(
            make_quote("grvt", "BTC", "short", funding_8h_bps="1.5"),
            horizon_hours=D(24),
        )
        for value in (
            cost.round_trip_fee_bps,
            cost.carry_cost_bps_per_8h,
            cost.carry_cost_bps,
            cost.total_bps,
            cost.total_usd,
            cost.breakeven_hours,
        ):
            assert isinstance(value, Decimal), value

    def test_rejects_non_positive_horizon(self):
        with pytest.raises(ValueError):
            hedge_cost(make_quote("grvt", "BTC", "short"), horizon_hours=D(0))

    def test_usd_uses_target_notional_not_quote_notional(self):
        quote = make_quote("grvt", "BTC", "short", notional_usd="10000")
        cost = hedge_cost(quote, horizon_hours=D(24), notional_usd=D(50000))
        assert cost.notional_usd == D(50000)
        assert cost.total_usd == D(8) * D(50000) / D(10000)
        assert cost.size_mismatch is True

    def test_matching_size_is_not_flagged(self):
        quote = make_quote("grvt", "BTC", "short", notional_usd="10000")
        assert hedge_cost(quote, horizon_hours=D(24), notional_usd=D(10200)).size_mismatch is False


class TestFundingSignConvention:
    """Positive `funding_rate_8h_bps` means the hedger RECEIVES. A flipped sign here
    would recommend the single worst venue while reporting it as the best."""

    def test_receiving_funding_lowers_cost(self):
        receives = hedge_cost(
            make_quote("grvt", "BTC", "short", funding_8h_bps="3"), horizon_hours=D(24)
        )
        assert receives.carry_cost_bps_per_8h == D(-3)
        assert receives.carry_cost_bps == D(-9)
        assert receives.total_bps == D(8) - D(9) == D(-1)
        assert receives.positive_carry is True
        assert receives.receives_funding is True

    def test_paying_funding_raises_cost(self):
        pays = hedge_cost(
            make_quote("grvt", "BTC", "short", funding_8h_bps="-3"), horizon_hours=D(24)
        )
        assert pays.carry_cost_bps_per_8h == D(3)
        assert pays.carry_cost_bps == D(9)
        assert pays.total_bps == D(17)
        assert pays.positive_carry is False
        assert pays.receives_funding is False

    def test_receiver_outranks_payer_with_identical_fees(self):
        quotes = [
            make_quote("grvt", "BTC", "short", funding_8h_bps="-5"),    # pays
            make_quote("pacifica", "BTC", "short", funding_8h_bps="5"),  # receives
        ]
        ranked, _ = rank_hedge_venues(
            "BTC", "short", D(100000), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["pacifica", "grvt"]
        assert ranked[0].total_bps == D(-7)
        assert ranked[1].total_bps == D(23)
        assert ranked[0].total_usd < D(0)   # you are paid to hold the hedge
        assert ranked[1].total_usd > D(0)

    def test_receiver_wins_even_against_a_cheaper_fee_venue_at_long_horizon(self):
        quotes = [
            make_quote(
                "ondo", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                funding_8h_bps="-4",
            ),
            make_quote(
                "grvt", "BTC", "short", open_fee_bps="6", close_fee_bps="6",
                funding_8h_bps="4",
            ),
        ]
        short_ranked, _ = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(8)
        )
        long_ranked, _ = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(72)
        )
        assert short_ranked[0].venue == "ondo"    # 6 bps vs 8 bps
        assert long_ranked[0].venue == "grvt"     # 6 bps vs 38 bps

    def test_borrow_is_always_a_cost_and_never_a_credit(self):
        cost = hedge_cost(
            make_quote("jupiter", "SOL", "short", funding_8h_bps="0", borrow_8h_bps="6"),
            horizon_hours=D(24),
        )
        assert cost.carry_cost_bps_per_8h == D(6)
        assert cost.carry_cost_bps == D(18)
        assert cost.positive_carry is False

    def test_borrow_offsets_received_funding(self):
        cost = hedge_cost(
            make_quote("jupiter", "SOL", "short", funding_8h_bps="4", borrow_8h_bps="6"),
            horizon_hours=D(8),
        )
        assert cost.carry_cost_bps_per_8h == D(2)

    def test_breakeven_hours_only_exists_for_positive_carry(self):
        receives = hedge_cost(
            make_quote("grvt", "BTC", "short", open_fee_bps="4", close_fee_bps="4",
                       funding_8h_bps="2"),
            horizon_hours=D(24),
        )
        # 8 bps of fees at 2 bps received per 8h -> 4 periods -> 32h
        assert receives.breakeven_hours == D(32)
        pays = hedge_cost(
            make_quote("grvt", "BTC", "short", funding_8h_bps="-2"), horizon_hours=D(24)
        )
        assert pays.breakeven_hours is None

    def test_zero_carry_has_no_breakeven(self):
        flat = hedge_cost(
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"), horizon_hours=D(24)
        )
        assert flat.breakeven_hours is None


# --------------------------------------------------------------------------------------
# Ranking, availability, hedge side
# --------------------------------------------------------------------------------------


class TestRanking:
    def test_unavailable_quotes_are_excluded_not_zero_costed(self):
        quotes = [
            make_quote("grvt", "BTC", "short", open_fee_bps="9", close_fee_bps="9",
                       funding_8h_bps="0"),
            make_quote(
                "pacifica", "BTC", "short", open_fee_bps="0", close_fee_bps="0",
                funding_8h_bps="0", available=False, notes="no live funding rate",
            ),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["grvt"]
        assert len(excluded) == 1
        assert excluded[0].venue == "pacifica"
        assert "no live funding rate" in excluded[0].reason

    def test_all_quotes_unavailable_yields_empty_ranking_with_reasons(self):
        quotes = [
            make_quote("grvt", "BTC", "short", available=False, notes="auth required"),
            make_quote("pacifica", "BTC", "short", available=False, notes="timeout"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert ranked == ()
        assert {x.reason for x in excluded} == {"auth required", "timeout"}

    def test_wrong_side_quotes_are_ignored(self):
        quotes = [
            make_quote("grvt", "BTC", "long", funding_8h_bps="99"),
            make_quote("pacifica", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["pacifica"]
        assert excluded == ()

    def test_wrong_asset_quotes_are_ignored(self):
        quotes = [
            make_quote("grvt", "ETH", "short", funding_8h_bps="99"),
            make_quote("pacifica", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, _ = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["pacifica"]

    def test_avantis_is_a_valid_hedge_destination(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert ranked[0].venue == "avantis"
        assert excluded == ()

    def test_verified_venue_is_not_flagged_unverified(self):
        ranked, _ = rank_hedge_venues(
            "BTC", "short", D(10000), [make_quote("grvt", "BTC", "short")],
            horizon_hours=D(24),
        )
        # GRVT has no live fee API so it's flagged as static-fallback
        assert ranked[0].fee_provenance == "static-fallback"
        assert ranked[0].fee_schedule_unverified is True
        assert ranked[0].fees_state_dependent is False

    def test_live_api_venue_is_not_flagged(self):
        ranked, _ = rank_hedge_venues(
            "SOL", "short", D(10000), [make_quote("jupiter", "SOL", "short")],
            horizon_hours=D(24),
        )
        assert ranked[0].fee_schedule_unverified is False
        assert ranked[0].fee_provenance == "live-or-adapter"

    def test_state_dependent_venues_are_flagged(self):
        """Ondo prices per market; Avantis is per-pair on the live snapshot.

        The Avantis fee schedule is now a live-sourced stub (§12.3 follow-up),
        so its `fees_state_dependent` flag is True -- consumers of Avantis
        commission must read the live pair record (RWA growth-mode pairs are
        0/0/0/0, crypto is 1.0 maker + 4.5 taker), not the stub numbers.
        """
        quotes = [
            make_quote("avantis", "BTC", "short"),
            make_quote("ondo", "BTC", "short"),
        ]
        ranked, _ = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        avantis_entry = next(c for c in ranked if c.venue == "avantis")
        ondo_entry = next(c for c in ranked if c.venue == "ondo")
        assert avantis_entry.fees_state_dependent is True
        assert ondo_entry.fees_state_dependent is True

    def test_hedge_below_the_venue_minimum_is_excluded(self):
        """Avantis rejects a crypto position under 100 USDC notional."""
        quotes = [
            make_quote("avantis", "BTC", "short", notional_usd="80"),
            make_quote("grvt", "BTC", "short", notional_usd="80"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(80), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["grvt"]
        assert excluded[0].venue == "avantis"
        assert "below the venue minimum" in excluded[0].reason

    def test_fx_and_metals_carry_a_higher_venue_minimum(self):
        quotes = [make_quote("avantis", "XAU", "short", notional_usd="200")]
        ranked, excluded = rank_hedge_venues(
            "XAU", "short", D(200), quotes, horizon_hours=D(24)
        )
        assert ranked == ()
        assert "300.00 USD" in excluded[0].reason

    def test_hedge_at_the_venue_minimum_is_allowed(self):
        quotes = [make_quote("avantis", "BTC", "short", notional_usd="100")]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(100), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["avantis"]
        assert excluded == ()

    def test_hedge_side_opposes_net_exposure(self):
        long_material, _ = net_exposures([make_position("grvt", "BTC", "long", "10000")])
        short_material, _ = net_exposures([make_position("grvt", "ETH", "short", "10000")])
        assert long_material[0].hedge_side == "short"
        assert short_material[0].hedge_side == "long"

    def test_delta_hedge_sizes_the_hedge_to_net_not_gross(self):
        positions = [
            make_position("grvt", "BTC", "long", "100000"),
            make_position("pacifica", "BTC", "short", "40000"),
        ]
        material, _ = net_exposures(positions)
        quotes = [make_quote("avantis", "BTC", "short", funding_8h_bps="0")]
        (opportunity,) = delta_hedge_opportunities(material, quotes)
        assert opportunity.kind == "delta_hedge"
        assert opportunity.hedge_side == "short"
        assert opportunity.hedge_notional_usd == D(60000)

    def test_positive_carry_opportunities_are_surfaced_first(self):
        positions = [
            make_position("grvt", "BTC", "long", "500000"),   # bigger, but costs money
            make_position("grvt", "ETH", "long", "10000"),    # smaller, pays carry
        ]
        material, _ = net_exposures(positions)
        quotes = [
            make_quote("pacifica", "BTC", "short", funding_8h_bps="-5"),
            make_quote("pacifica", "ETH", "short", funding_8h_bps="10"),
        ]
        opportunities = delta_hedge_opportunities(material, quotes)
        assert [o.base_asset for o in opportunities] == ["ETH", "BTC"]
        assert opportunities[0].best.positive_carry is True
        assert opportunities[0].positive_carry != ()
        assert opportunities[1].positive_carry == ()


# --------------------------------------------------------------------------------------
# §12.9 Avantis funding gate: exclude Avantis when hedging would not strictly
# improve the user's funding position. Scope is Avantis-only (standard perp
# plus Upside); every other venue continues to rank on all-in cost.
# --------------------------------------------------------------------------------------


class TestAvantisFundingGate:
    def _long_btc_paying_funding(self, notional="100000", rate="-5"):
        """Position holder is long and PAYING `rate` bps/8h in funding.

        Sign convention on Position.current_funding_rate_8h_bps is "position
        holder receives", so a paying position has a NEGATIVE rate.
        """
        return make_position(
            "hyperliquid",
            "BTC",
            "long",
            notional,
            current_funding_rate_8h_bps=D(rate),
        )

    def test_avantis_excluded_when_offered_funding_does_not_cover_user_cost(self):
        """User pays 5 bps/8h; Avantis offers only 3. Not doable."""
        position = self._long_btc_paying_funding(rate="-5")
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="3"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert [c.venue for c in ranked] == ["grvt"]
        assert [x.venue for x in excluded] == ["avantis"]
        assert "not higher than" in excluded[0].reason
        assert "12.9" in excluded[0].reason

    def test_avantis_included_when_offered_funding_strictly_exceeds_user_cost(self):
        """User pays 5 bps/8h; Avantis offers 6 — net +1 after hedge, so include."""
        position = self._long_btc_paying_funding(rate="-5")
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="6"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert "avantis" in {c.venue for c in ranked}
        assert excluded == ()

    def test_gate_is_strict_equal_rates_are_excluded(self):
        """User's phrasing was 'should be higher', so equal fails the check."""
        position = self._long_btc_paying_funding(rate="-5")
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [make_quote("avantis", "BTC", "short", funding_8h_bps="5")]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert ranked == ()
        assert [x.venue for x in excluded] == ["avantis"]

    def test_avantis_upside_is_also_gated(self):
        """Both Avantis instruments are subject to the gate (§12.9 scope)."""
        position = self._long_btc_paying_funding(rate="-5")
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="1"),
            make_quote("avantis_upside", "BTC", "short", funding_8h_bps="1"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert [c.venue for c in ranked] == ["grvt"]
        assert {x.venue for x in excluded} == {"avantis", "avantis_upside"}

    def test_gate_does_not_apply_to_other_venues(self):
        """§7.5.1 forbids rigging the ranking; other venues rank on all-in cost."""
        position = self._long_btc_paying_funding(rate="-5")
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [
            make_quote("hyperliquid", "BTC", "short", funding_8h_bps="1"),
            make_quote("pacifica", "BTC", "short", funding_8h_bps="1"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        # Neither would meet the Avantis gate (rate 1 <= 5), but both are
        # included because the gate is Avantis-scoped.
        assert {c.venue for c in ranked} == {"hyperliquid", "pacifica"}
        assert excluded == ()

    def test_gate_does_not_apply_when_user_funding_is_unknown(self):
        """No adapter supplied a live rate -> Avantis ranks normally."""
        position = make_position("hyperliquid", "BTC", "long", "100000")
        # Deliberately not setting current_funding_rate_8h_bps: it stays None.
        material, _ = net_exposures([position])
        (exposure,) = material
        assert exposure.weighted_current_funding_8h_bps is None
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="-99"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        # Avantis has terrible funding but the gate can't fire without a user
        # rate to compare against, so it stays in the ranking.
        assert "avantis" in {c.venue for c in ranked}
        assert excluded == ()

    def test_user_receiving_funding_still_allows_avantis_when_net_stays_positive(self):
        """User currently RECEIVES 3 bps/8h; Avantis takes 2 -> net +1 -> include."""
        position = make_position(
            "hyperliquid", "BTC", "long", "100000",
            current_funding_rate_8h_bps=D(3),  # positive = holder receives
        )
        material, _ = net_exposures([position])
        (exposure,) = material
        quotes = [make_quote("avantis", "BTC", "short", funding_8h_bps="-2")]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        # -2 > -3 -> passes: net funding stays a receive of 1 bps/8h.
        assert [c.venue for c in ranked] == ["avantis"]
        assert excluded == ()

    def test_weighted_average_across_multiple_venues(self):
        """Aggregate rate is notional-weighted across the user's positions."""
        positions = [
            # $80k long paying 10 bps/8h (rate -10 from holder perspective)
            make_position(
                "hyperliquid", "BTC", "long", "80000",
                current_funding_rate_8h_bps=D(-10),
            ),
            # $20k long paying nothing (rate 0)
            make_position(
                "pacifica", "BTC", "long", "20000",
                current_funding_rate_8h_bps=D(0),
            ),
        ]
        material, _ = net_exposures(positions)
        (exposure,) = material
        # Weighted average = (80000*-10 + 20000*0) / 100000 = -8
        assert exposure.weighted_current_funding_8h_bps == D(-8)

        # Avantis at 7 fails (7 <= 8); at 9 passes.
        quotes_fail = [make_quote("avantis", "BTC", "short", funding_8h_bps="7")]
        ranked_fail, excluded_fail = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes_fail,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert ranked_fail == ()
        assert [x.venue for x in excluded_fail] == ["avantis"]

        quotes_pass = [make_quote("avantis", "BTC", "short", funding_8h_bps="9")]
        ranked_pass, excluded_pass = rank_hedge_venues(
            "BTC", "short", exposure.abs_net_notional_usd, quotes_pass,
            horizon_hours=D(24),
            user_current_funding_8h_bps=exposure.weighted_current_funding_8h_bps,
        )
        assert [c.venue for c in ranked_pass] == ["avantis"]
        assert excluded_pass == ()

    def test_none_rates_are_skipped_not_treated_as_zero(self):
        """A missing per-position rate never dilutes the average toward zero."""
        positions = [
            # $80k with a known rate of -10
            make_position(
                "hyperliquid", "BTC", "long", "80000",
                current_funding_rate_8h_bps=D(-10),
            ),
            # $20k where the adapter did not supply a rate
            make_position("jupiter", "BTC", "long", "20000"),
        ]
        material, _ = net_exposures(positions)
        (exposure,) = material
        # Weighted average uses only the position with a rate: -10 (not -8).
        assert exposure.weighted_current_funding_8h_bps == D(-10)

    def test_delta_hedge_opportunities_wires_the_gate(self):
        """End-to-end: the gate must fire through delta_hedge_opportunities."""
        positions = [
            make_position(
                "hyperliquid", "BTC", "long", "100000",
                current_funding_rate_8h_bps=D(-5),
            ),
        ]
        material, _ = net_exposures(positions)
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="3"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        (opportunity,) = delta_hedge_opportunities(material, quotes)
        assert [c.venue for c in opportunity.ranked] == ["grvt"]
        assert [x.venue for x in opportunity.excluded] == ["avantis"]

    def test_scan_end_to_end_records_the_exclusion_in_json(self):
        """The gate's excluded rows must appear in scan_result_to_dict output."""
        positions = [
            make_position(
                "hyperliquid", "BTC", "long", "100000",
                current_funding_rate_8h_bps=D(-5),
            ),
        ]
        quotes = [
            make_quote("avantis", "BTC", "short", funding_8h_bps="1"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        result = scan(positions, quotes)
        payload = scan_result_to_dict(result)
        # Position level: the rate is exposed.
        assert payload["positions"][0]["current_funding_rate_8h_bps"] == "-5"
        # Exposure level: the weighted rate is exposed.
        btc_exposure = next(
            e for e in payload["net_exposures"] if e["base_asset"] == "BTC"
        )
        assert btc_exposure["weighted_current_funding_8h_bps"] == "-5"
        # Delta-hedge level: Avantis is in the EXCLUDED list, not the ranked one.
        btc_hedge = next(
            h for h in payload["delta_hedges"] if h["base_asset"] == "BTC"
        )
        assert "avantis" not in {c["venue"] for c in btc_hedge["ranked"]}
        avantis_excl = next(
            (x for x in btc_hedge["excluded"] if x["venue"] == "avantis"), None,
        )
        assert avantis_excl is not None
        assert "not higher than" in avantis_excl["reason"]
        # Assumptions section documents the gate.
        assert "avantis_funding_gate" in payload["assumptions"]


# --------------------------------------------------------------------------------------
# Avantis comparison line and Upside Perps
# --------------------------------------------------------------------------------------


def _btc_opportunity(quotes, *, horizon=D(24), notional="100000"):
    material, _ = net_exposures([make_position("grvt", "BTC", "long", notional)])
    config = ScanConfig(horizon_hours=horizon)
    (opportunity,) = delta_hedge_opportunities(material, quotes, config=config)
    return opportunity


class TestAvantisComparison:
    def test_reports_a_win_without_reordering_the_ranking(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       funding_8h_bps="0"),
        ]
        opportunity = _btc_opportunity(quotes)
        comparison = avantis_comparison(opportunity)
        assert comparison.verdict == "wins"
        assert comparison.avantis_rank == 1
        assert comparison.best_alternative.venue == "grvt"
        assert comparison.delta_bps == D(2) - D(9)
        assert comparison.delta_usd == D(-7) * D(100000) / D(10000)

    def test_reports_a_loss_in_plain_terms(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       funding_8h_bps="0"),
            make_quote("ondo", "BTC", "short", open_fee_bps="2.5", close_fee_bps="2.5",
                       funding_8h_bps="0"),
        ]
        comparison = avantis_comparison(_btc_opportunity(quotes))
        assert comparison.verdict == "loses"
        assert comparison.avantis_rank == 2
        assert comparison.delta_bps == D(4)
        assert comparison.delta_usd == D(40)

    def test_ranking_is_not_rigged_toward_avantis(self):
        """A dearer Avantis must actually rank second, not be floated to the top."""
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       funding_8h_bps="0"),
            make_quote("ondo", "BTC", "short", open_fee_bps="2.5", close_fee_bps="2.5",
                       funding_8h_bps="0"),
        ]
        opportunity = _btc_opportunity(quotes)
        assert [c.venue for c in opportunity.ranked] == ["ondo", "avantis"]

    def test_skew_improving_hedge_that_receives_funding_is_the_positive_carry_case(self):
        """Contract 7.6: a short into long-heavy Avantis skew prices as maker and may
        also receive funding. That combination is the Avantis-first pitch."""
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       price_impact_bps="2.61", funding_8h_bps="3"),
            make_quote("grvt", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       funding_8h_bps="-1"),
        ]
        comparison = avantis_comparison(_btc_opportunity(quotes))
        assert comparison.verdict == "wins"
        assert comparison.avantis.positive_carry is True
        assert comparison.avantis.total_bps == D("4.61") - D(9)

    def test_names_avantis_even_when_it_has_no_quote(self):
        quotes = [make_quote("grvt", "BTC", "short", funding_8h_bps="0")]
        comparison = avantis_comparison(_btc_opportunity(quotes))
        assert comparison.verdict == "no_quote"
        assert comparison.avantis is None
        assert comparison.excluded_reason

    def test_surfaces_the_exclusion_reason_when_avantis_is_unavailable(self):
        quotes = [
            make_quote("avantis", "BTC", "short", available=False,
                       notes="spread quote timed out"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0"),
        ]
        comparison = avantis_comparison(_btc_opportunity(quotes))
        assert comparison.verdict == "no_quote"
        assert comparison.excluded_reason == "spread quote timed out"

    def test_only_candidate_when_avantis_is_alone(self):
        quotes = [make_quote("avantis", "BTC", "short", funding_8h_bps="0")]
        comparison = avantis_comparison(_btc_opportunity(quotes))
        assert comparison.verdict == "only_candidate"
        assert comparison.best_alternative is None
        assert comparison.delta_bps is None

    def test_a_comparison_is_produced_for_every_delta_hedge(self):
        positions, quotes = _mixed_portfolio()
        result = scan(positions, quotes)
        assert {c.base_asset for c in result.avantis_comparisons} == {
            o.base_asset for o in result.delta_hedges
        }


class TestUpsidePerpComparison:
    def test_breakeven_adverse_move_is_the_decision_variable(self):
        """Standard hedge 9 bps; Upside fixed leg 2 bps; 25% profit share.

        Upside is cheaper while 2 + 0.25 * move < 9, i.e. move < 28 bps.
        """
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       price_impact_bps="2", funding_8h_bps="0"),
        ]
        opportunity = _btc_opportunity(quotes)
        comparison = upside_hedge_comparison(opportunity, quotes)
        assert comparison is not None
        assert comparison.standard_cost_bps == D(11)
        assert comparison.upside_fixed_cost_bps == D(2)
        assert comparison.profit_share_fraction == D("0.25")
        assert comparison.breakeven_adverse_move_bps == D(36)
        assert comparison.cheaper_when_hedge_unused is True
        assert comparison.derived_from_venue == "avantis"

    def test_funding_carries_into_the_upside_fixed_leg(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       price_impact_bps="2", funding_8h_bps="1"),
        ]
        comparison = upside_hedge_comparison(_btc_opportunity(quotes), quotes)
        # Spread 2 bps minus 1 bps/8h received over 24h = 2 - 3 = -1 bps.
        assert comparison.upside_fixed_cost_bps == D(-1)

    def test_never_cheaper_when_the_fixed_leg_already_costs_more(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="0", close_fee_bps="0",
                       price_impact_bps="20", funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="0"),
        ]
        comparison = upside_hedge_comparison(_btc_opportunity(quotes), quotes)
        assert comparison.standard_venue == "grvt"
        assert comparison.breakeven_adverse_move_bps == D(0)
        assert comparison.cheaper_when_hedge_unused is False

    def test_a_directly_quoted_upside_leg_is_preferred_over_a_derivation(self):
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       price_impact_bps="2", funding_8h_bps="0"),
            make_quote("avantis_upside", "BTC", "short", open_fee_bps="0",
                       close_fee_bps="0", price_impact_bps="1", funding_8h_bps="0"),
        ]
        comparison = upside_hedge_comparison(_btc_opportunity(quotes), quotes)
        assert comparison.derived_from_venue is None
        assert comparison.upside_fixed_cost_bps == D(1)

    def test_upside_ranks_as_its_own_row_alongside_the_standard_avantis_perp(self):
        """CONTRACT.md §12.4: Upside and standard Avantis coexist as separate rows.

        A hedger paging through the ranking table for BTC must see both
        instruments -- ``avantis`` and ``avantis_upside`` -- so the tradeoff is
        surfaced rather than buried. The comparison engine already keys on the
        ``avantis_upside`` string; this test pins the ranking side.
        """
        quotes = [
            make_quote("avantis", "BTC", "short", open_fee_bps="1", close_fee_bps="4.5",
                       price_impact_bps="2", funding_8h_bps="0"),
            make_quote("avantis_upside", "BTC", "short", open_fee_bps="0",
                       close_fee_bps="0", price_impact_bps="1", funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", open_fee_bps="4.5", close_fee_bps="4.5",
                       funding_8h_bps="0"),
        ]
        opportunity = _btc_opportunity(quotes)
        venues = [c.venue for c in opportunity.ranked]
        assert "avantis" in venues
        assert "avantis_upside" in venues
        # Both are ranked on their unconditional cost; the profit-share
        # obligation on Upside is contingent and is reported separately by
        # ``upside_hedge_comparison`` rather than folded into ``total_bps``.
        comparison = upside_hedge_comparison(opportunity, quotes)
        assert comparison is not None
        assert comparison.derived_from_venue is None
        # The standard-hedge reference in the Upside comparison must be a
        # *conventional* perp -- comparing Upside against itself would collapse
        # the section to a no-op ("cheaper if <never cheaper"). Since Upside
        # deliberately excludes its profit share from ``total_bps`` it usually
        # tops the ranking; the comparator must skip past it.
        assert comparison.standard_venue != "avantis_upside"

    def test_standard_venue_never_falls_back_to_upside_even_when_upside_leads(self):
        """CONTRACT.md §7.6 + §12.4: Upside vs itself is not a comparison.

        With no conventional Avantis quote, the cheapest conventional perp
        must be selected -- here that is grvt -- so the tradeoff between
        commission-based hedging and profit-share hedging is what the section
        actually reports.
        """
        quotes = [
            make_quote("avantis_upside", "BTC", "short", open_fee_bps="0",
                       close_fee_bps="0", price_impact_bps="0.5", funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="0"),
            make_quote("pacifica", "BTC", "short", open_fee_bps="4",
                       close_fee_bps="4", funding_8h_bps="0"),
        ]
        opportunity = _btc_opportunity(quotes)
        assert opportunity.ranked[0].venue == "avantis_upside"
        comparison = upside_hedge_comparison(opportunity, quotes)
        assert comparison is not None
        assert comparison.standard_venue == "grvt"

    def test_not_offered_outside_crypto_majors(self):
        material, _ = net_exposures([make_position("ondo", "NVDA", "long", "50000")])
        quotes = [make_quote("avantis", "NVDA", "short", funding_8h_bps="0")]
        (opportunity,) = delta_hedge_opportunities(material, quotes)
        assert upside_hedge_comparison(opportunity, quotes) is None

    def test_absent_when_avantis_is_not_a_candidate(self):
        quotes = [make_quote("grvt", "BTC", "short", funding_8h_bps="0")]
        assert upside_hedge_comparison(_btc_opportunity(quotes), quotes) is None


# --------------------------------------------------------------------------------------
# 3. Funding arbitrage
# --------------------------------------------------------------------------------------


class TestFundingArb:
    def test_existing_opposite_sign_pair_is_detected(self):
        positions = [
            make_position("pacifica", "BTC", "long", "50000"),
            make_position("grvt", "BTC", "short", "50000"),
        ]
        material, dust = net_exposures(positions)
        quotes = [
            make_quote("pacifica", "BTC", "long", funding_8h_bps="-2", close_fee_bps="4"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="6", close_fee_bps="4.5"),
        ]
        arbs = funding_arb_opportunities(list(material) + list(dust), quotes, positions)
        (arb,) = arbs
        assert arb.kind == "funding_arb"
        assert arb.basis == "existing"
        assert arb.long_venue == "pacifica" and arb.short_venue == "grvt"
        assert arb.opposite_funding_signs is True
        assert arb.net_carry_bps_per_8h == D(4)
        assert arb.notional_usd == D(50000)
        # Only the exits have to be earned back on an already-open pair.
        assert arb.fee_basis == "exit_only"
        assert arb.fee_bps == D("8.5")
        assert arb.breakeven_hours == D("8.5") / D(4) * D(8)
        assert arb.net_carry_usd_per_8h == D(4) * D(50000) / D(10000)

    def test_new_pair_charges_the_full_round_trip(self):
        quotes = [
            make_quote(
                "pacifica", "SOL", "long", funding_8h_bps="5",
                open_fee_bps="4", close_fee_bps="4",
            ),
            make_quote(
                "grvt", "SOL", "short", funding_8h_bps="3",
                open_fee_bps="4.5", close_fee_bps="4.5",
            ),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, [], config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        pacifica_long = [a for a in arbs if a.long_venue == "pacifica"]
        (arb,) = pacifica_long
        assert arb.basis == "new"
        assert arb.fee_basis == "round_trip"
        assert arb.fee_bps == D(17)
        assert arb.net_carry_bps_per_8h == D(8)
        assert arb.breakeven_hours == D(17) / D(8) * D(8)
        assert arb.opposite_funding_signs is False  # both receive; still positive carry

    def test_negative_carry_pair_is_not_reported(self):
        quotes = [
            make_quote("pacifica", "BTC", "long", funding_8h_bps="-4"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="-4"),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, [], config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        assert arbs == ()

    def test_carry_below_the_noise_floor_is_not_reported(self):
        quotes = [
            make_quote("pacifica", "BTC", "long", funding_8h_bps="0.02"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="0.02"),
        ]
        arbs = funding_arb_opportunities(
            [],
            quotes,
            [],
            config=ScanConfig(
                funding_arb_notional_usd=D(10000), min_arb_carry_bps_8h=D("0.10")
            ),
        )
        assert arbs == ()

    def test_borrow_costs_reduce_net_carry(self):
        quotes = [
            make_quote("jupiter", "SOL", "long", funding_8h_bps="0", borrow_8h_bps="5"),
            make_quote("grvt", "SOL", "short", funding_8h_bps="4"),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, [], config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        assert arbs == ()  # 4 received - 5 borrow = -1 bps/8h

    def test_unavailable_quotes_cannot_form_a_pair(self):
        quotes = [
            make_quote("pacifica", "BTC", "long", funding_8h_bps="5", available=False),
            make_quote("grvt", "BTC", "short", funding_8h_bps="5"),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, [], config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        assert arbs == ()

    def test_same_venue_is_never_paired_with_itself(self):
        quotes = [
            make_quote("grvt", "BTC", "long", funding_8h_bps="5"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="5"),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, [], config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        assert arbs == ()

    def test_net_pnl_turns_positive_past_breakeven(self):
        quotes = [
            make_quote("pacifica", "SOL", "long", funding_8h_bps="4",
                       open_fee_bps="4", close_fee_bps="4"),
            make_quote("grvt", "SOL", "short", funding_8h_bps="4",
                       open_fee_bps="4", close_fee_bps="4"),
        ]
        arbs = funding_arb_opportunities(
            [],
            quotes,
            [],
            config=ScanConfig(
                horizon_hours=D(8), funding_arb_notional_usd=D(10000)
            ),
        )
        arb = arbs[0]
        assert arb.fee_bps == D(16)
        assert arb.net_carry_bps_per_8h == D(8)
        assert arb.breakeven_hours == D(16)
        assert arb.at_horizon(D(8)).profitable_at_horizon is False
        assert arb.at_horizon(D(16)).net_pnl_bps == D(0)
        assert arb.at_horizon(D(24)).net_pnl_bps == D(8)
        assert arb.at_horizon(D(24)).net_pnl_usd == D(8) * D(10000) / D(10000)

    def test_existing_pairs_rank_above_prospective_pairs(self):
        positions = [
            make_position("pacifica", "BTC", "long", "10000"),
            make_position("grvt", "BTC", "short", "10000"),
        ]
        quotes = [
            make_quote("pacifica", "BTC", "long", funding_8h_bps="1"),
            make_quote("grvt", "BTC", "short", funding_8h_bps="1"),
            make_quote("ondo", "BTC", "short", funding_8h_bps="20"),
        ]
        arbs = funding_arb_opportunities(
            [], quotes, positions, config=ScanConfig(funding_arb_notional_usd=D(10000))
        )
        assert arbs[0].basis == "existing"
        assert any(a.basis == "new" for a in arbs)


# --------------------------------------------------------------------------------------
# 4. Horizon sensitivity and crossovers
# --------------------------------------------------------------------------------------


def _sensitivity_for(quotes, *, notional=D(100000), horizons=None, max_h=D(720)):
    material, _ = net_exposures([make_position("grvt", "BTC", "long", notional)])
    config = ScanConfig(
        horizons_hours=horizons or (D(8), D(24), D(72), D(168), D(720)),
        max_crossover_horizon_h=max_h,
    )
    (opportunity,) = delta_hedge_opportunities(material, quotes, config=config)
    return horizon_sensitivity(opportunity, config=config)


class TestHorizonSensitivity:
    def test_crossover_is_solved_exactly_not_sampled(self):
        """cheap-fee/pays-funding vs dear-fee/receives-funding.

        A: 4 bps fees, pays 1 bps/8h   -> cost(h) = 4 + 0.125h
        B: 16 bps fees, receives 1 bps/8h -> cost(h) = 16 - 0.125h
        Equal at h = 48, both at 10 bps. 48h is BETWEEN the 24h and 72h samples,
        so a grid search would report the wrong hour.
        """
        quotes = [
            make_quote("ondo", "BTC", "short", open_fee_bps="2", close_fee_bps="2",
                       funding_8h_bps="-1"),
            make_quote("grvt", "BTC", "short", open_fee_bps="8", close_fee_bps="8",
                       funding_8h_bps="1"),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert sensitivity.venue_is_horizon_dependent is True
        (crossover,) = sensitivity.crossovers
        assert crossover.at_hours == D(48)
        assert crossover.from_venue == "ondo"
        assert crossover.to_venue == "grvt"
        assert crossover.cost_bps_at_crossover == D(10)

    def test_grid_matches_the_crossover_story(self):
        quotes = [
            make_quote("ondo", "BTC", "short", open_fee_bps="2", close_fee_bps="2",
                       funding_8h_bps="-1"),
            make_quote("grvt", "BTC", "short", open_fee_bps="8", close_fee_bps="8",
                       funding_8h_bps="1"),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert sensitivity.cheapest_at(D(8)) == "ondo"
        assert sensitivity.cheapest_at(D(24)) == "ondo"
        assert sensitivity.cheapest_at(D(72)) == "grvt"
        assert sensitivity.cheapest_at(D(720)) == "grvt"
        assert sensitivity.grid["ondo"][D(24)] == D(7)
        assert sensitivity.grid["grvt"][D(24)] == D(13)

    def test_no_crossover_when_one_venue_dominates_everywhere(self):
        quotes = [
            make_quote("grvt", "BTC", "short", open_fee_bps="2", close_fee_bps="2",
                       funding_8h_bps="5"),
            make_quote("pacifica", "BTC", "short", open_fee_bps="8", close_fee_bps="8",
                       funding_8h_bps="-5"),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert sensitivity.crossovers == ()
        assert sensitivity.venue_is_horizon_dependent is False
        assert sensitivity.cheapest_at(D(8)) == "grvt"
        assert sensitivity.cheapest_at(D(720)) == "grvt"

    def test_identical_carry_never_crosses_regardless_of_fees(self):
        quotes = [
            make_quote("grvt", "BTC", "short", open_fee_bps="2", close_fee_bps="2",
                       funding_8h_bps="1"),
            make_quote("pacifica", "BTC", "short", open_fee_bps="8", close_fee_bps="8",
                       funding_8h_bps="1"),
        ]
        assert _sensitivity_for(quotes).crossovers == ()

    def test_crossover_beyond_the_search_window_is_not_reported(self):
        # Crossover sits at 48h; cap the search at 24h.
        quotes = [
            make_quote("ondo", "BTC", "short", open_fee_bps="2", close_fee_bps="2",
                       funding_8h_bps="-1"),
            make_quote("grvt", "BTC", "short", open_fee_bps="8", close_fee_bps="8",
                       funding_8h_bps="1"),
        ]
        assert _sensitivity_for(quotes, max_h=D(24)).crossovers == ()

    def test_three_venues_produce_a_two_step_frontier(self):
        """A cheapest early, B in the middle, C cheapest late.

        A: 2 bps fees, slope +0.25/h   (pays 2 bps/8h)
        B: 10 bps fees, slope 0        (flat carry)
        C: 26 bps fees, slope -0.25/h  (receives 2 bps/8h)
        A=B at h=32 (cost 10). B=C at h=64 (cost 10).
        """
        quotes = [
            make_quote("ondo", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="-2"),
            make_quote("pacifica", "BTC", "short", open_fee_bps="5", close_fee_bps="5",
                       funding_8h_bps="0"),
            make_quote("grvt", "BTC", "short", open_fee_bps="13", close_fee_bps="13",
                       funding_8h_bps="2"),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert [
            (c.at_hours, c.from_venue, c.to_venue) for c in sensitivity.crossovers
        ] == [(D(32), "ondo", "pacifica"), (D(64), "pacifica", "grvt")]

    def test_a_dominated_middle_venue_never_leads(self):
        """B is strictly worse than A everywhere, so the frontier skips it."""
        quotes = [
            make_quote("ondo", "BTC", "short", open_fee_bps="1", close_fee_bps="1",
                       funding_8h_bps="-1"),
            make_quote("pacifica", "BTC", "short", open_fee_bps="20", close_fee_bps="20",
                       funding_8h_bps="-1"),
            make_quote("grvt", "BTC", "short", open_fee_bps="4", close_fee_bps="4",
                       funding_8h_bps="1"),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert [c.to_venue for c in sensitivity.crossovers] == ["grvt"]
        assert "pacifica" not in {c.from_venue for c in sensitivity.crossovers}

    def test_single_candidate_has_no_crossover(self):
        quotes = [make_quote("grvt", "BTC", "short", funding_8h_bps="1")]
        sensitivity = _sensitivity_for(quotes)
        assert sensitivity.crossovers == ()
        assert sensitivity.venues == ("grvt",)

    def test_unavailable_venues_are_absent_from_the_grid(self):
        quotes = [
            make_quote("grvt", "BTC", "short", funding_8h_bps="1"),
            make_quote("pacifica", "BTC", "short", funding_8h_bps="9", available=False),
        ]
        sensitivity = _sensitivity_for(quotes)
        assert sensitivity.venues == ("grvt",)
        assert "pacifica" not in sensitivity.grid


# --------------------------------------------------------------------------------------
# quote_from_schedule
# --------------------------------------------------------------------------------------


class TestQuoteFromSchedule:
    def test_missing_carry_rate_yields_an_unavailable_quote(self):
        quote = quote_from_schedule(
            "grvt", "BTC", "short", D(10000), funding_rate_8h_bps=None
        )
        assert quote.available is False
        assert "no live funding" in quote.notes
        assert quote.funding_rate_8h_bps == D(0)

    def test_unavailable_quote_is_excluded_from_ranking(self):
        quotes = [
            quote_from_schedule("grvt", "BTC", "short", D(10000), funding_rate_8h_bps=None),
            quote_from_schedule("pacifica", "BTC", "short", D(10000), funding_rate_8h_bps=D(0)),
        ]
        ranked, excluded = rank_hedge_venues(
            "BTC", "short", D(10000), quotes, horizon_hours=D(24)
        )
        assert [c.venue for c in ranked] == ["pacifica"]
        assert [x.venue for x in excluded] == ["grvt"]

    def test_fees_come_from_the_schedule(self):
        quote = quote_from_schedule(
            "pacifica", "BTC", "short", D(10000), funding_rate_8h_bps=D("1.5")
        )
        assert quote.available is True
        assert quote.taker_fee_bps == FEE_SCHEDULE["pacifica"].open_fee_bps
        assert quote.close_fee_bps == FEE_SCHEDULE["pacifica"].close_fee_bps
        assert quote.base_asset == "BTC"
        assert "docs.pacifica.fi" in quote.notes

    def test_verified_venue_cites_source_in_notes(self):
        quote = quote_from_schedule(
            "jupiter", "SOL", "short", D(10000), funding_rate_8h_bps=D(0)
        )
        assert "jupiter-perps-fees.md" in quote.notes

    def test_state_dependent_venue_says_so_in_the_notes(self):
        quote = quote_from_schedule(
            "avantis", "BTC", "short", D(10000), funding_rate_8h_bps=D(0)
        )
        assert "openTakerFeeP" in quote.notes or "avantis" in quote.notes.lower()
        assert "UNVERIFIED PLACEHOLDER" not in quote.notes

    def test_ondo_fees_are_per_market_not_uniform(self):
        """Ondo posts 2.5 bps taker on most markets and 3.5 bps on twelve of them."""
        cheap = quote_from_schedule(
            "ondo", "BTC", "short", D(10000), funding_rate_8h_bps=D(0)
        )
        dear = quote_from_schedule(
            "ondo", "SOXL", "short", D(10000), funding_rate_8h_bps=D(0)
        )
        assert cheap.taker_fee_bps == D("2.5")
        assert dear.taker_fee_bps == D("3.5")
        assert dear.close_fee_bps == D("3.5")
        assert "promotional" in cheap.notes

    def test_borrow_only_venue_is_available_without_funding(self):
        quote = quote_from_schedule(
            "jupiter", "SOL", "short", D(10000),
            funding_rate_8h_bps=None, borrow_rate_8h_bps=D(5),
        )
        assert quote.available is True
        assert quote.borrow_rate_8h_bps == D(5)

    def test_unknown_venue_is_unavailable(self):
        quote = quote_from_schedule(
            "some_unknown_venue", "BTC", "short", D(10000), funding_rate_8h_bps=D(1)
        )
        assert quote.available is False


# --------------------------------------------------------------------------------------
# Base asset resolution
# --------------------------------------------------------------------------------------


class TestBaseAssetResolution:
    def test_explicit_base_asset_wins(self):
        quote = make_quote("grvt", "BTC", "short", market="BTC_USDT_Perp")
        assert engine.quote_base_asset(quote) == "BTC"

    @pytest.mark.parametrize(
        "market,expected",
        [
            ("BTC_USDT_Perp", "BTC"),
            ("BTC-PERP", "BTC"),
            ("ETH-PERP", "ETH"),
            ("SOL_USDC_Perp", "SOL"),
            ("WBTC", "BTC"),
            ("XBT", "BTC"),
        ],
    )
    def test_fallback_parses_venue_native_symbols(self, market, expected):
        assert engine._fallback_normalize_base(market) == expected

    def test_the_ingestion_alias_map_is_used_when_present(self):
        """`assets.py` ships `normalize_base_asset`; the engine must pick it up rather
        than silently falling back to its own cruder parser."""
        assets = pytest.importorskip("hedge_scanner.assets")
        if not hasattr(assets, "normalize_base_asset"):
            pytest.skip("ingestion alias map not available")
        assert engine._normalize_base is not None
        quote = make_quote("grvt", "", "short", market="BTC_USDT_Perp")
        quote.base_asset = ""
        assert engine.quote_base_asset(quote) == "BTC"

    def test_empty_base_asset_falls_back_to_the_market_string(self):
        quote = make_quote("grvt", "", "short", market="ETH-PERP")
        quote.base_asset = ""
        assert engine.quote_base_asset(quote) == "ETH"


# --------------------------------------------------------------------------------------
# Whole scan and JSON projection
# --------------------------------------------------------------------------------------


def _mixed_portfolio():
    positions = [
        make_position("grvt", "BTC", "long", "120000", mark_price="60000"),
        make_position("pacifica", "BTC", "short", "40000", mark_price="60000"),
        make_position("jupiter", "SOL", "long", "30000", mark_price="150"),
        make_position("pacifica", "ETH", "short", "20000", mark_price="3000"),
    ]
    quotes = [
        make_quote("pacifica", "BTC", "short", notional_usd="80000", funding_8h_bps="2.5"),
        make_quote("ondo", "BTC", "short", notional_usd="80000",
                   open_fee_bps="2.5", close_fee_bps="2.5", funding_8h_bps="-1"),
        make_quote("avantis", "BTC", "short", notional_usd="80000",
                   open_fee_bps="8", close_fee_bps="8", funding_8h_bps="4"),
        make_quote("grvt", "BTC", "short", notional_usd="80000",
                   open_fee_bps="4.5", close_fee_bps="4.5", available=False,
                   notes="requires user API key"),
        make_quote("grvt", "BTC", "long", notional_usd="80000", funding_8h_bps="3"),
        make_quote("pacifica", "BTC", "long", notional_usd="80000", funding_8h_bps="-1"),
        make_quote("grvt", "SOL", "short", notional_usd="30000", funding_8h_bps="-2"),
        make_quote("avantis", "ETH", "long", notional_usd="20000",
                   open_fee_bps="8", close_fee_bps="8", funding_8h_bps="1"),
    ]
    return positions, quotes


class TestScan:
    def test_end_to_end_shape(self):
        positions, quotes = _mixed_portfolio()
        result = scan(positions, quotes, addresses=["0x" + "ab" * 20])
        assert len(result.positions) == 4
        assert {e.base_asset for e in result.exposures} == {"BTC", "SOL", "ETH"}
        assert result.total_gross_notional_usd == D(210000)
        assert result.total_abs_net_notional_usd == D(130000)

        btc = next(o for o in result.delta_hedges if o.base_asset == "BTC")
        assert btc.hedge_side == "short"
        assert btc.hedge_notional_usd == D(80000)
        assert [x.venue for x in btc.excluded] == ["grvt"]
        assert "requires user API key" in btc.excluded[0].reason
        assert "grvt" not in {c.venue for c in btc.ranked}

        assert any(f.base_asset == "BTC" for f in result.self_hedge_findings)
        assert len(result.sensitivities) == len(
            [o for o in result.delta_hedges if o.ranked]
        )

    def test_horizon_config_changes_the_headline_ranking(self):
        positions, quotes = _mixed_portfolio()
        short = scan(positions, quotes, config=ScanConfig(horizon_hours=D(8)))
        long = scan(positions, quotes, config=ScanConfig(horizon_hours=D(720)))
        short_btc = next(o for o in short.delta_hedges if o.base_asset == "BTC")
        long_btc = next(o for o in long.delta_hedges if o.base_asset == "BTC")
        # 8h: pacifica 8 bps fees - 2.5 carry = 5.5, ondo 5 + 1 = 6, avantis 16 - 4 = 12.
        assert short_btc.best.venue == "pacifica"
        assert short_btc.best.total_bps == D("5.5")
        # 720h: carry dominates and the dearest-fee, best-carry venue wins.
        assert long_btc.best.venue == "avantis"
        assert long_btc.best.total_bps == D(16) - D(4) * D(90)
        assert long_btc.best.positive_carry is True

    def test_json_projection_is_serialisable_and_float_free(self):
        import json

        positions, quotes = _mixed_portfolio()
        result = scan(positions, quotes, addresses=["0x" + "ab" * 20])
        payload = scan_result_to_dict(result)
        encoded = json.dumps(payload)
        reloaded = json.loads(encoded)

        assert reloaded["schema_version"] == 1
        assert reloaded["assumptions"]["horizon_label"] == "1d"
        # Decimal('720').normalize() is '7.2E+2'; exponent notation must never escape.
        assert reloaded["assumptions"]["sensitivity_horizons_hours"] == [
            "8", "24", "72", "168", "720",
        ]
        assert not any(
            "E" in v for v in reloaded["assumptions"]["sensitivity_horizons_hours"]
        )
        assert reloaded["fee_schedule"]["jupiter"]["verified"] is True
        assert reloaded["fee_schedule"]["grvt"]["verified"] is True
        # Avantis is now a live-sourced stub (§12.3 follow-up): per-pair on the
        # live snapshot, so `fees_state_dependent` is True and `live` is True.
        assert reloaded["fee_schedule"]["avantis"]["fees_state_dependent"] is True
        assert reloaded["fee_schedule"]["avantis"]["live"] is True
        assert reloaded["avantis_comparison"]
        assert {c["verdict"] for c in reloaded["avantis_comparison"]} <= {
            "wins", "loses", "ties", "only_candidate", "no_quote",
        }

        def assert_no_floats(node):
            assert not isinstance(node, float), node
            if isinstance(node, dict):
                for value in node.values():
                    assert_no_floats(value)
            elif isinstance(node, list):
                for value in node:
                    assert_no_floats(value)

        assert_no_floats(payload)

    def test_venue_errors_are_carried_through(self):
        from hedge_scanner.models import VenueError

        errors = [
            VenueError(venue="grvt", kind="auth_required", message="requires user API key")
        ]
        result = scan([], [], venue_errors=errors)
        assert result.venue_errors == tuple(errors)
        payload = scan_result_to_dict(result)
        assert payload["venue_errors"][0]["kind"] == "auth_required"
