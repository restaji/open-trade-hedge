# Hedge Scanner — interface contract

Shared contract between the **ingestion layer** (venue adapters) and the **hedge engine**.
Both sides code against this file. Do not change a field name without updating this file first.

Project root: `/Users/ares/Documents/Fees Related (All of Fees Occured)/hedge-scanner/`

---

## 1. Product goal

User pastes a wallet address. We return every **open perp position** that address holds across
supported venues, then surface **hedging opportunities** against those positions.

Venues in scope for position reading: **GRVT, Pacifica, Jupiter Perps, Ondo perps**.
Avantis is explicitly **excluded from position reading** — it is treated as a *hedge destination*
venue only (its fee model is documented separately in `../avantis-fees.md`).

---

## 2. Address handling (important)

There is no single address namespace across these venues:

| Venue | Address namespace |
|---|---|
| GRVT | EVM (ZK validium account, EVM-style key) |
| Ondo perps | EVM (Ondo Chain / Ethereum) |
| Pacifica | Solana (base58 pubkey) |
| Jupiter Perps | Solana (base58 pubkey) |

Rules:
- Detect namespace from the input string: `^0x[a-fA-F0-9]{40}$` → EVM; base58 length 32–44 → Solana.
- Query **only** the venues matching the detected namespace. Never guess a cross-chain identity.
- Accept **multiple addresses** in one request so a user can supply both an EVM and a Solana address
  and get a unified portfolio. The API takes a list.
- Every returned position records which input address produced it.

---

## 3. Canonical `Position` schema

All adapters MUST normalize to this. Use `Decimal` for money, never `float`.

```python
@dataclass
class Position:
    venue: str              # "grvt" | "pacifica" | "jupiter" | "ondo"
    address: str            # the input address this came from
    market: str             # venue-native symbol, e.g. "BTC_USDT_Perp"
    base_asset: str         # normalized base, e.g. "BTC" -- used for cross-venue netting
    quote_asset: str        # e.g. "USDC"
    side: str               # "long" | "short"
    size_base: Decimal      # position size in base units, always POSITIVE
    notional_usd: Decimal   # signed: + for long, - for short. Mark-price based.
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None
    leverage: Decimal | None
    collateral_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    funding_paid_usd: Decimal | None   # cumulative; + received, − paid
    margin_mode: str | None            # "cross" | "isolated"
    opened_at: datetime | None
    raw: dict                          # untouched venue payload, for debugging
```

`base_asset` normalization is what makes netting work: `WBTC`, `BTC`, `XBT`, `BTC-PERP` all → `BTC`.
Maintain the alias map in `hedge_scanner/assets.py`.

## 4. Canonical `Quote` schema (hedge cost)

The hedge engine needs a cost estimate per candidate venue. Ingestion exposes:

```python
@dataclass
class Quote:
    venue: str
    market: str
    side: str               # side of the HEDGE trade
    notional_usd: Decimal
    taker_fee_bps: Decimal      # or open fee for pool-based venues
    close_fee_bps: Decimal
    price_impact_bps: Decimal   # size-dependent; 0 if venue is orderbook w/ deep book
    funding_rate_8h_bps: Decimal  # SIGNED from the perspective of the hedge side:
                                  # positive = hedger RECEIVES, negative = hedger PAYS
    borrow_rate_8h_bps: Decimal   # Jupiter-style one-sided borrow cost, always a cost
    est_slippage_bps: Decimal
    available: bool
    notes: str
```

## 5. Adapter interface

```python
class VenueAdapter(Protocol):
    venue: str
    namespace: str  # "evm" | "solana"

    async def get_positions(self, address: str) -> list[Position]: ...
    async def get_quote(self, base_asset: str, side: str, notional_usd: Decimal) -> Quote: ...
    async def health(self) -> bool: ...
```

Requirements:
- `httpx.AsyncClient` with explicit timeouts; all adapters run concurrently via `asyncio.gather`.
- One adapter failing must NEVER fail the whole request. Return partial results plus a per-venue
  error list. The UI must show "GRVT: unavailable" rather than silently omitting it.
- No API keys committed. Read from env via `.env` / `os.environ`; document required vars in README.
- If a venue requires authentication to read positions for an arbitrary address, that is a
  **hard finding** — record it explicitly rather than working around it. A read-only public
  endpoint is required for the paste-an-address UX to work at all.

## 6. Hedge opportunity definition

Two distinct opportunity types. Implement both, tag each result with `kind`.

**A. `delta_hedge`** — neutralize existing directional exposure.
For each `base_asset` with net non-zero `notional_usd` across the portfolio, find the cheapest
venue to open the opposing position. Rank by **all-in cost to hold for a chosen horizon**:

```
round_trip_fee_bps  = taker_fee_bps + close_fee_bps + price_impact_bps + est_slippage_bps
carry_cost_bps_8h   = borrow_rate_8h_bps - funding_rate_8h_bps
cost_bps(horizon_h) = round_trip_fee_bps + carry_cost_bps_8h * horizon_h / 8
```

**Sign correction (engine agent, 2026-08-19).** An earlier revision of this section wrote the
carry term as `+ (funding_or_borrow_8h * horizon_h / 8)`. Read literally against the §4
convention — *positive `funding_rate_8h_bps` = the hedger RECEIVES* — that **adds** received
funding to cost, inverting the ranking and recommending the worst venue as the best. Funding
received must be **subtracted** from cost; borrow is always **added**. §4's sign convention is
authoritative and unchanged; the formula above is the corrected form and is what
`hedge_scanner/engine.py` implements. Every cost value the engine emits is positive when money
leaves the user.

A hedge with negative net cost (funding received exceeds fees) is a **positive-carry hedge** —
surface these first, they are the actual opportunity.

**Liquidation cost is deliberately absent from `cost_bps`.** It is a *contingent* cost that fires
on one price path and is zero on all the others, so adding it to an expected-cost ranking would
double-count a risk the user has not taken yet and would penalise venues in proportion to a
penalty they may never pay. Liquidation is a **separate axis**, reported alongside the ranking and
never folded into it. See §11. Do not "fix" this formula by adding a liquidation term.

**B. `funding_arb`** — same base asset, opposite funding signs across two venues.
Position is already directional; if the user is long on venue X paying funding, and short on
venue Y also receiving funding, flag the pair. Report net carry in bps/8h and the breakeven
holding period after round-trip fees:

```
breakeven_hours = (total_round_trip_fee_bps / net_carry_bps_per_8h) * 8
```

Always report cost in **bps of notional** AND in **absolute USD**, and always state the assumed
holding horizon. Never present a funding rate as if it were annualized without labeling it.

## 7. Non-negotiables

- Read-only. This tool never signs, submits, or custodies anything.
- Never fabricate a fee or funding number. If a venue's rate is unavailable, mark the Quote
  `available=False` and exclude it from rankings rather than defaulting to zero.
- Funding rates are live values and must be fetched, not hardcoded. Static fee *schedules* may be
  hardcoded from the research files, with a source comment and a date.

## 7.5 Product decisions (confirmed by the user, 2026-08-19)

1. **Avantis-first hedge routing.** Positions are read from other venues; Avantis is the intended
   hedge destination. Ranking logic stays honest — compute true all-in cost for every candidate
   venue — but the output must always name Avantis explicitly as a comparison line and show where
   it wins or loses versus the cheapest alternative. Do NOT rig the ranking to favor Avantis; if
   Avantis is more expensive for a given asset and horizon, say so in plain terms. The value of
   this tool is a credible cost comparison, and a rigged one is worthless.
2. **CLI first, web UI later.** Ship the CLI once the data layer is proven. A FastAPI + web
   address-input surface comes after we know which venues actually support third-party reads.
3. **Default holding horizon: 24 hours.** Still compute and expose the 8h / 24h / 3d / 7d / 30d
   sensitivity table and the venue-crossover points, but headline numbers default to 24h and must
   be labeled as such.

## 7.6 Avantis pricing is NOT a static constant (verified 2026-08-19)

Two findings from the Avantis fee research materially change how the engine must price a hedge.
Do not model Avantis with a fixed fee number.

