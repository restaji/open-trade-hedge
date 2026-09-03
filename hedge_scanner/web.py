"""Read-only perps portfolio and hedge-quote API, plus the React UI.

Run API:     cd hedge-scanner && uv run python -m hedge_scanner.web
Run UI dev:  cd hedge-scanner/frontend && npm run dev
             (proxies /api to :8000; open http://127.0.0.1:5173)
Build UI:    cd hedge-scanner/frontend && npm run build
             then the API process serves the SPA at /

Serves:
  GET  /            → React SPA (hedge_scanner/static)
  GET  /api/health  → liveness probe
  GET  /api/prices  → per-venue mark prices (UI poll)
  GET  /api/scan    → ?addresses=<addr>[,<addr>…]
  POST /api/scan    → {addresses: [str]} → positions + hedge opportunities
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from hedge_scanner import portfolio
from hedge_scanner.adapters import (
    ADAPTER_CLASSES,
    GrvtAdapter,
    HyperliquidAdapter,
    JupiterAdapter,
    OndoAdapter,
    OstiumAdapter,
    PacificaAdapter,
)
from hedge_scanner.adapters.base import make_http_client
from hedge_scanner.assets import normalize_base_asset
from hedge_scanner.engine import (
    DEFAULT_DUST_USD,
    FEE_SCHEDULE,
    format_horizon,
    net_exposures,
    self_hedge_findings,
)
from hedge_scanner.hedge_venues import avantis
from hedge_scanner.liquidation import (
    LIQUIDATION_SPECS,
    compute_liquidation_risk,
)
from hedge_scanner.markets import VENUE_MARKETS

# Position-source venues we can price a source-side carry for (their public
# quote endpoints don't need auth, even for the ones whose positions do).
_SOURCE_ADAPTER_CLASSES: dict[str, type] = {
    "grvt": GrvtAdapter,
    "hyperliquid": HyperliquidAdapter,
    "jupiter": JupiterAdapter,
    "ondo": OndoAdapter,
    "ostium": OstiumAdapter,
    "pacifica": PacificaAdapter,
}

app = FastAPI(
    title="Hedge Scanner",
    description=(
        "Read-only perps portfolio and hedge-quote API. "
        "GET or POST `/api/scan` with one or more wallet addresses."
    ),
    version="0.1.0",
)

# Public by default so third-party clients (and the hosted UI) can call the API
# from a browser. Set HEDGE_SCANNER_CORS_ORIGINS to a comma-separated allowlist
# (e.g. "https://yourapp.com,https://localhost:3000") to lock it down.
_cors_origins = [
    origin.strip()
    for origin in os.environ.get("HEDGE_SCANNER_CORS_ORIGINS", "*").split(",")
    if origin.strip()
] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# CONTRACT.md section 7.5.3 — CLI ranking still defaults to 24h. The web UI
# headlines net APR / 24h earn from all-in funding (Avantis Net Rate =
# funding+marginFee; Jupiter borrow counted as funding).
HORIZON_HOURS = Decimal(24)
_HOURS_PER_YEAR = Decimal(8760)
_HOURS_PER_8H = Decimal(8)
_APR_PER_8H_BPS = _HOURS_PER_YEAR / _HOURS_PER_8H / Decimal(100)  # 10.95
_BPS_DENOM = Decimal(10_000)
_PERIODS_PER_24H = HORIZON_HOURS / _HOURS_PER_8H  # 3


def apr_pct_from_8h_bps(bps_8h: Decimal) -> Decimal:
    """Convert a signed bps-per-8h rate to a simple annualised percent."""
    return bps_8h * _APR_PER_8H_BPS


def hedge_funding_spread(
    source_funding_8h_bps: Decimal,
    hedge_funding_8h_bps: Decimal,
    notional_usd: Decimal,
    cover_bps: Decimal = Decimal(0),
    source_borrow_8h_bps: Decimal = Decimal(0),
    hedge_borrow_8h_bps: Decimal = Decimal(0),
    source_notional_usd: Decimal | None = None,
) -> dict[str, Decimal | None]:
    """Net of keeping the source position and opening the Avantis hedge.

    Each venue's holding cost is one **funding** number, holder-signed
    (positive = receive):

    * Avantis = ``fundingRate − marginFee`` (same as UI Net Rate, flipped
      so + is money in). ``marginFee`` is not shown separately.
    * Jupiter / Ostium = ``−borrow`` / ``−rollover``. Those venues have no
      two-sided funding; the borrow/rollover *is* the funding leg.
    * Other venues = their live funding; borrow is 0.

    ``net_8h = source_all_in + hedge_all_in``.
    ``source_usd_24h`` / ``hedge_usd_24h`` / ``earn_usd_24h`` are accrued
    carry only (funding, marginFee, borrow/rollover) — never open, close,
    or spread. ``source_notional_usd`` defaults to the hedge notional.
    """
    source_all_in = source_funding_8h_bps - source_borrow_8h_bps
    hedge_all_in = hedge_funding_8h_bps - hedge_borrow_8h_bps
    net_8h = source_all_in + hedge_all_in
    source_apr = apr_pct_from_8h_bps(source_all_in)
    hedge_apr = apr_pct_from_8h_bps(hedge_all_in)
    src_n = source_notional_usd if source_notional_usd is not None else notional_usd
    source_usd_24h = src_n * source_all_in * _PERIODS_PER_24H / _BPS_DENOM
    hedge_usd_24h = notional_usd * hedge_all_in * _PERIODS_PER_24H / _BPS_DENOM
    if net_8h > 0:
        breakeven: Decimal | None = cover_bps * _HOURS_PER_8H / net_8h
    else:
        breakeven = None
    return {
        "source_apr_pct": source_apr,
        "hedge_apr_pct": hedge_apr,
        "net_apr_pct": apr_pct_from_8h_bps(net_8h),
        "net_8h_bps": net_8h,
        "source_usd_24h": source_usd_24h,
        "hedge_usd_24h": hedge_usd_24h,
        "earn_usd_24h": source_usd_24h + hedge_usd_24h,
        "cover_bps": cover_bps,
        "cover_usd": notional_usd * cover_bps / _BPS_DENOM,
        "breakeven_hours": breakeven,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _d(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def _hedge_plan(
    positions: list[Any],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Quote Avantis on residual net only when the book is already offset.

    Same-asset longs and shorts are a SelfHedgeFinding (HEDGE_LOGIC.md §1).
    Opening a second full-size Avantis hedge on each leg would add exposure,
    not flatten it. Offsetting legs stay visible; only ``|net|`` is priced.
    """
    if not positions:
        return {}, []
    material, dust = net_exposures(positions)
    exposures = list(material) + list(dust)
    findings = self_hedge_findings(exposures)
    by_asset = {e.base_asset: e for e in exposures}

    carriers: dict[str, Any] = {}
    for finding in findings:
        exposure = by_asset[finding.base_asset]
        if (
            exposure.net_direction == "flat"
            or exposure.abs_net_notional_usd < DEFAULT_DUST_USD
        ):
            continue
        residual_side = exposure.net_direction
        candidates = [
            p
            for p in positions
            if (p.base_asset or "").strip().upper() == finding.base_asset
            and p.side == residual_side
        ]
        if candidates:
            carriers[finding.base_asset] = max(
                candidates, key=lambda p: abs(p.notional_usd)
            )

    plan: dict[int, dict[str, Any]] = {}
    for pos in positions:
        asset = (pos.base_asset or "").strip().upper()
        exposure = by_asset.get(asset)
        default_side = "short" if pos.side == "long" else "long"
        if exposure is None or not exposure.is_self_hedged:
            plan[id(pos)] = {
                "role": "full",
                "hedge_side": default_side,
                "hedge_notional": abs(pos.notional_usd),
            }
            continue
        if carriers.get(asset) is pos:
            plan[id(pos)] = {
                "role": "residual",
                "hedge_side": exposure.hedge_side,
                "hedge_notional": exposure.abs_net_notional_usd,
            }
        else:
            plan[id(pos)] = {
                "role": "offsetting",
                "hedge_side": default_side,
                "hedge_notional": Decimal(0),
            }
    return plan, [_finding_to_dict(f) for f in findings]


