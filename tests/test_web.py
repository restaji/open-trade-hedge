"""HTTP surface: health, CORS, and the scan request schema.

Does not hit live venues. The scan handler is exercised only far enough to
confirm FastAPI accepts the public JSON body.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from hedge_scanner.web import app

client = TestClient(app)


def test_health_is_public():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "hedge-scanner"}


def test_cors_preflight_allows_browser_clients():
    response = client.options(
        "/api/scan",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "*"


def test_scan_rejects_empty_address_list():
    response = client.post("/api/scan", json={"addresses": ["  ", ""]})
    assert response.status_code == 200
    assert response.json() == {"error": "No addresses provided"}


def test_scan_get_rejects_missing_addresses():
    response = client.get("/api/scan")
    assert response.status_code == 200
    assert response.json() == {"error": "No addresses provided"}


def test_scan_rejects_malformed_body():
    response = client.post("/api/scan", json={"wallet": "0xabc"})
    assert response.status_code == 422


def test_openapi_is_published():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert "/api/scan" in spec["paths"]
    assert "get" in spec["paths"]["/api/scan"]
    assert "post" in spec["paths"]["/api/scan"]
    assert "/api/health" in spec["paths"]
    assert "/api/prices" in spec["paths"]


def test_apr_pct_from_8h_bps_uses_8760_hour_year():
    """10 bps/8h × 8760/8 / 100 = 109.5% APR."""
    from hedge_scanner.web import apr_pct_from_8h_bps

    assert apr_pct_from_8h_bps(Decimal("10")) == Decimal("109.5")
    assert apr_pct_from_8h_bps(Decimal("-8")) == Decimal("-87.6")
    assert apr_pct_from_8h_bps(Decimal("0")) == Decimal("0")


def test_hedge_funding_spread_adds_source_and_avantis_funding():
    """Source paying 8 bps/8h, Avantis hedge receiving 12 bps/8h.

    Net 4 bps/8h × 3 periods = 12 bps in 24h. On $10,000 that is $12.
    Fees are not in this number.
    """
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("-8"),
        hedge_funding_8h_bps=Decimal("12"),
        notional_usd=Decimal("10000"),
    )
    assert spread["source_apr_pct"] == Decimal("-87.6")
    assert spread["hedge_apr_pct"] == Decimal("131.4")
    assert spread["net_apr_pct"] == Decimal("43.8")
    assert spread["net_8h_bps"] == Decimal("4")
    assert spread["earn_usd_24h"] == Decimal("12")
    assert spread["source_usd_24h"] == Decimal("-24")
    assert spread["hedge_usd_24h"] == Decimal("36")
    assert spread["breakeven_hours"] == Decimal("0")  # no hurdle
    assert spread["cover_bps"] == Decimal("0")


def test_breakeven_hours_is_hurdle_over_net_funding():
    """12 bps of Avantis fees+spread, net funding 4 bps/8h → 24 hours."""
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("-8"),
        hedge_funding_8h_bps=Decimal("12"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
    )
    assert spread["cover_usd"] == Decimal("12")
    assert spread["breakeven_hours"] == Decimal("24")


def test_breakeven_hours_is_none_when_net_funding_does_not_pay():
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("-8"),
        hedge_funding_8h_bps=Decimal("2"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("9"),
    )
    assert spread["net_8h_bps"] == Decimal("-6")
    assert spread["breakeven_hours"] is None


def test_avantis_margin_fee_is_folded_into_funding():
    """Avantis funding = fundingRate − marginFee, one number.

    Hedge receives 12 bps/8h, pays 4 bps/8h margin → all-in +8 bps/8h.
    No source funding, so Net equals that Avantis funding.
    """
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("12"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
        source_borrow_8h_bps=Decimal("0"),
        hedge_borrow_8h_bps=Decimal("4"),
    )
    assert spread["net_8h_bps"] == Decimal("8")
    assert spread["hedge_apr_pct"] == Decimal("87.6")
    assert spread["earn_usd_24h"] == Decimal("24")
    assert spread["breakeven_hours"] == Decimal("12")


def test_jupiter_borrow_is_source_funding():
    """Jupiter borrow is the source funding leg; Source APR is not 0.

    Avantis receives 12, pays 4 margin → holder +8. Jupiter borrow 3.
    Holder net = 8 − 3 = 5. Source APR = −3 × 10.95 = −32.85%.
    """
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("12"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
        source_borrow_8h_bps=Decimal("3"),
        hedge_borrow_8h_bps=Decimal("4"),
    )
    assert spread["source_apr_pct"] == Decimal("-32.85")
    assert spread["hedge_apr_pct"] == Decimal("87.6")
    assert spread["net_8h_bps"] == Decimal("5")
    assert spread["net_apr_pct"] == Decimal("54.75")
    assert spread["earn_usd_24h"] == Decimal("15")
    assert spread["source_usd_24h"] == Decimal("-9")
    assert spread["hedge_usd_24h"] == Decimal("24")
    assert spread["breakeven_hours"] == Decimal("12") * Decimal("8") / Decimal("5")


def test_net_funding_24h_is_carry_only_on_each_legs_notional():
    """Position $200k paying 4 bps/8h, Avantis residual $5k receiving 8 bps/8h.

    Source −$240, hedge +$12, net −$228. Open/close/spread are not in this.
    """
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("8"),
        notional_usd=Decimal("5000"),
        cover_bps=Decimal("18.3"),
        source_borrow_8h_bps=Decimal("4"),
        source_notional_usd=Decimal("200000"),
    )
    assert spread["source_usd_24h"] == Decimal("-240")
    assert spread["hedge_usd_24h"] == Decimal("12")
    assert spread["earn_usd_24h"] == Decimal("-228")
    assert spread["cover_usd"] == Decimal("9.15")


def test_btc_jupiter_long_vs_avantis_short_nearly_offsets():
    """Live BTC shape: Jupiter long 0.0013%/hr vs Avantis short net ~0.00093%/h.

    Jupiter 0.0013%/hr = 1.04 bps/8h pay.
    Avantis short: funding received 0.0011605945058913369%/h minus
    marginFee 0.00022824%/h = 0.0009323545058913369%/h receive
    = 0.74588360471306952 bps/8h.
    Net = −0.29411639528693048 bps/8h. On $208,900 → −$18.43 / 24h.
    """
    from hedge_scanner.web import hedge_funding_spread

    jup_8h = Decimal("0.0013") * Decimal(8) * Decimal(100)  # 1.04
    av_fund = Decimal("0.0011605945058913369") * Decimal(8) * Decimal(100)
    av_margin = Decimal("0.00022824") * Decimal(8) * Decimal(100)
    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=av_fund,
        notional_usd=Decimal("208900"),
        source_borrow_8h_bps=jup_8h,
        hedge_borrow_8h_bps=av_margin,
    )
    assert jup_8h == Decimal("1.04")
    assert spread["source_apr_pct"] == Decimal("-11.388")
    assert spread["net_8h_bps"] == av_fund - av_margin - jup_8h
    assert spread["earn_usd_24h"] == (
        Decimal("208900") * spread["net_8h_bps"] * Decimal(3) / Decimal(10_000)
    )
    assert spread["earn_usd_24h"] < 0
    assert abs(spread["earn_usd_24h"] - Decimal("-18.43")) < Decimal("0.01")


def test_even_in_never_when_both_legs_pay_funding():
    """Both legs pay: Avantis holder −8, Jupiter funding 3 → holder net −11."""
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("-8"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
        source_borrow_8h_bps=Decimal("3"),
    )
    assert spread["net_8h_bps"] == Decimal("-11")
    assert spread["breakeven_hours"] is None


def test_index_serves_spa_or_dev_shell():
    """/ is the React SPA (built static) or a shell pointing at Vite."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "Hedge Scanner" in html
    assert "Avantis (Upside)" not in html
    assert "All-in 24h" not in html
    assert "Earn / yr" not in html
    assert "Hedge 24h" not in html
    assert "0 funding" not in html
    assert "As on venue" not in html


