"""Per-venue marks: adapters, Avantis, and the /api/prices fan-out.

Live UI PnL must be marked from the position's own venue. These tests pin
that the nested ``{venue: {asset: usd}}`` shape cannot collapse two books
onto one number, and that a dead venue degrades to ``{}`` instead of 500ing
the poll.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from hedge_scanner.adapters.base import record_mark
from hedge_scanner.adapters.grvt import GrvtAdapter
from hedge_scanner.adapters.hyperliquid import HyperliquidAdapter
from hedge_scanner.adapters.jupiter import JupiterAdapter
from hedge_scanner.adapters.ondo import OndoAdapter
from hedge_scanner.adapters.ostium import OstiumAdapter, PRICE_PRECISION
from hedge_scanner.adapters.pacifica import PacificaAdapter
from hedge_scanner.hedge_venues import avantis
from hedge_scanner.web import app, collect_venue_marks

client = TestClient(app)


def test_record_mark_indexes_native_and_canonical():
    out: dict[str, Decimal] = {}
    record_mark(out, "xyz:BRENTOIL", Decimal("70"))
    assert out["xyz:BRENTOIL"] == Decimal("70")
    assert out["BRENT"] == Decimal("70")


def test_record_mark_first_writer_wins_canonical():
    out: dict[str, Decimal] = {}
    record_mark(out, "BTC", Decimal("100"))
    record_mark(out, "BTC_UPSIDE", Decimal("101"))
    assert out["BTC"] == Decimal("100")
    assert out["BTC_UPSIDE"] == Decimal("101")


def test_record_mark_skips_non_positive():
    out: dict[str, Decimal] = {}
    record_mark(out, "BTC", Decimal("0"))
    record_mark(out, "ETH", None)
    record_mark(out, "", Decimal("1"))
    assert out == {}


async def test_pacifica_marks_use_venue_mark(replay_client, fixture):
    adapter = PacificaAdapter(client=replay_client)
    marks = await adapter.get_marks()
    raw = next(
        row for row in fixture("pacifica_info_prices.json")["data"] if row["symbol"] == "BTC"
    )
    assert marks["BTC"] == Decimal(raw["mark"])


async def test_jupiter_marks_use_the_doves_oracle(replay_client, fixture):
    """Jupiter marks must come from the Doves oracle, not price.jup.ag.

    Jupiter Perps computes PnL, liquidation, and health checks against the
    Doves oracle. Previously this test asserted the DEX aggregator was the
    source, which caused per-asset drift versus jup.ag/portfolio -- $334 on a
    2.6 BTC position, $149 on a 1000 SOL position (2026-09-02 investigation).
    Since 2026-09-02 the adapter reads Doves as primary with DEX-agg fallback
    only when Doves is stale or missing; this test locks in the new order.
    """
    doves_fixture = fixture("jupiter_doves.json")
    now_s = lambda: int(doves_fixture["_captured_at"]) + 15  # noqa: E731
    adapter = JupiterAdapter(client=replay_client, now_s=now_s)
    marks = await adapter.get_marks()

    # BTC must equal the Doves value, not the DEX-aggregator value.
    from hedge_scanner.adapters.jupiter import decode_doves_price
    import base64 as _b64
    btc_pubkey = "hUqAT1KQ7eW1i6Csp9CXYtpPfSAvi835V7wKi5fRfmC"
    btc_idx = doves_fixture["_requested_pubkeys"].index(btc_pubkey)
    btc_account = doves_fixture["result"]["value"][btc_idx]
    doves_btc, _ = decode_doves_price(_b64.b64decode(btc_account["data"][0]))
    assert marks["BTC"] == doves_btc
    assert marks["BTC-PERP"] == doves_btc

    dex_agg_btc = Decimal(
        str(fixture("jupiter_price_v3.json")["3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"]["usdPrice"])
    )
    assert marks["BTC"] != dex_agg_btc, (
        "BTC mark equals price.jup.ag; the Doves-primary path is not being taken."
    )
    assert "ETH" in marks and "SOL" in marks


async def test_hyperliquid_marks_are_native_mids(replay_client, fixture):
    adapter = HyperliquidAdapter(
        client=replay_client, api_url="https://api.hyperliquid.xyz/info"
    )
    marks = await adapter.get_marks()
    mids = fixture("hyperliquid/all_mids.json")
    assert marks["BTC"] == Decimal(mids["BTC"])
    assert marks["ETH"] == Decimal(mids["ETH"])
    # HIP-3 is namespaced so it cannot overwrite native BTC.
    assert marks["xyz:BTC"] == Decimal(mids["BTC"])
    assert "@1" not in marks
    assert not any(k.startswith("#") for k in marks)


async def test_ondo_marks_use_index_price(replay_client, fixture):
    adapter = OndoAdapter(client=replay_client)
    marks = await adapter.get_marks()
    btc = next(
        row
        for row in fixture("ondo_contracts.json")["result"]
        if row["baseCurrency"] == "BTC"
    )
    assert marks["BTC"] == Decimal(btc["indexPrice"])
    assert marks["BTC-USD.P"] == Decimal(btc["indexPrice"])


async def test_grvt_marks_are_empty_without_a_bulk_feed(replay_client):
    adapter = GrvtAdapter(client=replay_client)
    assert await adapter.get_marks() == {}


async def test_ostium_marks_divide_last_trade_price(monkeypatch):
    adapter = OstiumAdapter()
    raw = str(Decimal("65000") * PRICE_PRECISION)

    async def fake_pairs():
        return {"1": {"from": "BTC", "to": "USD", "lastTradePrice": raw}}

    monkeypatch.setattr(adapter, "_get_pairs", fake_pairs)
    marks = await adapter.get_marks()
    assert marks["BTC"] == Decimal("65000")
    assert marks["BTC/USD"] == Decimal("65000")


async def test_ostium_fx_marks_do_not_collide_on_bare_eur(monkeypatch):
    adapter = OstiumAdapter()
    eur_usd = str(Decimal("1.08") * PRICE_PRECISION)
    eur_gbp = str(Decimal("0.84") * PRICE_PRECISION)

    async def fake_pairs():
        return {
            "2": {"from": "EUR", "to": "USD", "lastTradePrice": eur_usd},
            "9": {"from": "EUR", "to": "GBP", "lastTradePrice": eur_gbp},
        }

    monkeypatch.setattr(adapter, "_get_pairs", fake_pairs)
    marks = await adapter.get_marks()
    assert marks["EURUSD"] == Decimal("1.08")
    assert marks["EURGBP"] == Decimal("0.84")
    assert "EUR" not in marks


async def test_collect_marks_isolates_a_dead_venue(monkeypatch):
    class Boom:
        venue = "ostium"

        async def get_marks(self):
            raise RuntimeError("subgraph down")

        async def aclose(self):
            pass

    class Ok:
        venue = "pacifica"

        async def get_marks(self):
            return {"BTC": Decimal("1")}

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "hedge_scanner.web.ADAPTER_CLASSES", (Boom, Ok)
    )

    async def no_avantis():
        return {}

    monkeypatch.setattr("hedge_scanner.web._avantis_marks", no_avantis)
    prices = await collect_venue_marks()
    assert prices["ostium"] == {}
    assert prices["pacifica"]["BTC"] == 1.0
    assert prices["avantis"] == {}


def test_api_prices_is_nested_by_venue(monkeypatch):
    async def fake():
        return {
            "ostium": {"BTC": 100.0},
            "hyperliquid": {"BTC": 101.0},
            "avantis": {"BTC": 102.0},
        }

    monkeypatch.setattr("hedge_scanner.web.collect_venue_marks", fake)
    response = client.get("/api/prices")
    assert response.status_code == 200
    prices = response.json()["prices"]
    assert prices["ostium"]["BTC"] == 100.0
    assert prices["hyperliquid"]["BTC"] == 101.0
    assert prices["ostium"]["BTC"] != prices["hyperliquid"]["BTC"]