def _finding_to_dict(finding: Any) -> dict[str, Any]:
    return {
        "base_asset": finding.base_asset,
        "long_notional_usd": _d(finding.long_notional_usd),
        "short_notional_usd": _d(finding.short_notional_usd),
        "net_notional_usd": _d(finding.net_notional_usd),
        "offsetting_notional_usd": _d(finding.offsetting_notional_usd),
        "gross_net_gap_usd": _d(finding.gross_net_gap_usd),
        "long_venues": list(finding.long_venues),
        "short_venues": list(finding.short_venues),
        "unwind_fee_bps": _d(finding.unwind_fee_bps),
        "unwind_fee_usd": _d(finding.unwind_fee_usd),
        "fully_offset": finding.fully_offset,
    }


def _pos_to_dict(p: Any) -> dict:
    return {
        "venue": p.venue,
        "market": p.market,
        "base_asset": p.base_asset,
        "side": p.side,
        "size_base": _d(p.size_base),
        "notional_usd": _d(p.notional_usd),
        "entry_price": _d(p.entry_price),
        "mark_price": _d(p.mark_price),
        "liquidation_price": _d(p.liquidation_price),
        "leverage": _d(p.leverage),
        "collateral_usd": _d(p.collateral_usd),
        "unrealized_pnl_usd": _d(p.unrealized_pnl_usd),
        "funding_paid_usd": _d(p.funding_paid_usd),
        "margin_mode": p.margin_mode,
    }


