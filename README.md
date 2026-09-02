# hedge-scanner

Read-only perps portfolio scanner. Paste one or more wallet addresses, get every
open perpetuals position those addresses hold, plus live hedge quotes.

This README covers the **ingestion layer** (venue adapters, address routing,
normalization). The hedge engine and CLI are documented separately.

---

## Venue support matrix

The blunt version. "Arbitrary address" means: can a third party read positions
for an address they do not control, with no credential belonging to that address?

| Venue | Arbitrary address? | How | Blocked by |
|---|---|---|---|
| **Jupiter Perps** | **YES** | Solana RPC `getProgramAccounts` on `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`, memcmp on the `Position` discriminator + `owner` at byte 8 | Nothing. Needs an RPC endpoint; the public one works but rate-limits |
| **Pacifica** | **YES** | `GET https://api.pacifica.fi/api/v1/positions?account=<pubkey>` | Nothing. No auth of any kind |
| **GRVT** | **NO — requires user's API key** | `POST https://trades.grvt.io/full/v1/positions` → **HTTP 401**, `{"code":1000,"message":"You need to authenticate prior to using this functionality"}` | Every account endpoint needs a `gravity=` session cookie, issued only in exchange for the account's own API key or an EIP-712 signature from its wallet. No public address-keyed read exists |
| **Ondo Perps** | **NO — requires user's API key** | `GET https://api.ondoperps.xyz/v1/perps/positions` → **HTTP 401**, `{"error":"No Authorization header","error_code":"auth_missing"}` | Worse than GRVT: the endpoint takes **no account or address parameter at all**. It always means "the authenticated account". Auth is a SIWE-derived JWT or an HMAC API key |

Ondo Perps **does exist** and is live in public beta — off-chain matching with
on-chain custody on Ethereum, listing crypto, equity, index and commodity perps.
It is not announced-only and not testnet-only. It is simply a closed account system.

### Quotes are a different story — all four work

Every venue publishes market data without credentials, so the hedge engine can
price a hedge on GRVT and Ondo even though it can never see the user's positions
there. `portfolio.quotes_for` also prices **Avantis**, which is a hedge
destination only and never a position source, so it has no adapter and is
quoted alongside them.

| Venue | Quote source | Live funding? | Live fees? | Depth-walked slippage? |
|---|---|---|---|---|
| Jupiter | Custody accounts on-chain + `lite-api.jup.ag/price/v3` | N/A — Jupiter has **no funding rate**, only a one-sided borrow fee | Yes, `increasePositionBps` / `decreasePositionBps` read on-chain | N/A — fills at oracle price; the price impact fee stands in for slippage |
| Pacifica | `api.pacifica.fi/api/v1/{info/prices, info/fees, book}` | Yes, hourly | Yes, live tier table | Yes |
| GRVT | `market-data.grvt.io/full/v1/{all_instruments, ticker, book}` | Yes, 8h-normalized | No — static Level 1 tier from `help.grvt.io` via `../grvt-fees.md` | Yes |
| Ondo | `api.ondoperps.xyz/v1/perps/{contracts, depth}` | Yes, hourly | Yes, live (and explicitly promotional) | Yes |

---

## HTTP API

The FastAPI app in `hedge_scanner/web.py` is the public surface. Same process
serves the paste-an-address UI at `/` and the JSON API under `/api`.

| Method | Path | Body | What it does |
|---|---|---|---|
| `GET` | `/` | — | Web UI |
| `GET` | `/api/health` | — | Liveness. Does not hit venues |
| `GET` | `/docs` | — | Interactive Swagger UI (generated from the app) |
| `GET` | `/api/prices` | — | Per-venue marks `{venue: {asset: usd}}` (the UI polls this) |
| `GET` | `/api/scan` | `?addresses=0x…` (repeat or comma-separate) | Positions + Avantis hedge quotes |
| `POST` | `/api/scan` | `{"addresses": ["0x…", "SolanaPubkey"]}` | Same payload as GET |

```bash
curl -sS "https://YOUR-DEPLOYMENT.vercel.app/api/scan?addresses=0xYOUR_EVM_ADDRESS"

curl -sS -X POST https://YOUR-DEPLOYMENT.vercel.app/api/scan \
  -H 'Content-Type: application/json' \
  -d '{"addresses":["0xYOUR_EVM_ADDRESS","YOUR_SOLANA_PUBKEY"]}'
```

