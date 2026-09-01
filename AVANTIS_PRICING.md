# Avantis hedge pricing — implementation notes

Module: `hedge_scanner/hedge_venues/avantis.py` · Tests: `tests/test_avantis_quote.py`
Fixtures: `tests/fixtures/avantis/` (real recorded responses, see `capture_meta.json`)
Written 2026-08-19. Source study: `../avantis-fees.md`. Contract: `CONTRACT.md` §4, §7.6.

## 1. Endpoints (all verified live, this session)

| Method | URL | Used for |
|---|---|---|
| GET | `https://prod-api.avantisfi.com/data/v2/trading` | 120 pair records: commission, `marginFee`, `fundingRate`, `coinOI`, minimums, profit cap |
| GET | `https://feed-v3.avantisfi.com/v1/price-feeds/last-price` | oracle price by `pairIndex`, to convert USD notional → coin size |
| POST | `https://prod-api.avantisfi.com/risk/v2/spread` | live directional, size-dependent spread |

`GET /core/v2/open-interests` also returns OI (plus `pendingLongOI`/`pendingShortOI`) and is
recorded as a fixture, but it is **not** called at runtime: `/data/v2/trading` already carries
`coinOI` per pair, so one fetch covers fees, rates and skew together.

The spread request body was taken from the SDK (`avantis_trader_sdk/markets/api.py`) and confirmed
against production:

```json
{"pairIndex": 1, "trader": "0x000...000", "coinSize10": "1546483521",
 "isLong": true, "isOpen": true, "orderType": 0}
```

It returns **HTTP 201** (not 200). Percentages come back as integer strings at **1e10** scale in
`estimatedSpreadPctWithFlow10` / `spreadPctWithoutFlow10`; the with-flow value is preferred, which is
what the UI shows. Note the SDK's own docstring claims this route is "not serving yet" on mainnet —
**that comment is stale**; mainnet answers normally.

## 2. Field mapping to the canonical `Quote`

| `Quote` field | Avantis source | Transform |
|---|---|---|
| `taker_fee_bps` | `additionalPairParams2.openMakerFeeP` (see below) | `× 100` (percent → bps) |
| `close_fee_bps` | `.closeMakerFeeP` (see below) | `× 100` (percent → bps) |
| `price_impact_bps` | `POST /risk/v2/spread`, `isOpen=true` | `/1e10 × 100` |
| `est_slippage_bps` | `POST /risk/v2/spread`, `isOpen=false` | same. Avantis has no orderbook slippage distinct from spread, so the two spread legs are mapped to the two contract fields — summing them gives the true round-trip spread under `CONTRACT.md` §6 |
| `funding_rate_8h_bps` | `fundingRate.{long,short}` | `× 8 × 100`, then **sign flipped** (§4) |
| `borrow_rate_8h_bps` | `marginFee.{long,short}` | `× 8 × 100`, absolute value (always a cost) |
| `available` / `notes` | `isPairListed`, `closeOnlyMode`, `minLevPosUSDC`, `feed.attributes.isOpen` | §6 |
| `base_asset` | caller's normalized base | `CONTRACT.md` §9 addition |

**Maker open + maker close, both live (product decision 2026-08-30, `CONTRACT.md` §12.8).**
Both legs price at the pair's maker commission, read off the live pair record per invocation:
`openMakerFeeP` for the open, `closeMakerFeeP` for the close. On crypto that is a **2.0 bps**
round trip at current rates. Neither number is hardcoded.

This deliberately quotes the favourable end of the range, and §12.8 records why that is a
choice rather than a fact. Against an **unchanged** book a round trip always pays one maker leg
and one taker leg (5.5 bps on crypto), because Avantis nets our own size back out of our side at
close and undoes the skew improvement the open was paid for — verified by sweeping
`classify_skew_fee()` over 200 skew/size combinations with zero both-legs-maker results. A maker
close is only reachable if the pair's skew drifts in our favour while the hedge is held, which is
real but unknowable at quote time. Every non-promotional quote therefore carries a note labelling
the round trip an ASSUMPTION and stating the taker-close alternative, read from `closeTakerFeeP`
**for disclosure only, never for pricing**.