def _avantis_can_hedge(base_asset: str) -> bool:
    avantis_markets = VENUE_MARKETS.get("avantis", {})
    norm = normalize_base_asset(base_asset)
    if norm in avantis_markets:
        return True
    for sym in avantis_markets:
        if normalize_base_asset(sym) == norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Source-side current carry (funding + borrow) from the position's perspective
# ---------------------------------------------------------------------------


async def _one_source_carry(
    venue: str, base_asset: str, side: str, notional_usd: Decimal
) -> tuple[Decimal, Decimal, bool] | None:
    """Return (funding_bps_8h, borrow_bps_8h, available) from the position's side.

    Each adapter's ``get_quote`` signs ``funding_rate_8h_bps`` from the ``side``
    passed in: positive = that side receives, negative = that side pays. We pass
    the position's own side, so the returned rate is already oriented from the
    position holder's perspective. ``borrow_rate_8h_bps`` is unsigned — always a
    cost — so net carry from the position's side is ``funding - borrow`` and
    ``< 0`` means the position is currently paying carry.
    """
    cls = _SOURCE_ADAPTER_CLASSES.get(venue)
    if cls is None:
        return None
    adapter = cls()
    try:
        quote = await adapter.get_quote(base_asset, side, notional_usd)
    except Exception:  # noqa: BLE001 - one dead venue must not kill the scan
        return None
    finally:
        await adapter.aclose()
    return quote.funding_rate_8h_bps, quote.borrow_rate_8h_bps, quote.available