1. **Maker vs taker on Avantis is decided by OI-skew improvement, not order type.** A trade that
   moves the pair's open interest toward balance pays the **maker** rate (1.0 bps crypto); a trade
   that worsens the imbalance pays **taker** (4.5 bps crypto). This inverts the convention on GRVT
   and Pacifica, where maker/taker is about resting vs crossing.

   Consequence: whether an Avantis hedge gets maker pricing depends on the *direction* of the hedge
   relative to Avantis' current skew — which is a live variable the engine must fetch.

   **CORRECTION (verified against live API 2026-08-19 15:08 UTC):** Two earlier assumptions are
   wrong and must not be implemented:

   (a) **Funding sign is NOT tied to OI skew on Avantis.** Crypto funding is anchored to external
   exchanges (Binance/Hyperliquid), not to Avantis' internal book. Observed: BTC long-heavy with
   longs receiving, ETH short-heavy with shorts receiving — the heavy side collecting in both
   cases. Commission tier (maker vs taker) and funding direction must be checked independently.
   A maker hedge is cheap on commission but NOT automatically positive-carry. Do not assume or
   present funding as a consequence of skew.

   (b) **Against an unchanged book the commission round trip is symmetric at 5.5 bps for crypto,
   regardless of direction.** Avantis re-evaluates maker/taker at close, netting your own size out
   of your side. So a maker open (1.0 bps) closes as taker (4.5 bps), and a taker open (4.5 bps)
   closes as maker (1.0 bps). This is a mechanical consequence, not a rate table: over a round trip
   your net contribution to open interest is zero, so your net contribution to the skew is zero,
   and two effects summing to zero cannot both be improvements. Verified by sweeping
   `classify_skew_fee()` over 200 skew/size combinations — zero both-legs-maker cases, every
   combination totalling exactly 5.500 bps, including the `mixed` blends.

   **This paragraph states the mechanic.** §12.8 briefly overrode it by quoting
   both legs at maker regardless of skew; §12.11 restores this classification as
   what the tool quotes: dominant side = taker, lighter side = maker, both legs
   of a hedge at that tier. Read (b) as the unchanged-book *unwind* identity
   (maker open mechanically closes as taker if you net your own size out), which
   is still true of how Avantis charges a round trip against a frozen book. The
   quote itself does not simulate that unwind — it prices the side of the book
   the hedge sits on, matching
   [Avantis maker-and-taker](https://docs.avantisfi.com/trading/fees/maker-and-taker).
   The mechanic itself is verified and must not be edited away.

   The real variable that drives the Avantis cost differential is **spread**, which is directional,
   volatile (ranged from 1.2 to 5.6 bps for BTC within 20 minutes), and non-monotonic in size.

   The engine MUST therefore fetch live per-pair skew, spread, and funding from Avantis and
   present spread as the dominant dynamic cost, not commission. A hardcoded Avantis fee is wrong.
   Spread must be quoted per side per size, not curve-fitted.

2. **All Avantis RWA markets are 0 bps open and close** (FX, metals, oil, indices, equities) under
   a temporary, explicitly revocable "growth mode" tied to unstated OI milestones. Since Ondo Perps
   is entirely equities/indices/commodities and Pacifica lists RWA perps, this means RWA exposure
   may be hedgeable on Avantis at **zero commission** — leaving only spread, borrow, and funding.
   Treat 0 bps as live-but-temporary: fetch it, never hardcode it, and label it as promotional in
   output so a user isn't shown a durable-looking number that can vanish.

Also note for cost modelling: Avantis charges the **closing fee on `notional + grossPnL`**, not on
fixed notional, so close cost is PnL-dependent. Keeper, gas, and oracle fees are genuinely zero.
Minimum position size is 100 USDC (300 on 20 FX/metals pairs) — the engine must not recommend a
hedge below the pair minimum.

**Upside Perps as a third hedge instrument.** Avantis "Upside Perps" charge no open, close, or
borrow fee, and instead take a **profit share of 25% / 20% / 10% / 5%** by ROI band, with **zero
cost if the position loses**. As a hedge leg this is a genuinely different risk shape: you pay only
when the hedge pays off (i.e. when the underlying position moved against the user). Evaluate it as
a distinct candidate alongside the standard perp, and present the tradeoff honestly — it is cheaper
when the hedge is unnecessary and more expensive when the hedge works.

## 8.5 Verified venue facts that override earlier assumptions (2026-08-19)

Recorded here because several widely-published figures — including some on the venues' own docs
pages — are stale. Anything not confirmed by a live API call in this project should be treated as
suspect.

**Ondo Perps fees are PER-MARKET, not uniform.** `GET https://api.ondoperps.xyz/v1/markets`
(public, no auth) returns `makerFee`/`takerFee` per market, and there are exactly two pairs:
- **1.0 bps maker / 2.5 bps taker** on 40 markets (all crypto, both indices, XAU/XAG/WTI/BRENT,
  QQQ/SPY/DRAM/EWY, and liquid equities such as AAPL/NVDA/TSLA/MSFT/META).
- **1.5 bps maker / 3.5 bps taker** on 12 markets (ARM, AVGO, BABA, CRWV, CXMT, GLW, IBM, LITE,
  TSM, COPPER, NATGAS, SOXL).
Ondo's `/fees` page claim that rates are "identical across all markets" is false. Do not model Ondo
with a single fee constant — read per-market fees from the markets endpoint. At 2.5 bps taker, Ondo
is currently the cheapest taker commission in the venue set.

**Ondo market coverage: 52 markets, not the 28 on its stale `/markets` docs page.** Tag breakdown:
34 Stock, 6 Commodity, 5 ETF, 5 Crypto (BTC, ETH, SOL, HYPE, ONDO), 2 Index. So Ondo crypto
exposure IS hedgeable elsewhere; the 47 non-crypto markets are the hedging problem. Max leverage is
25x on BTC/ETH/XAU/XAG/US100/US500 (the `/leverage` page's 20x cap is out of date).

**Ondo funding: hourly, with a 0.5x dampener on everything EXCEPT crypto.** 43 of 52 markets sit at
`0.0000063`/hr, exactly half the undamped `0.0000125`/hr. Crypto markets pay the full baseline. So
Ondo crypto perps carry roughly double the funding baseline of its equity perps (~10.95% vs ~5.5%
APR). The `fundingIntervalDivisions: 8` field is a premium *smoothing divisor*, NOT intervals per
day — do not read it as 8x-daily funding.

**Ondo volume tiers: confirmed unpublished, not merely unfound.** No tiers endpoint, no tier field
on `AccountInfo`, no static table in the app bundle. The frontend reveals the stack
(`Base Fee Tier → % Off Promotion → % Off Referral Discount → Final Fee`) but all values are
server-supplied per account. **Do not model Ondo tier discounts.** The USDC withdrawal fee is a
flat per-account USD amount (`AccountInfo.withdrawalFeeUSD`) whose value is auth-gated — treat as
unknown, not zero.

**Avantis:** the widely-cited "6/8 bps per side" schedule is obsolete (V2 replaced it; old doc URLs
404). See section 7.6.

**GRVT:** the live tier ladder (effective 2026-03-23) is NOT the one in the help center, which
expired. Maker is negative at every tier including base. Liquidation forfeits 100% of residual
margin rather than charging a percentage penalty — on cross margin, the entire cross account equity.

**Pacifica:** fees are uniform across all 75 markets with no per-market overrides and no maker
rebate at any tier (maker floors at 0.0 bps).

## 9. Schema additions — integration record (authoritative)

`hedge_scanner/models.py` is owned by the ingestion agent, but the engine needed it to exist in
order to import anything, so it was transcribed early. Whoever writes the final version MUST
include the four additions below, or the engine and CLI will fail at import. These are now part
of the contract, not optional extras:

1. **`Quote.base_asset: str = ""`** — the engine nets exposure by normalized base asset and cannot
   reliably re-derive it from a venue-native `market` string (e.g. `BTC_USDT_Perp`, `BTC-PERP`,
   `1000PEPE`). Adapters must populate it.
2. **`VenueError`** — `venue`, `message`, `kind` ∈ {`auth_required`, `unavailable`,
   `unsupported_namespace`, `error`}, optional `address`. Required because some venues (GRVT is the
   likely case) cannot be read for a third-party address at all, and the product must surface
   "requires user API key" rather than silently omitting the venue.
3. **`PortfolioSnapshot`** — `addresses`, `positions`, `errors`. The return type of the fan-out, so
   partial success is representable.
4. **`LiquidationSpec`** — per-venue liquidation parameters, consumed by
   `hedge_scanner/liquidation.py`, which imports it from `models.py` and is itself imported by the
   engine. Same import-breakage warning as the three above. Fields and semantics are specified in
   §11; it lives in `models.py` rather than `liquidation.py` so the model layer stays the single
   place a schema is declared.

All optional `Position` fields must carry defaults (`= None`) so adapters can construct partial
positions from venues that don't expose liquidation price, leverage, or funding paid.

Sections 3 and 4 remain the source of truth for field names and the funding sign convention
(**positive = the hedger receives**). Do not rename a field without updating this file.

## 10. Ingestion findings that change the contract (2026-08-19)

Recorded by the ingestion agent after probing all four venues live. Sections 3, 4 and 9 are
unchanged — no field was renamed and no schema was broken. What follows are semantics the
engine must honour, plus one stack deviation.

### 10.1 Only two of the four venues can be read at all

| Venue | Third-party position read | Evidence |
|---|---|---|
| Jupiter | **yes** | `getProgramAccounts` on `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`, memcmp on discriminator + `owner` at byte 8 |
| Pacifica | **yes** | `GET api.pacifica.fi/api/v1/positions?account=<pubkey>` → 200, no auth |
| GRVT | **no** | `POST trades.grvt.io/full/v1/positions` → 401 `{"code":1000,...}`; session cookie is account-bound |
| Ondo | **no** | `GET api.ondoperps.xyz/v1/perps/positions` → 401; the endpoint accepts **no** account parameter |

**Consequence for the product:** the paste-an-address UX works for Solana addresses only. An EVM
address today produces two `auth_required` rows and zero positions. This is not a bug to route
around — it is the shape of the market. Section 2's address table is still correct about which
namespace each venue uses; it just cannot deliver positions for the EVM half.

Quotes are unaffected: all four venues publish market data without credentials, so the engine can
still price and rank a hedge on GRVT and Ondo.

### 10.2 `Quote.available=False` also means "cannot price this size"

Section 7 says to mark a Quote unavailable rather than defaulting a missing rate to zero. Ingestion
extends that to **depth**: when a venue's visible orderbook cannot absorb `notional_usd`, the
adapter returns `available=False` with the shortfall in `notes`, rather than reporting
`est_slippage_bps = 0`. A zero there would make the thinnest venue look free and win a ranking it
should lose. Observed: at $5M notional on BTC, GRVT, Pacifica and Ondo all go unavailable and only
Jupiter (oracle-priced, no book) can quote.

The engine must therefore treat `available=False` as "excluded at this size", not "venue down",
and should say so when a venue drops out as size increases.

### 10.3 Jupiter's `funding_rate_8h_bps = 0` is a real value, not a missing one

Jupiter Perps has no funding mechanism at all — "positions always pay borrow fees and are never
paid funding". The entire carry sits in `borrow_rate_8h_bps`, which is one-sided and always a cost.
Do not exclude Jupiter from `funding_arb` scanning on the grounds of a zero; do exclude it from any
"receives funding" logic, because it can never receive.

### 10.4 `funding_paid_usd` sign convention

Holder-PnL sign, same as `unrealized_pnl_usd` and `current_funding_rate_8h_bps`:
**positive = the position has received, negative = it has paid** (a burden).
On Jupiter this is accrued borrow fee since the position's last on-chain update,
derived from the collateral custody's cumulative interest counter and negated
(borrow is always a cost, so the field is ≤ 0).

### 10.5 Stack deviation: no `solana` / `solders`

Section 8 suggests `solana`/`solders` for Solana RPC. Ingestion uses `httpx` against the JSON-RPC
endpoint directly plus `base58` for pubkey encoding. Reasons: the only RPC calls needed are
`getProgramAccounts`, `getMultipleAccounts` and `getHealth`; `httpx` is already a dependency and is
natively async, matching the `asyncio.gather` fan-out; and it avoids a compiled dependency. No
transaction is ever built or signed, so the SDK's real value does not apply here. Account decoding
is hand-written Borsh against verified byte offsets and is pinned by tests against recorded
mainnet accounts.

### 10.6 `models.py` ownership resolved

Ingestion reviewed the engine agent's transcription and **adopted it unchanged**. It is a faithful
implementation of sections 3, 4 and 9. There is no competing version.

## 11. Liquidation risk (documented 2026-08-28)

Liquidation was built into the engine, CLI and JSON output before it was written down here. This
section is the retrospective contract for it. The values live in `LIQUIDATION_SPECS` in
`hedge_scanner/liquidation.py`, which is the source of truth for *numbers*; what follows is the
source of truth for *semantics*.

### 11.1 Why it is not part of the cost ranking

§6 ranks by expected cost over a horizon. A liquidation penalty is not an expected cost — it is a
tail outcome. Reporting it inside `cost_bps` would make a venue look expensive for a loss the user
takes only if price moves far enough to force-close the hedge, and it would let a large penalty
outvote a real, certain fee difference. So the engine reports liquidation as its own section
(`DeltaHedgeOpportunity.liquidation_risks`, and `liquidation_specs` in the `--json` payload) and
leaves the ranking untouched.

The two axes also disagree in a way the output must preserve rather than resolve: the cheapest
venue on carry is frequently the most dangerous on liquidation. Avantis wins the BTC ranking at
1.5% liquidation distance while Pacifica loses it at 9%. Collapsing that into one number destroys
the finding.

### 11.2 `LiquidationSpec` semantics

```python
@dataclass(frozen=True)
class LiquidationSpec:
    venue: str
    maintenance_margin_pct: Decimal      # meaning depends on `liquidation_model`
    liquidation_fee_pct: Decimal         # 0 when the venue charges no % penalty
    liquidation_fee_type: str            # see the four shapes below
    partial_liquidation: bool
    cross_margin_risk: str               # "position_only" | "full_account"
    notes: str
    source: str
    as_of: str = ""
    maintenance_margin_source: str = "static"   # "live_api" | "static"
    liquidation_model: str = "standard"         # "standard" | "health_ratio"
```

**`liquidation_fee_type` — four genuinely different penalty shapes.** These are not variations on a
percentage; they differ in *what the percentage is taken from*, which changes the answer by an
order of magnitude:

| Type | Penalty | Venues |
|---|---|---|
| `pct_of_notional` | `fee_pct × notional` | Pacifica (0.75%), Ondo (1.5%) |
| `pct_of_collateral` | `fee_pct × collateral` | Avantis (~15% bounty) |
| `full_margin_forfeit` | **all** remaining collateral | GRVT |
| `residual_forfeit` | `maintenance_margin_pct × notional` | Hyperliquid, Jupiter |

A venue with `liquidation_fee_pct = 0` is not cheap to be liquidated on. GRVT and Hyperliquid both
sit at zero and are the two worst outcomes in the set, because the forfeit types ignore the field
entirely. Never rank or summarise liquidation risk on `liquidation_fee_pct` alone.

**`liquidation_model` — Avantis measures the trigger differently.** Under `standard`, maintenance
margin is a fraction of *notional* and the survivable move is `1/leverage - MM/100`. Under
`health_ratio` (Avantis only), `maintenance_margin_pct` is the fraction of *initial collateral*
that may be lost before the trigger fires, so the survivable move is `(MM/100) / leverage`. Reading
Avantis' 15 as a standard maintenance margin gives a wildly wrong liquidation price. The field name
is shared; the meaning is not.

**`cross_margin_risk` is a product-visible warning, not metadata.** `full_account` means a
liquidation can consume equity belonging to unrelated positions. GRVT and Hyperliquid are both
`full_account`, and the renderer is required to warn on them — a $1,000 hedge on a $50,000 GRVT
cross-margin account can cost the whole $50,000. Silently omitting this would be the most
expensive omission in the tool.

### 11.3 What a liquidation number here does NOT include

Stated so nobody reads more precision into the output than it has:

- **The penalty is not the total loss.** It excludes the position's own PnL at liquidation, the
  normal close fee, and accrued funding and borrow. Jupiter's spec notes this explicitly (0.20%
  cap *plus* the 6 bps close fee *plus* accrued borrow).
- **Prices assume a fresh position with no accrued PnL** at the leverage the engine assumed.
- **A delta hedge does not neutralise liquidation risk.** The two legs sit in separate margin
  accounts and can be liquidated independently. This is already in the CLI's basis-of-preparation
  footer and must stay there.

### 11.4 Known gaps (open, not resolved)

Recorded rather than quietly carried, per §7:

1. **Every `maintenance_margin_pct` is currently `static`.** No adapter yet supplies `live_api`,
   despite the field existing to support it. Tiered venues are pinned at tier 1 (Hyperliquid 3.33%
   for BTC/ETH ≤$4M notional, GRVT 1.0% at 50x), so a large hedge is modelled at a maintenance
   margin it would not actually receive.
2. **The Avantis ~15% liquidation bounty is UNVERIFIED.** It is flagged that way in the `notes`
   string, but `LiquidationSpec` has no `verified: bool` and the renderer therefore does not mark
   it, unlike the `*` convention that `VenueFeeSchedule.verified` drives for fees. An unverified
   liquidation penalty currently prints looking exactly as authoritative as a documented one. §7's
   "never fabricate a fee" applies here and this does not yet satisfy it.
3. **Hedge leverage is fixed at 10x** (`DEFAULT_HEDGE_LEVERAGE` in `engine.py`). It is not a
   `ScanConfig` field and has no CLI flag, so every liquidation price in the report is a 10x figure
   the user cannot change. The footer refers to "the indicated leverage" while the rendered table
   has no leverage column, so it is in fact not indicated.

Fixing (2) or (3) changes rendered output and the `--json` shape, so update this section with it.

## 12. Integration gaps found by pre-deploy review (2026-08-28)

Recorded per §7: found by reviewing what a live run actually emits, rather than
what the modules are individually capable of. Two were fixed; two are open.

### 12.1 FIXED — Avantis was built but never wired into the live quote path

`hedge_scanner/hedge_venues/avantis.py` was complete and tested, and imported by
nothing except its own test file. `portfolio.quotes_for` fanned out to the five
position *adapters* only, so no live scan ever produced a quote with
`venue == "avantis"`. The engine handled the absence gracefully, which is why it
went unnoticed: every live report printed

    Avantis: not ranked for this asset — no quote returned for this asset and side.

while the basis-of-preparation footer went on explaining Avantis' closing-fee and
skew mechanics as though it had been priced. §7.5.1 — the reason this tool exists
— was therefore unmet in every live run, and only met when a fixture happened to
supply an Avantis quote by hand.

`portfolio.quotes_for` now prices Avantis alongside the adapters. Consequences:

1. **`quotes_for` gained a fourth parameter, `horizon_hours`** (default 24h per
   §7.5.3). Avantis is the only venue that computes its own all-in figure, and it
   needs a horizon to do so. It is keyword-optional, so a three-argument call
   still works. The horizon does **not** affect the canonical `Quote` fields and
   therefore cannot affect the ranking — the engine still recomputes cost per
   horizon itself.
2. **An asset Avantis does not list yields `available=False` with a reason**, not
   an omission, so the Avantis line stays present and explains itself.
3. **A failing Avantis is one `VenueError` row**, same contract as any adapter.

`tests/test_portfolio.py` now covers `quotes_for`, which previously had no test at
all — the reason a missing venue could pass 345 green tests.

### 12.2 FIXED — the `hedge-scanner` console script cannot import the package

§8 documents `uv run hedge-scanner scan <address>`. That raises
`ModuleNotFoundError` on an editable install, because uv flags `.venv` contents
with the macOS `UF_HIDDEN` bit, CPython ≥3.11.14 skips hidden `.pth` files in
`site.addpackage`, and hatchling's editable install *is* a `.pth`. `chflags
nohidden` does not stick — uv re-applies it. Use `python -m hedge_scanner`
(added as `__main__.py`) for development, or `uv sync --no-editable` for a
working console script. A deployed wheel is unaffected. Details in the README.

### 12.3 FIXED — the maker-hedge product decision replaces skew-based selection (2026-08-30)

**Historical statement (preserved for context).** `classify_skew_fee()` is
implemented and unit-tested, and `AVANTIS_PRICING.md` §2 previously mapped
`taker_fee_bps` to "`openMakerFeeP` **or** `.openTakerFeeP`, selected by live
skew". `quote_hedge` did not do this: it read `openTakerFeeP`/`closeTakerFeeP`
unconditionally and hardcoded `fee_tier="taker"`, modelling Avantis commission
at a **9.0 bps round trip** rather than the **5.5 bps** that §7.6(b) states is
symmetric and unavoidable — overstating Avantis by 3.5 bps. §7.6(b) remains the
authoritative statement of the round-trip commission and is unchanged.

**Resolution (user-confirmed 2026-08-30).** Every Avantis hedge opened by this
tool is now modelled as a **maker** open. The close in the same direction is
therefore a **taker** close, because Avantis re-evaluates maker/taker at close
and nets the trader's own size out of the trader's side — so a skew-improving
open unwinds as a skew-worsening close.

Consequences:

1. **The commission round trip is 5.5 bps on crypto regardless of direction**
   (1.0 bps `openMakerFeeP` + 4.5 bps `closeTakerFeeP`). This is the split that
   §7.6(b) already declared symmetric; the maker-hedge decision only pins which
   bucket each bps lands in, it does not change the sum.

   > **SUPERSEDED 2026-08-30 by §12.8.** The close leg now reads
   > `closeMakerFeeP` rather than `closeTakerFeeP`, making the quoted round trip
   > 2.0 bps on crypto. §7.6(b)'s mechanic is unchanged and still describes what
   > Avantis charges against an unchanged book; §12.8 records the decision to
   > quote the favourable end of that range and the disclosure that ships with
   > it. The rest of this subsection is preserved as the history of how the
   > 9.0 bps overstatement was found and fixed.
2. **`quote_hedge` now returns `fee_tier="maker"`** on standard crypto pairs,
   referring to the open side (the side the decision pins). The `"taker"` /
   `"mixed"` values from the SDK-faithful classifier are no longer emitted by
   `quote_hedge`.
3. **`classify_skew_fee()` is retained, not deleted.** Its unit tests still
   pin the SDK-faithful mechanic (which is the ground truth for how Avantis
   charges), and any future caller — e.g. a research script or a
   reintroduction of skew-based routing — can consume it without a re-port.
   A code comment in `quote_hedge` records that the live-skew path is
   intentionally bypassed.
4. **RWA growth-mode pairs are unaffected.** All four `additionalPairParams2`
   commission fields sit at 0 on those pairs (§7.6.2), so the promotional
   check is equivalent to checking all four, correctly preserves the
   promotional flag, and continues to price those hedges at 0 bps commission.
   (§12.8 changed which two of the four the check reads; the equivalence
   argument is unaffected.)
5. **Refusal semantics unchanged.** A missing commission field returns
   `available=False` with a reason — never a fallback to another tier, never a
   silent zero (§7 non-negotiables). §12.8 changed which fields are required.
6. **The suite's earlier disagreement is resolved.** Fixture tests assert
   `fee_tier == "maker"`, and the live-smoke test covers both hedge
   directions. The old `{"maker", "taker"}`-disjoint-legs assertion belonged to
   the live-skew path and has been removed. (§12.8 updated the asserted bps
   split from 1.0/4.5 to 1.0/1.0.)

Not changed by this: spread, borrow, funding, and the close-fee base
(`notional + grossPnL`) still dominate the Avantis cost differential, and §7.6
remains the authoritative statement on the ranking-visible dynamics.

**Post-fix follow-up (2026-08-30).** `hedge-scanner fees` was also switched
from a hardcoded `open_fee_bps=4.5 / close_fee_bps=4.5 / round_trip=9.0` row
to a live fetch of `additionalPairParams2` off the same
`prod-api.avantisfi.com/data/v2/trading` snapshot the ranker reads, so the
display and the ranker share one source of truth (§7 non-negotiable). The
Avantis row in `FEE_SCHEDULE` is retained as a live-marker stub (new
`live=True` field) carrying only the minimum-position enforcement §12.4 pins
here; a fetch failure prints "Avantis fee schedule: unavailable" with the
underlying error and never falls back to hardcoded numbers.

### 12.4 FIXED — Upside Perps now rank as a distinct venue row (2026-08-30)

**Historical statement (preserved for context).** `quote_upside_hedge` returned
`venue="avantis"`, but `engine.upside_hedge_comparison` looked for
`venue == "avantis_upside"`. Wiring Upside into `quotes_for` as-is would have
put two different instruments under one venue name and corrupted the ranking,
so it was deliberately **not** wired. The engine's documented fallback —
deriving the Upside leg from the standard Avantis quote — is what ran, and
Upside never appeared as its own labelled row in the ranking table.

**Resolution.** The Upside leg now carries a distinct venue string and is
quoted alongside the standard perp for every asset, so a hedger sees two
Avantis rows for the same base asset and can compare "standard Avantis perp"
against "Avantis (Upside)" directly.

Consequences:

1. **Venue string is `avantis_upside`.** `quote_upside_hedge` returns this on
   both the available and unavailable paths (a below-minimum or unlisted
   Upside call must not masquerade as a second `venue="avantis"` row).
   `hedge_venues/avantis.py` now exports `UPSIDE_VENUE` alongside `VENUE`;
   `_unavailable()` gained an optional `venue=` parameter so Upside refusals
   carry the right label.
2. **`portfolio.quotes_for` fans out to both.** A new `_avantis_upside_quote`
   runs in parallel with `_avantis_quote` and appends a distinct `Quote` per
   call. An asset that has no Upside pair returns `available=False` with a
   reason — the Upside line stays present and explains itself, same
   non-negotiable as §7.5.1 for the standard Avantis line. A failing Upside
   call is one `VenueError` row per §7 and §12.1.
3. **Human-facing renderers display it as "Avantis (Upside)".** `render.py`
   grew a `venue_display_name()` helper backed by a small overrides map;
   `cli.py` (via `render.py`) and `web.py` route venue-to-label lookups
   through it. The raw `avantis_upside` venue string is preserved in the
   `--json` payload (and in the `/api/scan` JSON returned by the web UI),
   because the schema is what other tooling consumes.
    4. **The "derive Upside from the standard Avantis quote" logic is retained as
       a safety net.** `engine.upside_hedge_comparison` now finds the direct
       `avantis_upside` quote first (that is what §12.4's original fix
       contemplated) and uses it. The derivation only runs when Upside is
       unavailable for that asset/side but standard Avantis is quotable, which
       preserves a useful comparison line rather than dropping the whole section.
       `derived_from_venue` is set to `None` when the direct quote wins and to
       `"avantis"` when the derivation runs, so the output tells the truth about
       where the number came from. The **standard-hedge reference** on the
       comparison side is selected as the cheapest ranked venue *excluding*
       `avantis_upside` itself — Upside typically leads the ranking on
       unconditional cost (its 25/20/10/5% profit share is deliberately not in
       `total_bps` per §7), and comparing Upside against itself would collapse
       the section to a no-op ("cheaper if <never cheaper").
5. **The ranking is not rigged toward Upside.** Upside ranks on its
   unconditional cost only (spread + funding + any live commission); the
   25 / 20 / 10 / 5 % profit share is contingent and cannot be reduced to bps
   of notional without an assumed price move (§7). The dedicated
   `AvantisComparison` / Upside-comparison section is where the tradeoff is
   quantified, and both the CLI ranking table and the basis-of-preparation
   footer now spell out that Upside's all-in bps excludes the profit share.
6. **Minimum-size and pair-listing gates still apply.** The standard
   Avantis-schedule minimum (100 USDC crypto, 300 USDC on FX/metals) is
   enforced in the engine via `FEE_SCHEDULE["avantis"]`. Upside carries its
   own per-pair minimum from the live pair record and the check inside
   `quote_upside_hedge._tradability_reason` refuses below it — a below-minimum
   call returns `available=False` with the reason, not a fabricated number.
7. **Tests pin the new behaviour.** `tests/test_avantis_quote.py` asserts
   `quote.venue == "avantis_upside"` on both the available and the
   below-minimum paths. `tests/test_portfolio.py` adds three cases:
   `quotes_for` returns both `avantis` and `avantis_upside` for a listable
   asset; an Upside-unlisted asset yields an unavailable row rather than an
   omission; and a failing Upside call becomes one `VenueError` without
   costing the standard Avantis row. `tests/test_engine.py` adds a ranking
   test that both venues appear as distinct rows.

Not changed by this: the maker-hedge product decision (§12.3) still pins the
standard Avantis perp's open side to maker. Upside is unaffected — its
commission fields come from `openTakerFeeP`/`closeTakerFeeP` on the live pair
record, and the pair record's own values (typically zero for the crypto-major
Upside pairs, per the growth-mode default) are surfaced without translation.

### 12.5 §10.1 is now stale in the product's favour — Hyperliquid reads EVM addresses

§10.1 concludes that "the paste-an-address UX works for Solana addresses only" and
that an EVM address yields two `auth_required` rows and zero positions. A
Hyperliquid adapter has since been added: `POST api.hyperliquid.xyz/info` with
`{"type": "clearinghouseState", "user": <address>}` returns full position data for
any address, no credential. EVM input therefore does produce positions.

Hyperliquid is not in the §1 venue scope and appears in §11.2's liquidation table
without ever being introduced. §1 and §10.1 should be updated to admit it; the
venue set is now five readable-or-quotable venues plus Avantis as destination.

### 12.6 FIXED — Hyperliquid HIP-3 sub-DEX positions were invisible (2026-08-29)

`HyperliquidAdapter.get_positions` posted a single `clearinghouseState` request
without a `dex` field, which returns positions on Hyperliquid's native perp DEX
only. Since HIP-3 shipped, Hyperliquid also hosts **builder-deployed sub-DEXs**
alongside the native one — verified today: `xyz` (XYZ), `flx` (Felix Exchange),
`vntl` (Ventuals), `hyna` (HyENA), `km`/`mkts` (Markets by Kinetiq), `cash`
(dreamcash), `para` (Paragon), `io` (EntropyIO), `abcd` (ABCDEx). Their markets
are namespaced `<dex>:<coin>` (`xyz:BRENTOIL`, `flx:SILVER`, `hyna:BTC`).

Concrete false negative: `0x46921f6961bdb411b756c9712f6bdb58fbd9164f` holds 19
open positions worth ~$14M notional on `xyz` alone (equities, RWAs, oil, metals).
Prior to this fix the tool reported that address as flat with no positions and
no `VenueError` — the worst kind of read failure, because it looked authoritative.

Wire-level fix: discover sub-DEXs via `{"type":"perpDexs"}`, then fan out one
`clearinghouseState` call per sub-DEX (in parallel with the native one) and
merge `assetPositions`. A sub-DEX call failing is silently dropped; only a
native-DEX failure raises `VenueUnavailableError` so the portfolio layer can
record it. Discovery is cached for 10 minutes.

Two contract-visible consequences:

1. `Position.market` on an HIP-3 position is the full `<dex>:<coin>` string
   (e.g. `xyz:BRENTOIL`). This preserves DEX disambiguation and is what the
   CLI's MARKET column shows.
2. `Position.base_asset` normalises the `<dex>:` prefix away so cross-venue
   netting still works — `xyz:BRENTOIL` nets against Ostium's BRENT, `xyz:GOLD`
   against XAU, `xyz:SILVER` against XAG. `assets.py::_strip_hip3_prefix`
   handles this; a `BRENTOIL → BRENT` alias was added.

Also fixed in the same pass: `_to_position` was using `positionValue` (a
notional) as a *price* fallback when `allMids` missed a symbol, which silently
corrupted `mark_price` and `notional_usd` on any newly listed or HIP-3 coin.
Mark price is now derived from `positionValue / szi` first (always coherent
with the position payload), with `allMids` and `entryPx` as fallbacks.

### 12.7 FIXED — every venue request 403'd when the server ran inside a Cursor terminal (2026-08-29)

`httpx.AsyncClient` defaults to `trust_env=True`, which means it picks up
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` from the process environment. Cursor's
sandboxed terminals unconditionally export
`HTTP_PROXY=http://127.0.0.1:61076` (an internal Cursor proxy that enforces the
IDE's network allowlist). None of the perp venues we hit are on that allowlist,
so every outbound request — Hyperliquid `/info`, Ostium subgraph, Avantis
`/data/v2/trading`, Pacifica, Jupiter RPC — got rejected upstream with
`HTTP 403 Forbidden`, surfaced through httpx as `httpx.ProxyError: 403 Forbidden`
and translated by adapters into `VenueUnavailableError("... 403 Forbidden")`.

The failure mode is uniquely bad because:

1. It happens deterministically for anyone who launches the server from a
   Cursor terminal, but works fine from the same shell without the sandbox, or
   from Terminal.app / iTerm — so it looked like intermittent "sometimes it
   won't run" flakiness that depended on which terminal the server started in.
2. Every venue fails simultaneously (they all go through the same proxy), so
   the response comes back in ~50ms with 4 errors and 0 positions. That is
   indistinguishable at the response shape level from "the address is flat and
   two venues are auth-gated", which is a completely normal answer.
3. `/api/prices` background polls also 500, which spams the server log with
   ProxyError tracebacks but doesn't change what the UI shows.

Wire-level fix: `hedge_scanner.adapters.base.make_http_client(**kwargs)` sets
`trust_env=False` by default and is the only way any adapter or hedge-venue
module is allowed to construct an `httpx.AsyncClient`. Ambient proxy env is
now completely ignored. If a future deployment genuinely needs a proxy, it must
be threaded through explicitly (`proxies=...`).

Concrete false negative: `0x46921f...` and `0xC26Bb...` both returned zero
positions with proxy env set; after the fix, 0x46921 correctly returns its 19
HIP-3 equities/RWAs and 0xC26 returns its Ostium HYPE + XAU book, whether the
server is launched from a Cursor terminal or a plain shell.

Two contract-visible consequences:

1. Venue clients no longer respect `HTTP_PROXY` / `HTTPS_PROXY` from the shell.
   This is a deliberate reversal of httpx's default — documented here rather
   than left as a per-file surprise.
2. `HEDGE_SCANNER_RELOAD=0 uv run python -m hedge_scanner.web` is now the
   recommended incantation for long-running scans, because uvicorn's `--reload`
   kills in-flight requests on every source edit and the browser surfaces that
   dropped socket as `TypeError: Failed to fetch` — the same string as a real
   outage, so it also read as spurious flakiness.

### 12.8 CHANGED — both Avantis legs now price at the live maker rate (2026-08-30)

> **SUPERSEDED 2026-09-02 by §12.11.** Always-maker pricing treated a dominant-side
> hedge as maker. The ranker now classifies from live OI skew: lighter side =
> maker both legs, heavier side = taker both legs. The rest of this subsection
> is preserved as the history of the 2.0 bps maker-round-trip experiment.

**Decision (user-directed, 2026-08-30).** `quote_hedge` prices *both* legs of an
Avantis hedge at the pair's maker commission, read live from
`additionalPairParams2`: `openMakerFeeP` for the open and `closeMakerFeeP` for
the close. At current crypto rates that is a **2.0 bps round trip** (1.0 + 1.0),
replacing the 5.5 bps (1.0 `openMakerFeeP` + 4.5 `closeTakerFeeP`) that §12.3
established. The user's requirement was explicit on both points: a ~2 bps round
trip, and **no hardcoded number** — both rates come off the live pair record per
invocation, exactly as before.

**This overrides a verified mechanic, and that is recorded rather than hidden.**
§7.6(b) is not wrong and was not edited away. Against an unchanged book a round
trip always pays one maker leg and one taker leg, because closing nets your own
size back out of your own side and undoes the skew improvement the open was paid
for. Sweeping `classify_skew_fee()` over 200 skew/size combinations produced zero
both-legs-maker cases and a round trip of exactly 5.500 bps in every one,
including the `mixed` blends:

```
open=maker  close=taker  -> 5.500 bps  (67 cases)
open=mixed  close=mixed  -> 5.500 bps  (23 cases)
open=taker  close=maker  -> 5.500 bps  (110 cases)
```

A 2.0 bps round trip is nevertheless **reachable**: if other traders flip the
pair's skew while the hedge is held, the close is no longer undoing the open
against the same book and genuinely earns maker. Worked example — a short hedge
opened maker on a 600/400 long-heavy book, closed after the pair went 400/650
short-heavy, classifies `maker` on both legs for a true 2.0 bps round trip. What
makes this a modelling *choice* rather than a fact is that the close-time book is
unknowable at quote time, so quoting 2.0 bps assumes a favourable drift.

Consequences and the guardrails that come with them:

1. **Required fields changed.** `quote_hedge` now requires `openMakerFeeP` and
   `closeMakerFeeP`. A pair missing either returns `available=False` with a
   reason — never a fall back to the taker rate, never a silent zero. §7
   non-negotiables and §12.3 point 5 are otherwise unchanged.
2. **The quote discloses the assumption.** Every non-promotional Avantis quote
   carries a note naming both fields, labelling the round trip an ASSUMPTION,
   and stating what the same hedge would cost with a taker close (read from
   `closeTakerFeeP` purely for that disclosure — never for pricing). The number
   must never ship as if it were guaranteed.
3. **This makes Avantis cheaper by 3.5 bps on crypto, so §7.5 point 1 applies
   with full force.** The ranking is still computed honestly for every venue and
   Avantis must still be named when it loses. Reviewers should know the direction
   of this change: §12.3 fixed a 3.5 bps *overstatement*, and this moves 3.5 bps
   the other way. Anyone auditing an Avantis win at a small margin should check
   whether a taker close would flip it.
4. **Promotional detection still correct.** The check now reads
   `open_maker == 0 and close_maker == 0`. All four commission fields sit at 0
   together on RWA growth-mode pairs (§7.6.2), so the equivalence argument in
   §12.3 point 4 is unaffected and RWA hedges still price at 0 bps commission.
5. **`fee_tier="maker"` now describes the whole round trip**, not just the open
   side as under §12.3 point 2.
6. **`hedge-scanner fees` follows the ranker.** The OPEN/CLOSE/RT columns show
   `openMakerFeeP` / `closeMakerFeeP` / their sum. The detail block additionally
   prints the taker-close round trip so the alternative stays visible, and the
   JSON gained `maker_round_trip_bps` and `taker_close_round_trip_bps`, replacing
   `maker_open_round_trip_bps`.
7. **Upside Perps unaffected.** They read `openTakerFeeP`/`closeTakerFeeP` off
   the pair record (§12.4) and are untouched by this.

Unchanged: spread, borrow, funding and the close-fee base (`notional + grossPnL`)
still dominate the Avantis cost differential, so commission remains the smaller
half of the story either way.

### 12.9 ADDED — Avantis funding gate: exclude when hedging would not improve funding (2026-08-30)

**Decision (user-directed, 2026-08-30).** When the user's live funding rate is
known for an asset, Avantis is included in that asset's ranking only when the
funding rate it offers the hedger **strictly exceeds** the rate the user is
currently paying on their existing position(s) in that asset. When Avantis'
offered funding is equal to or lower than the user's current position funding,
the Avantis row is excluded with a reason and the ranking continues without
it. This gate applies to both ``avantis`` (standard perp) and
``avantis_upside`` (Upside Perps), because both are Avantis instruments and
the product decision named the venue, not the instrument.

**Rationale.** Hedging on Avantis is only "doable" — from the funding side of
the ledger — when it strictly improves the user's funding position. If the
Avantis hedge would leave the user's net funding negative or unchanged, the
route is not surfaced: the hedge is technically executable but does not
achieve the funding goal that motivates using Avantis in the first place.

**This is a deliberate asymmetry with the rest of the ranking.** §7.5.1's
rule is "never rig the ranking to favor Avantis", and this gate does not: it
tightens Avantis' inclusion criterion, so if anything it makes Avantis look
worse than the pure all-in cost ranking would. Every other hedge venue
(Hyperliquid, Pacifica, GRVT, Ondo, Jupiter, Ostium) continues to rank on
all-in cost as before, unaffected by this filter. The scope was chosen
explicitly by the user: this is a business rule about Avantis routing quality,
not a generic funding filter.

Consequences and the guardrails that come with them:

1. **New Position field.** ``Position.current_funding_rate_8h_bps`` (Decimal
   or None) carries the CURRENT live funding rate the position is accruing,
   signed from the **position holder's perspective**: positive = the holder
   is currently receiving funding, negative = paying. Distinct from
   ``funding_paid_usd``, which is cumulative history in the same sign
   convention (positive = received, negative = paid). ``None`` means the adapter did not supply a
   live rate; §7 non-negotiable applies — a missing rate is never treated as
   zero.
2. **Aggregation is notional-weighted.** ``NetExposure.weighted_current_funding_8h_bps``
   averages the per-position rates weighted by absolute notional across every
   contributing position for the asset. Positions with ``None`` rates are
   skipped rather than counted as zero. When every contributing position has
   ``None``, the exposure's weighted rate is ``None`` and the gate is not
   applied for that asset — the Avantis rows rank normally.
3. **The gate is strict.** The check is
   ``quote.funding_rate_8h_bps > -weighted_current_funding_8h_bps``. Equal
   rates fail the check, matching the user's "should be higher" phrasing.
4. **Reason strings state both numbers.** The ``ExcludedQuote.reason`` emitted
   when the gate fires names the user's current funding side, the Avantis
   offered rate, and the net funding the hedge would leave. The CLI's
   EXCLUDED table renders this unchanged (§12.4 point 4 pattern).
5. **Adapter coverage as of this section.** Populated:
   ``hyperliquid`` (per-coin ``fundingHistory`` fan-out in
   ``get_positions``, sign flipped against position side) and
   ``pacifica`` (piggybacks on the price snapshot's ``funding`` field, same
   sign flip). Not yet populated: ``ostium`` (rollover semantics differ from
   funding, deliberately left ``None``), ``jupiter`` (no funding mechanism
   per §10.3, ``None`` is honest), and the auth-gated venues that don't
   read positions anyway (``grvt``, ``ondo``). Broadening coverage
   subsequently only increases how often the gate fires — no schema change
   required.
6. **Sign convention verified end-to-end.** Hyperliquid and Pacifica both
   publish hourly funding with "positive = longs pay shorts", so the
   position holder's rate flips against the position's side (long holder
   pays when the venue rate is positive, short holder receives). Avantis'
   ``Quote.funding_rate_8h_bps`` follows §4 (positive = HEDGER receives), so
   the check compares one side's holder rate against the other side's
   hedger rate, both under the "positive = money in" convention.
7. **JSON output.** ``positions[].current_funding_rate_8h_bps`` and
   ``net_exposures[].weighted_current_funding_8h_bps`` are exposed as
   strings under the same Decimal-encoding rule as every other rate.
   ``assumptions.avantis_funding_gate`` documents the gate in the
   ``--json`` payload so downstream tooling knows why an Avantis row is
   sometimes absent.

Not changed by this: §7's "never fabricate a fee" (a missing rate is still
``None``, never zero), §7.5.1's ranking-honesty rule (other venues rank the
same), and the Avantis quote's live commission (the gate uses the same live
rate the ranker already reads; which tier that commission lands on is §12.11).