`classify_skew_fee()` remains in the module as the SDK-faithful Decimal port of
`maker_or_taker_fee_p` and is still pinned by its unit tests (§3 below); it is the ground truth
for how Avantis actually charges. `quote_hedge` does not call it. Refusal semantics are unchanged
— a missing `openMakerFeeP` **or** `closeMakerFeeP` returns `available=False` with a reason; we
never fall back to another tier or to zero (§7 non-negotiables, §6 below).

RWA pairs under growth mode still price at 0 bps: all four `additionalPairParams2` commission
fields sit at 0 together on those pairs, so the promotional check
`open_maker == 0 and close_maker == 0` is equivalent to checking all four and correctly preserves
the promotional flag.

Deliberately **not** used: legacy `openFeeP` / `closeFeeP` / `skewEqParams`, and the SDK's
`get_opening_fee()` which reads them. Their live decile table is degenerate (`a = 0` on every
decile) and the path returns 0.02% on RWA pairs whose actual commission is 0.

Extras live on `AvantisQuote`, a **subclass** of `Quote` (`models.py` is owned by another agent, so
it was not edited): `fee_tier`, `promotional_zero_fee`, `borrow_rate_annual_pct`,
`funding_rate_annual_pct`, `horizon_hours`, `all_in_cost_bps`/`all_in_cost_usd`,
`min_position_usd`, `max_gain_pct_of_collateral`, `profit_share_schedule`, `pair_index`,
`close_fee_base`. `isinstance(q, Quote)` holds, so the engine consumes it unchanged.

## 3. Maker vs taker — the underlying mechanic (retained but bypassed by the hedge quote)

On Avantis, maker/taker is **not** order type. A leg that moves the pair's *coin-denominated* long
share toward 0.5 is a **maker** (1.0 bps crypto); one that moves it away is a **taker** (4.5 bps);
one large enough to cross 0.5 pays a size-weighted **mixed** blend. `classify_skew_fee()` is a
Decimal port of `maker_or_taker_fee_p` in `avantis_trader_sdk/compute/fees.py`, including its two
fall-through cases (empty book and an exactly balanced book both price as taker). It is retained,
unit-tested, and still exported.

**`quote_hedge` intentionally does not invoke `classify_skew_fee`.** The product decision
(2026-08-30, §2 above, `CONTRACT.md` §12.8) prices both legs at the pair's maker rate. The
classifier would instead return one maker leg and one taker leg for the same round trip
(`CONTRACT.md` §7.6(b)), which is what the venue charges against an unchanged book — so the
divergence between this module and the classifier is deliberate and is disclosed in the quote
notes rather than reconciled in code.

The classifier is kept in place because (a) its unit tests still pin the SDK-faithful mechanic,
which is the ground truth for how Avantis charges, and (b) any future caller that needs the
per-leg tier (e.g. a research script or a reintroduction of skew-based routing) can consume it
without a re-port. Deleting it would leak product coupling into the pricing layer.

**Documentation conflict, recorded in code.** The docs contradict themselves on the maker rate:
`maker-and-taker.md` says `0.001%` (0.1 bps — a missing decimal place),
`fee-schedule-by-asset-class.md` says 1 bps in its body, and that same page's summary table prints
the maker and taker labels **swapped** ("4.5 bps - Maker / 1 bps - Taker"). Live
`openMakerFeeP = 0.01` percent settles it at **1.0 bps maker / 4.5 bps taker**. The module reads the
live fields and never the docs.

**Close fee — modelled, not API-verified.** The close is classified against the book *including* our
own leg, then nets our size back out. Consequence: at unchanged external skew, a skew-improving open
unwinds as a skew-worsening close, so mechanically **maker open implies taker close** (and vice
versa) — a 5.5 bps crypto round trip. `quote_hedge` nevertheless quotes `closeMakerFeeP` for a
2.0 bps round trip per §12.8, which is the outcome when external skew drifts favourably during the
hold. Avantis re-evaluates against actual skew at close time, so the quoted figure is the
favourable end of a 2.0–5.5 bps range. Flagged in `notes` on every quote.

**Close fee base.** Avantis charges close on `notional + grossPnL`, not fixed notional, so `close_fee_bps`
is a *rate* that is only notional-equivalent at flat price. `close_fee_usd(rate, notional, gross_pnl)`
exposes the real base; `close_fee_base` records it on the quote.