async def _source_carry_map(
    positions: list[Any],
) -> dict[tuple[str, str, str], tuple[Decimal, Decimal, bool] | None]:
    """Query current source-side carry rates for every unique (venue, base_asset, side).

    Deduplication matters: 18 Ostium positions on the same wallet all share one
    per-pair rollover rate; a naive loop would fire 18 identical GraphQL
    requests. Funding and borrow are notional-independent for these adapters,
    so any non-zero notional per key is fine.
    """
    keys: dict[tuple[str, str, str], Decimal] = {}
    for pos in positions:
        key = (pos.venue, pos.base_asset, pos.side)
        notional = abs(pos.notional_usd) if pos.notional_usd else Decimal("1")
        if notional <= 0:
            notional = Decimal("1")
        # Keep the first notional seen; identical rate regardless.
        keys.setdefault(key, notional)

    if not keys:
        return {}

    async def one(key: tuple[str, str, str], notional: Decimal):
        v, a, s = key
        return key, await _one_source_carry(v, a, s, notional)

    results = await asyncio.gather(
        *(one(k, n) for k, n in keys.items()),
        return_exceptions=False,
    )
    return dict(results)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    addresses: list[str]


def _normalize_addresses(raw: list[str]) -> list[str]:
    """Flatten query/body values; allow comma-separated addresses in one slot."""
    out: list[str] = []
    for item in raw:
        for part in item.split(","):
            addr = part.strip()
            if addr:
                out.append(addr)
    return out


async def _enrich_avantis(
    entry: dict[str, Any],
    pos: Any,
    hedge_side: str,
    notional: float,
    client: httpx.AsyncClient,
) -> None:
    """Populate Avantis funding on ``entry``, plus hours to cover fees and spread."""
    try:
        quote = await avantis.quote_hedge(
            pos.base_asset, hedge_side,
            Decimal(str(notional)), HORIZON_HOURS,
            client=client,
        )
    except Exception as exc:
        entry["avantis_unavailable"] = (
            f"Avantis quote request failed ({exc.__class__.__name__})."
        )
        return

    if quote is None:
        # Base asset not listed on Avantis at all; leave columns None.
        return

    if not quote.available:
        # A refusal carries its reason in `notes` (CONTRACT.md 10.2). Dropping it
        # leaves the UI unable to say why, which is the difference between "too
        # small to hedge" and "venue down".
        entry["avantis_unavailable"] = (
            quote.notes or "Avantis returned no usable quote for this size."
        )
        return

    # Funding drives the hedge. Fees + both spread legs are the hurdle that
    # funding has to repay — not a ranking input.
    cover_bps = (
        quote.taker_fee_bps
        + quote.close_fee_bps
        + quote.price_impact_bps
        + quote.est_slippage_bps
    )
    entry["avantis_quote"] = {
        "market": quote.market,
        "side": hedge_side,
        "funding_rate_8h_bps": _d(quote.funding_rate_8h_bps),
        "borrow_rate_8h_bps": _d(quote.borrow_rate_8h_bps),
        "fee_tier": getattr(quote, "fee_tier", None),
        "open_fee_bps": _d(quote.taker_fee_bps),
        "close_fee_bps": _d(quote.close_fee_bps),
        "spread_bps": _d(quote.price_impact_bps + quote.est_slippage_bps),
    }

    sc = entry.get("source_carry") or {}
    if sc.get("funding_8h_bps") is not None:
        spread = hedge_funding_spread(
            Decimal(str(sc["funding_8h_bps"])),
            quote.funding_rate_8h_bps,
            Decimal(str(notional)),
            cover_bps=cover_bps,
            source_borrow_8h_bps=Decimal(str(sc.get("borrow_8h_bps") or 0)),
            hedge_borrow_8h_bps=Decimal(str(quote.borrow_rate_8h_bps or 0)),
            source_notional_usd=abs(pos.notional_usd),
        )
        entry["hedge_funding"] = {key: _d(value) for key, value in spread.items()}


@app.get("/api/health")
async def api_health():
    """Cheap liveness probe for Vercel / uptime checks. Does not hit venues."""
    return {"ok": True, "service": "hedge-scanner"}


@app.get("/api/scan")
async def api_scan_get(
    addresses: list[str] = Query(
        default=[],
        description="Wallet addresses. Repeat the param or comma-separate.",
    ),
):
    return await _run_scan(_normalize_addresses(addresses))


@app.post("/api/scan")
async def api_scan_post(req: ScanRequest):
    return await _run_scan(_normalize_addresses(req.addresses))


