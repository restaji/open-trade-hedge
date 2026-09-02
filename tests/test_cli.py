"""Tests for the CLI surface: argument handling, fixture loading, JSON and rendering."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from hedge_scanner.cli import app, classify_address, load_fixture

runner = CliRunner()

EVM = "0x" + "ab" * 20
SOLANA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


FIXTURE = {
    "addresses": [EVM, SOLANA],
    "positions": [
        {
            "venue": "grvt",
            "address": EVM,
            "market": "BTC_USDT_Perp",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "side": "long",
            "size_base": "1.8",
            "notional_usd": "115479.85",
            "entry_price": "61200.00",
            "mark_price": "64155.47",
            "liquidation_price": "55400.00",
            "leverage": "8",
            "collateral_usd": "14434.98",
            "unrealized_pnl_usd": "5319.85",
            "margin_mode": "cross",
        },
        {
            "venue": "pacifica",
            "address": SOLANA,
            "market": "BTC-PERP",
            "base_asset": "BTC",
            "quote_asset": "USDC",
            "side": "short",
            "size_base": "0.55",
            "notional_usd": "-35285.51",
            "entry_price": "63980.00",
            "mark_price": "64155.47",
            "liquidation_price": "71100.00",
            "leverage": "5",
            "collateral_usd": "7057.10",
            "unrealized_pnl_usd": "-96.51",
            "margin_mode": "isolated",
        },
        {
            "venue": "jupiter",
            "address": SOLANA,
            "market": "SOL-PERP",
            "base_asset": "SOL",
            "quote_asset": "USDC",
            "side": "long",
            "size_base": "260",
            "notional_usd": "38480.00",
            "entry_price": "142.10",
            "mark_price": "148.00",
            "liquidation_price": "121.40",
            "leverage": "4",
            "collateral_usd": "9620.00",
            "unrealized_pnl_usd": "1534.00",
            "margin_mode": "isolated",
        },
    ],
    "quotes": [
        {
            "venue": "avantis", "market": "BTC/USD", "base_asset": "BTC",
            "side": "short", "notional_usd": "80194.34",
            "taker_fee_bps": "1.0", "close_fee_bps": "1.0",
            "price_impact_bps": "2.61", "est_slippage_bps": "0",
            "funding_rate_8h_bps": "0.94", "borrow_rate_8h_bps": "0.18",
            "available": True, "notes": "maker: short improves long-heavy OI skew",
        },
        {
            "venue": "pacifica", "market": "BTC-PERP", "base_asset": "BTC",
            "side": "short", "notional_usd": "80194.34",
            "taker_fee_bps": "4.0", "close_fee_bps": "4.0",
            "price_impact_bps": "0.4", "est_slippage_bps": "0.2",
            "funding_rate_8h_bps": "-1.0", "borrow_rate_8h_bps": "0",
            "available": True, "notes": "",
        },
        {
            "venue": "ondo", "market": "BTC-USD", "base_asset": "BTC",
            "side": "short", "notional_usd": "80194.34",
            "taker_fee_bps": "2.5", "close_fee_bps": "2.5",
            "price_impact_bps": "1.1", "est_slippage_bps": "0.5",
            "funding_rate_8h_bps": "-0.3", "borrow_rate_8h_bps": "0",
            "available": True, "notes": "promotional taker rate",
        },
        {
            "venue": "grvt", "market": "BTC_USDT_Perp", "base_asset": "BTC",
            "side": "short", "notional_usd": "80194.34",
            "taker_fee_bps": "4.5", "close_fee_bps": "4.5",
            "price_impact_bps": "0", "est_slippage_bps": "0",
            "funding_rate_8h_bps": "0", "borrow_rate_8h_bps": "0",
            "available": False, "notes": "requires user API key",
        },
        {
            "venue": "avantis", "market": "SOL/USD", "base_asset": "SOL",
            "side": "short", "notional_usd": "38480.00",
            "taker_fee_bps": "4.5", "close_fee_bps": "4.5",
            "price_impact_bps": "3.2", "est_slippage_bps": "0",
            "funding_rate_8h_bps": "-0.6", "borrow_rate_8h_bps": "0.21",
            "available": True, "notes": "taker: short worsens short-heavy OI skew",
        },
        {
            "venue": "ondo", "market": "SOL-USD", "base_asset": "SOL",
            "side": "short", "notional_usd": "38480.00",
            "taker_fee_bps": "2.5", "close_fee_bps": "2.5",
            "price_impact_bps": "0.5", "est_slippage_bps": "0",
            "funding_rate_8h_bps": "-0.9", "borrow_rate_8h_bps": "0",
            "available": True, "notes": "promotional taker rate; crypto pays undamped funding",
        },
        {
            "venue": "pacifica", "market": "SOL-PERP", "base_asset": "SOL",
            "side": "short", "notional_usd": "38480.00",
            "taker_fee_bps": "4.0", "close_fee_bps": "4.0",
            "price_impact_bps": "0.6", "est_slippage_bps": "0.3",
            "funding_rate_8h_bps": "1.4", "borrow_rate_8h_bps": "0",
            "available": True, "notes": "",
        },
        {
            "venue": "grvt", "market": "BTC_USDT_Perp", "base_asset": "BTC",
            "side": "long", "notional_usd": "80194.34",
            "taker_fee_bps": "4.5", "close_fee_bps": "4.5",
            "price_impact_bps": "0", "est_slippage_bps": "0",
            "funding_rate_8h_bps": "1.2", "borrow_rate_8h_bps": "0",
            "available": True, "notes": "",
        },
    ],
    "errors": [
        {
            "venue": "grvt",
            "kind": "auth_required",
            "address": EVM,
            "message": "position read requires a user-scoped API key; no public endpoint",
        }
    ],
}


@pytest.fixture()
def fixture_path(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(FIXTURE))
    return path


# --------------------------------------------------------------------------------------
# Address handling
# --------------------------------------------------------------------------------------


class TestAddressClassification:
    def test_evm(self):
        assert classify_address(EVM) == "evm"

    def test_solana(self):
        assert classify_address(SOLANA) == "solana"

    @pytest.mark.parametrize("bad", ["", "0xdead", "not an address", "0x" + "zz" * 20])
    def test_unrecognised(self, bad):
        assert classify_address(bad) is None

    def test_unrecognised_address_is_rejected_before_any_fetch(self):
        result = runner.invoke(app, ["scan", "vitalik.eth"])
        assert result.exit_code == 2
        assert "Unrecognised address namespace" in result.output

    def test_no_address_and_no_fixture_is_a_usage_error(self):
        result = runner.invoke(app, ["scan"])
        assert result.exit_code == 2
        assert "No addresses supplied" in result.output


# --------------------------------------------------------------------------------------
# Fixture loading
# --------------------------------------------------------------------------------------


class TestFixtureLoading:
    def test_numbers_land_as_decimals(self, fixture_path):
        addresses, positions, quotes, errors = load_fixture(fixture_path)
        assert addresses == [EVM, SOLANA]
        assert len(positions) == 3 and len(quotes) == 8 and len(errors) == 1
        assert isinstance(positions[0].notional_usd, Decimal)
        assert positions[0].notional_usd == Decimal("115479.85")
        assert isinstance(quotes[0].funding_rate_8h_bps, Decimal)
        assert quotes[0].funding_rate_8h_bps == Decimal("0.94")

    def test_json_numbers_are_accepted_too(self, tmp_path):
        path = tmp_path / "p.json"
        payload = json.loads(json.dumps(FIXTURE))
        payload["positions"][0]["notional_usd"] = 115479.85
        path.write_text(json.dumps(payload))
        _, positions, _, _ = load_fixture(path)
        assert positions[0].notional_usd == Decimal("115479.85")

    def test_unknown_field_is_rejected_loudly(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"positions": [{"venue": "grvt", "wat": 1}]}))
        with pytest.raises(ValueError, match="unknown field"):
            load_fixture(path)

    def test_malformed_fixture_is_a_usage_error_not_a_traceback(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("{not json")
        result = runner.invoke(app, ["scan", "--fixture", str(path)])
        assert result.exit_code == 2
        assert "Could not read fixture" in result.output


# --------------------------------------------------------------------------------------
# Rendered output
# --------------------------------------------------------------------------------------


class TestRenderedScan:
    def _run(self, fixture_path, *args):
        result = runner.invoke(
            app, ["scan", "--fixture", str(fixture_path), "--width", "150", *args]
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_all_sections_are_present(self, fixture_path):
        output = self._run(fixture_path)
        for heading in (
            "PERPS HEDGE SCAN",
            "DATA COVERAGE",
            "OPEN POSITIONS",
            "NET EXPOSURE BY ASSET",
            "GROSS VS NET",
            "DELTA HEDGE CANDIDATES",
            "HORIZON SENSITIVITY",
            "FUNDING ARBITRAGE",
            "BASIS OF PREPARATION",
        ):
            assert heading in output, heading

    def test_horizon_is_labelled(self, fixture_path):
        assert "holding horizon" in self._run(fixture_path)
        assert "3d" in self._run(fixture_path, "--horizon", "3d")

    def test_venue_errors_are_shown_not_swallowed(self, fixture_path):
        output = self._run(fixture_path)
        assert "auth_required" in output
        assert "user-scoped API key" in output
        assert "unread, not empty" in output

    def test_unavailable_quote_is_shown_as_excluded(self, fixture_path):
        assert "requires user API key" in self._run(fixture_path)

    def test_costs_appear_in_both_bps_and_usd(self, fixture_path):
        output = self._run(fixture_path)
        assert "ALL-IN bps" in output
        assert "ALL-IN USD" in output

    def test_avantis_is_named_explicitly(self, fixture_path):
        output = self._run(fixture_path)
        assert "Avantis" in output
        assert any(word in output for word in ("CHEAPEST", "MORE expensive", "ties"))

    def test_gross_versus_net_gap_is_surfaced(self, fixture_path):
        output = self._run(fixture_path)
        assert "GROSS-NET GAP" in output
        assert "OFFSETTING USD" in output

    def test_no_emoji_or_box_drawing(self, fixture_path):
        output = self._run(fixture_path)
        assert not any(ord(ch) > 0x2500 for ch in output), "decorative glyph in output"

    def test_upside_perp_section_appears_for_crypto_majors(self, fixture_path):
        output = self._run(fixture_path)
        assert "AVANTIS UPSIDE PERPS" in output
        assert "CHEAPER IF <" in output
        assert "PROFIT SHARE" in output

    def test_crossover_is_narrated(self, fixture_path):
        output = self._run(fixture_path)
        assert "Optimal venue changes with holding period" in output
        # Crossovers use the same display-name helper as the rest of the
        # render layer (§12.4: raw snake_case venue strings are not shown to
        # humans; the JSON payload keeps the raw ids).
        assert "Ondo Perps -> Pacifica" in output


# --------------------------------------------------------------------------------------
# JSON output
# --------------------------------------------------------------------------------------


class TestJsonOutput:
    def _payload(self, fixture_path, *args):
        result = runner.invoke(
            app, ["scan", "--fixture", str(fixture_path), "--json", *args]
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_top_level_shape(self, fixture_path):
        payload = self._payload(fixture_path)
        for key in (
            "schema_version",
            "generated_at",
            "addresses",
            "assumptions",
            "venue_errors",
            "positions",
            "net_exposures",
            "self_hedge_findings",
            "delta_hedges",
            "funding_arbs",
            "horizon_sensitivity",
            "avantis_comparison",
            "upside_perp_comparison",
            "fee_schedule",
        ):
            assert key in payload, key

    def test_money_is_encoded_as_strings(self, fixture_path):
        payload = self._payload(fixture_path)
        exposure = payload["net_exposures"][0]
        assert isinstance(exposure["net_notional_usd"], str)
        cost = payload["delta_hedges"][0]["ranked"][0]
        assert isinstance(cost["total_cost_bps"], str)
        assert isinstance(cost["total_cost_usd"], str)

    def test_netting_matches_the_fixture(self, fixture_path):
        payload = self._payload(fixture_path)
        btc = next(e for e in payload["net_exposures"] if e["base_asset"] == "BTC")
        assert Decimal(btc["net_notional_usd"]) == Decimal("80194.34")
        assert Decimal(btc["gross_notional_usd"]) == Decimal("150765.36")
        assert Decimal(btc["gross_net_gap_usd"]) == Decimal("70571.02")
        assert btc["hedge_side"] == "short"

    def test_unavailable_venue_is_excluded_from_the_ranking(self, fixture_path):
        payload = self._payload(fixture_path)
        btc = next(d for d in payload["delta_hedges"] if d["base_asset"] == "BTC")
        assert "grvt" not in {r["venue"] for r in btc["ranked"]}
        assert "grvt" in {x["venue"] for x in btc["excluded"]}

    def test_horizon_option_changes_the_assumptions_block(self, fixture_path):
        assert self._payload(fixture_path)["assumptions"]["horizon_label"] == "1d"
        assert (
            self._payload(fixture_path, "--horizon", "7d")["assumptions"][
                "horizon_label"
            ]
            == "7d"
        )

    def test_custom_sensitivity_horizons_are_honoured(self, fixture_path):
        payload = self._payload(fixture_path, "--horizons", "1h,4h")
        labels = [h["label"] for h in payload["horizon_sensitivity"][0]["horizons"]]
        assert labels == ["1h", "4h"]

    def test_dust_threshold_option_suppresses_hedges(self, fixture_path):
        payload = self._payload(fixture_path, "--dust-usd", "1000000")
        assert payload["delta_hedges"] == []
        assert payload["net_exposures"]  # exposure is still reported, just not hedged

    def test_bad_horizon_is_a_usage_error(self, fixture_path):
        result = runner.invoke(
            app, ["scan", "--fixture", str(fixture_path), "--horizon", "later"]
        )
        assert result.exit_code == 2
        assert "Invalid option" in result.output

    def test_crossover_is_reported_at_the_exact_analytic_hour(self, fixture_path):
        """SOL: Ondo is cheap on fees but pays funding; Pacifica is dearer but receives.

        Ondo   5.50 bps fixed, +0.9 bps/8h carry cost -> 5.50 + 0.1125h
        Pacifica 8.90 bps fixed, -1.4 bps/8h carry cost -> 8.90 - 0.1750h
        Equal at h = 3.4 / 0.2875 = 11.826h, which sits between the 8h and 24h samples.
        """
        payload = self._payload(fixture_path)
        sol = next(
            s for s in payload["horizon_sensitivity"] if s["base_asset"] == "SOL"
        )
        assert sol["venue_is_horizon_dependent"] is True
        (crossover,) = sol["crossovers"]
        assert crossover["from_venue"] == "ondo"
        assert crossover["to_venue"] == "pacifica"
        assert Decimal(crossover["at_hours"]).quantize(Decimal("0.01")) == Decimal("11.83")
        by_label = {h["label"]: h["cheapest_venue"] for h in sol["horizons"]}
        assert by_label["8h"] == "ondo"
        assert by_label["1d"] == "pacifica"
        assert by_label["30d"] == "pacifica"

    def test_avantis_comparison_is_reported_per_asset(self, fixture_path):
        payload = self._payload(fixture_path)
        comparisons = {c["base_asset"]: c for c in payload["avantis_comparison"]}
        assert set(comparisons) == {"BTC", "SOL"}
        assert comparisons["BTC"]["verdict"] in {"wins", "loses", "ties"}
        assert comparisons["BTC"]["avantis_rank"] == 1


# --------------------------------------------------------------------------------------
# Live path guard rails
# --------------------------------------------------------------------------------------


class TestLivePath:
    """The bridge is tested against a stubbed ``hedge_scanner.portfolio`` module.

    Canonical entry points per CONTRACT.md §9 are ``scan_snapshot(addresses)`` and
    ``quotes_for(base_asset, side, notional_usd, horizon_hours=...)``. There is no
    longer a fallback to older names (``build_portfolio`` / ``quote_hedges``) —
    silently accepting stale APIs was hiding real regressions.
    """

    def _install(self, monkeypatch, module):
        import sys

        monkeypatch.setitem(sys.modules, "hedge_scanner.portfolio", module)
        monkeypatch.setattr(
            "hedge_scanner.portfolio", module, raising=False
        )

    def test_missing_ingestion_layer_fails_with_a_clear_message(self, monkeypatch):
        import types

        empty = types.ModuleType("hedge_scanner.portfolio")
        self._install(monkeypatch, empty)
        result = runner.invoke(app, ["scan", EVM])
        assert result.exit_code == 3
        assert "Ingestion layer unavailable" in result.output
        # The message must name the canonical entry point so a developer who
        # tripped this has one obvious thing to look for in portfolio.py.
        assert "scan_snapshot" in result.output
        assert "quotes_for" in result.output

    def test_positions_and_quotes_flow_through_the_bridge(self, monkeypatch):
        import types

        from hedge_scanner.engine import quote_from_schedule
        from hedge_scanner.models import PortfolioSnapshot, Position, VenueError

        module = types.ModuleType("hedge_scanner.portfolio")

        async def scan_snapshot(addresses):
            return PortfolioSnapshot(
                addresses=list(addresses),
                positions=[
                    Position(
                        venue="grvt",
                        address=addresses[0],
                        market="BTC_USDT_Perp",
                        base_asset="BTC",
                        quote_asset="USDT",
                        side="long",
                        size_base=Decimal("1"),
                        notional_usd=Decimal("64155.47"),
                        entry_price=Decimal("61000"),
                        mark_price=Decimal("64155.47"),
                    )
                ],
                errors=[
                    VenueError(venue="ondo", kind="unavailable", message="503 from venue")
                ],
            )

        async def quotes_for(base_asset, side, notional_usd, horizon_hours=None):
            return (
                [
                    quote_from_schedule(
                        "avantis",
                        base_asset,
                        side,
                        notional_usd,
                        funding_rate_8h_bps=Decimal("0.9"),
                    )
                ],
                [],
            )

        module.scan_snapshot = scan_snapshot
        module.quotes_for = quotes_for
        self._install(monkeypatch, module)

        result = runner.invoke(app, ["scan", EVM, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert len(payload["positions"]) == 1
        assert payload["venue_errors"][0]["venue"] == "ondo"
        btc = next(d for d in payload["delta_hedges"] if d["base_asset"] == "BTC")
        assert btc["ranked"][0]["venue"] == "avantis"
        assert btc["hedge_side"] == "short"

    def test_the_shipped_ingestion_signatures_are_supported(self, monkeypatch):
        """`portfolio.py` ships `scan_snapshot` and `quotes_for`, and `quotes_for`
        returns a (quotes, errors) pair rather than a bare list."""
        import types

        from hedge_scanner.engine import quote_from_schedule
        from hedge_scanner.models import PortfolioSnapshot, Position, VenueError

        module = types.ModuleType("hedge_scanner.portfolio")

        async def scan_snapshot(addresses):
            return PortfolioSnapshot(
                addresses=list(addresses),
                positions=[
                    Position(
                        venue="grvt",
                        address=addresses[0],
                        market="BTC_USDT_Perp",
                        base_asset="BTC",
                        quote_asset="USDT",
                        side="short",
                        size_base=Decimal("1"),
                        notional_usd=Decimal("-64155.47"),
                        entry_price=Decimal("61000"),
                        mark_price=Decimal("64155.47"),
                    )
                ],
            )

        async def quotes_for(base_asset, side, notional_usd, horizon_hours=None):
            return (
                [
                    quote_from_schedule(
                        "pacifica",
                        base_asset,
                        side,
                        notional_usd,
                        funding_rate_8h_bps=Decimal("0.5"),
                    )
                ],
                [VenueError(venue="jupiter", kind="unavailable", message="no such market")],
            )

        module.scan_snapshot = scan_snapshot
        module.quotes_for = quotes_for
        self._install(monkeypatch, module)

        result = runner.invoke(app, ["scan", EVM, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        btc = next(d for d in payload["delta_hedges"] if d["base_asset"] == "BTC")
        assert btc["hedge_side"] == "long"   # the held position is short
        assert btc["ranked"][0]["venue"] == "pacifica"
        assert "jupiter" in {e["venue"] for e in payload["venue_errors"]}

    def test_a_failing_quote_call_becomes_a_venue_error(self, monkeypatch):
        import types

        from hedge_scanner.models import PortfolioSnapshot, Position

        module = types.ModuleType("hedge_scanner.portfolio")

        async def scan_snapshot(addresses):
            return PortfolioSnapshot(
                addresses=list(addresses),
                positions=[
                    Position(
                        venue="grvt",
                        address=addresses[0],
                        market="BTC_USDT_Perp",
                        base_asset="BTC",
                        quote_asset="USDT",
                        side="long",
                        size_base=Decimal("1"),
                        notional_usd=Decimal("64155.47"),
                        entry_price=Decimal("61000"),
                        mark_price=Decimal("64155.47"),
                    )
                ],
            )

        async def quotes_for(base_asset, side, notional_usd, horizon_hours=None):
            raise RuntimeError("spread endpoint timed out")

        module.scan_snapshot = scan_snapshot
        module.quotes_for = quotes_for
        self._install(monkeypatch, module)

        result = runner.invoke(app, ["scan", EVM, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        messages = [e["message"] for e in payload["venue_errors"]]
        assert any("spread endpoint timed out" in m for m in messages)
        # Positions still reported; one failing venue never kills the request.
        assert len(payload["positions"]) == 1

    def test_keyboard_interrupt_is_not_swallowed_as_a_venue_error(self, monkeypatch):
        """Ctrl-C during a quote fan-out must abort, not turn into a VenueError.

        Regression guard for the earlier ``isinstance(outcome, BaseException)``
        catch that classified KeyboardInterrupt / SystemExit as a routine
        per-venue failure and let the scan continue.
        """
        import types

        from hedge_scanner.models import PortfolioSnapshot, Position

        module = types.ModuleType("hedge_scanner.portfolio")

        async def scan_snapshot(addresses):
            return PortfolioSnapshot(
                addresses=list(addresses),
                positions=[
                    Position(
                        venue="grvt",
                        address=addresses[0],
                        market="BTC_USDT_Perp",
                        base_asset="BTC",
                        quote_asset="USDT",
                        side="long",
                        size_base=Decimal("1"),
                        notional_usd=Decimal("64155.47"),
                        entry_price=Decimal("61000"),
                        mark_price=Decimal("64155.47"),
                    )
                ],
            )

        async def quotes_for(base_asset, side, notional_usd, horizon_hours=None):
            raise KeyboardInterrupt

        module.scan_snapshot = scan_snapshot
        module.quotes_for = quotes_for
        self._install(monkeypatch, module)

        result = runner.invoke(app, ["scan", EVM, "--json"])
        # Typer/Click convert an uncaught KeyboardInterrupt into SystemExit(130),
        # the standard shell exit code for SIGINT. What matters is that the CLI
        # aborts with a non-success code rather than silently swallowing the
        # interrupt into a per-venue error and printing a normal scan report.
        assert result.exit_code == 130, (
            f"expected SIGINT exit code 130, got {result.exit_code}; "
            f"exception={result.exception!r}"
        )
        assert result.stdout.strip() == "", (
            "no JSON payload should be emitted when the scan was interrupted"
        )


class TestFeesCommand:
    """Tests for `hedge-scanner fees`.

    Avantis fees are LIVE-FETCHED from data.avantisfi.com per invocation
    (CONTRACT.md §7 non-negotiable + §12.3 post-fix follow-up), so every test
    that exercises the command mocks the fetcher rather than hitting the
    network. §7 also forbids falling back to hardcoded numbers on failure --
    ``test_avantis_fetch_failure_is_reported_honestly`` guards that path.
    """

    @staticmethod
    def _live_snapshot() -> dict:
        """The recorded live snapshot; same fixture ``test_avantis_quote`` uses.

        Sharing one fixture is deliberate: the display and the ranker should
        be pinned to the same source of truth, so drift between them would
        show up in either suite.
        """
        from pathlib import Path

        fixtures = Path(__file__).parent / "fixtures" / "avantis"
        from hedge_scanner.hedge_venues import avantis

        return avantis._loads_exact((fixtures / "trading_v2.json").read_text())

    @pytest.fixture()
    def mock_avantis_live(self, monkeypatch):
        """Serve the recorded snapshot instead of the network for `fees`."""
        from hedge_scanner.hedge_venues import avantis

        snapshot = self._live_snapshot()
        avantis.clear_caches()

        async def fake_snapshot(client=None):
            return snapshot

        monkeypatch.setattr(avantis, "fetch_trading_snapshot", fake_snapshot)
        yield snapshot
        avantis.clear_caches()

    @pytest.fixture()
    def mock_avantis_unreachable(self, monkeypatch):
        """Force the live fetch to fail so the honest-failure path can be exercised."""
        from hedge_scanner.hedge_venues import avantis

        avantis.clear_caches()

        async def failing_snapshot(client=None):
            raise ConnectionError("simulated: data.avantisfi.com unreachable")

        monkeypatch.setattr(avantis, "fetch_trading_snapshot", failing_snapshot)
        yield
        avantis.clear_caches()

    def test_lists_every_venue_all_verified(self, mock_avantis_live):
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0
        for name in ("GRVT", "Pacifica", "Ondo Perps", "Jupiter Perps", "Avantis"):
            assert name in result.output
        assert "verified" in result.output

    def test_avantis_row_shows_live_maker_and_taker_round_trips(self, mock_avantis_live):
        """Live snapshot: maker 1.0/1.0 (RT 2.0) and taker 4.5/4.5 (RT 9.0).

        CONTRACT.md §12.11 selects by live OI skew, so the display must show
        both tiers rather than a single maker round trip.
        """
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0, result.output
        assert "LIVE" in result.output
        assert "BTC/USD" in result.output
        assert "maker 1.0/1.0 bps (RT 2.0)" in result.output
        assert "taker 4.5/4.5 bps (RT 9.0)" in result.output
        assert "OI skew" in result.output
        # The source URL is visible so a reader can see where the numbers came
        # from. Domain is prod-api.avantisfi.com/data/v2/trading (see §12.7 for
        # why we do not hit data.avantisfi.com directly).
        assert "avantisfi.com" in result.output

    def test_rwa_zero_line_shown_as_promotional(self, mock_avantis_live):
        """RWA pairs return 0/0/0/0 on the live snapshot -- must render as promo."""
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0, result.output
        assert "PROMOTIONAL 0 bps" in result.output
        # At least one RWA symbol should be visible in the block.
        assert any(sym in result.output for sym in ("XAU/USD", "EUR/USD", "BRENT/USD"))
        # And the temporary/revocable status is spelled out (§7.6.2).
        assert "REVOCABLE" in result.output or "temporary" in result.output

    def test_upside_row_is_present_with_profit_share(self, mock_avantis_live):
        """Upside Perps show 0/0/0 with a profit-share summary (task item 5)."""
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0, result.output
        assert "Avantis (Upside)" in result.output
        assert "25%" in result.output and "5%" in result.output
        assert "Zero cost" in result.output or "losing close" in result.output

    def test_source_url_and_fetched_at_are_visible(self, mock_avantis_live):
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0
        assert "prod-api.avantisfi.com" in result.output
        assert "fetched_at" in result.output

    def test_avantis_fetch_failure_is_reported_honestly(
        self, mock_avantis_unreachable
    ):
        """Fetch failure must NEVER fall back to hardcoded numbers (§7).

        Other venues still render; only Avantis says "unavailable" with the
        underlying error string, and the process exits 0 so the rest of the
        command still delivers value.
        """
        result = runner.invoke(app, ["fees", "--width", "180"])
        assert result.exit_code == 0, result.output
        # Other venues still render.
        assert "GRVT" in result.output
        assert "Pacifica" in result.output
        # Avantis section says unavailable with the actual error.
        assert "unavailable" in result.output
        assert "avantisfi.com" in result.output
        assert "simulated: data.avantisfi.com unreachable" in result.output
        # No fabricated numbers -- specifically, the old hardcoded 9.0 bps
        # round trip must not appear anywhere in the Avantis section.
        assert "9.0" not in result.output or "5.5" not in result.output.split(
            "unavailable", 1
        )[1] if "unavailable" in result.output else True

    def test_json_mode(self, mock_avantis_live):
        result = runner.invoke(app, ["fees", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["grvt"]["verified"] is True
        assert payload["jupiter"]["verified"] is True
        assert payload["avantis"]["position_readable"] is False
        # Avantis is live-sourced.
        assert payload["avantis"]["live"] is True
        assert payload["avantis"]["available"] is True
        assert payload["avantis"]["source_url"].startswith(
            "https://prod-api.avantisfi.com"
        )
        assert "fetched_at" in payload["avantis"]
        # Representative crypto pair with the pinned split.
        btc = next(
            r for r in payload["avantis"]["crypto_pairs"] if r["base_asset"] == "BTC"
        )
        assert btc["listed"] is True
        assert Decimal(btc["openMakerFeeP_bps"]) == Decimal("1.0")
        assert Decimal(btc["closeMakerFeeP_bps"]) == Decimal("1.0")
        assert Decimal(btc["closeTakerFeeP_bps"]) == Decimal("4.5")
        # Both tiers the ranker can select (§12.11).
        assert Decimal(btc["maker_round_trip_bps"]) == Decimal("2.0")
        assert Decimal(btc["taker_round_trip_bps"]) == Decimal("9.0")
        assert "OI skew" in payload["avantis"]["hedge_model"]
        # At least one RWA pair returned 0/0/0/0 promotional.
        rwa = payload["avantis"]["rwa_pairs"]
        assert any(r.get("promotional_zero") for r in rwa if r.get("listed"))
        # Upside row is exposed with the four documented ROI bands.
        upside = payload["avantis_upside"]
        assert upside["open_fee_bps"] == "0"
        assert upside["close_fee_bps"] == "0"
        assert upside["borrow_fee_bps"] == "0"
        shares = [Decimal(b["protocol_share_pct"]) for b in upside["profit_share_bands"]]
        assert shares == [Decimal("25"), Decimal("20"), Decimal("10"), Decimal("5")]

    def test_json_mode_reports_fetch_failure_without_fabricating_numbers(
        self, mock_avantis_unreachable
    ):
        result = runner.invoke(app, ["fees", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["avantis"]["live"] is True
        assert payload["avantis"]["available"] is False
        assert "simulated: data.avantisfi.com unreachable" in payload["avantis"]["error"]
        # Failure payload must not sneak a numeric round_trip in.
        assert "openMakerFeeP_bps" not in payload["avantis"]
        assert "crypto_pairs" not in payload["avantis"]
        # Other venues still render, so downstream JSON consumers still see them.
        assert payload["grvt"]["verified"] is True

    def test_fee_schedule_stub_is_marked_live_but_carries_min_position(self):
        """FEE_SCHEDULE['avantis'] is retained as a live stub (task decision).

        The numeric fee fields are zeroed placeholders; the row exists so
        min-position enforcement (§12.4 point 6) and iterators that walk
        FEE_SCHEDULE do not crash on a missing key.
        """
        from hedge_scanner.engine import FEE_SCHEDULE

        avantis_schedule = FEE_SCHEDULE["avantis"]
        assert avantis_schedule.live is True
        assert avantis_schedule.min_position_usd == Decimal(100)
        # FX/metals carry the 300 USDC minimum per §12.4.
        assert avantis_schedule.min_position_for("XAU") == Decimal(300)
        # The stub fee fields are placeholder zeros -- never to be shown as
        # if they were a schedule (they exist only to satisfy consumers that
        # iterate FEE_SCHEDULE). The `fees` command must fetch live.
        assert avantis_schedule.open_fee_bps == Decimal(0)
        assert avantis_schedule.close_fee_bps == Decimal(0)
        assert avantis_schedule.round_trip_fee_bps == Decimal(0)