## 4. Sign conventions

Avantis publishes `fundingRate.{long,short}` in **%/hour where positive means that side PAYS**.
`CONTRACT.md` §4 requires the opposite: **positive = the hedger RECEIVES**. The sign is therefore
flipped exactly once, in `funding_8h_bps_for_side()`.

Both sides are read independently and **never** derived by negating the other — they are not
negations (live BTC: long `-0.00010008` against short `+0.00010654`), because the pool absorbs the
residual.

**Funding sign is independent of OI skew — verified, and it contradicts the study.** The study (and
the docs) describe funding as skew-driven, "heavier side pays lighter side". Live counterexamples
observed this session: BTC long-heavy with **longs receiving**, and ETH short-heavy with **shorts
receiving**. Crypto funding is anchored to external venues (Binance / Hyperliquid), so it is not a
function of Avantis' internal book. **A maker hedge is therefore not automatically positive-carry**,
and the two must be evaluated separately. Pinned by
`test_funding_sign_is_independent_of_oi_skew`.

Borrow is always a cost, so `borrow_rate_8h_bps` is non-negative regardless of wire sign.

## 5. Unit derivations

`marginFee` and `fundingRate` are both **percent per hour**. The docs never state this; it was
re-derived and cross-checked on two independent pairs:

- `marginFee = 0.00022824` × 8760 h = **1.99938 %/yr** → the 2.00% annualised protocol default.
- XAU long `0.0012249` × 8760 = **10.73 %/yr**, against that pair's `minLongBorrowFee = 10`.

Conversion to the contract's 8h basis: `pct_per_hour × 8 × 100` bps. BTC borrow →
**0.182592 bps/8h**.

`minLongBorrowFee` / `maxLongBorrowFee` are **ignored**: BTC states 15/100 yet observes 2.00%/yr, so
their units do not reconcile. Semantics remain unverified; `marginFee` is preferred, as the study
recommended.

## 6. Refusals, promotional rates, constraints

- **Never default to zero.** A missing `openMakerFeeP`, `marginFee.{side}`, `fundingRate.{side}`,
  `coinOI` or oracle price returns `available=False` with a reason. A spread engine refusal (403 =
  blocked, 404 = no computable spread) or a literal zero from it is treated as "do not execute",
  never as a free fill.
- **Promotional 0 bps** is detected by all four commission fields reading 0 **in live data**, not
  from an asset-class list — 27 live records carry a blank `assetType` and mix crypto (FET, SHIB,
  PEPE) in with RWAs (USOILSPOT, USD/TRY), so classifying by asset class would misprice them. When
  detected, `promotional_zero_fee=True` and `notes` states the rate is revocable growth-mode pricing
  tied to unstated RWA OI milestones. Spread, borrow and funding still apply and are still charged.
- **Minimums** come from `minLevPosUSDC` per pair (live: 100 crypto, 300 FX/metals, 10 WTI/BRENT).
  Below-minimum returns `available=False`; exactly-at-minimum is allowed.
- Also gated: `isPairListed=False`, `closeOnlyMode=True`. `feed.attributes.isOpen=False` adds a
  market-closed warning to `notes` (net rate accrues while a market is shut).
- `maxGainP` (profit cap, % of collateral) is surfaced on every quote.

## 7. Upside Perps

`quote_upside_hedge()` prices the separate `{BASE}_UPSIDE` pair records (live: ETH 115, BTC 116,
SOL 117, XRP 118, HYPE 119), gated on `storagePairParams.isPnlTypeAllowed = 1`.

Verified live on pair 116: `marginFee = {long: 0, short: 0}` (borrow genuinely zero), and
`pnlFees.tierP/feesP` collapses to exactly the documented bands — **25% (ROI 1–500%) / 20%
(500–1500%) / 10% (1500–2500%) / 5% (≥2500%)**, returned in `profit_share_schedule`.

`all_in_cost_bps` is deliberately left `None`. Converting a profit share into bps of notional
requires a price-move assumption, and inventing one would be a fabricated rate. Funding and spread
are the only unconditional costs; `notes` states the cost is PnL-contingent and that the instrument
is cheaper when the hedge proves unnecessary and more expensive when it works.

