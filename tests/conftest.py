"""Serve the recorded fixtures through httpx so adapters run their real code paths.

Nothing here invents a response shape. Every body handed to an adapter is a
byte-for-byte replay of what the venue actually returned on 2026-08-19; see
`tests/capture_fixtures.py` for how they were recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# The order the custody accounts were requested in when the fixture was recorded;
# `getMultipleAccounts` responses are positional, not keyed.
CUSTODY_ORDER = (
    "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz",  # SOL
    "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn",  # ETH
    "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm",  # BTC
    "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa",  # USDC
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture() -> callable:
    return load


def _solana_rpc_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body["method"]
    if method == "getProgramAccounts":
        return httpx.Response(200, json=load("jupiter_program_accounts.json"))
    if method == "getMultipleAccounts":
        # The RPC returns one slot per requested pubkey, in request order, so
        # the replay has to honour the request rather than dump every custody.
        recorded = load("jupiter_custodies.json")
        by_pubkey = dict(
            zip(CUSTODY_ORDER, recorded["result"]["value"], strict=True)
        )
        requested = body["params"][0]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "context": recorded["result"]["context"],
                    "value": [by_pubkey.get(pubkey) for pubkey in requested],
                },
            },
        )
    if method == "getHealth":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})
    raise AssertionError(f"unexpected Solana RPC method: {method}")


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "api.mainnet-beta.solana.com" in url:
        return _solana_rpc_response(request)
    if "lite-api.jup.ag/price/v3" in url:
        return httpx.Response(200, json=load("jupiter_price_v3.json"))

    if "api.pacifica.fi" in url:
        if "/positions" in url:
            return httpx.Response(200, json=load("pacifica_positions.json"))
        if "/info/prices" in url:
            return httpx.Response(200, json=load("pacifica_info_prices.json"))
        if "/info/fees" in url:
            return httpx.Response(200, json=load("pacifica_info_fees.json"))
        if "/book" in url:
            return httpx.Response(200, json=load("pacifica_book_btc.json"))

    if "market-data.grvt.io" in url:
        if url.endswith("/all_instruments"):
            return httpx.Response(200, json=load("grvt_all_instruments.json"))
        if url.endswith("/book"):
            return httpx.Response(200, json=load("grvt_book_btc.json"))
        if url.endswith("/ticker") or url.endswith("/instrument"):
            return httpx.Response(200, json=load("grvt_ticker_btc.json"))
    if "trades.grvt.io" in url:
        recorded = load("grvt_positions_unauthenticated.json")
        return httpx.Response(recorded["status_code"], json=recorded["body"])

    if "api.hyperliquid.xyz" in url:
        body = json.loads(request.content)
        req_type = body.get("type", "")
        if req_type == "clearinghouseState":
            dex = body.get("dex")
            if dex is None:
                # Native perp DEX: the recorded HLP vault snapshot.
                return httpx.Response(
                    200, json=load("hyperliquid/clearinghouse_state.json")
                )
            if dex == "xyz":
                # Recorded live payload for the HIP-3 XYZ sub-DEX.
                return httpx.Response(
                    200, json=load("hyperliquid/clearinghouse_state_xyz.json")
                )
            # Every other sub-DEX returns an empty account by default,
            # matching what live probes return for accounts that only trade
            # on one sub-DEX.
            return httpx.Response(
                200, json=load("hyperliquid/clearinghouse_state_empty.json")
            )
        if req_type == "perpDexs":
            return httpx.Response(200, json=load("hyperliquid/perp_dexs.json"))
        if req_type == "allMids":
            return httpx.Response(200, json=load("hyperliquid/all_mids.json"))
        if req_type == "meta":
            return httpx.Response(200, json=load("hyperliquid/meta.json"))
        if req_type == "fundingHistory":
            return httpx.Response(
                200, json=load("hyperliquid/funding_history_btc.json")
            )
        if req_type == "userFees":
            return httpx.Response(
                200, json=load("hyperliquid/user_fees.json")
            )

    if "api.ondoperps.xyz" in url:
        if "/perps/contracts" in url:
            return httpx.Response(200, json=load("ondo_contracts.json"))
        if "/perps/depth" in url:
            return httpx.Response(200, json=load("ondo_depth_btc.json"))
        if "/perps/positions" in url:
            recorded = load("ondo_positions_unauthenticated.json")
            return httpx.Response(recorded["status_code"], json=recorded["body"])

    raise AssertionError(f"no fixture registered for {url}")


@pytest.fixture
def replay_client() -> httpx.AsyncClient:
    """An AsyncClient that replays recorded venue responses."""
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))
