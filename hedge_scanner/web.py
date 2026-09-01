"""Lightweight web UI for the hedge scanner.

Run:  cd hedge-scanner && uv run python -m hedge_scanner.web

Serves:
  GET  /            → single-page app (HTML embedded below)
  GET  /api/health  → liveness probe
  GET  /api/prices  → Ostium mark prices (UI poll)
  POST /api/scan    → {addresses: [str]} → positions + hedge opportunities
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from hedge_scanner import portfolio
from hedge_scanner.adapters import (
    GrvtAdapter,
    HyperliquidAdapter,
    JupiterAdapter,
    OndoAdapter,
    OstiumAdapter,
    PacificaAdapter,
)
from hedge_scanner.adapters.base import make_http_client
from hedge_scanner.adapters.ostium import PRICE_PRECISION
from hedge_scanner.assets import normalize_base_asset
from hedge_scanner.engine import FEE_SCHEDULE, format_horizon, hedge_cost
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
        "POST `/api/scan` with one or more wallet addresses."
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

# CONTRACT.md section 7.5.3 — headline numbers default to 24h and must be labeled.
HORIZON_HOURS = Decimal(24)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _d(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


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


async def _enrich_avantis(
    entry: dict[str, Any],
    pos: Any,
    hedge_side: str,
    notional: float,
    mark: float,
    lev: float,
    client: httpx.AsyncClient,
) -> None:
    """Populate the Avantis columns on `entry` in place.

    Runs one Avantis quote (~1.4s of network I/O for a fresh pair) and mutates
    `entry`. Safe to call concurrently for many positions on a shared client —
    `quote_hedge`'s two per-scan reads (`fetch_trading_snapshot`, `fetch_prices`)
    are TTL-cached, so parallel callers only duplicate the two per-position
    `fetch_spread_bps` legs, which is what we want.
    """
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

    # Single source of truth for the all-in number: the engine's own cost model,
    # not a formula re-derived in the browser.
    cost = hedge_cost(
        quote,
        horizon_hours=HORIZON_HOURS,
        notional_usd=Decimal(str(notional)),
    )
    entry["avantis_quote"] = {
        "market": quote.market,
        "side": hedge_side,
        "open_fee_bps": _d(quote.taker_fee_bps),
        "close_fee_bps": _d(quote.close_fee_bps),
        "funding_rate_8h_bps": _d(quote.funding_rate_8h_bps),
        "borrow_rate_8h_bps": _d(quote.borrow_rate_8h_bps),
        "spread_bps": _d(quote.price_impact_bps + quote.est_slippage_bps),
        "round_trip_bps": _d(cost.round_trip_fee_bps),
        "carry_bps_8h": _d(cost.carry_cost_bps_per_8h),
        "carry_bps": _d(cost.carry_cost_bps),
        "total_bps": _d(cost.total_bps),
        "total_usd": _d(cost.total_usd),
        "positive_carry": cost.positive_carry,
        "breakeven_hours": _d(cost.breakeven_hours),
        "fee_schedule_unverified": cost.fee_schedule_unverified,
        "notes": quote.notes,
    }

    avantis_spec = LIQUIDATION_SPECS.get("avantis")
    if avantis_spec and mark > 0 and lev > 0:
        av_risk = compute_liquidation_risk(
            "avantis", hedge_side,
            Decimal(str(mark)), Decimal(str(lev)),
            Decimal(str(notional)),
            fees_pct=quote.taker_fee_bps / Decimal(100),
            spec=avantis_spec,
        )
        if av_risk:
            entry["avantis_liq"] = {
                "liq_price": _d(av_risk.liq_price),
                "distance_pct": _d(av_risk.distance_pct),
                "penalty_usd": _d(av_risk.penalty_usd),
                "penalty_bps": _d(av_risk.penalty_bps),
                "cross_margin_risk": "position_only",
                "model": "health_ratio",
            }


async def _enrich_upside(
    entry: dict[str, Any],
    pos: Any,
    hedge_side: str,
    notional: float,
    client: httpx.AsyncClient,
) -> None:
    """Populate the Avantis (Upside) columns on `entry` in place.

    Upside Perps price a fundamentally different risk shape from the standard
    perp (CONTRACT.md §7.6): zero commission and borrow, profit-share instead.
    The pane surfaces both quotes so the user can compare the unconditional
    spread + funding cost of Upside against the standard perp, alongside the
    contingent profit-share obligation on a winning close. Runs in parallel
    with the standard Avantis quote on a shared client.
    """
    try:
        quote = await avantis.quote_upside_hedge(
            pos.base_asset, hedge_side,
            Decimal(str(notional)), client=client,
        )
    except Exception as exc:
        entry["upside_unavailable"] = (
            f"Avantis Upside quote request failed ({exc.__class__.__name__})."
        )
        return

    if quote is None:
        # Base asset has no Upside pair (crypto majors only).
        entry["upside_unavailable"] = (
            f"Avantis does not list an Upside Perp for {pos.base_asset}."
        )
        return

    if not quote.available:
        entry["upside_unavailable"] = (
            quote.notes or "Avantis Upside returned no usable quote for this size."
        )
        return

    schedule = getattr(quote, "profit_share_schedule", None) or []
    entry["upside_quote"] = {
        "venue": quote.venue,
        "market": quote.market,
        "side": hedge_side,
        "open_fee_bps": _d(quote.taker_fee_bps),
        "close_fee_bps": _d(quote.close_fee_bps),
        "funding_rate_8h_bps": _d(quote.funding_rate_8h_bps),
        "borrow_rate_8h_bps": _d(quote.borrow_rate_8h_bps),
        "spread_bps": _d(quote.price_impact_bps + quote.est_slippage_bps),
        "profit_share_schedule": [
            [_d(lower), _d(share)] for lower, share in schedule
        ],
        "notes": quote.notes,
    }


@app.get("/api/health")
async def api_health():
    """Cheap liveness probe for Vercel / uptime checks. Does not hit venues."""
    return {"ok": True, "service": "hedge-scanner"}


@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    addresses = [a.strip() for a in req.addresses if a.strip()]
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

    results: list[dict[str, Any]] = []
    filtered_out = {"not_hedgeable": 0, "not_paying": 0, "no_carry_data": 0}
    avantis_targets: list[tuple[dict[str, Any], Any, str, float, float, float]] = []

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

        entry = _pos_to_dict(pos)
        entry["can_hedge_on_avantis"] = True

        hedge_side = "short" if pos.side == "long" else "long"
        entry["hedge_side"] = hedge_side
        entry["avantis_quote"] = None
        entry["avantis_liq"] = None
        entry["avantis_unavailable"] = None
        entry["upside_quote"] = None
        entry["upside_unavailable"] = None
        entry["source_liq"] = None
        entry["liq_distance_pct"] = None
        entry["source_carry"] = {
            "funding_8h_bps": _d(funding_bps),
            "borrow_8h_bps": _d(borrow_bps),
            "net_8h_bps": _d(net_bps),
        }

        notional = abs(float(pos.notional_usd))
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
            if src_risk:
                entry["source_liq"] = {
                    "liq_price": _d(src_risk.liq_price),
                    "distance_pct": _d(src_risk.distance_pct),
                    "penalty_usd": _d(src_risk.penalty_usd),
                    "penalty_bps": _d(src_risk.penalty_bps),
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

        avantis_targets.append((entry, pos, hedge_side, notional, mark, lev))
        results.append(entry)

    if avantis_targets:
        async with make_http_client(timeout=avantis.HTTP_TIMEOUT) as client:
            # Fan out the standard Avantis perp and the Upside quote in parallel
            # on the shared client. Both consume the same TTL-cached snapshot
            # and price feeds; only the per-position spread calls duplicate.
            await asyncio.gather(
                *(_enrich_avantis(e, p, hs, n, m, lv, client)
                  for (e, p, hs, n, m, lv) in avantis_targets),
                *(_enrich_upside(e, p, hs, n, client)
                  for (e, p, hs, n, _m, _lv) in avantis_targets),
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
    }


@app.get("/api/prices")
async def api_prices():
    """Lightweight endpoint returning current mark prices per Ostium pair.

    The frontend polls this every few seconds to update PnL in place
    without re-running the full scan.
    """
    adapter = OstiumAdapter()
    try:
        pairs = await adapter._get_pairs()
    finally:
        await adapter.aclose()

    prices: dict[str, float] = {}
    for pair in pairs.values():
        sym = pair.get("from", "")
        raw = pair.get("lastTradePrice")
        if sym and raw:
            try:
                prices[sym] = float(Decimal(str(raw)) / PRICE_PRECISION)
            except (ArithmeticError, ValueError):
                pass
    return {"prices": prices}


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Scanner</title>
<style>
  :root {
    --bg:    #12110f;
    --bg2:   #191815;
    --rule:  #2b2924;
    --rule2: #201e1a;
    --ink:   #ece9e2;
    --ink2:  #a49f95;
    --ink3:  #6f6a61;
    --up:    #6f9f6b;
    --down:  #c4695a;
    --warn:  #bf9a2f;
    --mono:  ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    --display: "Helvetica Neue", Inter, system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { color-scheme: dark; }
  body {
    background: var(--bg); color: var(--ink);
    font: 12.5px/1.55 var(--mono);
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 56px 28px 90px; }

  /* Masthead */
  .mast { padding-bottom: 22px; border-bottom: 1px solid var(--rule); }
  h1 { font: 600 46px/1.04 var(--display); letter-spacing: -.03em; }
  .sub { margin-top: 11px; color: var(--ink3); font-size: 12px; }

  /* Search */
  .search { display: flex; align-items: center; gap: 14px; margin-top: 22px;
            border-bottom: 1px solid var(--rule); padding-bottom: 9px; }
  .search:focus-within { border-bottom-color: var(--ink3); }
  .search input { flex: 1; background: none; border: 0; outline: 0;
                  color: var(--ink); font: 14px var(--mono); padding: 2px 0; }
  .search input::placeholder { color: var(--ink3); }
  .search button { background: none; border: 0; cursor: pointer; padding: 4px 0;
                   color: var(--ink2); font: 11px var(--mono);
                   letter-spacing: .12em; text-transform: uppercase; }
  .search button:hover { color: var(--ink); }
  .search button:disabled { color: var(--ink3); cursor: default; }

  /* Lede + stats */
  .lede { font: 400 18px/1.4 var(--display); letter-spacing: -.01em;
          max-width: 64ch; margin-top: 34px; }
  .lede em { font-style: normal; color: var(--up); }
  .stats { display: flex; flex-wrap: wrap; gap: 4px 30px; margin-top: 16px; }
  .stats div { font-size: 10px; letter-spacing: .11em; text-transform: uppercase;
               color: var(--ink3); }
  .stats b { font: 400 12.5px var(--mono); letter-spacing: 0; text-transform: none;
             color: var(--ink2); margin-left: 7px; }
  .stats b.up { color: var(--up); }
  .stats b.down { color: var(--down); }

  /* Table */
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; margin-top: 28px; }
  th { font-size: 10px; letter-spacing: .11em; text-transform: uppercase;
       color: var(--ink3); font-weight: 400; text-align: right;
       padding-bottom: 9px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
  td { padding: 9px 0; text-align: right; white-space: nowrap;
       border-bottom: 1px solid var(--rule2); }
  th:first-child, td:first-child { text-align: left; }
  th:nth-child(2), tbody td:nth-child(2) { text-align: left; padding-left: 26px; }
  th + th, tbody td + td { padding-left: 20px; }
  tr.row { cursor: pointer; }
  tr.row:hover > td { background: var(--bg2); }
  tr.row.open > td { background: var(--bg2); border-bottom-color: transparent; }

  .sym { letter-spacing: .02em; }
  .side { font-size: 10px; letter-spacing: .1em; text-transform: uppercase; margin-left: 9px; }
  .caret { color: var(--ink3); margin-right: 9px; display: inline-block; width: 7px; }
  .up { color: var(--up); }
  .down { color: var(--down); }
  .warn { color: var(--warn); }
  .dim { color: var(--ink3); }
  .mut { color: var(--ink2); }

  /* Expanded detail */
  tr.detail > td { background: var(--bg2); padding: 14px 0 20px; white-space: normal;
                   border-bottom: 1px solid var(--rule); }
  .panes { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 32px; }
  @media (max-width: 900px) { .panes { grid-template-columns: 1fr; } }
  .pane h4 { font: 400 10px var(--mono); letter-spacing: .11em; text-transform: uppercase;
             color: var(--ink3); padding-bottom: 9px; margin-bottom: 8px;
             border-bottom: 1px solid var(--rule2); }
  .kv { display: flex; justify-content: space-between; gap: 20px; padding: 2.5px 0; }
  .kv > span:first-child { color: var(--ink3); }
  .kv.total { margin-top: 7px; padding-top: 8px; border-top: 1px solid var(--rule2); }
  .kv.total > span:first-child { color: var(--ink2); }
  .note { color: var(--ink3); font-size: 11.5px; line-height: 1.6; margin-top: 12px;
          max-width: 52ch; }

  /* Footnotes, errors, states */
  .foot { margin-top: 18px; color: var(--ink3); font-size: 11px; line-height: 1.7; }
  .aside { margin-top: 30px; padding-top: 14px; border-top: 1px solid var(--rule2);
           color: var(--ink3); font-size: 11px; line-height: 1.8; }
  /* Two asides can stack (hidden-rows summary + venue errors); one rule between
     them is enough, otherwise it reads as two unrelated footers. */
  .aside + .aside { margin-top: 10px; padding-top: 0; border-top: 0; }
  .aside b { font-weight: 400; color: var(--ink2); }
  .aside h5 { font: 400 10px var(--mono); letter-spacing: .11em; text-transform: uppercase;
              color: var(--ink3); }
  .state { margin-top: 36px; color: var(--ink2); }
</style>
</head>
<body>
<div class="wrap">

  <header class="mast">
    <h1>Hedge Scanner</h1>
    <p class="sub">Open perps currently paying funding on venues Avantis can hedge.</p>
  </header>

  <div class="search">
    <input id="addr" type="text" spellcheck="false" autocomplete="off"
           placeholder="0x… or Solana address">
    <button id="go">Scan</button>
  </div>

  <div id="out"></div>

</div>

<script>
const $ = s => document.querySelector(s);
let HORIZON = '24h';
let POSITIONS = [];     // live reference for price updates
let POLL_ID = null;     // setInterval handle

/* ---------- formatting ---------- */
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nf = (n, d) => Number(n).toLocaleString('en-US',
  {minimumFractionDigits: d, maximumFractionDigits: d});

function usd(n, d) {
  if (n == null) return '—';
  const v = Number(n);
  const dec = d != null ? d : (Math.abs(v) >= 1000 ? 0 : 2);
  return (v < 0 ? '-$' : '$') + nf(Math.abs(v), dec);
}
function signedUsd(n) {
  if (n == null) return '—';
  const v = Number(n);
  if (v === 0) return '$0';
  const dec = Math.abs(v) >= 1000 ? 0 : 2;
  return (v > 0 ? '+$' : '-$') + nf(Math.abs(v), dec);
}
function compactUsd(n) {
  const v = Math.abs(Number(n || 0));
  if (v >= 1e9) return '$' + nf(v / 1e9, 2) + 'B';
  if (v >= 1e6) return '$' + nf(v / 1e6, 2) + 'M';
  if (v >= 1e3) return '$' + nf(v / 1e3, 1) + 'K';
  return '$' + nf(v, 2);
}
function price(n) {
  if (n == null) return '—';
  const v = Math.abs(Number(n));
  return '$' + nf(n, v >= 100 ? 2 : v >= 1 ? 4 : 6);
}
const bps = (n, d) => n == null ? '—' : nf(n, d == null ? 1 : d) + ' bps';
const pct = (n, d) => n == null ? '—' : nf(n, d == null ? 1 : d) + '%';
const kv = (k, v) => '<div class="kv"><span>' + k + '</span><span>' + v + '</span></div>';

/* ---------- scan ---------- */
async function scan() {
  const raw = $('#addr').value.trim();
  if (!raw) return;
  const btn = $('#go');
  btn.disabled = true;
  btn.textContent = 'Scanning';
  $('#out').innerHTML = '<div class="state">Scanning…</div>';
  try {
    const resp = await fetch('/api/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({addresses: raw.split(/[,\\s]+/).filter(Boolean)}),
    });
    render(await resp.json());
  } catch (e) {
    $('#out').innerHTML = '<div class="state"><span class="down">The scanner did not respond.</span> ' + esc(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan';
  }
}
$('#go').addEventListener('click', scan);
$('#addr').addEventListener('keydown', e => { if (e.key === 'Enter') scan(); });

/* ---------- render ---------- */
function render(data) {
  const positions = (data.positions || []).slice();
  const errors = data.errors || [];
  const filter = data.filter || null;
  if (data.horizon_hours != null) HORIZON = nf(data.horizon_hours, 0) + 'h';

  POSITIONS = positions;
  if (POLL_ID) clearInterval(POLL_ID);

  if (!positions.length) {
    // Distinguish "wallet is empty" from "wallet has positions, none matched
    // the filter" — the second is a very different action item.
    const totalScanned = filter ? filter.total : 0;
    const emptyMsg = totalScanned === 0
      ? 'No open positions.'
      : totalScanned + ' open position' + (totalScanned === 1 ? '' : 's') +
        ', none currently paying funding on an Avantis-listed pair.';
    $('#out').innerHTML =
      '<div class="state">' + emptyMsg + '</div>' +
      filterHtml(filter) + asideHtml(errors);
    return;
  }

  // Positive carry first — CONTRACT.md section 6 calls these the actual
  // opportunity — then anything hedgeable, then by size. Every row here is
  // already filtered to "currently paying source funding AND hedgeable on
  // Avantis", so the tail buckets are always empty in practice; kept as
  // fallback ordering for the priced-but-flat-carry case.
  const rank = p => p.avantis_quote ? (p.avantis_quote.positive_carry ? 0 : 1) : 2;
  positions.sort((a, b) =>
    rank(a) - rank(b) || Math.abs(b.notional_usd || 0) - Math.abs(a.notional_usd || 0));

  $('#out').innerHTML =
    ledeHtml(positions) + statsHtml(positions) + tableHtml(positions) +
    footHtml() + filterHtml(filter) + asideHtml(errors);

  document.querySelectorAll('tr.row').forEach(row => {
    row.addEventListener('click', () => {
      const open = row.classList.toggle('open');
      row.nextElementSibling.style.display = open ? '' : 'none';
      row.querySelector('.caret').textContent = open ? '–' : '+';
    });
  });

  POLL_ID = setInterval(pollPrices, 5000);
  pollPrices();
}

function ledeHtml(ps) {
  const n = ps.length;
  const quoted = ps.filter(p => p.avantis_quote);
  const carry = quoted.filter(p => p.avantis_quote.positive_carry);
  const cost = quoted.reduce((s, p) => s + (p.avantis_quote.total_usd || 0), 0);
  // Denominator is the SHOWN count, never the pre-filter total: hidden rows
  // were dropped before the Avantis quote pass, so they were never evaluated
  // for carry and "1 of 11" would be a denominator we never measured. The
  // kept-vs-total framing belongs to the Hidden aside, which states it once.
  const some = k => k === n ? '' : ' &mdash; ' + k + ' of ' + n;

  let text;
  if (carry.length) {
    text = '<em>Positive carry</em> on Avantis' + some(carry.length) + '.';
  } else if (quoted.length) {
    text = 'Hedge on Avantis: ' + usd(cost) + ' over ' + HORIZON + some(quoted.length) + '.';
  } else {
    // Filter guarantees hedgeable-on-Avantis, so any missing quote is a live
    // pricing failure, not a listing gap.
    const why = ps.map(p => p.avantis_unavailable).filter(Boolean);
    const same = why.length === ps.length && new Set(why).size === 1;
    text = same ? esc(why[0]) : 'Paying funding on Avantis-listed pairs, not priceable right now.';
  }
  return '<p class="lede">' + text + '</p>';
}

function statsHtml(ps) {
  const gross = ps.reduce((s, p) => s + Math.abs(p.notional_usd || 0), 0);
  const pnl = ps.reduce((s, p) => s + (p.unrealized_pnl_usd || 0), 0);
  const paid = ps.reduce((s, p) => s + (p.funding_paid_usd || 0), 0);
  const quoted = ps.filter(p => p.avantis_quote).length;
  return '<div class="stats">' +
    '<div>Positions<b>' + ps.length + '</b></div>' +
    '<div>Notional<b>' + compactUsd(gross) + '</b></div>' +
    '<div>PnL<b class="' + tone(pnl) + '">' + signedUsd(pnl) + '</b></div>' +
    '<div>Funding<b class="' + tone(paid) + '">' + signedUsd(paid) + '</b></div>' +
    '<div>Priced<b>' + quoted + ' / ' + ps.length + '</b></div>' +
    '</div>';
}

const tone = v => v > 0 ? 'up' : v < 0 ? 'down' : 'dim';

function tableHtml(ps) {
  let h = '<div class="scroll"><table><thead><tr>' +
    '<th>Market</th><th>Venue</th><th>Notional</th><th>Lev</th><th>Entry</th><th>Mark</th>' +
    '<th>PnL</th><th>Funding</th><th>Liq</th>' +
    '<th>Hedge ' + HORIZON + '</th>' +
    '</tr></thead><tbody>';
  ps.forEach((p, i) => { h += rowHtml(p, i); });
  return h + '</tbody></table></div>';
}

function rowHtml(p, i) {
  const q = p.avantis_quote;
  const sideCls = p.side === 'long' ? 'up' : 'down';

  const liqPrice = p.liquidation_price != null ? p.liquidation_price
                 : (p.source_liq ? p.source_liq.liq_price : null);
  const dist = p.liq_distance_pct != null ? p.liq_distance_pct
             : (p.source_liq ? p.source_liq.distance_pct : null);
  const liq = liqPrice == null ? '<span class="dim">—</span>'
    : price(liqPrice) + (dist == null ? '' :
        ' <span class="' + (dist < 10 ? 'down' : 'dim') + '">' + pct(dist) + '</span>');

  const f = p.funding_paid_usd;
  const funding = f == null ? '<span class="dim">—</span>'
    : f < 0 ? '<span class="down">' + signedUsd(f) + '</span>'
    : f > 0 ? '<span class="up">' + signedUsd(f) + '</span>'
    : '<span class="dim">$0</span>';

  const hedge = q
    ? '<span class="' + (q.positive_carry ? 'up' : '') + '">' + bps(q.total_bps) + '</span>' +
      ' <span class="' + (q.positive_carry ? 'up' : 'dim') + '">' + usd(q.total_usd) + '</span>'
    : '<span class="dim">' + (!p.can_hedge_on_avantis ? 'not listed'
        : p.avantis_unavailable ? 'unavailable' : 'no quote') + '</span>';

  return '<tr class="row" data-i="' + i + '">' +
    '<td><span class="caret">+</span><span class="sym">' + esc(p.base_asset) + '</span>' +
      '<span class="side ' + sideCls + '">' + esc(p.side) + '</span></td>' +
    '<td class="mut">' + esc(p.venue) + '</td>' +
    '<td>' + usd(Math.abs(p.notional_usd)) + '</td>' +
    '<td class="mut">' + (p.leverage ? nf(p.leverage, 1) + 'x' : '—') + '</td>' +
    '<td class="mut">' + price(p.entry_price) + '</td>' +
    '<td>' + price(p.mark_price) + '</td>' +
    '<td class="' + tone(p.unrealized_pnl_usd) + '">' + signedUsd(p.unrealized_pnl_usd) + '</td>' +
    '<td>' + funding + '</td>' +
    '<td>' + liq + '</td>' +
    '<td>' + hedge + '</td>' +
    '</tr>' +
    '<tr class="detail" style="display:none"><td colspan="10">' + detailHtml(p) + '</td></tr>';
}

function detailHtml(p) {
  const sl = p.source_liq, al = p.avantis_liq, q = p.avantis_quote;
  const sc = p.source_carry;

  let left = '<div class="pane"><h4>' + esc(p.venue) + ' &middot; ' + esc(p.market) + '</h4>';
  left += kv('Size', p.size_base != null
    ? nf(p.size_base, Math.abs(p.size_base) >= 1000 ? 2 : 4) + ' ' + esc(p.base_asset) : '—');
  left += kv('Entry', price(p.entry_price));
  left += kv('Collateral', usd(p.collateral_usd));
  left += kv('Margin', esc(p.margin_mode || '—'));
  if (sc) {
    // "Funding 8h" here is signed from the POSITION's side (positive = the
    // position receives, negative = pays). Every row is filtered on net < 0
    // so the highlight color is always down; kept as a visual anchor.
    left += kv('Funding 8h', bps(sc.funding_8h_bps) +
      (sc.funding_8h_bps > 0 ? ' <span class="up">received</span>'
       : sc.funding_8h_bps < 0 ? ' <span class="down">paid</span>' : ''));
    // With no borrow leg, net == funding, so a "Net 8h" row would just repeat
    // the line above it. Only show it when borrow actually moves the number.
    if (sc.borrow_8h_bps > 0) {
      left += kv('Borrow 8h', bps(sc.borrow_8h_bps));
      left += kv('Net 8h', '<span class="down">' + bps(sc.net_8h_bps) + ' paid</span>');
    }
  }
  if (p.funding_paid_usd != null) {
    left += kv('Paid to date', signedUsd(p.funding_paid_usd));
  }
  if (sl) {
    const slLiq = sl.liq_price != null ? price(sl.liq_price) : '—';
    const slDist = sl.distance_pct != null ? pct(sl.distance_pct, 2) : '—';
    left += kv('Liquidation', slLiq + ' &middot; ' + slDist);
    if (sl.penalty_usd != null)
      left += kv('Penalty', usd(sl.penalty_usd) + ' (' + bps(sl.penalty_bps, 0) + ')');
    left += kv('Exposed', sl.cross_margin_risk === 'full_account'
      ? '<span class="down">whole account</span>' : 'position only');
    left += kv('Model', esc(sl.model).replace(/_/g, ' '));
  }
  left += '</div>';

  let right = '<div class="pane">';
  if (q) {
    right += '<h4>Avantis hedge &middot; ' + esc(q.side) + ' ' + esc(q.market) + '</h4>';
    right += kv('Fees', bps((q.open_fee_bps || 0) + (q.close_fee_bps || 0)));
    right += kv('Spread', bps(q.spread_bps));
    right += kv('Round trip', bps(q.round_trip_bps));
    right += kv('Funding 8h', bps(q.funding_rate_8h_bps) +
      (q.funding_rate_8h_bps > 0 ? ' <span class="up">received</span>'
       : q.funding_rate_8h_bps < 0 ? ' <span class="down">paid</span>' : ''));
    right += kv('Borrow 8h', bps(q.borrow_rate_8h_bps));
    right += kv('Carry ' + HORIZON, bps(q.carry_bps));
    right += '<div class="kv total"><span>All-in ' + HORIZON + '</span><span class="' +
      (q.positive_carry ? 'up' : '') + '">' + bps(q.total_bps) + ' &middot; ' + usd(q.total_usd) +
      '</span></div>';
    if (q.breakeven_hours != null) {
      right += kv('Breakeven', nf(q.breakeven_hours, 1) + ' h');
    }
    if (al) {
      right += kv('Liquidation', price(al.liq_price) + ' &middot; ' + pct(al.distance_pct, 2));
      right += kv('Exposed', 'position only');
    }
    if (q.fee_schedule_unverified) {
      right += '<p class="note">Static fee fallback, not a live read.</p>';
    }
    if (q.notes) right += '<p class="note">' + esc(q.notes) + '</p>';
  } else if (p.can_hedge_on_avantis) {
    right += '<h4>Avantis hedge &middot; ' + esc(p.hedge_side) + ' ' + esc(p.base_asset) + '</h4>';
    right += '<p class="note">' + (p.avantis_unavailable
      ? esc(p.avantis_unavailable) : 'No live quote for this size.') + '</p>';
  } else {
    right += '<h4>Avantis hedge</h4><p class="note">Not listed on Avantis.</p>';
  }
  right += '</div>';

  // Third pane: Avantis (Upside), a distinct hedge instrument. Zero commission
  // and zero borrow, in exchange for a share of gross profit only on a winning
  // close. Not a cheaper Avantis — a different risk shape (see CONTRACT.md §7.6).
  const uq = p.upside_quote;
  let upside = '<div class="pane">';
  if (uq) {
    upside += '<h4>Avantis (Upside) &middot; ' + esc(uq.side) + ' ' + esc(uq.market) + '</h4>';
    upside += kv('Open fee', bps(uq.open_fee_bps));
    upside += kv('Close fee', bps(uq.close_fee_bps));
    upside += kv('Spread', bps(uq.spread_bps));
    upside += kv('Funding 8h', bps(uq.funding_rate_8h_bps) +
      (uq.funding_rate_8h_bps > 0 ? ' <span class="up">received</span>'
       : uq.funding_rate_8h_bps < 0 ? ' <span class="down">paid</span>' : ''));
    upside += kv('Borrow 8h', bps(uq.borrow_rate_8h_bps));
    if (uq.profit_share_schedule && uq.profit_share_schedule.length) {
      const bands = uq.profit_share_schedule
        .map(b => 'ROI &ge;' + nf(b[0], 0) + '% &rarr; ' + nf(b[1], 0) + '%')
        .join(' &middot; ');
      upside += '<p class="note"><b>Profit share (live pnlFees):</b> ' + bands +
        '. Zero cost if the hedge closes at a loss.</p>';
    }
    upside += '<p class="note">Cheaper than the standard Avantis perp when the ' +
      'hedge turns out unnecessary; more expensive when the hedge actually pays ' +
      'off, because you surrender the profit share above. Market orders only; ' +
      'crypto majors only (BTC/ETH/SOL/XRP/HYPE).</p>';
  } else if (p.can_hedge_on_avantis) {
    upside += '<h4>Avantis (Upside) &middot; ' + esc(p.hedge_side) + ' ' + esc(p.base_asset) + '</h4>';
    upside += '<p class="note">' + (p.upside_unavailable
      ? esc(p.upside_unavailable)
      : 'No Upside quote for this size.') + '</p>';
  } else {
    upside += '<h4>Avantis (Upside)</h4><p class="note">Not listed on Avantis.</p>';
  }
  upside += '</div>';

  return '<div class="panes">' + left + right + upside + '</div>';
}

/* ---------- live price polling ---------- */
async function pollPrices() {
  if (!POSITIONS.length) return;
  try {
    const resp = await fetch('/api/prices');
    const data = await resp.json();
    const px = data.prices || {};
    let totalPnl = 0, totalNotional = 0;

    POSITIONS.forEach((p, i) => {
      const newMark = px[p.base_asset];
      if (newMark == null || !p.size_base) return;
      p.mark_price = newMark;
      p.notional_usd = p.size_base * newMark;
      if (p.side === 'long') {
        p.unrealized_pnl_usd = p.size_base * (newMark - p.entry_price);
      } else {
        p.unrealized_pnl_usd = p.size_base * (p.entry_price - newMark);
      }

      const row = document.querySelector('tr.row[data-i="' + i + '"]');
      if (!row) return;
      const cells = row.querySelectorAll('td');
      // cols: 0=market, 1=venue, 2=notional, 3=lev, 4=entry, 5=mark, 6=unrealized, 7=funding, 8=liq, 9=hedge
      cells[2].textContent = usd(Math.abs(p.notional_usd));
      cells[5].textContent = price(p.mark_price);
      const pnl = p.unrealized_pnl_usd;
      cells[6].className = tone(pnl);
      cells[6].innerHTML = signedUsd(pnl);
    });

    // Update summary stats
    POSITIONS.forEach(p => {
      totalPnl += (p.unrealized_pnl_usd || 0);
      totalNotional += Math.abs(p.notional_usd || 0);
    });
    const statEls = document.querySelectorAll('.stats div');
    if (statEls[1]) statEls[1].querySelector('b').textContent = compactUsd(totalNotional);
    if (statEls[2]) {
      const b = statEls[2].querySelector('b');
      b.className = tone(totalPnl);
      b.textContent = signedUsd(totalPnl);
    }
  } catch (_) {}
}

function footHtml() {
  // The filter rule is stated once in the masthead subtitle and its effect once
  // in the Hidden aside; repeating it here made this a two-line paragraph.
  return '<p class="foot">All-in ' + HORIZON + ' = open + close + spread + ' +
    '(borrow &minus; funding) &times; ' + HORIZON + '/8h. Negative is positive carry. Read-only.</p>';
}

function filterHtml(filter) {
  if (!filter) return '';
  const parts = [];
  if (filter.not_paying_funding)
    parts.push(filter.not_paying_funding + ' not paying funding');
  if (filter.not_hedgeable_on_avantis)
    parts.push(filter.not_hedgeable_on_avantis + ' not listed on Avantis');
  if (filter.no_carry_data)
    parts.push(filter.no_carry_data + ' no live rate');
  if (!parts.length) return '';
  return '<div class="aside"><h5>Hidden</h5><div>' +
    filter.kept + ' of ' + filter.total + ' shown &middot; ' +
    esc(parts.join(', ')) + '</div></div>';
}

function asideHtml(errors) {
  // Server pre-filters `auth_required` (GRVT/Ondo) — those venues can't serve
  // positions for a third-party address and every scan would surface the same
  // useless "needs your API key" row. What remains here is genuinely
  // actionable: transient venue outages, unsupported address namespaces, etc.
  if (!errors.length) return '';
  const rows = errors.map(e => '<div><b>' + esc(e.venue) + '</b> ' +
    esc(e.message) + '</div>').join('');
  return '<div class="aside"><h5>Not read</h5>' + rows + '</div>';
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


if __name__ == "__main__":
    import uvicorn

    # `--reload` restarts the worker on every source edit. That is convenient
    # while iterating but drops in-flight requests, which the browser surfaces
    # as "Failed to fetch" -- indistinguishable from the server being down.
    # Default to reload for local dev, allow HEDGE_SCANNER_RELOAD=0 to disable
    # when running long scans against real addresses.
    reload = os.environ.get("HEDGE_SCANNER_RELOAD", "1") != "0"
    uvicorn.run("hedge_scanner.web:app", host="127.0.0.1", port=8899, reload=reload)