**Unresolved conflict.** The docs and `CONTRACT.md` §7.6 both say Upside charges no open or close
fee, but the live Upside records still publish
`additionalPairParams2 = {openMakerFeeP: 0.01, openTakerFeeP: 0.045, ...}`, identical to standard
crypto pairs. The module reports the **live** values in `taker_fee_bps` / `close_fee_bps` and flags
the conflict, because zeroing them on documentation alone would understate Avantis — the one
direction this tool must never err in. Needs confirmation with the team.

## 8. Caching

TTL caches with async single-flight locking: trading snapshot 15s, prices 10s, spread 5s. Spread is
cached on the **exact** `(pairIndex, coinSize10, isLong, isOpen)` tuple, so nothing is ever shared
across sizes or directions. `clear_caches()` resets. A fan-out of concurrent quotes shares one
snapshot fetch (pinned by `test_snapshot_is_cached_within_ttl`).

All JSON is parsed with `json.loads(..., parse_float=Decimal, parse_int=Decimal)`. `response.json()`
is never used: it would route rates like `0.00022824` through binary float.

## 9. Verified vs assumed

**Verified against the live API:** all three endpoints and the spread request/response schema; the
1.0/4.5 bps crypto maker/taker split; 0 bps on all FX, metals, oil, index and equity pairs; the
%/hour unit for `marginFee` and `fundingRate` (two independent cross-checks); the funding wire sign
and that the two sides are not negations; that funding sign is independent of OI skew; per-pair
minimums; `maxGainP`; Upside `marginFee = 0` and the 25/20/10/5 `pnlFees` bands; spread being
directional and non-monotonic in size; `limitOrderFeeP = 0` and `twapFee = 0`.

**Assumed / modelled, and labelled as such:**
1. **Close-fee maker/taker** — the *rate* is live (`closeMakerFeeP`), but the *tier* for a future
   close cannot be. Per §12.8 the close is priced maker, which the mechanic in §3 says requires
   external skew to drift in our favour during the hold; against an unchanged book it would be
   taker. This is the one assumption in this file that moves cost **down**, so it is the one to
   re-examine first when auditing a narrow Avantis win.
2. **Close fee on notional** — the rate is applied to notional for a flat-price estimate; the real
   base is `notional + grossPnL`.
3. **No fee discounts applied.** AVNT staking (−5% to −30%) and referral (−5%) discounts are
   wallet-specific and unknowable from an address alone, so quotes are **undiscounted list price**.
   This overstates cost for a staked user rather than understating it.
4. **Loss protection modelled as zero** — live `longSkewConfig`/`shortSkewConfig` thresholds are 101
   (unreachable) on 116/120 pairs, so the rebate cannot trigger. Switchable by the team.
5. **Market-order spread only** — TWAP slicing would pay less spread but changes execution shape.
6. **Simple annualisation** (`× 8760`), not compounded, for the `*_annual_pct` display fields.
7. **Builder-code fees excluded** — zero on the native app; only relevant to aggregator routing.

## 10. Contract reconciliation needed

1. `Quote` has no field for which side of the fee schedule a leg landed on, whether a 0 bps rate is
   promotional, or the Upside profit-share schedule. Handled today by the `AvantisQuote` subclass
   (§2). If `Quote` absorbs `fee_tier` and `promotional_zero_fee`, delete the subclass. **The
   promotional flag matters for the UI contract** — `CONTRACT.md` §7.6 requires 0 bps to be labelled
   non-durable, and `notes` is currently the only guaranteed carrier.
2. `CONTRACT.md` §6's cost formula has one `price_impact` and one `slippage` term. For Avantis these
   are the open and close legs of the same spread. Documented in §2; worth stating in the contract so
   another venue does not double-count.
3. `CONTRACT.md` §7.6 states Upside charges no open/close fee. Live data disagrees (§7). The
   contract should either cite the live field or record the conflict.
4. `CONTRACT.md` §7.6 and the study describe funding as skew-driven ("heavier side pays lighter
   side"). Live data refutes this for crypto (§4). The positive-carry claim should be stated as
   "maker commission **and** a favourable funding sign, checked independently", not as one
   consequence of skew.