def _pos(venue: str, asset: str, side: str, notional: str):
    from hedge_scanner.models import Position

    n = Decimal(notional)
    signed = -abs(n) if side == "short" else abs(n)
    return Position(
        venue=venue,
        address="test",
        market=f"{asset}-PERP",
        base_asset=asset,
        quote_asset="USDC",
        side=side,
        size_base=abs(n) / Decimal(100),
        notional_usd=signed,
        entry_price=Decimal(100),
        mark_price=Decimal(100),
    )


def test_hedge_plan_quotes_residual_only_on_offsetting_book():
    """Jupiter BTC long $206k vs short $211k: Avantis only on the $5k net short."""
    from hedge_scanner.web import _hedge_plan

    long = _pos("jupiter", "BTC", "long", "206000")
    short = _pos("jupiter", "BTC", "short", "211000")
    plan, findings = _hedge_plan([long, short])
    assert len(findings) == 1
    assert findings[0]["base_asset"] == "BTC"
    assert findings[0]["fully_offset"] is False
    assert findings[0]["offsetting_notional_usd"] == 206000.0
    assert plan[id(short)]["role"] == "residual"
    assert plan[id(short)]["hedge_side"] == "long"
    assert plan[id(short)]["hedge_notional"] == Decimal("5000")
    assert plan[id(long)]["role"] == "offsetting"
    assert plan[id(long)]["hedge_notional"] == Decimal(0)


