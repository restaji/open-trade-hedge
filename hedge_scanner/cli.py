"""Command-line interface.

    hedge-scanner scan <address> [<address>...] [--horizon 24h] [--json]

Reads positions through the ingestion layer (`hedge_scanner.portfolio`), runs the
hedge engine, and renders an institutional-style research note. `--fixture` swaps
the ingestion layer for a JSON file, which is how the analytics are exercised
without touching the network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

import typer  # pyright: ignore[reportMissingImports]
from rich.console import Console  # pyright: ignore[reportMissingImports]

from hedge_scanner import engine
from hedge_scanner.engine import (
    FEE_SCHEDULE,
    DEFAULT_DUST_USD,
    ScanConfig,
    parse_horizon,
    parse_horizons,
    scan_result_to_dict,
)
from hedge_scanner.hedge_venues import avantis as avantis_venue
from hedge_scanner.models import Position, PortfolioSnapshot, Quote, VenueError
from hedge_scanner.portfolio import detect_namespace
from hedge_scanner.render import render_scan

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read-only perps portfolio and hedge opportunity scanner.",
)

DEFAULT_HORIZON_TEXT = "24h"
DEFAULT_HORIZONS_TEXT = "8h,24h,3d,7d,30d"


class IngestionUnavailable(RuntimeError):
    """The ingestion layer is not importable or does not expose the contract API."""


def _console(width: int | None) -> Console:
    """Build the output console.

    `height` has to be passed alongside `width` or rich ignores the override and
    clamps to 80 columns whenever `TERM` is `dumb` or `unknown`, which is exactly
    the case when output is piped into a file or a report.
    """
    if width:
        return Console(width=width, height=10_000)
    return Console()


def classify_address(address: str) -> str | None:
    """CONTRACT.md section 2 namespace detection. None means unrecognised.

    Thin re-export of :func:`hedge_scanner.portfolio.detect_namespace`. The
    reference is captured at import time so a test that later stubs
    ``sys.modules['hedge_scanner.portfolio']`` for the ingestion bridge still
    gets real address validation up front.
    """
    return detect_namespace(address)


# --------------------------------------------------------------------------------------
# Fixture loading
# --------------------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("expected a number, got null")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


_DECIMAL_POSITION_FIELDS = {"size_base", "notional_usd", "entry_price", "mark_price"}
_OPTIONAL_DECIMAL_POSITION_FIELDS = {
    "liquidation_price",
    "leverage",
    "collateral_usd",
    "unrealized_pnl_usd",
    "funding_paid_usd",
}
_DECIMAL_QUOTE_FIELDS = {
    "notional_usd",
    "taker_fee_bps",
    "close_fee_bps",
    "price_impact_bps",
    "funding_rate_8h_bps",
    "borrow_rate_8h_bps",
    "est_slippage_bps",
}


def _coerce(
    target: type, payload: dict[str, Any], decimals: set[str], optionals: set[str]
) -> Any:
    known = {f.name for f in dataclass_fields(target)}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(
            f"{target.__name__}: unknown field(s) {sorted(unknown)}; "
            f"expected a subset of {sorted(known)}"
        )
    kwargs = dict(payload)
    for name in decimals & set(kwargs):
        kwargs[name] = _decimal(kwargs[name])
    for name in optionals & set(kwargs):
        kwargs[name] = _optional_decimal(kwargs[name])
    return target(**kwargs)


def load_fixture(path: Path) -> tuple[list[str], list[Position], list[Quote], list[VenueError]]:
    """Load positions, quotes and venue errors from a JSON fixture.

    Numbers may be JSON strings or JSON numbers; both are parsed straight into
    `Decimal` via `str()`, so a fixture never introduces a float.
    """
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("fixture must be a JSON object")

    positions = [
        _coerce(
            Position,
            item,
            _DECIMAL_POSITION_FIELDS,
            _OPTIONAL_DECIMAL_POSITION_FIELDS,
        )
        for item in payload.get("positions", [])
    ]
    quotes = [
        _coerce(Quote, item, _DECIMAL_QUOTE_FIELDS, set())
        for item in payload.get("quotes", [])
    ]
    errors = [VenueError(**item) for item in payload.get("errors", [])]
    addresses = list(payload.get("addresses", []))
    return addresses, positions, quotes, errors


# --------------------------------------------------------------------------------------
# Live ingestion bridge (CONTRACT.md section 9)
# --------------------------------------------------------------------------------------


async def _fetch_live(
    addresses: Sequence[str], config: ScanConfig
) -> tuple[PortfolioSnapshot, list[Quote]]:
    """Run the shipped ingestion layer and price hedges for every asset with exposure.

    Imports are inline so pytest can monkeypatch ``sys.modules[hedge_scanner.portfolio]``
    with a stub module. The bridge treats missing symbols as a hard failure — the
    canonical names are ``scan_snapshot`` and ``quotes_for`` per CONTRACT.md §9,
    and silently reaching for older names hides real regressions.
    """
    try:
        from hedge_scanner.portfolio import quotes_for, scan_snapshot
    except (ImportError, ModuleNotFoundError) as exc:
        raise IngestionUnavailable(
            "hedge_scanner.portfolio does not expose scan_snapshot(addresses) and "
            "quotes_for(base_asset, side, notional_usd) — the canonical entry points "
            "per CONTRACT.md section 9. Use --fixture <file.json> to run the engine "
            "against a saved portfolio while ingestion is being repaired."
        ) from exc

    snapshot = await scan_snapshot(list(addresses))

    exposures, _dust = engine.net_exposures(
        snapshot.positions, dust_threshold_usd=config.dust_threshold_usd
    )
    # Dust exposures are excluded from the quote fan-out: neither
    # ``delta_hedge_opportunities`` (skips anything below dust_threshold) nor
    # ``funding_arb_opportunities`` (takes only non-dust exposures) consumes
    # quotes for them, and every extra pair here is two live venue calls.
    # Both sides are still quoted for every non-dust asset: delta_hedge needs
    # the opposing side, funding_arb needs both to evaluate a cross-venue pair.
    requests = [
        (exposure.base_asset, side, exposure.abs_net_notional_usd)
        for exposure in exposures
        for side in ("long", "short")
        if exposure.abs_net_notional_usd > 0
    ]
    gathered = await asyncio.gather(
        *(
            quotes_for(asset, side, notional, horizon_hours=config.horizon_hours)
            for asset, side, notional in requests
        ),
        return_exceptions=True,
    )
    quotes: list[Quote] = []
    for (asset, side, _notional), outcome in zip(requests, gathered):
        # ``return_exceptions=True`` swallows exceptions but we only want the
        # normal Exception branch as a VenueError. BaseException (Ctrl-C,
        # SystemExit, asyncio.CancelledError) must propagate so the user can
        # abort a hung scan and CI can time out cleanly.
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            snapshot.errors.append(
                VenueError(
                    venue="(quotes)",
                    kind="error",
                    message=f"quoting {asset} {side} failed: {outcome}",
                )
            )
            continue
        found, errors = _split_quote_result(outcome)
        quotes.extend(found)
        snapshot.errors.extend(errors)
    return snapshot, quotes


def _split_quote_result(outcome: Any) -> tuple[list[Quote], list[VenueError]]:
    """Accept either `list[Quote]` or `(list[Quote], list[VenueError])`.

    ``portfolio.quotes_for`` returns the pair so per-venue quote failures stay
    visible; a stub in a test may return a bare list. Anything else is a
    contract violation and yields a typed error so the render surface can flag
    it, rather than an unrelated ``TypeError`` deep in the engine.
    """
    if isinstance(outcome, tuple) and len(outcome) == 2:
        found, errors = outcome
        return list(found or []), list(errors or [])
    if isinstance(outcome, list):
        return list(outcome), []
    return [], [
        VenueError(
            venue="(quotes)",
            kind="error",
            message=(
                f"quotes_for returned {type(outcome).__name__}, "
                "expected list[Quote] or (list[Quote], list[VenueError])"
            ),
        )
    ]


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


@app.command()
def scan(
    addresses: list[str] = typer.Argument(
        None, help="One or more wallet addresses (EVM 0x..., or Solana base58)."
    ),
    horizon: str = typer.Option(
        DEFAULT_HORIZON_TEXT,
        "--horizon",
        "-H",
        help="Assumed holding period for the headline cost ranking, e.g. 8h, 24h, 3d, 1w.",
    ),
    horizons: str = typer.Option(
        DEFAULT_HORIZONS_TEXT,
        "--horizons",
        help="Comma-separated horizons for the sensitivity grid and crossover search.",
    ),
    dust_usd: str = typer.Option(
        str(DEFAULT_DUST_USD),
        "--dust-usd",
        help="Absolute net exposure below this is treated as flat and not hedged.",
    ),
    arb_notional_usd: str = typer.Option(
        None,
        "--arb-notional-usd",
        help="Notional to size prospective funding-arb pairs on. Defaults to net exposure.",
    ),
    fixture: Path = typer.Option(
        None,
        "--fixture",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Read positions and quotes from a JSON fixture instead of live venues.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit structured JSON instead of the rendered note."
    ),
    width: int = typer.Option(
        None, "--width", help="Fix the output width, for reproducible pasted output."
    ),
) -> None:
    """Scan addresses for open perps positions and rank hedging opportunities."""
    stderr = Console(stderr=True)

    try:
        config = ScanConfig(
            horizon_hours=parse_horizon(horizon),
            horizons_hours=parse_horizons(horizons),
            dust_threshold_usd=_decimal(dust_usd),
            funding_arb_notional_usd=(
                _decimal(arb_notional_usd) if arb_notional_usd else None
            ),
        )
    except ValueError as exc:
        stderr.print(f"[red]Invalid option:[/red] {exc}")
        raise typer.Exit(code=2)

    address_list = list(addresses or [])
    venue_errors: list[VenueError] = []

    if fixture is not None:
        try:
            fixture_addresses, positions, quotes, fixture_errors = load_fixture(fixture)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            stderr.print(f"[red]Could not read fixture {fixture}:[/red] {exc}")
            raise typer.Exit(code=2)
        address_list = address_list or fixture_addresses
        venue_errors.extend(fixture_errors)
    else:
        if not address_list:
            stderr.print(
                "[red]No addresses supplied.[/red] Pass at least one address, or use "
                "--fixture to analyse a saved portfolio."
            )
            raise typer.Exit(code=2)
        unrecognised = [a for a in address_list if classify_address(a) is None]
        if unrecognised:
            stderr.print(
                f"[red]Unrecognised address namespace:[/red] {', '.join(unrecognised)}. "
                "Expected an EVM address (0x + 40 hex) or a Solana base58 pubkey. "
                "Cross-chain identity is never guessed."
            )
            raise typer.Exit(code=2)
        try:
            snapshot, quotes = asyncio.run(_fetch_live(address_list, config))
        except IngestionUnavailable as exc:
            stderr.print(f"[red]Ingestion layer unavailable:[/red] {exc}")
            raise typer.Exit(code=3)
        positions = list(snapshot.positions)
        venue_errors.extend(snapshot.errors)
        address_list = list(snapshot.addresses) or address_list

    result = engine.scan(
        positions,
        quotes,
        addresses=address_list,
        venue_errors=venue_errors,
        config=config,
    )

    if json_output:
        sys.stdout.write(json.dumps(scan_result_to_dict(result), indent=2) + "\n")
        return

    render_scan(_console(width), result)


# --------------------------------------------------------------------------------------
# Live Avantis fee snapshot for the `fees` command
# --------------------------------------------------------------------------------------

# Fees-command sample: BTC anchors the maker round trip (§12.8); RWA covers
# metal / FX / commodity so growth-mode 0 bps is visible (§7.6.2).
_AVANTIS_FEES_CRYPTO_SAMPLE: tuple[str, ...] = ("BTC", "ETH", "SOL")
_AVANTIS_FEES_RWA_SAMPLE: tuple[str, ...] = ("XAU", "EUR", "BRENT")

# Display-only; the fetch reuses `avantis_venue.fetch_trading_snapshot`.
_AVANTIS_LIVE_SOURCE_URL = avantis_venue.TRADING_URL


def _pair_fees_bps(record: dict[str, Any]) -> dict[str, Decimal] | None:
    """Extract the four commission fields from a live pair record, in bps.

    Returns ``None`` if any of the four fields is missing on the record -- we
    refuse to display a fabricated zero for a missing field (§7 non-negotiable).
    Growth-mode RWA pairs return real zeros because the fields are present and
    set to zero on the live snapshot; that is not a fabrication, it is what the
    endpoint says.
    """
    fees = record.get("additionalPairParams2") or {}
    keys = ("openMakerFeeP", "closeTakerFeeP", "openTakerFeeP", "closeMakerFeeP")
    if any(fees.get(k) is None for k in keys):
        return None
    return {
        "open_maker_bps": Decimal(str(fees["openMakerFeeP"])) * Decimal(100),
        "close_taker_bps": Decimal(str(fees["closeTakerFeeP"])) * Decimal(100),
        "open_taker_bps": Decimal(str(fees["openTakerFeeP"])) * Decimal(100),
        "close_maker_bps": Decimal(str(fees["closeMakerFeeP"])) * Decimal(100),
    }


@dataclass(frozen=True)
class AvantisLiveFeeRow:
    """One pair's live-fetched commission row for the `fees` command."""

    symbol: str
    base_asset: str
    open_maker_bps: Decimal
    close_taker_bps: Decimal
    open_taker_bps: Decimal
    close_maker_bps: Decimal
    promotional_zero: bool
    listed: bool
    note: str = ""

    @property
    def maker_round_trip_bps(self) -> Decimal:
        """Round trip the ranker prices: maker open + maker close (§12.8)."""
        return self.open_maker_bps + self.close_maker_bps

    @property
    def taker_close_round_trip_bps(self) -> Decimal:
        """Same open, but the taker close an unchanged book would charge (§12.8)."""
        return self.open_maker_bps + self.close_taker_bps


