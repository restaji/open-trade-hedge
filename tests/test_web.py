"""HTTP surface: health, CORS, and the scan request schema.

Does not hit live venues. The scan handler is exercised only far enough to
confirm FastAPI accepts the public JSON body.
"""

from __future__ import annotations

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