### 12.9 CHANGED — Avantis `marginFee` is in carry again (2026-09-02)

**Current (2026-09-02, user-directed).** Do not drop `marginFee`. The
avantisfi.com header **Net Rate (L/S) 24h** is `(fundingRate + marginFee) × 24`
from `https://data.avantisfi.com/v2/trading` (identical JSON to
`prod-api.avantisfi.com/data/v2/trading`). `_INCLUDE_MARGIN_FEE_IN_CARRY = True`.
`AvantisQuote.borrow_rate_8h_bps` is live `marginFee.<side>` converted to
bps/8h. Web Avantis 24h / Avantis APR / Net APR use holder-signed
`fundingRate − marginFee`. Jupiter borrow stays out of Net APR (§12.12).

**Was (2026-08-31).** The tool zeroed `marginFee` at Quote construction because
the API still published non-zero rates after an on-chain funding-only shift.
That override is reversed. Historical numbers and rationale below.

Concretely, `hedge_venues/avantis.py` sets `borrow_rate_8h_bps = 0` on every
`AvantisQuote` (standard and Upside), so the engine's `carry = borrow − funding`
reduces to `carry = −funding`. `marginFee` is still read from the live pair
record and surfaced in the quote's `notes` text so the value is not
memory-holed, but it is not part of `total_bps`, `total_usd`, `positive_carry`,
or the ranking key.