def test_hedge_plan_full_size_when_one_sided():
    from hedge_scanner.web import _hedge_plan

    long = _pos("jupiter", "SOL", "long", "10000")
    plan, findings = _hedge_plan([long])
    assert findings == []
    assert plan[id(long)]["role"] == "full"
    assert plan[id(long)]["hedge_notional"] == Decimal("10000")
    assert plan[id(long)]["hedge_side"] == "short"


def test_hedge_plan_fully_offset_quotes_neither_leg():
    from hedge_scanner.web import _hedge_plan

    long = _pos("jupiter", "BTC", "long", "10000")
    short = _pos("jupiter", "BTC", "short", "10010")
    plan, findings = _hedge_plan([long, short])
    assert findings[0]["fully_offset"] is True
    assert plan[id(long)]["role"] == "offsetting"
    assert plan[id(short)]["role"] == "offsetting"


def test_hedge_plan_cross_venue_residual_on_net_side():
    from hedge_scanner.web import _hedge_plan

    hl = _pos("hyperliquid", "BTC", "long", "100000")
    jup = _pos("jupiter", "BTC", "short", "40000")
    plan, findings = _hedge_plan([hl, jup])
    assert findings[0]["offsetting_notional_usd"] == 40000.0
    assert plan[id(hl)]["role"] == "residual"
    assert plan[id(hl)]["hedge_notional"] == Decimal("60000")
    assert plan[id(hl)]["hedge_side"] == "short"
    assert plan[id(jup)]["role"] == "offsetting"


def test_avantis_ui_net_rate_long_is_positive_when_longs_pay():
    """Funding print: + = pays, − = receives.

    Quote.funding is holder-signed. Display negates it so a paying long
    is positive, matching avantisfi.com Net Rate (L/S).
    """
    funding_pct_h = Decimal("0.0012")  # wire: long pays
    margin_pct_h = Decimal("0.00022824")
    funding_8h_bps = -funding_pct_h * Decimal(8) * Decimal(100)
    borrow_8h_bps = margin_pct_h * Decimal(8) * Decimal(100)
    holder_pct_h = (funding_8h_bps - borrow_8h_bps) / Decimal(8) / Decimal(100)
    ui_net_pct_h = -holder_pct_h
    assert ui_net_pct_h == funding_pct_h + margin_pct_h
    assert ui_net_pct_h > 0


def test_jupiter_borrow_prints_as_pay_not_receive():
    """Jupiter has no receive side. Short borrow 0.0007%/h prints +0.0007."""
    funding_8h_bps = Decimal(0)
    borrow_8h_bps = Decimal("0.0007") * Decimal(8) * Decimal(100)
    holder_pct_h = (funding_8h_bps - borrow_8h_bps) / Decimal(8) / Decimal(100)
    print_pct_h = -holder_pct_h
    assert print_pct_h == Decimal("0.0007")
    assert print_pct_h > 0
