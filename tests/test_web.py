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
    Fees and borrow are not in this number.
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


def test_avantis_margin_fee_is_in_hedge_apr_matching_ui_net_rate():
    """Avantis 24h = funding − marginFee, same as UI Net Rate (L/S) 24h.

    Hedge receives 12 bps/8h funding, pays 4 bps/8h borrow → net 8 bps/8h.
    Jupiter funding is 0 so Net APR is that Avantis net. Jupiter borrow is
    only in even-in recoup.
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


def test_jupiter_zero_funding_net_apr_is_avantis_only():
    """Jupiter has no funding rate. Borrow must not leak into Net APR."""
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("12"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
        source_borrow_8h_bps=Decimal("8"),
    )
    assert spread["source_apr_pct"] == Decimal("0")
    assert spread["net_8h_bps"] == Decimal("12")
    assert spread["net_apr_pct"] == Decimal("131.4")
    # Even-in recoups fees from funding net minus Jupiter borrow: (12-8)=4 bps/8h → 24h
    assert spread["breakeven_hours"] == Decimal("24")


def test_even_in_never_when_jupiter_borrow_exceeds_avantis_funding():
    from hedge_scanner.web import hedge_funding_spread

    spread = hedge_funding_spread(
        source_funding_8h_bps=Decimal("0"),
        hedge_funding_8h_bps=Decimal("8"),
        notional_usd=Decimal("10000"),
        cover_bps=Decimal("12"),
        source_borrow_8h_bps=Decimal("10"),
    )
    assert spread["net_apr_pct"] == Decimal("87.6")
    assert spread["breakeven_hours"] is None


def test_index_html_headlines_funding_apr_and_earn_24h():
    """The served page headlines funding APR and 24h dollar earn, not fees."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "Net APR" in html
    assert "Earn 24h" in html
    assert "Even in" in html
    assert "Fees occurred" in html
    assert "Funding / 24h" in html
    assert "even-box" in html
    assert "Hedge on Avantis" in html
    assert "https://www.avantisfi.com/trade?asset=" in html
    assert "Hurdle" not in html
    assert "Earn / yr" not in html
    assert "Hedge 24h" not in html
    assert "Avantis (Upside)" not in html
    assert "All-in 24h" not in html
