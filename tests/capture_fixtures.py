"""Re-record the fixtures in tests/fixtures/ from live venue endpoints.

Not a test. Run it by hand when a venue changes shape:

    uv run python tests/capture_fixtures.py

Everything it writes is a real, unedited response body so the tests pin actual
venue behaviour rather than a shape someone imagined. The 401 fixtures are the
whole point for GRVT and Ondo: they are the evidence that third-party position
reads are impossible there, and the tests assert we still raise the right typed
error when that changes shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"

# A real mainnet wallet holding open Jupiter perps positions, found by walking
# recent signers of the perps program. Recorded, not fabricated.
JUPITER_WALLET = "2JVs9RekjARxu9tRYq8Dbq2eGNRegzRSGJMrCBXKj8ti"
# A real Pacifica account (a public vault) holding open positions.
PACIFICA_ACCOUNT = "5X8BEVZ8kQSNyRyMBNYWaBUCD3a4azTNn1vnYenML35f"

JUPITER_CUSTODIES = [
    "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz",  # SOL
    "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn",  # ETH
    "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm",  # BTC
    "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa",  # USDC
]
JUPITER_MINTS = [
    "So11111111111111111111111111111111111111112",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
]
POSITION_DISCRIMINATOR_B58 = "VZMoMoKgZQb"


def write(name: str, payload: object) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"wrote {path.relative_to(Path.cwd())} ({path.stat().st_size} bytes)")


def rpc(client: httpx.Client, method: str, params: list) -> dict:
    resp = client.post(
        "https://api.mainnet-beta.solana.com",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    resp.raise_for_status()
    return resp.json()


def capture_jupiter(client: httpx.Client) -> None:
    write(
        "jupiter_program_accounts.json",
        rpc(
            client,
            "getProgramAccounts",
            [
                "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu",
                {
                    "encoding": "base64",
                    "filters": [
                        {"memcmp": {"offset": 0, "bytes": POSITION_DISCRIMINATOR_B58}},
                        {"memcmp": {"offset": 8, "bytes": JUPITER_WALLET}},
                    ],
                },
            ],
        ),
    )
    write(
        "jupiter_custodies.json",
        rpc(client, "getMultipleAccounts", [JUPITER_CUSTODIES, {"encoding": "base64"}]),
    )
    resp = client.get(
        "https://lite-api.jup.ag/price/v3", params={"ids": ",".join(JUPITER_MINTS)}
    )
    write("jupiter_price_v3.json", resp.json())


def capture_pacifica(client: httpx.Client) -> None:
    base = "https://api.pacifica.fi/api/v1"
    write(
        "pacifica_positions.json",
        client.get(f"{base}/positions", params={"account": PACIFICA_ACCOUNT}).json(),
    )
    write("pacifica_info_prices.json", client.get(f"{base}/info/prices").json())
    write("pacifica_info_fees.json", client.get(f"{base}/info/fees").json())
    write(
        "pacifica_book_btc.json",
        client.get(f"{base}/book", params={"symbol": "BTC"}).json(),
    )


def capture_grvt(client: httpx.Client) -> None:
    md = "https://market-data.grvt.io/full/v1"
    write(
        "grvt_all_instruments.json",
        client.post(f"{md}/all_instruments", json={"is_active": True}).json(),
    )
    write(
        "grvt_ticker_btc.json",
        client.post(f"{md}/ticker", json={"instrument": "BTC_USDT_Perp"}).json(),
    )
    write(
        "grvt_book_btc.json",
        client.post(
            f"{md}/book", json={"instrument": "BTC_USDT_Perp", "depth": 100}
        ).json(),
    )
    resp = client.post(
        "https://trades.grvt.io/full/v1/positions",
        json={"sub_account_id": "1", "kind": ["PERPETUAL"]},
    )
    write(
        "grvt_positions_unauthenticated.json",
        {"status_code": resp.status_code, "body": resp.json()},
    )


def capture_ondo(client: httpx.Client) -> None:
    base = "https://api.ondoperps.xyz/v1"
    write("ondo_contracts.json", client.get(f"{base}/perps/contracts").json())
    write(
        "ondo_depth_btc.json",
        client.get(
            f"{base}/perps/depth", params={"market": "BTC-USD.P", "depth": 100}
        ).json(),
    )
    resp = client.get(f"{base}/perps/positions")
    body: object
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    write(
        "ondo_positions_unauthenticated.json",
        {"status_code": resp.status_code, "body": body},
    )


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60.0) as client:
        capture_jupiter(client)
        capture_pacifica(client)
        capture_grvt(client)
        capture_ondo(client)


if __name__ == "__main__":
    main()