Avantis's public documentation still describes the old model (which is why
this section is not a routine bug fix — the docs and JSON both currently say
borrow is applied on standard perps):

> **Net rate combines borrow fees and funding.** Borrow fee compensates LPs
> for their cost of capital, while funding is exchanged directly with other
> traders, set by OI imbalance and prevailing market funding.
> — `docs.avantisfi.com/trading/fees/net-rate-funding-+-borrow`

Live JSON at decision time (verified 2026-08-31):

| Pair | `marginFee.short` (%/h) | Annualised | `fundingRate.short` (%/h) |
|---|---|---|---|
| SOL (standard) | `0.00057078` | 5.00 % / yr | `−0.00020` |
| BTC (standard) | `0.00022824` | 2.00 % / yr | `−0.00118` |
| SOL_UPSIDE | `0` | 0 % / yr | `−0.00020` |
| BTC_UPSIDE | `0` | 0 % / yr | `−0.00107` |

Notice Upside already reports zero — matching the docs and matching on-chain
behaviour. Standard perps report non-zero but (per the user's on-chain
observations) no longer charge it. If a later API push zeroes these fields
for standard perps too, the tool's behaviour will remain unchanged and this
section becomes documentation of the transition period rather than an active
override.

**Empirical scale of the correction.** For a SOL short right now the tool
reports carry as `−0.32 bps/8h` (received) instead of the API-implied
`+0.14 bps/8h` (paid). Over 24h on a $34k notional that's a `~$4.30/day`
shift. Annualised the correction equals whatever the stale `marginFee` currently
is: **≈5 % APR on SOL, ≈2 % APR on BTC** at decision time. This shift is
consistent with the user's observation that hedges do NOT actually accrue this
cost on their positions — otherwise a systematic multi-% APR under-report
would show up immediately in reconciliation.