CORS is open (`*`) so a browser on another origin can call the API. Restrict it
with `HEDGE_SCANNER_CORS_ORIGINS` (comma-separated origins) in the host's env.

A scan fans out to several venues and can take 10–60s. Vercel's incoming-request
proxy still caps at **120 seconds** even though the function is allowed 300s —
callers should set a timeout of at least 60s, ideally 120s.

---

## Deploy on Vercel

Yes — this repo is set up so you can upload the **`hedge-scanner/` folder** (or
point a Git repo at it) and deploy. Vercel runs the FastAPI app as one Python
function. After deploy, anyone can hit `/api/scan` as above.

### What to upload

The Vercel project root **must** be this directory — the one that contains
`app.py`, `requirements.txt`, `pyproject.toml`, and `vercel.json`. If the Git
repo is the parent workspace, set **Root Directory** in Vercel to
`hedge-scanner`.

### Dashboard steps

1. [vercel.com/new](https://vercel.com/new) → **Import** a Git repo, or
   **Upload** this folder.
2. Framework: leave auto-detect (FastAPI / Other). Do not pick Next.js.
3. Root Directory: this folder, if the repo is larger.
4. Environment variables (Production + Preview):

   | Name | Required | Notes |
   |---|---|---|
   | `SOLANA_RPC_URL` | **strongly recommended** | Public Solana RPC rate-limits Jupiter `getProgramAccounts`. Use Helius / Triton / QuickNode. |
   | `HEDGE_SCANNER_CORS_ORIGINS` | no | Default `*`. Set to your site origin(s) if you want to lock CORS. |

5. Deploy. When it finishes, the UI is at `https://<project>.vercel.app/` and
   the API is at `https://<project>.vercel.app/api/scan`. Interactive docs:
   `https://<project>.vercel.app/docs`.

Hobby is enough: Fluid compute allows up to 300s function duration. No Vercel
API keys or Python build command to fill in — `app.py`, `requirements.txt`,
`pyproject.toml`, and `.python-version` (`3.12`) are what Vercel reads.

Local check of the same entrypoint:

```bash
uv run uvicorn app:app --reload --port 8899
```

---

## Running it

```bash
uv sync
uv run pytest
uv run python -m hedge_scanner scan <address>
```

Live smoke test against mainnet:

```bash
uv run python -c "
import asyncio; from hedge_scanner import portfolio
print(asyncio.run(portfolio.scan(['2JVs9RekjARxu9tRYq8Dbq2eGNRegzRSGJMrCBXKj8ti'])))
"
```

### The `hedge-scanner` console script does not work from an editable install

Use `python -m hedge_scanner` for development. The console script only works
from a non-editable install:

```bash
uv sync --no-editable      # console script works
uv sync                    # console script raises ModuleNotFoundError
```

This is a three-way toolchain interaction, not a packaging bug in this project:
uv sets the macOS `UF_HIDDEN` flag on `.venv` and its contents, CPython 3.11.14
added a check in `site.addpackage` that skips hidden `.pth` files, and hatchling
ships editable installs as `_editable_impl_hedge_scanner.pth`. The package
therefore never reaches `sys.path`, and `.venv/bin/hedge-scanner` cannot import
it — a console script's `sys.path[0]` is `.venv/bin`, not the project root.
`chflags nohidden` does not stick; uv re-applies the flag on the next `uv run`.

Nothing else is affected: `pytest` sets `pythonpath = ["."]`, and
`python -m hedge_scanner` resolves the package from the working directory. A
real deployment installs a wheel non-editable and hits none of this.

### Environment variables

Nothing is required — every read path this layer uses is public.

| Variable | Default | Why you'd set it |
|---|---|---|
| `SOLANA_RPC_URL` | `https://api.mainnet-beta.solana.com` | **Strongly recommended.** The public RPC rate-limits `getProgramAccounts` hard. Point this at Helius, Triton, QuickNode or your own node for anything beyond casual use. Set this in Vercel too |
| `PACIFICA_BASE_URL` | `https://api.pacifica.fi/api/v1` | Testnet: `https://test-api.pacifica.fi/api/v1` |
| `GRVT_MARKET_DATA_URL` | `https://market-data.grvt.io/full/v1` | Testnet / staging hosts |
| `ONDO_BASE_URL` | `https://api.ondoperps.xyz/v1` | Sandbox host |
| `HEDGE_SCANNER_CORS_ORIGINS` | `*` | Comma-separated browser origins allowed to call the API. Leave unset for public access |

No API keys are read, stored, or committed. If GRVT or Ondo position reads are
ever added, they will need per-user credentials supplied at request time.

---

## Layout

```
hedge_scanner/
  models.py        Position, Quote, VenueError, PortfolioSnapshot
  assets.py        base-asset alias normalization (WBTC/XBT/BTC-PERP -> BTC)
  portfolio.py     namespace detection + concurrent fan-out
  __main__.py      `python -m hedge_scanner` entry point
  adapters/
    base.py        VenueAdapter protocol, typed errors, shared orderbook walk
    jupiter.py     on-chain reads, full position + quote support
    pacifica.py    public REST, full position + quote support
    grvt.py        quote only; positions raise VenueRequiresAuthError
    ondo.py        quote only; positions raise VenueRequiresAuthError
    hyperliquid.py public REST; the one EVM venue that reads arbitrary addresses
  hedge_venues/
    avantis.py     hedge destination only, never a position source
tests/
  fixtures/        raw recorded responses from live venues (2026-08-19)
  capture_fixtures.py   re-records them
```

### Address routing

`^0x[a-fA-F0-9]{40}$` → EVM → GRVT, Ondo.
Base58, 32–44 chars → Solana → Jupiter, Pacifica.

An address is only ever sent to venues in its own namespace; the scanner never
guesses that an EVM address and a Solana address are the same person. Pass both
in one call to get a unified portfolio.

### Error handling

Adapters run concurrently under `asyncio.gather`. Every failure becomes a
`VenueError` row rather than an exception, so a dead venue costs you one line of
output, not the request. Kinds: `auth_required`, `unavailable`,
`unsupported_namespace`, `error`.

---

## Things a consumer of this layer should know

- **Jupiter lists only SOL, ETH and BTC.** Nine possible positions per wallet
  (3 long, 6 short — one per stable collateral). Nothing else is tradable there.
- **A Jupiter `Position` account can exist while being closed.** The accounts are
  PDAs derived from (owner, custody, collateralCustody), so they are reused and
  left behind zeroed. `sizeUsd == 0` is the venue's own definition of closed and
  the adapter filters on it. Skip that check and every wallet that ever traded
  looks like it holds nine open positions.
- **Jupiter liquidation prices are reported as `None`.** The real formula depends
  on accrued borrow fees and the close fee at liquidation time; a recomputed
  number would drift from what the venue actually uses.
- **Pacifica returns negative liquidation prices** for cross positions the rest
  of the account collateralizes away. Those surface as `None`, with the raw value
  preserved in `Position.raw`.
- **`funding_paid_usd` is negative when the position has paid, positive when
  it has received.** Holder-PnL sign, consistent across venues. On Jupiter it
  is accrued borrow fee (always a cost, so always ≤ 0) since the position's
  last update, derived from the collateral custody's cumulative interest
  counter.
- **Quotes go `available=False` rather than reporting zero slippage** when the
  visible orderbook cannot absorb the requested size. A zero would make a thin
  venue look free and could win a hedge ranking it should lose. At $5M notional
  on BTC this currently rules out GRVT, Pacifica and Ondo, leaving only Jupiter.
- **GRVT's fee schedule is the only hardcoded number in the layer** (Level 1:
  4.5 bps taker, −0.01 bps maker rebate), because GRVT publishes tiers in the
  help center rather than an API. Source and date are in the module docstring.
- **Ondo's published fees are explicitly promotional** ("50% off, limited time")
  and are fetched live rather than pinned, but should be labeled as temporary in
  any user-facing output.
- **Jupiter's price impact quote excludes the additive OI-imbalance component**,
  so it is a floor when the book is skewed.

---

## Fixtures

`tests/fixtures/` holds unedited response bodies recorded from live venues on
2026-08-19, including the GRVT and Ondo 401s. The tests parse those bodies
through the real adapter code paths rather than asserting against invented
shapes, so a venue changing its response breaks the suite instead of silently
producing wrong positions.

Re-record with `uv run python tests/capture_fixtures.py`.