@dataclass(frozen=True)
class AvantisLiveFeeSnapshot:
    """Result of the `fees` command's live Avantis fetch."""

    available: bool
    fetched_at: datetime
    source_url: str
    crypto_rows: tuple[AvantisLiveFeeRow, ...] = ()
    rwa_rows: tuple[AvantisLiveFeeRow, ...] = ()
    error: str = ""

    @property
    def representative_crypto(self) -> AvantisLiveFeeRow | None:
        """The pair pinned as the reference (BTC/USD per §12.3)."""
        for row in self.crypto_rows:
            if row.base_asset == "BTC" and row.listed:
                return row
        return next((r for r in self.crypto_rows if r.listed), None)


def _row_from_snapshot(
    snapshot: dict[str, Any], base_asset: str
) -> AvantisLiveFeeRow:
    record = avantis_venue.resolve_pair(snapshot, base_asset)
    if record is None:
        return AvantisLiveFeeRow(
            symbol=f"{base_asset}/USD",
            base_asset=base_asset,
            open_maker_bps=Decimal(0),
            close_taker_bps=Decimal(0),
            open_taker_bps=Decimal(0),
            close_maker_bps=Decimal(0),
            promotional_zero=False,
            listed=False,
            note="not listed on Avantis",
        )
    fees = _pair_fees_bps(record)
    if fees is None:
        return AvantisLiveFeeRow(
            symbol=f"{record.get('from')}/{record.get('to')}",
            base_asset=base_asset,
            open_maker_bps=Decimal(0),
            close_taker_bps=Decimal(0),
            open_taker_bps=Decimal(0),
            close_maker_bps=Decimal(0),
            promotional_zero=False,
            listed=False,
            note="live snapshot missing openMakerFeeP/closeTakerFeeP/openTakerFeeP/closeMakerFeeP",
        )
    promotional = (
        fees["open_maker_bps"] == 0
        and fees["close_taker_bps"] == 0
        and fees["open_taker_bps"] == 0
        and fees["close_maker_bps"] == 0
    )
    return AvantisLiveFeeRow(
        symbol=f"{record.get('from')}/{record.get('to')}",
        base_asset=base_asset,
        open_maker_bps=fees["open_maker_bps"],
        close_taker_bps=fees["close_taker_bps"],
        open_taker_bps=fees["open_taker_bps"],
        close_maker_bps=fees["close_maker_bps"],
        promotional_zero=promotional,
        listed=True,
    )