async def _run_scan(addresses: list[str]):
    if not addresses:
        return {"error": "No addresses provided"}

    # `only_public=True` skips venues whose position endpoints require an
    # account-bound credential (GRVT, Ondo) — a paste-an-address tool can
    # never satisfy that, so the "needs your API key" row on every scan is
    # noise. Public quote endpoints for those same venues are untouched and
    # remain available to the rest of the app.
    positions, errors = await portfolio.scan(addresses, only_public=True)

    # Filter policy (user-requested): only surface open trades that are
    #   (a) currently paying source-side carry — the source venue's live
    #       funding net of any borrow/rollover is negative from the position
    #       holder's perspective — and
    #   (b) hedgeable on Avantis (Avantis lists the base asset).
    # Everything else is dropped so the UI only shows actionable rows. Reasons
    # are counted so the aside can say "12 trades hidden: 8 not paying, 4 not
    # listed on Avantis".
    #
    # Source-side carry is fetched in one deduped fan-out (one call per unique
    # (venue, base_asset, side)) BEFORE the Avantis quote pass, so we only pay
    # the ~1.4s Avantis latency for positions that already pass the filter.
    carry_map = await _source_carry_map(positions)
    # Net longs vs shorts on the full open book, not the paying subset, so a
    # receiving short still offsets a paying long.
    hedge_plan, self_hedge = _hedge_plan(positions)

    results: list[dict[str, Any]] = []
    filtered_out = {"not_hedgeable": 0, "not_paying": 0, "no_carry_data": 0}
    avantis_targets: list[tuple[dict[str, Any], Any, str, float]] = []

    for pos in positions:
        if not _avantis_can_hedge(pos.base_asset):
            filtered_out["not_hedgeable"] += 1
            continue

        carry = carry_map.get((pos.venue, pos.base_asset, pos.side))
        if carry is None:
            # Adapter refused or threw — treat as "unknown", drop the row.
            # These are visible in the aside so the user can tell "no data"
            # from "not paying".
            filtered_out["no_carry_data"] += 1
            continue
        funding_bps, borrow_bps, carry_available = carry
        # Net rate signed from the POSITION's side:
        #   funding_rate_8h_bps > 0 → position receives funding
        #   borrow_rate_8h_bps    ≥ 0 → position always pays borrow/rollover
        # so net = funding − borrow; net < 0 means the position is paying net
        # carry right now, which is exactly the filter the user asked for.
        net_bps = funding_bps - borrow_bps
        if not carry_available or net_bps >= 0:
            filtered_out["not_paying"] += 1
            continue

        spec = hedge_plan.get(id(pos), {
            "role": "full",
            "hedge_side": "short" if pos.side == "long" else "long",
            "hedge_notional": abs(pos.notional_usd),
        })
        hedge_side = spec["hedge_side"]
        hedge_notional = Decimal(spec["hedge_notional"])
        pos_notional = abs(pos.notional_usd)

        entry = _pos_to_dict(pos)
        entry["can_hedge_on_avantis"] = True
        entry["hedge_side"] = hedge_side
        entry["hedge_role"] = spec["role"]
        entry["hedge_notional_usd"] = (
            _d(hedge_notional) if spec["role"] != "offsetting" else None
        )
        entry["avantis_quote"] = None
        entry["avantis_liq"] = None
        entry["avantis_unavailable"] = None
        entry["hedge_funding"] = None
        entry["source_liq"] = None
        entry["liq_distance_pct"] = None
        entry["source_carry"] = {
            "funding_8h_bps": _d(funding_bps),
            "borrow_8h_bps": _d(borrow_bps),
            "net_8h_bps": _d(net_bps),
            "usd_24h": _d(pos_notional * net_bps * _PERIODS_PER_24H / _BPS_DENOM),
        }

        mark = float(pos.mark_price) if pos.mark_price else float(pos.entry_price)
        lev = float(pos.leverage) if pos.leverage else 1.0

        # When the venue returns a real liquidation price (Hyperliquid does for
        # cross-margin), compute the distance from *mark* (not entry) so the
        # user sees "how far from here" rather than "how far from where I
        # opened".  For cross-margin accounts the API liq price already accounts
        # for shared equity — the static model cannot replicate that.
        if pos.liquidation_price is not None and mark > 0:
            liq_f = float(pos.liquidation_price)
            if pos.side == "long" and liq_f > 0:
                entry["liq_distance_pct"] = (mark - liq_f) / mark * 100
            elif pos.side == "short" and liq_f > 0:
                entry["liq_distance_pct"] = (liq_f - mark) / mark * 100

        is_cross = pos.margin_mode == "cross"

        src_spec = LIQUIDATION_SPECS.get(pos.venue)
        if src_spec and mark > 0 and lev > 0 and not is_cross:
            src_risk = compute_liquidation_risk(
                pos.venue, pos.side, pos.entry_price, pos.leverage or Decimal(1),
                pos.notional_usd,
            )
            venue_liq = pos.liquidation_price
            if src_risk or venue_liq is not None:
                # Venue liq is canonical when present (CONTRACT.md §12.13).
                # The static model still supplies the penalty shape.
                entry["source_liq"] = {
                    "liq_price": _d(
                        venue_liq if venue_liq is not None
                        else (src_risk.liq_price if src_risk else None)
                    ),
                    "distance_pct": (
                        entry["liq_distance_pct"]
                        if venue_liq is not None
                        else (_d(src_risk.distance_pct) if src_risk else None)
                    ),
                    "penalty_usd": _d(src_risk.penalty_usd) if src_risk else None,
                    "penalty_bps": _d(src_risk.penalty_bps) if src_risk else None,
                    "cross_margin_risk": src_spec.cross_margin_risk,
                    "model": src_spec.liquidation_model,
                }

        if src_spec and is_cross:
            entry["source_liq"] = {
                "liq_price": _d(pos.liquidation_price),
                "distance_pct": entry["liq_distance_pct"],
                "penalty_usd": None,
                "penalty_bps": None,
                "cross_margin_risk": src_spec.cross_margin_risk,
                "model": "cross_margin",
            }

        if spec["role"] != "offsetting" and hedge_notional >= DEFAULT_DUST_USD:
            avantis_targets.append(
                (entry, pos, hedge_side, float(hedge_notional))
            )
        results.append(entry)

    if avantis_targets:
        async with make_http_client(timeout=avantis.HTTP_TIMEOUT) as client:
            await asyncio.gather(
                *(_enrich_avantis(e, p, hs, n, client)
                  for (e, p, hs, n) in avantis_targets),
                return_exceptions=False,
            )

    # `only_public=True` above already prevents auth-gated venues from being
    # queried, so `errors` no longer contains `auth_required` entries in
    # practice. The list-comprehension keeps the filter as a defence in depth
    # for any future adapter that starts raising `auth_required` without
    # setting `public_positions = False` on its class.
    venue_errors = [
        {"venue": e.venue, "message": e.message, "kind": e.kind}
        for e in errors
        if e.kind != "auth_required"
    ]

    fee_schedule = {}
    for venue, s in FEE_SCHEDULE.items():
        fee_schedule[venue] = {
            "display_name": s.display_name,
            "open_fee_bps": _d(s.open_fee_bps),
            "close_fee_bps": _d(s.close_fee_bps),
            "round_trip_bps": _d(s.round_trip_fee_bps),
            "position_readable": s.position_readable,
        }

    return {
        "positions": results,
        "errors": venue_errors,
        "fee_schedule": fee_schedule,
        "horizon_hours": _d(HORIZON_HOURS),
        "horizon_label": format_horizon(HORIZON_HOURS),
        "filter": {
            "kept": len(results),
            "total": len(positions),
            "not_hedgeable_on_avantis": filtered_out["not_hedgeable"],
            "not_paying_funding": filtered_out["not_paying"],
            "no_carry_data": filtered_out["no_carry_data"],
        },
        "self_hedge_findings": self_hedge,
    }


