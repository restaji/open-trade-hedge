"""Terminal rendering for the hedge scanner.

House style: dense, tabular, monochrome. Restrained colour is used only where it
carries information (sign of a cost, sign of a carry rate), never for decoration.
No emoji, no ASCII art, no gradients, no borders heavier than a single rule.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from hedge_scanner.engine import (
    FEE_SCHEDULE,
    PRIMARY_HEDGE_VENUE,
    ZERO,
    AvantisComparison,
    DeltaHedgeOpportunity,
    FundingArbOpportunity,
    HedgeCost,
    HorizonSensitivity,
    LiquidationRiskResult,
    NetExposure,
    ScanResult,
    SelfHedgeFinding,
    UpsideHedgeComparison,
    format_horizon,
    _signed_notional,
)
from hedge_scanner.models import Position

UNVERIFIED_MARK = "*"
SIZE_MISMATCH_MARK = "~"
STATE_DEPENDENT_MARK = "+"

HEADER_STYLE = "dim"
LABEL_STYLE = "dim"
RULE_STYLE = "dim"
GOOD_STYLE = "green"
BAD_STYLE = "red"
BEST_STYLE = "bold"

# Venue-name overrides for rendering. Some venues are quoted as their own row
# but are not in ``FEE_SCHEDULE`` (Upside Perps have no static fee schedule -- a
# hardcoded taker/maker constant would misrepresent the profit-share model).
# These map the raw venue string used in JSON and rankings to a human label.
_VENUE_DISPLAY_OVERRIDES: dict[str, str] = {
    "avantis_upside": "Avantis (Upside)",
}


def venue_display_name(venue: str) -> str:
    """Human-facing venue label. The raw ``venue`` string is preserved in JSON."""
    override = _VENUE_DISPLAY_OVERRIDES.get(venue)
    if override is not None:
        return override
    schedule = FEE_SCHEDULE.get(venue)
    if schedule is not None:
        return schedule.display_name
    return venue


# --------------------------------------------------------------------------------------
# Scalar formatting
# --------------------------------------------------------------------------------------


def fmt_usd(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "--"
    quantum = Decimal(1).scaleb(-places)
    return f"{Decimal(value).quantize(quantum):,.{places}f}"


def fmt_signed_usd(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "--"
    value = Decimal(value)
    sign = "-" if value < ZERO else "+"
    return f"{sign}{fmt_usd(abs(value), places)}"


def fmt_bps(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "--"
    quantum = Decimal(1).scaleb(-places)
    return f"{Decimal(value).quantize(quantum):,.{places}f}"


def fmt_signed_bps(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "--"
    value = Decimal(value)
    return f"{'-' if value < ZERO else '+'}{fmt_bps(abs(value), places)}"


def fmt_hours(value: Decimal | None) -> str:
    if value is None:
        return "never"
    hours = Decimal(value)
    if hours >= Decimal(48):
        return f"{(hours / Decimal(24)).quantize(Decimal('0.1'))}d"
    return f"{hours.quantize(Decimal('0.1'))}h"


def fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "--"
    value = Decimal(value)
    magnitude = abs(value)
    if magnitude >= Decimal(10_000):
        places = 0
    elif magnitude >= Decimal(1):
        places = 2
    else:
        places = 6
    return fmt_usd(value, places)


def _cost_text(value: Decimal, formatter, *, best: bool = False) -> Text:
    """Colour a cost by sign: negative cost is money in."""
    style = GOOD_STYLE if value < ZERO else ""
    if best:
        style = f"{BEST_STYLE} {style}".strip()
    return Text(formatter(value), style=style or None)


def _section(console: Console, title: str) -> None:
    console.print()
    console.print(Rule(Text(title, style="bold"), align="left", style=RULE_STYLE))


def _base_table(**kwargs) -> Table:
    return Table(
        box=box.SIMPLE,
        header_style=HEADER_STYLE,
        pad_edge=False,
        show_edge=False,
        **kwargs,
    )


def _note(console: Console, text: str, *, style: str = LABEL_STYLE, indent: int = 2) -> None:
    """Print a wrapped commentary line with a hanging indent."""
    console.print(Padding(Text(text, style=style), (0, 0, 0, indent)))


# --------------------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------------------


def render_header(console: Console, result: ScanResult) -> None:
    horizon = format_horizon(result.horizon_hours)
    console.print(Rule(Text("PERPS HEDGE SCAN", style="bold"), align="left", style=RULE_STYLE))

    meta = _base_table(show_header=False)
    meta.add_column(style=LABEL_STYLE, no_wrap=True)
    meta.add_column()
    meta.add_row(
        "addresses",
        ", ".join(result.addresses) if result.addresses else "(none supplied)",
    )
    meta.add_row("generated", result.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    meta.add_row("holding horizon", f"{horizon} (all cost figures assume this horizon)")
    meta.add_row(
        "portfolio",
        f"{len(result.positions)} open position(s) | "
        f"gross {fmt_usd(result.total_gross_notional_usd, 0)} USD | "
        f"abs net {fmt_usd(result.total_abs_net_notional_usd, 0)} USD",
    )
    console.print(meta)


# --------------------------------------------------------------------------------------
# Data coverage
# --------------------------------------------------------------------------------------


def render_venue_errors(console: Console, errors: Iterable[object]) -> None:
    errors = list(errors)
    if not errors:
        return
    _section(console, "DATA COVERAGE — venues not read")
    table = _base_table()
    table.add_column("VENUE", no_wrap=True)
    table.add_column("KIND", no_wrap=True)
    table.add_column("ADDRESS", no_wrap=True, style=LABEL_STYLE)
    table.add_column("DETAIL")
    for error in errors:
        address = getattr(error, "address", None) or ""
        table.add_row(
            str(getattr(error, "venue", "?")),
            str(getattr(error, "kind", "error")),
            f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address,
            str(getattr(error, "message", error)),
        )
    console.print(table)
    console.print(
        Text(
            "These venues are unread, not empty. Net exposure below may be incomplete.",
            style=LABEL_STYLE,
        )
    )


# --------------------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------------------


def render_positions(console: Console, positions: Iterable[Position]) -> None:
    positions = list(positions)
    _section(console, "OPEN POSITIONS")
    if not positions:
        console.print(Text("No open positions found for the supplied address(es).", style=LABEL_STYLE))
        return

    by_venue: dict[str, list[Position]] = {}
    for position in positions:
        by_venue.setdefault(position.venue, []).append(position)

    table = _base_table()
    table.add_column("VENUE", no_wrap=True)
    table.add_column("MARKET", no_wrap=True)
    table.add_column("ASSET", no_wrap=True)
    table.add_column("SIDE", no_wrap=True)
    table.add_column("SIZE", justify="right")
    table.add_column("NOTIONAL", justify="right")
    table.add_column("ENTRY", justify="right")
    table.add_column("MARK", justify="right")
    table.add_column("LEV", justify="right")
    table.add_column("LIQ", justify="right")
    table.add_column("uPNL", justify="right")

    for venue in sorted(by_venue):
        group = sorted(by_venue[venue], key=lambda p: -abs(p.notional_usd))
        subtotal = sum((_signed_notional(p) for p in group), ZERO)
        for position in group:
            pnl = position.unrealized_pnl_usd
            table.add_row(
                venue_display_name(venue),
                position.market,
                position.base_asset,
                position.side,
                fmt_usd(position.size_base, 4),
                fmt_signed_usd(_signed_notional(position), 0),
                fmt_price(position.entry_price),
                fmt_price(position.mark_price),
                f"{Decimal(position.leverage).quantize(Decimal('0.1'))}x"
                if position.leverage is not None
                else "--",
                fmt_price(position.liquidation_price),
                Text(
                    fmt_signed_usd(pnl, 0),
                    style=(BAD_STYLE if pnl is not None and pnl < ZERO else GOOD_STYLE)
                    if pnl is not None
                    else None,
                ),
            )
        if len(group) > 1:
            table.add_row(
                "",
                Text("subtotal", style=LABEL_STYLE),
                "", "", "",
                Text(fmt_signed_usd(subtotal, 0), style=LABEL_STYLE),
                "", "", "", "", "",
            )
    console.print(table)


# --------------------------------------------------------------------------------------
# Net exposure
# --------------------------------------------------------------------------------------


def render_net_exposure(
    console: Console,
    exposures: Iterable[NetExposure],
    dust: Iterable[NetExposure],
    dust_threshold_usd: Decimal,
) -> None:
    exposures = list(exposures)
    dust = list(dust)
    _section(console, "NET EXPOSURE BY ASSET")
    if not exposures and not dust:
        console.print(Text("Nothing to net.", style=LABEL_STYLE))
        return

    table = _base_table()
    table.add_column("ASSET", no_wrap=True)
    table.add_column("LONG USD", justify="right")
    table.add_column("SHORT USD", justify="right")
    table.add_column("GROSS USD", justify="right")
    table.add_column("NET USD", justify="right")
    table.add_column("DIR", no_wrap=True)
    table.add_column("GROSS-NET GAP", justify="right", no_wrap=True)
    table.add_column("LEGS", justify="right")
    table.add_column("VENUES")

    for exposure in exposures + dust:
        is_dust = exposure.abs_net_notional_usd < dust_threshold_usd
        direction = Text(
            "flat" if is_dust else exposure.net_direction,
            style=LABEL_STYLE if is_dust else None,
        )
        table.add_row(
            Text(exposure.base_asset, style=LABEL_STYLE if is_dust else None),
            fmt_usd(exposure.long_notional_usd, 0),
            fmt_usd(exposure.short_notional_usd, 0),
            fmt_usd(exposure.gross_notional_usd, 0),
            fmt_signed_usd(exposure.net_notional_usd, 0),
            direction,
            fmt_usd(exposure.gross_net_gap_usd, 0)
            if exposure.gross_net_gap_usd > ZERO
            else Text("--", style=LABEL_STYLE),
            str(exposure.position_count),
            ", ".join(exposure.venues),
        )
    console.print(table)
    console.print(
        Text(
            f"Net direction decides the hedge side. Assets with abs(net) below "
            f"{fmt_usd(dust_threshold_usd, 0)} USD are treated as flat and get no hedge "
            f"proposal.",
            style=LABEL_STYLE,
        )
    )


def render_self_hedge(console: Console, findings: Iterable[SelfHedgeFinding]) -> None:
    findings = list(findings)
    if not findings:
        return
    _section(console, "GROSS VS NET — exposure you are already paying to offset")
    table = _base_table()
    table.add_column("ASSET", no_wrap=True)
    table.add_column("LONG ON")
    table.add_column("SHORT ON")
    table.add_column("OFFSETTING USD", justify="right")
    table.add_column("NET USD", justify="right")
    table.add_column("UNWIND FEE bps", justify="right")
    table.add_column("UNWIND FEE USD", justify="right")
    table.add_column("STATUS")

    for finding in findings:
        table.add_row(
            finding.base_asset,
            ", ".join(finding.long_venues),
            ", ".join(finding.short_venues),
            fmt_usd(finding.offsetting_notional_usd, 0),
            fmt_signed_usd(finding.net_notional_usd, 0),
            fmt_bps(finding.unwind_fee_bps) + (UNVERIFIED_MARK if finding.fee_schedule_unverified else ""),
            fmt_usd(finding.unwind_fee_usd),
            "fully offset — pure fee drag"
            if finding.fully_offset
            else "partially offset",
        )
    console.print(table)
    console.print(
        Text(
            "The offsetting notional carries no directional exposure but still pays "
            "funding on both legs and will pay an exit fee on both. Collapsing it is "
            "normally cheaper than holding it, unless the two legs are deliberately "
            "capturing a funding spread — see the funding arbitrage section.",
            style=LABEL_STYLE,
        )
    )


# --------------------------------------------------------------------------------------
# Delta hedge
# --------------------------------------------------------------------------------------


def _flags(cost: HedgeCost) -> str:
    marks = ""
    if cost.fee_schedule_unverified:
        marks += UNVERIFIED_MARK
    if cost.fees_state_dependent:
        marks += STATE_DEPENDENT_MARK
    if cost.size_mismatch:
        marks += SIZE_MISMATCH_MARK
    return marks


def _render_avantis_line(console: Console, comparison: AvantisComparison) -> None:
    """Always name Avantis, whether it wins or loses. Contract 7.5, item 1."""
    name = venue_display_name(PRIMARY_HEDGE_VENUE)
    verdict = comparison.verdict

    if verdict == "no_quote":
        _note(
            console,
            f"{name}: not ranked for this asset — {comparison.excluded_reason}.",
        )
        return

    if verdict == "only_candidate":
        _note(
            console,
            f"{name}: the only usable hedge venue for this asset at "
            f"{fmt_bps(comparison.avantis.total_bps)} bps "
            f"({fmt_usd(comparison.avantis.total_usd)} USD). No alternative to "
            f"compare against.",
        )
        return

    delta_bps = comparison.delta_bps
    delta_usd = comparison.delta_usd
    alternative = comparison.best_alternative
    alt_name = venue_display_name(alternative.venue)
    if verdict == "wins":
        body = (
            f"{name}: CHEAPEST, rank {comparison.avantis_rank}. "
            f"{fmt_signed_bps(comparison.avantis.total_bps)} bps vs "
            f"{fmt_signed_bps(alternative.total_bps)} bps on {alt_name} — "
            f"saves {fmt_bps(abs(delta_bps))} bps ({fmt_usd(abs(delta_usd))} USD)."
        )
        style = GOOD_STYLE
    elif verdict == "ties":
        body = (
            f"{name}: ties {alt_name} at "
            f"{fmt_signed_bps(comparison.avantis.total_bps)} bps."
        )
        style = LABEL_STYLE
    else:
        body = (
            f"{name}: rank {comparison.avantis_rank}, "
            f"{fmt_bps(delta_bps)} bps ({fmt_usd(delta_usd)} USD) MORE expensive than "
            f"{alt_name} at this horizon "
            f"({fmt_signed_bps(comparison.avantis.total_bps)} vs "
            f"{fmt_signed_bps(alternative.total_bps)} bps)."
        )
        style = LABEL_STYLE
    _note(console, body, style=style)


def render_upside_comparisons(
    console: Console, comparisons: Iterable[UpsideHedgeComparison]
) -> None:
    comparisons = list(comparisons)
    if not comparisons:
        return
    _section(
        console, "AVANTIS UPSIDE PERPS — profit-share hedge, evaluated separately"
    )
    table = _base_table()
    table.add_column("ASSET", no_wrap=True)
    table.add_column("STANDARD HEDGE", no_wrap=True)
    table.add_column("STANDARD bps", justify="right", no_wrap=True)
    table.add_column("UPSIDE FIXED bps", justify="right", no_wrap=True)
    table.add_column("PROFIT SHARE", justify="right", no_wrap=True)
    table.add_column("CHEAPER IF <", justify="right", no_wrap=True)
    table.add_column("SOURCE", no_wrap=True)

    for comparison in comparisons:
        threshold = comparison.breakeven_adverse_move_bps
        table.add_row(
            comparison.base_asset,
            venue_display_name(comparison.standard_venue),
            fmt_signed_bps(comparison.standard_cost_bps),
            fmt_signed_bps(comparison.upside_fixed_cost_bps),
            f"{(comparison.profit_share_fraction * Decimal(100)).normalize()}%",
            (
                f"{fmt_bps(threshold)} bps "
                f"({(threshold / Decimal(100)).quantize(Decimal('0.01'))}%)"
                if threshold is not None and threshold > ZERO
                else "never cheaper"
            ),
            "quoted" if comparison.derived_from_venue is None else "derived",
        )
    console.print(table)
    _note(
        console,
        (
            "Avantis (Upside) charges no commission and no borrow, and nothing at all if "
            "the hedge closes at a loss. It instead takes a share of gross profit -- "
            "25% up to +500% ROI, then 20%, 10%, 5% at +1500% and +2500% -- which you "
            "pay precisely when the hedge works. So it is the cheaper hedge only while "
            "the underlying stays inside the move shown above; past that point the "
            "profit share costs more than the commission it saved. Market orders only, "
            "crypto majors only (BTC/ETH/SOL/XRP/HYPE). 'quoted' means Avantis returned "
            "a live Upside pair record; 'derived' means the fixed leg was computed from "
            "the standard Avantis quote by zeroing commission and borrow, both "
            "documented as zero on Upside, and keeping spread and funding, both of "
            "which still apply."
        ),
        indent=0,
    )


def render_delta_hedges(
    console: Console,
    opportunities: Iterable[DeltaHedgeOpportunity],
    horizon_hours: Decimal,
    avantis_comparisons: Iterable[AvantisComparison] = (),
) -> None:
    opportunities = list(opportunities)
    comparison_by_asset = {c.base_asset: c for c in avantis_comparisons}
    horizon = format_horizon(horizon_hours)
    _section(console, f"DELTA HEDGE CANDIDATES — all-in cost to hold {horizon}")
    if not opportunities:
        console.print(
            Text(
                "No asset carries material net exposure, so there is nothing to delta hedge.",
                style=LABEL_STYLE,
            )
        )
        return

    for opportunity in opportunities:
        console.print()
        headline = Text()
        headline.append(f"{opportunity.base_asset}  ", style="bold")
        headline.append(
            f"net {fmt_signed_usd(opportunity.exposure.net_notional_usd, 0)} USD "
            f"({opportunity.exposure.net_direction}) -> hedge by going ",
            style=LABEL_STYLE,
        )
        headline.append(opportunity.hedge_side, style="bold")
        headline.append(
            f" {fmt_usd(opportunity.hedge_notional_usd, 0)} USD", style=LABEL_STYLE
        )
        console.print(headline)

        if not opportunity.ranked:
            _note(
                console,
                "No venue produced a usable quote. Nothing is ranked; see exclusions.",
            )
        else:
            table = _base_table()
            table.add_column("RANK", justify="right", no_wrap=True)
            table.add_column("VENUE", no_wrap=True)
            table.add_column("FEES bps", justify="right", no_wrap=True)
            table.add_column("IMPACT bps", justify="right", no_wrap=True)
            table.add_column("FUND/8h", justify="right", no_wrap=True)
            table.add_column("BORROW/8h", justify="right", no_wrap=True)
            table.add_column(f"CARRY {horizon}", justify="right", no_wrap=True)
            table.add_column("ALL-IN bps", justify="right", no_wrap=True)
            table.add_column("ALL-IN USD", justify="right", no_wrap=True)
            table.add_column("B/E", justify="right", no_wrap=True)

            for index, cost in enumerate(opportunity.ranked, start=1):
                best = index == 1
                funding = cost.funding_rate_8h_bps
                table.add_row(
                    str(index),
                    Text(
                        venue_display_name(cost.venue) + _flags(cost),
                        style=BEST_STYLE if best else None,
                    ),
                    fmt_bps(cost.open_fee_bps + cost.close_fee_bps),
                    fmt_bps(cost.price_impact_bps + cost.slippage_bps),
                    Text(
                        fmt_signed_bps(funding),
                        style=GOOD_STYLE if funding > ZERO else (BAD_STYLE if funding < ZERO else None),
                    ),
                    fmt_bps(cost.borrow_rate_8h_bps),
                    _cost_text(cost.carry_cost_bps, fmt_signed_bps),
                    _cost_text(cost.total_bps, fmt_signed_bps, best=best),
                    _cost_text(cost.total_usd, fmt_signed_usd, best=best),
                    fmt_hours(cost.breakeven_hours),
                )
            console.print(table)

            carry_hedges = opportunity.positive_carry
            if carry_hedges:
                best = carry_hedges[0]
                _note(
                    console,
                    f"Positive carry: {venue_display_name(best.venue)} pays you "
                    f"{fmt_usd(abs(best.total_usd))} USD "
                    f"({fmt_bps(abs(best.total_bps))} bps) to hold this hedge for "
                    f"{horizon}. Funding received exceeds the round trip.",
                    style=GOOD_STYLE,
                )
            else:
                best = opportunity.ranked[0]
                _note(
                    console,
                    f"Cheapest hedge costs {fmt_usd(best.total_usd)} USD "
                    f"({fmt_bps(best.total_bps)} bps) over {horizon}: "
                    f"{fmt_bps(best.round_trip_fee_bps)} bps of one-time fees and "
                    f"spread, plus {fmt_signed_bps(best.carry_cost_bps)} bps of carry. "
                    f"No venue pays you to hold this side over {horizon}.",
                )

        comparison = comparison_by_asset.get(opportunity.base_asset)
        if comparison is not None:
            _render_avantis_line(console, comparison)

        # If Avantis Upside is present in the ranking, add a one-line reminder
        # that its all-in bps is UNCONDITIONAL cost only -- the 25/20/10/5%
        # profit share is contingent and cannot be reduced to bps of notional
        # without an assumed price move (CONTRACT.md §7.6, §7). The dedicated
        # "AVANTIS UPSIDE PERPS" section is where the tradeoff is quantified.
        if any(c.venue == "avantis_upside" for c in opportunity.ranked):
            _note(
                console,
                (
                    "Avantis (Upside) is ranked on unconditional cost only (spread + "
                    "funding + any live commission). The 25/20/10/5% profit-share "
                    "obligation on a winning close is not in ALL-IN bps -- see the "
                    "Avantis Upside Perps section for the breakeven adverse move."
                ),
            )

        if opportunity.excluded:
            excluded = _base_table()
            excluded.add_column("EXCLUDED", no_wrap=True, style=LABEL_STYLE)
            excluded.add_column("SIDE", no_wrap=True, style=LABEL_STYLE)
            excluded.add_column("REASON", style=LABEL_STYLE)
            for exclusion in opportunity.excluded:
                excluded.add_row(exclusion.venue, exclusion.side, exclusion.reason)
            console.print(excluded)


# --------------------------------------------------------------------------------------
# Liquidation risk
# --------------------------------------------------------------------------------------


def fmt_pct(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "--"
    quantum = Decimal(1).scaleb(-places)
    return f"{Decimal(value).quantize(quantum):,.{places}f}%"


def render_liquidation_risk(
    console: Console, opportunities: Iterable[DeltaHedgeOpportunity]
) -> None:
    opportunities = list(opportunities)
    has_any = any(o.liquidation_risks for o in opportunities)
    if not has_any:
        return

    _section(console, "LIQUIDATION RISK — force-close price and penalty per hedge venue")

    for opportunity in opportunities:
        if not opportunity.liquidation_risks:
            continue

        console.print()
        headline = Text()
        headline.append(f"{opportunity.base_asset}  ", style="bold")
        headline.append(
            f"{opportunity.hedge_side} "
            f"{fmt_usd(opportunity.hedge_notional_usd, 0)} USD hedge",
            style=LABEL_STYLE,
        )
        console.print(headline)

        table = _base_table()
        table.add_column("VENUE", no_wrap=True)
        table.add_column("LIQ PRICE", justify="right", no_wrap=True)
        table.add_column("DISTANCE", justify="right", no_wrap=True)
        table.add_column("PENALTY USD", justify="right", no_wrap=True)
        table.add_column("PENALTY bps", justify="right", no_wrap=True)
        table.add_column("FEE TYPE", no_wrap=True)
        table.add_column("PARTIAL", no_wrap=True)
        table.add_column("MARGIN RISK", no_wrap=True)

        for risk in opportunity.liquidation_risks:
            is_grvt = risk.venue == "grvt"
            is_full_account = risk.spec.cross_margin_risk == "full_account"
            margin_risk_text = risk.spec.cross_margin_risk.replace("_", " ")
            if is_full_account:
                margin_risk_text = margin_risk_text.upper()

            display_name = venue_display_name(risk.venue)

            table.add_row(
                Text(display_name, style=BAD_STYLE if is_grvt else None),
                fmt_price(risk.liq_price),
                fmt_pct(risk.distance_pct),
                Text(
                    fmt_usd(risk.penalty_usd),
                    style=BAD_STYLE if risk.penalty_usd >= risk.collateral_usd else None,
                ),
                fmt_bps(risk.penalty_bps),
                risk.spec.liquidation_fee_type.replace("_", " "),
                "yes" if risk.spec.partial_liquidation else "no",
                Text(
                    margin_risk_text,
                    style=BAD_STYLE if is_full_account else None,
                ),
            )

        console.print(table)

        grvt_risks = [r for r in opportunity.liquidation_risks if r.venue == "grvt"]
        full_account_risks = [
            r for r in opportunity.liquidation_risks
            if r.spec.cross_margin_risk == "full_account" and r.venue != "grvt"
        ]

        if grvt_risks:
            _note(
                console,
                "WARNING: GRVT forfeits 100% of residual margin on liquidation. "
                "On cross-margin mode, that is your ENTIRE cross-account equity — "
                "not just this position's margin. A $1,000 position on a $50,000 "
                "cross-margin account liquidates the whole $50,000. Use isolated "
                "margin or size defensively.",
                style=BAD_STYLE,
            )
        if full_account_risks:
            venues = ", ".join(r.venue for r in full_account_risks)
            _note(
                console,
                f"Cross-margin warning: {venues} can forfeit full account equity "
                f"on liquidation, not just position margin. Prefer isolated margin.",
                style=BAD_STYLE,
            )

    _note(
        console,
        "Liquidation prices assume a new position at the indicated leverage "
        "with no accrued PnL. Actual liquidation may differ due to accrued "
        "funding, borrow, and fees. Distances show the adverse price move the "
        "hedge survives before force-close.",
        indent=0,
    )


# --------------------------------------------------------------------------------------
# Horizon sensitivity
# --------------------------------------------------------------------------------------


def render_horizon_sensitivity(
    console: Console, sensitivities: Iterable[HorizonSensitivity]
) -> None:
    sensitivities = list(sensitivities)
    if not sensitivities:
        return
    _section(console, "HORIZON SENSITIVITY — all-in cost bps by holding period")

    for sensitivity in sensitivities:
        console.print()
        console.print(
            Text.assemble(
                (f"{sensitivity.base_asset}  ", "bold"),
                (
                    f"{sensitivity.hedge_side} "
                    f"{fmt_usd(sensitivity.notional_usd, 0)} USD hedge",
                    LABEL_STYLE,
                ),
            )
        )

        horizons = sensitivity.horizons_hours
        table = _base_table()
        table.add_column("VENUE", no_wrap=True)
        for hours in horizons:
            table.add_column(format_horizon(hours), justify="right")

        cheapest = {hours: sensitivity.cheapest_at(hours) for hours in horizons}
        for venue in sensitivity.venues:
            cells = [Text(venue_display_name(venue))]
            for hours in horizons:
                value = sensitivity.grid[venue][hours]
                cells.append(
                    _cost_text(value, fmt_signed_bps, best=cheapest[hours] == venue)
                )
            table.add_row(*cells)

        table.add_section()
        table.add_row(
            Text("cheapest", style=LABEL_STYLE),
            *[
                Text(
                    venue_display_name(cheapest[hours]) if cheapest[hours] else "--",
                    style=LABEL_STYLE,
                )
                for hours in horizons
            ],
        )
        console.print(table)

        if sensitivity.crossovers:
            _note(console, "Optimal venue changes with holding period:")
            for crossover in sensitivity.crossovers:
                console.print(
                    Padding(
                        Text.assemble(
                            ("at ", LABEL_STYLE),
                            (fmt_hours(crossover.at_hours), "bold"),
                            (
                                f" ({fmt_bps(crossover.at_hours)}h) "
                                f"{venue_display_name(crossover.from_venue)} -> "
                                f"{venue_display_name(crossover.to_venue)}, both at "
                                f"{fmt_signed_bps(crossover.cost_bps_at_crossover)} bps",
                                LABEL_STYLE,
                            ),
                        ),
                        (0, 0, 0, 4),
                    )
                )
            _note(
                console,
                "Below the first crossover the low-fee venue wins; above it the venue "
                "with better carry wins. Pick the venue for the horizon you actually "
                "intend to hold, not the headline fee.",
            )
        else:
            leader = cheapest[horizons[0]] if horizons else None
            leader_name = venue_display_name(leader) if leader else "--"
            _note(
                console,
                f"No crossover within "
                f"{format_horizon(sensitivity.max_horizon_hours)}: {leader_name} is "
                f"cheapest at every horizon tested.",
            )


# --------------------------------------------------------------------------------------
# Funding arb
# --------------------------------------------------------------------------------------


def render_funding_arbs(
    console: Console, arbs: Iterable[FundingArbOpportunity], horizon_hours: Decimal
) -> None:
    arbs = list(arbs)
    horizon = format_horizon(horizon_hours)
    _section(console, "FUNDING ARBITRAGE — delta-neutral cross-venue carry")
    if not arbs:
        console.print(
            Text(
                "No same-asset venue pair shows positive net carry after fees.",
                style=LABEL_STYLE,
            )
        )
        return

    table = _base_table()
    table.add_column("ASSET", no_wrap=True)
    table.add_column("LONG / SHORT", no_wrap=True)
    table.add_column("BASIS", no_wrap=True)
    table.add_column("NOTIONAL", justify="right", no_wrap=True)
    table.add_column("L FUND/8h", justify="right", no_wrap=True)
    table.add_column("S FUND/8h", justify="right", no_wrap=True)
    table.add_column("NET/8h bps", justify="right", no_wrap=True)
    table.add_column("FEES bps", justify="right", no_wrap=True)
    table.add_column("B/E", justify="right", no_wrap=True)
    table.add_column(f"P&L {horizon}", justify="right", no_wrap=True)

    for arb in arbs:
        table.add_row(
            arb.base_asset,
            f"{arb.long_venue} / {arb.short_venue}",
            arb.basis,
            fmt_usd(arb.notional_usd, 0),
            Text(
                fmt_signed_bps(arb.long_funding_8h_bps),
                style=GOOD_STYLE if arb.long_funding_8h_bps > ZERO else BAD_STYLE,
            ),
            Text(
                fmt_signed_bps(arb.short_funding_8h_bps),
                style=GOOD_STYLE if arb.short_funding_8h_bps > ZERO else BAD_STYLE,
            ),
            Text(fmt_signed_bps(arb.net_carry_bps_per_8h), style=GOOD_STYLE),
            fmt_bps(arb.fee_bps) + (UNVERIFIED_MARK if arb.fee_schedule_unverified else ""),
            fmt_hours(arb.breakeven_hours),
            Text(
                fmt_signed_usd(arb.net_pnl_usd),
                style=GOOD_STYLE if arb.profitable_at_horizon else BAD_STYLE,
            ),
        )
    console.print(table)
    _note(
        console,
        "basis=existing: both legs are already open, so only the exit fees have to be "
        "earned back. basis=new: the full round trip on both legs is charged. B/E is "
        "the holding period at which accrued carry repays those fees, and P&L is carry "
        f"earned over {horizon} minus those fees.",
        indent=0,
    )


# --------------------------------------------------------------------------------------
# Footnotes
# --------------------------------------------------------------------------------------


def render_footnotes(console: Console, result: ScanResult) -> None:
    _section(console, "BASIS OF PREPARATION")
    unverified = sorted(v for v, s in FEE_SCHEDULE.items() if not s.verified)
    state_dependent = sorted(
        v for v, s in FEE_SCHEDULE.items() if s.fees_state_dependent
    )
    promotional = sorted(v for v, s in FEE_SCHEDULE.items() if s.promotional)
    notes: list[tuple[str, str]] = [
        (
            "-",
            f"All cost figures assume a {format_horizon(result.horizon_hours)} holding "
            f"period. Fees are one-time; carry accrues with time. Change --horizon and "
            f"the ranking can change.",
        ),
        (
            "-",
            "Funding sign convention: positive means the hedge leg RECEIVES funding. "
            "Every column labelled as a cost is positive when money leaves.",
        ),
        (
            "-",
            "Funding rates are point-in-time snapshots, not forecasts. They mean-revert. "
            "Extrapolating a live 8h rate out to 30d is indicative only.",
        ),
        (
            "-",
            "A delta hedge flattens price exposure. It does NOT neutralise liquidation "
            "risk: the two legs sit in separate margin accounts, so one can be "
            "liquidated while the other is deep in profit, leaving you naked and "
            "realised at the worst possible moment.",
        ),
        (
            "-",
            "Avantis charges its closing fee on notional plus gross PnL, so a hedge "
            "that works costs more to close than the flat-notional model above shows.",
        ),
        (
            "-",
            "Avantis (Upside) is a distinct hedge instrument, not a cheaper version of "
            "the standard Avantis perp: no open/close/borrow fees, but a profit share "
            "of 25% / 20% / 10% / 5% by ROI band on a winning close and zero cost on a "
            "loss. It is cheaper when the hedge turns out unnecessary and more "
            "expensive when the hedge works. Any all-in bps figure shown for it is "
            "unconditional cost only; the profit share is contingent and not folded in.",
        ),
        (
            SIZE_MISMATCH_MARK,
            "Quote was priced at a materially different size than the hedge. Price "
            "impact and slippage are per-venue estimates and do not scale linearly.",
        ),
    ]
    if state_dependent:
        notes.append(
            (
                STATE_DEPENDENT_MARK,
                f"State-dependent fee schedule: {', '.join(state_dependent)}. Avantis "
                f"decides maker versus taker by whether the trade improves open-interest "
                f"skew, not by order type, so the rate depends on the hedge direction "
                f"against live skew. Ondo prices per market. Confirm both against a live "
                f"quote before sizing.",
            )
        )
    if promotional:
        notes.append(
            (
                "-",
                f"Promotional and revocable pricing: {', '.join(promotional)}. Avantis "
                f"RWA markets are at zero commission under a growth mode tied to "
                f"unstated open-interest milestones, and Ondo's taker rate is billed as "
                f"50% off with no published expiry. Neither is durable.",
            )
        )
    if unverified:
        notes.insert(
            0,
            (
                UNVERIFIED_MARK,
                f"UNVERIFIED PLACEHOLDER fee schedule for: {', '.join(unverified)}. "
                f"These are NOT researched numbers and must not be traded on. See "
                f"FEE_SCHEDULE in engine.py.",
            ),
        )
    notes.append(
        (
            "-",
            "Read-only analysis. Nothing here is signed, submitted or custodied. Full "
            "methodology and limitations: HEDGE_LOGIC.md.",
        )
    )

    bullets = _base_table(show_header=False, padding=(0, 1, 0, 0))
    bullets.add_column(style=LABEL_STYLE, no_wrap=True, justify="right", width=1)
    bullets.add_column(style=LABEL_STYLE)
    for marker, note in notes:
        bullets.add_row(marker, note)
    console.print(bullets)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def render_scan(console: Console, result: ScanResult) -> None:
    render_header(console, result)
    render_venue_errors(console, result.venue_errors)
    render_positions(console, result.positions)
    render_net_exposure(
        console,
        result.exposures,
        result.dust_exposures,
        result.config.dust_threshold_usd,
    )
    render_self_hedge(console, result.self_hedge_findings)
    render_delta_hedges(
        console,
        result.delta_hedges,
        result.horizon_hours,
        result.avantis_comparisons,
    )
    render_liquidation_risk(console, result.delta_hedges)
    render_horizon_sensitivity(console, result.sensitivities)
    render_upside_comparisons(console, result.upside_comparisons)
    render_funding_arbs(console, result.funding_arbs, result.horizon_hours)
    render_footnotes(console, result)