async def _fetch_avantis_live_fees() -> AvantisLiveFeeSnapshot:
    """Fetch and shape the live Avantis fee snapshot for the `fees` command.

    Reuses ``avantis_venue.fetch_trading_snapshot`` and
    ``avantis_venue.resolve_pair`` -- the same helpers ``quote_hedge`` uses --
    so the display and the ranker share one source of truth (§12.3 follow-up).
    On failure, returns a snapshot with ``available=False`` carrying the
    underlying error; the caller must NOT fall back to hardcoded numbers
    (§7 non-negotiable, §12.3 refusal semantics).
    """
    fetched_at = datetime.now(timezone.utc)
    try:
        snapshot = await avantis_venue.fetch_trading_snapshot()
    except Exception as exc:  # noqa: BLE001
        return AvantisLiveFeeSnapshot(
            available=False,
            fetched_at=fetched_at,
            source_url=_AVANTIS_LIVE_SOURCE_URL,
            error=f"{type(exc).__name__}: {exc}",
        )
    crypto = tuple(_row_from_snapshot(snapshot, a) for a in _AVANTIS_FEES_CRYPTO_SAMPLE)
    rwa = tuple(_row_from_snapshot(snapshot, a) for a in _AVANTIS_FEES_RWA_SAMPLE)
    return AvantisLiveFeeSnapshot(
        available=True,
        fetched_at=fetched_at,
        source_url=_AVANTIS_LIVE_SOURCE_URL,
        crypto_rows=crypto,
        rwa_rows=rwa,
    )