async def _marks_from_adapter(cls: type) -> tuple[str, dict[str, float]]:
    adapter = cls()
    venue = getattr(adapter, "venue", getattr(cls, "venue", "unknown"))
    try:
        marks = await adapter.get_marks()
        return venue, {k: float(v) for k, v in marks.items()}
    except Exception:
        return venue, {}
    finally:
        closer = getattr(adapter, "aclose", None)
        if closer is not None:
            await closer()


async def _avantis_marks() -> dict[str, float]:
    try:
        marks = await avantis.get_marks()
        return {k: float(v) for k, v in marks.items()}
    except Exception:
        return {}


async def collect_venue_marks() -> dict[str, dict[str, float]]:
    """Fan out to every public mark feed. One dead venue returns ``{}``."""
    adapter_tasks = [_marks_from_adapter(cls) for cls in ADAPTER_CLASSES]
    results = await asyncio.gather(
        *adapter_tasks, _avantis_marks(), return_exceptions=True
    )
    prices: dict[str, dict[str, float]] = {}
    avantis_result = results[-1]
    for item in results[:-1]:
        if isinstance(item, tuple) and len(item) == 2:
            venue, marks = item
            prices[venue] = marks if isinstance(marks, dict) else {}
    prices["avantis"] = avantis_result if isinstance(avantis_result, dict) else {}
    return prices