**Wire-level fix.** `hedge_venues/avantis.py` gains a module-level flag:

```python
# Include marginFee so Avantis 24h matches UI Net Rate (L/S) 24h.
# Was False 2026-08-31 → 2026-09-02 (funding-only override). Now True.
_INCLUDE_MARGIN_FEE_IN_CARRY = True
```

Both `quote_hedge` and `quote_hedge_upside` compute `borrow_8h` from
`marginFee.<side>` as before (for the notes text) and then derive
`borrow_8h_effective = borrow_8h if _INCLUDE_MARGIN_FEE_IN_CARRY else Decimal(0)`,
passing `borrow_8h_effective` to the `AvantisQuote`. The engine, ranker, web UI,
and JSON exports need no changes because they read `Quote.borrow_rate_8h_bps`
generically — which is now the "effective" value, not Avantis's raw.

**Contract-visible consequences.**

1. `AvantisQuote.borrow_rate_8h_bps == 0` for every quote while the flag is
   False. Any downstream consumer that assumed this field carried Avantis's
   raw `marginFee.<side>` will read zero. The unabridged rate lives in
   `AvantisQuote.borrow_rate_annual_pct` (already populated) and in the notes
   string, both unchanged.
2. `positive_carry` on standard-perp quotes will flip True far more often
   because carry is now just `−funding_received`. Any positive `fundingRate.short`
   on the hedge side (i.e. shorts receive) is now enough to mark the hedge as
   positive-carry, regardless of whether the borrow rent exceeds it.