def _fmt_bps(value: Decimal) -> str:
    """One-decimal-place bps, without exponent notation on integers."""
    quantized = value.quantize(Decimal("0.1"))
    return f"{quantized:f}"


@app.command("fees")
def fees(
    width: int = typer.Option(None, "--width", help="Fix the output width."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the fee schedule per venue.

    Static venues (GRVT, Pacifica, Ondo, Jupiter, Hyperliquid, Ostium) render
    from the transcribed schedules in ``engine.FEE_SCHEDULE``. Avantis is
    LIVE-FETCHED from https://prod-api.avantisfi.com/data/v2/trading at
    invocation time (§7 non-negotiable + §12.3 follow-up), so the display and
    the ranker share one source of truth. If the fetch fails, Avantis prints
    an "unavailable" line and the other venues still render.
    """
    live = asyncio.run(_fetch_avantis_live_fees())

    if json_output:
        payload: dict[str, Any] = {}
        for venue, s in FEE_SCHEDULE.items():
            if s.live and venue == "avantis":
                payload[venue] = _avantis_json_payload(s, live)
                continue
            payload[venue] = {
                "display_name": s.display_name,
                "open_fee_bps": str(s.open_fee_bps),
                "close_fee_bps": str(s.close_fee_bps),
                "maker_fee_bps": str(s.maker_fee_bps),
                "round_trip_fee_bps": str(s.round_trip_fee_bps),
                "verified": s.verified,
                "live": s.live,
                "source": s.source,
                "as_of": s.as_of,
                "hedge_destination": s.hedge_destination,
                "position_readable": s.position_readable,
                "notes": s.notes,
            }
        payload["avantis_upside"] = _avantis_upside_json_payload()
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    from rich import box  # pyright: ignore[reportMissingImports]
    from rich.padding import Padding  # pyright: ignore[reportMissingImports]
    from rich.table import Table  # pyright: ignore[reportMissingImports]
    from rich.text import Text  # pyright: ignore[reportMissingImports]

    console = _console(width)
    table = Table(box=box.SIMPLE, header_style="dim", show_edge=False, pad_edge=False)
    table.add_column("VENUE", no_wrap=True)
    table.add_column("OPEN bps", justify="right")
    table.add_column("CLOSE bps", justify="right")
    table.add_column("RT bps", justify="right")
    table.add_column("MAKER bps", justify="right")
    table.add_column("HEDGE DEST", no_wrap=True)
    table.add_column("READ POS", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("AS OF", no_wrap=True)

    for venue, schedule in FEE_SCHEDULE.items():
        if schedule.live and venue == "avantis":
            _add_avantis_live_row(table, schedule, live)
            continue
        table.add_row(
            schedule.display_name,
            str(schedule.open_fee_bps),
            str(schedule.close_fee_bps),
            str(schedule.round_trip_fee_bps),
            str(schedule.maker_fee_bps),
            "yes" if schedule.hedge_destination else "no",
            "yes" if schedule.position_readable else "no",
            Text("verified", style="green")
            if schedule.verified
            else Text("PLACEHOLDER", style="red"),
            schedule.as_of,
        )
    _add_avantis_upside_row(table)
    console.print(table)

    for venue, schedule in FEE_SCHEDULE.items():
        if schedule.live and venue == "avantis":
            continue  # detailed live block rendered below, not the stub source line
        console.print(Padding(Text(f"{venue}: {schedule.source}", style="dim"), (0, 0, 0, 2)))
        if schedule.notes:
            console.print(Padding(Text(schedule.notes, style="dim"), (0, 0, 0, 4)))

    _render_avantis_live_block(console, live)
    _render_avantis_upside_block(console)


def _add_avantis_live_row(table: Any, schedule: Any, live: AvantisLiveFeeSnapshot) -> None:
    """Add the Avantis row to the summary table, populated from the live fetch."""
    from rich.text import Text  # pyright: ignore[reportMissingImports]

    if not live.available:
        table.add_row(
            schedule.display_name,
            "n/a", "n/a", "n/a", "n/a",
            "yes" if schedule.hedge_destination else "no",
            "yes" if schedule.position_readable else "no",
            Text("UNAVAILABLE", style="red"),
            "live-fetch failed",
        )
        return
    row = live.representative_crypto
    if row is None:
        table.add_row(
            schedule.display_name,
            "n/a", "n/a", "n/a", "n/a",
            "yes" if schedule.hedge_destination else "no",
            "yes" if schedule.position_readable else "no",
            Text("UNAVAILABLE", style="red"),
            "no crypto pair in live snapshot",
        )
        return
    # The engine prices both legs of an Avantis hedge at the pair's maker rate
    # (§12.8), so the OPEN/CLOSE columns show the two maker fields. The taker
    # close an unchanged book would charge is shown in the detail block below.
    table.add_row(
        f"{schedule.display_name} ({row.symbol})",
        _fmt_bps(row.open_maker_bps),
        _fmt_bps(row.close_maker_bps),
        _fmt_bps(row.maker_round_trip_bps),
        _fmt_bps(row.open_maker_bps),
        "yes" if schedule.hedge_destination else "no",
        "yes" if schedule.position_readable else "no",
        Text("LIVE", style="green"),
        live.fetched_at.strftime("%Y-%m-%d %H:%MZ"),
    )


def _add_avantis_upside_row(table: Any) -> None:
    """Static row for Upside Perps -- no live fetch needed (§7.6, task item 5)."""
    from rich.text import Text  # pyright: ignore[reportMissingImports]

    table.add_row(
        "Avantis (Upside)",
        "0.0", "0.0", "0.0", "n/a",
        "yes", "no",
        Text("profit-share", style="cyan"),
        "n/a",
    )


def _render_avantis_live_block(console: Any, live: AvantisLiveFeeSnapshot) -> None:
    """Detailed Avantis section beneath the summary table."""
    from rich.padding import Padding  # pyright: ignore[reportMissingImports]
    from rich.text import Text  # pyright: ignore[reportMissingImports]

    console.print(
        Padding(Text(f"avantis: {live.source_url}", style="dim"), (0, 0, 0, 2))
    )
    if not live.available:
        console.print(
            Padding(
                Text(
                    "Avantis fee schedule: unavailable -- could not reach "
                    f"data.avantisfi.com ({live.error}). No fallback is emitted "
                    "(§7 non-negotiable).",
                    style="red",
                ),
                (0, 0, 0, 4),
            )
        )
        return
    console.print(
        Padding(
            Text(
                f"fetched_at: {live.fetched_at.isoformat()} (live per invocation)",
                style="dim",
            ),
            (0, 0, 0, 4),
        )
    )
    console.print(
        Padding(
            Text(
                "crypto pairs (maker round trip per §12.8: openMakerFeeP + "
                "closeMakerFeeP, both live; the taker close an unchanged book "
                "would charge is shown alongside):",
                style="dim",
            ),
            (0, 0, 0, 4),
        )
    )
    for row in live.crypto_rows:
        if not row.listed:
            console.print(
                Padding(
                    Text(f"- {row.symbol}: {row.note}", style="dim"),
                    (0, 0, 0, 6),
                )
            )
            continue
        console.print(
            Padding(
                Text(
                    f"- {row.symbol}: openMakerFeeP={_fmt_bps(row.open_maker_bps)} bps, "
                    f"closeMakerFeeP={_fmt_bps(row.close_maker_bps)} bps -> "
                    f"round trip {_fmt_bps(row.maker_round_trip_bps)} bps "
                    f"(taker close would make it "
                    f"{_fmt_bps(row.taker_close_round_trip_bps)} bps; "
                    f"openTakerFeeP={_fmt_bps(row.open_taker_bps)}, "
                    f"closeTakerFeeP={_fmt_bps(row.close_taker_bps)})",
                    style="dim",
                ),
                (0, 0, 0, 6),
            )
        )
    console.print(
        Padding(
            Text(
                "RWA pairs (growth-mode promotion per §7.6.2 -- REVOCABLE, "
                "explicitly temporary):",
                style="dim",
            ),
            (0, 0, 0, 4),
        )
    )
    for row in live.rwa_rows:
        if not row.listed:
            console.print(
                Padding(
                    Text(f"- {row.symbol}: {row.note}", style="dim"),
                    (0, 0, 0, 6),
                )
            )
            continue
        label = "PROMOTIONAL 0 bps" if row.promotional_zero else "commission active"
        console.print(
            Padding(
                Text(
                    f"- {row.symbol}: openMaker={_fmt_bps(row.open_maker_bps)}, "
                    f"closeTaker={_fmt_bps(row.close_taker_bps)}, "
                    f"openTaker={_fmt_bps(row.open_taker_bps)}, "
                    f"closeMaker={_fmt_bps(row.close_maker_bps)} bps -- {label}",
                    style="dim",
                ),
                (0, 0, 0, 6),
            )
        )
    console.print(
        Padding(
            Text(
                "Closing fee is charged on (notional + gross PnL), so a winning "
                "hedge pays more than the closeMakerFeeP rate applied to notional.",
                style="dim",
            ),
            (0, 0, 0, 4),
        )
    )


def _render_avantis_upside_block(console: Any) -> None:
    """Static Upside Perps footer -- no fetch, static text per task item 5."""
    from rich.padding import Padding  # pyright: ignore[reportMissingImports]
    from rich.text import Text  # pyright: ignore[reportMissingImports]

    console.print(
        Padding(
            Text("avantis_upside: Avantis Upside Perps (§7.6, §12.4)", style="dim"),
            (0, 0, 0, 2),
        )
    )
    console.print(
        Padding(
            Text(
                "No open, close, or borrow fee. Profit share taken on a winning "
                "close only: 25% (ROI >=1%) / 20% (>=500%) / 10% (>=1500%) / "
                "5% (>=2500%). Zero cost if the position closes at a loss.",
                style="dim",
            ),
            (0, 0, 0, 4),
        )
    )


def _avantis_json_payload(schedule: Any, live: AvantisLiveFeeSnapshot) -> dict[str, Any]:
    """Live-fetched Avantis section for the JSON output.

    Emits the fetched-at timestamp, source URL, per-pair rows, and -- on
    failure -- ``available: false`` with the underlying error. Never
    fabricates numbers on a failed fetch (§7 non-negotiable).
    """
    if not live.available:
        return {
            "display_name": schedule.display_name,
            "live": True,
            "available": False,
            "source_url": live.source_url,
            "fetched_at": live.fetched_at.isoformat(),
            "error": live.error,
            "verified": schedule.verified,
            "hedge_destination": schedule.hedge_destination,
            "position_readable": schedule.position_readable,
            "notes": (
                "Avantis fee schedule unavailable: could not reach the live API. "
                "No hardcoded fallback is emitted (§7 non-negotiable)."
            ),
        }

    def _row(row: AvantisLiveFeeRow) -> dict[str, Any]:
        if not row.listed:
            return {
                "symbol": row.symbol,
                "base_asset": row.base_asset,
                "listed": False,
                "note": row.note,
            }
        return {
            "symbol": row.symbol,
            "base_asset": row.base_asset,
            "listed": True,
            "openMakerFeeP_bps": str(row.open_maker_bps),
            "closeTakerFeeP_bps": str(row.close_taker_bps),
            "openTakerFeeP_bps": str(row.open_taker_bps),
            "closeMakerFeeP_bps": str(row.close_maker_bps),
            "maker_round_trip_bps": str(row.maker_round_trip_bps),
            "taker_close_round_trip_bps": str(row.taker_close_round_trip_bps),
            "promotional_zero": row.promotional_zero,
        }

    return {
        "display_name": schedule.display_name,
        "live": True,
        "available": True,
        "source_url": live.source_url,
        "fetched_at": live.fetched_at.isoformat(),
        "hedge_model": (
            "both legs priced at the pair's live maker rate per CONTRACT.md "
            "§12.8 (openMakerFeeP + closeMakerFeeP); this ASSUMES a maker close, "
            "which an unchanged book would charge as taker instead -- see "
            "taker_close_round_trip_bps for that alternative"
        ),
        "crypto_pairs": [_row(r) for r in live.crypto_rows],
        "rwa_pairs": [_row(r) for r in live.rwa_rows],
        "close_fee_base": "notional + gross PnL",
        "verified": schedule.verified,
        "hedge_destination": schedule.hedge_destination,
        "position_readable": schedule.position_readable,
        "notes": schedule.notes,
    }


def _avantis_upside_json_payload() -> dict[str, Any]:
    """Static Upside JSON payload (task item 5 -- no additional fetch)."""
    return {
        "display_name": "Avantis (Upside)",
        "live": False,
        "open_fee_bps": "0",
        "close_fee_bps": "0",
        "borrow_fee_bps": "0",
        "verified": True,
        "hedge_destination": True,
        "position_readable": False,
        "profit_share_bands": [
            {"roi_pct_min": "1", "protocol_share_pct": "25"},
            {"roi_pct_min": "500", "protocol_share_pct": "20"},
            {"roi_pct_min": "1500", "protocol_share_pct": "10"},
            {"roi_pct_min": "2500", "protocol_share_pct": "5"},
        ],
        "notes": (
            "No open, close, or borrow fee. Profit share is taken on a winning "
            "close only, and is zero on a losing close. Contingent cost, not "
            "reducible to bps of notional without an assumed price move (§7)."
        ),
    }


def main() -> None:
    app()


if __name__ == "__main__":
    main()