@app.get("/api/prices")
async def api_prices():
    """Per-venue mark prices. Nested ``{venue: {market_or_asset: usd}}``.

    The UI polls this to refresh PnL in place. Each position is marked
    from *its* venue — mixing books would mis-state the residual on a
    cross-venue hedge. A venue that cannot serve a bulk mark feed (GRVT)
    is present as ``{}``; the poll then leaves that row's scan-time mark.
    """
    return {"prices": await collect_venue_marks()}


# ---------------------------------------------------------------------------
# Frontend (React SPA in hedge_scanner/static)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX = _STATIC_DIR / "index.html"

_DEV_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Scanner</title>
<style>
  body { background:#12110f; color:#ece9e2; font:13px/1.5 Inter, Helvetica, sans-serif;
         max-width:40rem; margin:4rem auto; padding:0 1.5rem; }
  code { font-family: ui-monospace, Menlo, monospace; color:#a49f95; }
</style>
</head>
<body>
<h1>Hedge Scanner</h1>
<p>The React UI is not built yet.</p>
<p>API is up. In another terminal:</p>
<pre><code>cd frontend && npm install && npm run dev</code></pre>
<p>Open <code>http://127.0.0.1:5173</code> (it proxies <code>/api</code> here).</p>
<p>To serve the UI from this process: <code>npm run build</code> then reload.</p>
</body>
</html>
"""

if _STATIC_DIR.is_dir() and (_STATIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

_venues_dir = _STATIC_DIR / "static" / "venues"
if _venues_dir.is_dir():
    app.mount("/static/venues", StaticFiles(directory=_venues_dir), name="venue-icons")


@app.get("/", response_class=HTMLResponse)
async def index():
    if _INDEX.is_file():
        return FileResponse(_INDEX)
    return HTMLResponse(_DEV_SHELL)



if __name__ == "__main__":
    import uvicorn

    # `--reload` restarts the worker on every source edit. That is convenient
    # while iterating but drops in-flight requests, which the browser surfaces
    # as "Failed to fetch" -- indistinguishable from the server being down.
    # Default to reload for local dev, allow HEDGE_SCANNER_RELOAD=0 to disable
    # when running long scans against real addresses.
    reload = os.environ.get("HEDGE_SCANNER_RELOAD", "1") != "0"
    uvicorn.run("hedge_scanner.web:app", host="127.0.0.1", port=8899, reload=reload)