3. The UI's "Borrow 8h" row will read `0.0 bps` for standard perps. The
   accompanying notes paragraph explicitly names the excluded rate so the
   user can size the gap without reading the source. When the flag is True
   the row prints Avantis's real value and the notes paragraph swaps to the
   "borrow included" form.

**When to reconsider.**

- If Avantis's API zeroes `marginFee` organically (matching Upside), quotes
  already follow the JSON — no flag change.
- If a real Avantis position ever *does not* accrue `marginFee` on-chain while
  the API still publishes it, flip `_INCLUDE_MARGIN_FEE_IN_CARRY = False` in
  `hedge_scanner/hedge_venues/avantis.py`. The delta is the `marginFee`
  component. That was the 2026-08-31 override; it is not current policy.
- Liquidation-risk math for a long-held Avantis hedge should read
  `marginFee.<side>` the same way the Quote does (the field is live again).

Not changed by this: §7's "never fabricate a fee" (a missing `marginFee` is
still `None`, not zero — the zeroing here happens explicitly on a value the
adapter did read successfully), Avantis commission classification (§12.11,
which superseded §12.8's always-maker decision), and §12.5's paste-an-address
scope (this is a modelling choice, not a data source).

### 12.10 CHANGED — live PnL marks are per-venue, not Ostium-for-everyone (2026-09-01)

`GET /api/prices` used to return a flat `{BTC: ostium_last_trade, …}` map.
The UI stamped that Ostium last-trade onto every open row, so a Hyperliquid
or Pacifica BTC position's live PnL was computed off Ostium's book. That is
the wrong number for a cross-venue hedge: the residual is `source_mark −
hedge_mark`, not `ostium_mark − entry` on both legs.

The payload is now nested `{venue: {market_or_canonical_asset: usd}}`.
Each adapter's `get_marks()` (and Avantis `hedge_venues.avantis.get_marks`)
reads that venue's own public mark feed. The UI looks up
`prices[position.venue][position.market]` then `base_asset`; it never falls
back to another venue. A venue that cannot serve a bulk mark (GRVT: ticker
is per-instrument) is present as `{}` and the poll leaves the scan-time
mark on that row.

Contract-visible consequences:

1. The `/api/prices` JSON shape is a breaking change for any external poller
   that expected the flat Ostium map.
2. HIP-3 Hyperliquid marks are keyed `xyz:BRENTOIL` (and setdefault onto
   `BRENT`) so they cannot overwrite native-DEX `BTC`.
3. Avantis is included even though it is not a position source, so a later
   hedge-side mark comparison has the destination book in the same payload.

Not changed by this: scan-time `Position.mark_price` (already venue-native),
§12.6's `positionValue / szi` derivation, or the Avantis quote path.

### 12.11 CHANGED — Avantis maker/taker is live OI-skew, not always-maker (2026-09-02)

**Decision (user-directed, 2026-09-02).** Avantis maker vs taker is **not**
order type, and it is not "always maker". A hedge that adds to the heavier
(dominant) side of `coinOI` is a **taker**; a hedge that joins the lighter
side is a **maker**. Both open and close of that hedge take the same tier.
This is unique to Avantis in the venue set — GRVT / Pacifica / Ondo still
decide maker/taker by resting vs crossing. Source:
[Maker and Taker](https://docs.avantisfi.com/trading/fees/maker-and-taker).

This supersedes §12.8, which priced every standard-perp hedge at
`openMakerFeeP` + `closeMakerFeeP` (2.0 bps crypto) regardless of direction.
That treated a dominant-side fill as a maker. Against the fixture BTC book
(long-heavy), a long hedge is now **4.5 + 4.5 = 9.0 bps taker**, a short
hedge remains **1.0 + 1.0 = 2.0 bps maker**.

`quote_hedge` calls `classify_skew_fee()` against live `coinOI` for the hedge
side, then classifies the close against the **same** book and side (not as an
unwind that nets our own size out). Empty and exactly-balanced books still
fall through to taker, matching the SDK. A size large enough to cross 0.5
is `mixed` (size-weighted blend) on both legs.

Consequences:

1. **Required fields.** `openMakerFeeP`, `openTakerFeeP`, `closeMakerFeeP`,
   `closeTakerFeeP`, and `coinOI.{long,short}` are all required. Missing any
   returns `available=False` — never a fallback to the other tier, never a
   silent zero (§7).
2. **`fee_tier` is directional.** `"maker"` / `"taker"` / `"mixed"` describe
   the round trip. Promotional 0 bps RWA still reports `"n/a"`.
3. **`hedge-scanner fees` shows both tiers.** OPEN/CLOSE/RT columns are
   `maker / taker`. JSON replaces `taker_close_round_trip_bps` with
   `taker_round_trip_bps` (`openTakerFeeP + closeTakerFeeP`).
4. **Upside Perps unaffected.** They still read `openTakerFeeP` /
   `closeTakerFeeP` off the Upside pair record (§12.4).
5. **Funding remains independent of skew** (§7.6(a)). A maker hedge is cheap
   on commission; it is not automatically positive-carry.

Unchanged: spread, the close-fee base
(`notional + grossPnL`), and promotional RWA detection (all four commission
fields sit at 0 together). `marginFee` is included in carry again (see §12.9 reversal 2026-09-02).

### 12.12 CHANGED — web UI headlines net funding APR, not 24h all-in (2026-09-02)

**Decision (user-directed, 2026-09-02).** The paste-an-address UI no longer
headlines a 24-hour all-in hedge cost and no longer shows Avantis Upside.

1. **Net APR** replaces the `Hedge 24h` column. It is Avantis **net** rate
   (holder-signed `fundingRate − marginFee`, matching the UI
   Net Rate (L/S) 24h) minus the other venue's funding, annualised
   `bps/8h × 8760/8 / 100` = `bps/8h × 10.95`. Jupiter borrow is not in
   this rate.
2. **Earn 24h** is that net 8h rate over three periods on current notional
   (`notional × net_8h_bps × 3 / 10_000`). Positive means the paired book
   is paid to hold over the next day.
3. **Even in** is how long that net, after Jupiter borrow, takes to repay
   Avantis open fee, close fee, and both spread legs:
   `cover_bps × 8 / (net_8h_bps − source_borrow_8h_bps)`. Jupiter's paying
   rate is `longBorrowRatePercent` / `shortBorrowRatePercent` from
   `GET /v1/pool-info?mint=` (the 0.0013%/hr header on jup.ag/perps).
   `None` / "never" when that recoup rate is not a receive.
4. **Avantis Upside is removed from the scan UI.** The scanner no longer
   fetches `quote_upside_hedge` per row. CLI ranking and `quote_upside_hedge`
   itself are unchanged (§12.4).
5. **§7.5.3 still applies to the CLI.** Headline 24h ranking, sensitivity
   table, and venue-crossover stay on the `scan` command.
6. **Even-in box links out to Avantis trade.** Bottom of the box is
   `Hedge on Avantis` → `https://www.avantisfi.com/trade?asset={BASE}-USD`,
   with `{BASE}` taken from the row's Avantis market (`BTC/USD` → `BTC-USD`).

### 12.13 CHANGED — liquidation price is read from the venue, not re-derived (2026-09-02)

**Decision (user-directed, 2026-09-02).** Same principle as §12.10 for marks:
the tool's `Position.liquidation_price` must be **the venue's own liq**, not a
re-derivation. Every adapter is expected to read liq from the same source the
venue's UI reads. A tool that publishes a "computed elsewhere" liq is
guaranteed to drift from what the trader sees on the venue frontend the
moment either side changes a formula, and the drift is silent.

Audit result across the adapter set:

| Venue | Source of `liquidation_price` | Path |
|---|---|---|
| Hyperliquid | `clearinghouseState.assetPositions[*].position.liquidationPx` | wire-canonical |
| Pacifica | `positions[*].liquidation_price` | wire-canonical |
| Jupiter Perps | `perps-api.jup.ag/v1/positions[*].liquidationPrice` (with on-chain decode as a fallback) | wire-canonical (primary), computed (fallback) |
| Ostium | `_compute_liq_price` — pinned to the published formula, no venue endpoint exists | formula-canonical |
| GRVT | account-scoped `mm_ratio` and margin math (auth-gated, not read today) | n/a for anonymous scans |

Jupiter (this change):
- `perps-api.jup.ag/v1/positions?walletAddress=<addr>` is the same endpoint
  `jup.ag/portfolio` polls. It returns `entryPrice`, `markPrice`,
  **`liquidationPrice`**, `leverage`, `collateral`, `pnlAfterFeesUsd`, and
  full fee breakdown per position. `JupiterAdapter.get_positions()` now
  tries this endpoint first (`_fetch_positions_via_api`) and returns
  Position rows built entirely from it (`_position_from_api`). Values match
  `jup.ag/portfolio` to display precision.
- The on-chain decode path (`getProgramAccounts` + Doves marks +
  `_to_position`) remains as the fallback. It runs only when the perps API
  returns non-200 / invalid JSON / non-list `dataList`. An **empty** but
  successful API response returns `[]` and never falls back — a wallet
  with no positions must not be papered over by the fallback path
  hallucinating from lingering zero-size accounts.
- `get_marks()` is untouched (§12.10). Marks are still cross-venue via Doves
  + DEX-aggregator, because the perps API is per-wallet.

Ostium (audit only, no code change):
- Ostium's subgraph `Trade` type stores only raw parameters (openPrice,
  collateral, leverage, rollover accumulator, funding). No `liquidationPrice`
  field, no `Position` object, no REST API to hit. The frontend and the
  Python SDK both compute liq client-side.
- The published formula (docs.ostium.com/traders/trading/liquidation) is:
  `Threshold = 100% − (Leverage / MaxLevPair × 25%)`;
  `Liq_long = Entry × (1 − Threshold/Leverage)`;
  `Liq_short = Entry × (1 + Threshold/Leverage)`.
  Accrued rollover shifts liq toward entry by `|fees|/collateral × Entry/Leverage`.
  `OstiumAdapter._compute_liq_price` implements this verbatim.
- `tests/test_ostium.py` pins every row of the docs' worked table (5x, 10x,
  20x, 50x, 100x, 200x on max 200x) plus the fee-shift math. If Ostium ever
  changes the 0.25 backstop coefficient, or moves to a nonlinear fee-shift,
  those tests fail loudly and force a re-derivation with the new docs page
  as the citation.

Not changed by this: on-chain decode still exists for Jupiter (fallback),
and Hyperliquid / Pacifica already read venue-canonical liq. The engine's
liquidation-distance math (§7.13) still consumes `Position.liquidation_price`
without caring which path produced it.

## 8. Stack

Python ≥3.11, managed with `uv` (consistent with the user's `avantis-bot/backend`).
`httpx`, `pydantic`, `solana`/`solders` for Solana RPC, `typer` for CLI, `pytest` for tests.
Deliver a CLI first (`uv run python -m hedge_scanner scan <address>`); a FastAPI layer
can wrap it later. See section 10.5 for the Solana RPC deviation and section 12.2 for
why the `hedge-scanner` console script is not the documented invocation.
