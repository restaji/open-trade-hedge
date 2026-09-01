# Hedge methodology

How `hedge_scanner/engine.py` turns a set of open perp positions into ranked hedging
opportunities. Every formula is written out with its variables. The **Limitations**
section at the end is the most important part of this document — read it before acting
on any number the tool prints.

Companion documents: `CONTRACT.md` (data schemas and the ingestion/engine boundary),
and the five per-venue fee inventories in the parent directory.

---

## 0. Conventions

### 0.1 Sign conventions

Getting these wrong inverts conclusions, so they are stated once and enforced everywhere.

| Quantity | Convention |
|---|---|
| `Position.notional_usd` | Signed. Positive = long, negative = short. |
| `Position.side` | `"long"` or `"short"`. **Authoritative for direction.** |
| `Quote.funding_rate_8h_bps` | Signed **from the hedger's perspective**. Positive = the hedge leg **receives** funding. Negative = the hedge leg **pays**. |
| `Quote.borrow_rate_8h_bps` | Always a **cost**, always non-negative. Never a credit. |
| Every `*_cost_bps` the engine produces | A **cost**. Positive = money out. Negative = money in. |

Because `funding_rate_8h_bps` is positive when the hedger receives, it is **subtracted**
when forming a cost. This is the single most dangerous sign in the codebase and is
covered by dedicated tests (`TestFundingSignConvention`).

`_signed_notional()` treats `side` as the authority on direction and takes only the
magnitude from `notional_usd`. An adapter that emits an unsigned notional for a short
would otherwise cause that short to be netted as a long, silently doubling reported
exposure instead of cancelling it. That is a wrong answer that looks plausible, which is
worse than a crash.

### 0.2 Units

All rates are **basis points of notional** (1 bp = 0.01% = 1/10,000). Funding and borrow
are quoted **per 8 hours** regardless of a venue's actual settlement interval (see
Limitation L4). Horizons are in **hours**, parsed from `8h` / `24h` / `3d` / `1w` /
a bare number of hours.

### 0.3 Arithmetic

`Decimal` throughout. There is no `float` anywhere in `engine.py`. JSON output encodes
money and bps as **strings**, not JSON numbers, because a JSON number is a float in
almost every consumer and a float round-trip silently discards precision that the rest
of the pipeline was careful to preserve. A test asserts the payload contains no floats.

---

## 1. Portfolio netting

Positions are bucketed by normalized `base_asset` (the alias map in
`hedge_scanner/assets.py` maps `WBTC`, `XBT`, `BTC-PERP` and friends onto `BTC`). For
each asset, with $L$ = the set of long legs and $S$ = the set of short legs:

```
long_notional   = Σ |notional_i|   for i in L
short_notional  = Σ |notional_i|   for i in S
net_notional    = long_notional − short_notional        (signed)
gross_notional  = long_notional + short_notional
```

Two derived quantities matter more than the net alone:

```
offsetting_notional = min(long_notional, short_notional)
gross_net_gap       = gross_notional − |net_notional|
                    = 2 × offsetting_notional
```

`offsetting_notional` is the amount of exposure the user is **already holding both sides
of** — long on one venue, short on another, in the same asset. It carries no directional
exposure but is not free: it pays funding on both legs continuously, and it will pay an
exit fee on both legs whenever it is closed. `gross_net_gap` is twice that figure because
both legs are redundant.

This is reported as a `SelfHedgeFinding` rather than being quietly netted away, because
"you are paying two venues to be flat" is a finding in its own right. The cost to collapse
it is estimated as the **exit fee on both legs only**:

```
unwind_fee_bps = close_fee_bps(venue_long, asset) + close_fee_bps(venue_short, asset)
unwind_fee_usd = unwind_fee_bps × offsetting_notional / 10,000
```

Entry fees are excluded on purpose: they are sunk and are not an input to the decision
of whether to keep the pair open.

### 1.1 Hedge side

```
hedge_side = "short"  if net_notional > 0
             "long"   if net_notional ≤ 0
```

### 1.2 Dust

An asset whose $|net\_notional|$ is below `dust_threshold_usd` (default **25 USD**,
`--dust-usd`) is classified **flat** and gets no hedge proposal — hedging 4 dollars of
residual exposure for 8 bps of fees is not advice. Dust exposures are still reported in
the net-exposure table, and they still generate a `SelfHedgeFinding` if they arose from
two offsetting legs, which is the common and interesting case.

---

## 2. `delta_hedge` — cost of neutralising directional exposure

For each asset with material net exposure, the engine prices the opposing position on
every candidate venue, including Avantis, and ranks by all-in cost to hold for the chosen
horizon.

### 2.1 The cost model

Let $h$ = holding horizon in hours. For one candidate venue:

```
round_trip_fee_bps  = taker_fee_bps            (open / entry commission)
                    + close_fee_bps            (exit commission)
                    + price_impact_bps         (size-dependent, both sides)
                    + est_slippage_bps         (execution estimate)

carry_cost_bps_8h   = borrow_rate_8h_bps − funding_rate_8h_bps

carry_cost_bps(h)   = carry_cost_bps_8h × h / 8

total_cost_bps(h)   = round_trip_fee_bps + carry_cost_bps(h)

total_cost_usd(h)   = total_cost_bps(h) × hedge_notional_usd / 10,000
```

Note the **minus sign** on `funding_rate_8h_bps`: funding received reduces cost, funding
paid increases it. Borrow always adds.

> **Contract deviation, recorded.** `CONTRACT.md` §6 writes the carry term as
> `+ (funding_or_borrow_8h × horizon_h / 8)`. Taken literally alongside §4's stated sign
> convention (*positive = hedger receives*), that formula **adds** received funding to
> cost, which is backwards and would rank the worst venue first. The engine implements
> `borrow − funding` as above. Since the funding sign convention in §4 is unambiguous and
> is the one adapters populate, the formula in §6 is the part that is wrong. See
> "Reconciliation notes" at the end.

### 2.2 Ranking

Candidates are sorted ascending by `total_cost_bps(h)`, with ties broken by
`carry_cost_bps_8h`, then `round_trip_fee_bps`, then venue name.

A **positive-carry hedge** is one where `total_cost_bps(h) < 0`: over the horizon, funding
received exceeds the round trip, so the hedger is *paid* to hold the hedge. These surface
first automatically — their total cost is negative, so the single sort criterion puts them
on top. No special-casing is applied, which keeps the ranking one consistent economic
comparison rather than a policy.

For a venue with positive carry, the engine also reports how long the hedge must be held
for the carry to repay the fees:

```
breakeven_hours = round_trip_fee_bps / (−carry_cost_bps_8h) × 8
```
undefined (reported as `never`) when `carry_cost_bps_8h ≥ 0`.

### 2.3 Exclusions — never a silent zero

A candidate is **excluded with a stated reason**, never defaulted to zero cost, when:

| Condition | Reason shown |
|---|---|
| `Quote.available == False` | the quote's own `notes`, e.g. "requires user API key" |
| Venue is not a permitted hedge destination | "not a permitted hedge destination" |
| Hedge notional is below the venue's minimum position size | "hedge size X is below the venue minimum of Y" |
| No quote returned for the asset and side | absent from both lists; the Avantis line says so explicitly |

Treating an unavailable venue as zero-cost would make the venue we know least about look
like the cheapest, which is the exact opposite of correct. Exclusions are printed in the
terminal output so an absent venue is visibly absent rather than invisibly missing.

The minimum-position check matters in practice: Avantis rejects crypto positions under
**100 USDC** notional and FX/metals positions under **300 USDC**, so a small residual
exposure genuinely cannot be hedged there.

### 2.4 The Avantis comparison line

Per `CONTRACT.md` §7.5, Avantis is named on every asset whether it wins or loses. The
`AvantisComparison` type reports Avantis' rank, its cost, the best non-Avantis
alternative, and the signed gap in both bps and USD:

```
delta_bps = avantis_total_cost_bps − best_alternative_total_cost_bps      (positive = Avantis dearer)
delta_usd = delta_bps × hedge_notional_usd / 10,000
```

Verdicts: `wins`, `ties`, `loses`, `only_candidate`, `no_quote`. This type only reads the
ranking; it never reorders it. A test asserts that a more expensive Avantis actually ranks
second (`test_ranking_is_not_rigged_toward_avantis`), because a comparison the user cannot
trust is worth less than no comparison.

---

## 3. Horizon sensitivity and crossover detection

This is the analytically load-bearing output. Fees are **one-time** and carry is
**time-proportional**, so the cheapest venue is a function of how long the hedge is held.
A venue with high commission that pays funding will overtake a cheap venue that charges
funding, and the hour at which that happens is the actual decision.

### 3.1 Cost is affine in the holding period

Rewrite the cost model as a line in $h$:

```
cost_v(h) = F_v + s_v · h

where   F_v = round_trip_fee_bps           (intercept, bps)
        s_v = carry_cost_bps_8h / 8        (slope, bps per hour)
```

The cheapest-venue frontier is therefore the **lower envelope of a set of straight
lines**, and its breakpoints can be solved algebraically rather than found by sampling.

### 3.2 Exact crossover solution

Two venues $a$ and $b$ have equal cost where $F_a + s_a h = F_b + s_b h$, so:

```
h* = (F_a − F_b) / (s_b − s_a)
```

`_lower_envelope_crossovers()` walks the envelope:

1. The leader as $h \to 0^+$ is the line with the lowest $F$, tie-broken on lowest slope.
2. From the current leader at $h_\text{cur}$, consider only lines with $s_o < s_\text{leader}$
   (a line with an equal or steeper slope can never overtake). Compute each $h^*$ and keep
   those with $h_\text{cur} < h^* \le h_\text{max}$.
3. Take the smallest such $h^*$. Record the breakpoint, make that line the leader, set
   $h_\text{cur} = h^*$, repeat.

At most $n-1$ breakpoints exist for $n$ lines, so the loop is bounded. Dominated venues
never appear as a leader and so never generate a spurious crossover.

The search window defaults to **720h (30d)**; crossovers beyond it are not reported.

### 3.3 Why not just sample the grid

The rankings are also tabulated at 8h / 24h / 3d / 7d / 30d (`--horizons`), but the
crossover hour is solved exactly, not inferred from those samples. In the shipped test
fixture the SOL crossover falls at **11.83h**, between the 8h and 24h columns. A grid
search would report either "8h" or "24h" — and the whole point of the number is to tell a
trader that a 12-hour hedge and a 36-hour hedge belong on different venues.

---

## 4. `funding_arb` — delta-neutral cross-venue carry

A pair of legs in the same base asset, long on venue $A$ and short on venue $B$, has no
net price exposure, so its entire P&L is carry minus fees.

```
net_carry_bps_8h = funding_A + funding_B − borrow_A − borrow_B
```

(each `funding` signed positive-if-received, per §0.1). Positive means the pair is paid to
exist. In USD:

```
net_carry_usd_8h = net_carry_bps_8h × notional_usd / 10,000
```

Two flavours are detected, each tagged with `basis`, and the fees that must be earned back
differ between them:

| `basis` | Situation | `fee_bps` (`fee_basis`) |
|---|---|---|
| `existing` | Both legs already open in the portfolio | `close_A + close_B` (`exit_only`) — entry fees are sunk |
| `new` | A pair the user could open | full round trip on both legs: open + close + impact + slippage, each side (`round_trip`) |

Sizing: `existing` pairs use `min(held_long, held_short)`; `new` pairs use
`--arb-notional-usd` if given, else the asset's net exposure.

Breakeven and P&L:

```
breakeven_hours = fee_bps / net_carry_bps_8h × 8              (None if net_carry ≤ 0)
net_pnl_bps(h)  = net_carry_bps_8h × h / 8 − fee_bps
net_pnl_usd(h)  = net_pnl_bps(h) × notional_usd / 10,000
```

Only pairs with `net_carry_bps_8h > min_arb_carry_bps_8h` (default **0.10 bps/8h**) are
reported. A pair that bleeds carry is not an arbitrage, and a pair earning 0.01 bps/8h is
inside the noise of the rate measurement itself.

`opposite_funding_signs` is reported separately from profitability. Opposite signs is the
*mechanism* the contract points at, but the *criterion* is net carry: two venues that both
pay the hedger are an even better pair, and they do not have opposite signs.

---

## 5. Fee inputs — what is real and what is not

The static schedule lives in exactly one place: `FEE_SCHEDULE` in `engine.py`. Replacing a
number is a one-line edit. Live per-quote fees supplied by an adapter always win over the
table; the table is a fallback and a reference.

| Venue | Open / close | Status | Source |
|---|---|---|---|
| GRVT | 4.5 / 4.5 bps | **Verified** | Level 1 perp taker, live ladder effective 2026-03-23. Maker is **−0.01 bps** (a rebate) at every tier. |
| Pacifica | 4.0 / 4.0 bps | **Verified** | Tier 1 taker, uniform across all 75 markets, confirmed live against `GET /api/v1/info/fees`. Maker 1.5 bps, never a rebate. |
| Ondo Perps | 2.5 / 2.5 bps, **3.5 on 12 markets** | **Verified, per-market, promotional** | `GET /v1/markets`. Ondo's own `/fees` page claim of uniform pricing is false. 2.5 bps is "50% off" a 5.0 bps base with no published expiry. |
| Avantis | 4.5 / 4.5 bps (taker reference) | **Verified, state-dependent** | Crypto is 1.0 bps maker / 4.5 bps taker, where maker/taker is set by **OI-skew improvement, not order type**. RWA pairs are **0 bps** under a revocable growth mode. |
| Jupiter Perps | 6.0 / 6.0 bps | **UNVERIFIED PLACEHOLDER** | `TODO(source)`: `../jupiter-perps-fees.md` did not exist when this was written. **Not a researched number.** |

Anything derived from an unverified row is marked `*` in the terminal and carries
`fee_schedule_unverified: true` in JSON. State-dependent rows are marked `+`.

**Funding and borrow rates are never in the table.** They are live values. `Quote`s built
via `quote_from_schedule()` require an explicit carry rate; if both funding and borrow are
`None` the quote comes back `available=False` with a stated reason and is excluded from
ranking. A missing rate is never treated as zero.

### 5.1 Avantis Upside Perps — a different risk shape

Upside Perps charge no commission and no borrow, and **nothing at all on a losing close**.
They instead take a share of gross profit, **25%** in the 1–500% ROI band (20% / 10% / 5%
above). Spread and funding still apply.

Ranking them as "0 bps" alongside conventional perps would be actively misleading, so they
are evaluated as a separate candidate with an explicit threshold. Let $C_\text{std}$ be the
cheapest conventional hedge's all-in cost, $C_\text{ups}$ the Upside fixed leg (spread plus
carry), and $\sigma$ the profit share. A hedge that ends up in the money by an adverse move
of $m$ bps has gross profit $\approx m$ bps of notional, so the profit share costs
$\sigma \cdot m$. Upside is cheaper while:

```
C_ups + σ·m < C_std        ⟺        m < (C_std − C_ups) / σ
```

That threshold is reported as `breakeven_adverse_move_bps`. Below it the hedge was barely
needed and Upside is cheaper; above it the profit share exceeds the commission it saved.
When an adapter does not quote `avantis_upside` directly, the fixed leg is **derived** from
the standard Avantis quote by zeroing commission and borrow (both documented as zero on
Upside) and keeping spread and funding (both of which still apply). The output labels which
of the two it used.

---

## 6. Limitations

These are ordered by how likely they are to cost the user money.

**L1 — A delta hedge does not neutralise liquidation risk.** This is the most
underappreciated risk in the whole tool and it is not a rounding error. The two legs sit
in **separate margin accounts on separate venues**. A move that is neutral for the combined
position is still fully adverse for one leg. That leg can be liquidated while the other is
deep in profit — and once it is, the user is naked, directionally exposed, and has realised
the loss. Cross-margin does not help across venues. Concretely: GRVT's liquidation
forfeits **100% of residual margin** (on cross margin, the entire cross account equity),
Ondo charges **1.5% of closed notional**, Pacifica charges `max(0.75%, MMR × 0.4)`, and
Avantis liquidates Upside positions around **−85% ROI**. Any of those dwarfs the few bps of
fee optimisation this tool performs. **The engine models none of it.** A hedge is only as
good as the margin buffer on its weakest leg, and sizing that buffer is out of scope here.

**L2 — Funding rates are point-in-time and mean-revert; the 30d column is not a forecast.**
Every carry figure extrapolates a single live 8h observation linearly. Perp funding is
strongly mean-reverting and regularly changes sign within a day. The 30d column multiplies
one snapshot by 90 and should be read as *"what this rate would be worth if it persisted"*,
never as an expectation. The 8h and 24h columns are defensible; 7d is indicative; 30d is
illustrative. Crossovers computed far out on the horizon inherit this weakness in full —
a crossover at 400h is a statement about today's rate, not about next month.

**L3 — Positive carry is compensation for risk, not free money.** A venue paying you to
hold a short is usually paying because the book is crowded long and it wants the other
side. That crowding is exactly the condition under which a squeeze produces a violent move
against your leg (see L1). Ranking positive-carry hedges first is a *cost* ranking, not a
*risk-adjusted* one.

**L4 — Funding intervals are normalised to 8h, but the venues do not agree.** GRVT settles
8-hourly (escalating to hourly under stress). Pacifica, Ondo and Avantis settle **hourly**.
The engine works in a single `funding_rate_8h_bps` unit, so adapters must convert. That
conversion is exact for a constant rate and approximate otherwise, and it discards
**settlement timing**: a position closed 10 minutes before an 8-hourly settlement pays
nothing for those 8 hours, while an hourly venue would have charged 7 of them. For short
horizons — the 8h column especially — that discretisation is a material fraction of the
number.

**L5 — Price impact and slippage are estimates, and they do not scale linearly.** They
arrive on the `Quote` sized for a particular notional. When the hedge size differs from the
quoted size by more than 5% the row is flagged `~`, and the engine does **not** attempt to
rescale, because the true depth curve is unknown and a linear extrapolation of impact is
wrong in the direction that flatters large trades. On Avantis, spread is also
**directional and asymmetric** — 1.86 bps to open long versus 2.61 bps to open short at the
same size — so a quote for one side says little about the other.

**L6 — The Avantis maker/taker rate depends on live OI skew and can flip.** Avantis prices
by whether a trade *improves* open-interest skew, not by order type, so the same hedge is
1.0 bps or 4.5 bps depending on which way the book is leaning **at execution time**. Skew
moves. A ranking computed on a maker classification is invalidated if the skew flips before
the order lands. Rows are marked `+`.

**L7 — Avantis charges its closing fee on notional *plus gross PnL*.** The model charges
close fees on flat notional, so a hedge that works costs **more** to close than shown. The
error grows with how far the hedge moved in your favour, i.e. it is largest exactly when the
hedge did its job.

**L8 — Promotional pricing is modelled as if durable, and it is not.** Ondo's 2.5 bps taker
is billed as "50% off" with no published expiry (base 5.0 bps). All 54 Avantis RWA pairs are
at 0 bps under a "growth mode" tied to unstated OI milestones and explicitly revocable. Both
are the current live rate and both can vanish without notice. A ranking built on them is a
ranking with an expiry date.

**L9 — Fee tiers, rebates and builder fees are ignored.** Every figure is the **base tier**.
A high-volume user pays materially less (GRVT Level 9 is 2.4 bps versus 4.5; Pacifica VIP 3
is 2.8 versus 4.0), and a patient user posting maker orders on GRVT earns a rebate at every
tier, which can take the round trip to roughly zero and change the ranking outright. In the
other direction, routing through a third-party frontend can add up to **10 bps per fill** on
GRVT and Ondo, and up to 1% of collateral on Avantis. Ondo's volume tiers are confirmed to
exist but are unpublished and server-supplied per account, so they cannot be modelled at all.

**L10 — Cross-venue netting assumes the assets are actually the same risk.** Netting `BTC`
on GRVT against `BTC` on Pacifica assumes identical underlying exposure. Index and
pre-launch markets, differing oracle sources, and differing mark-price methodologies all
break that assumption to some degree. Basis between two venues' marks is real, is not
modelled, and shows up as tracking error on a nominally flat book.

**L11 — Ondo is mostly not crypto, and market hours are a real cost.** 47 of Ondo's 52
markets are equities, commodities, ETFs and indices, and Avantis RWA pairs follow exchange
hours. Funding **accrues while a market is closed** while the hedge cannot be adjusted, and
reopen gaps can liquidate. The engine has no concept of market hours.

**L12 — Execution is assumed instantaneous and complete.** One price, one fill, no partial
fills, no latency, no failed transactions, no oracle staleness. Real hedges leg in. The
window between the two legs being open is unhedged, and its cost is not modelled.

**L13 — Nothing tax-, accounting- or capital-related is modelled.** No cost of the
additional margin the hedge leg locks up, no collateral haircuts (Ondo credits SPYon/QQQon
at 90% of mark), no withdrawal or bridging costs to move collateral to the hedge venue, and
no tax consequence of realising a hedge. For a small hedge, moving collateral to the venue
can cost more than the fee difference the ranking is optimising.

---

## 7. Reconciliation notes

Changes made to `CONTRACT.md` and items the ingestion agent needs to be aware of.

1. **`CONTRACT.md` §6 carry sign (documented above, §2.1).** The literal formula
   contradicts the §4 sign convention. The engine follows §4 (`borrow − funding`). §6's
   formula should be corrected to match.
2. **`Quote.base_asset`, `VenueError`, `PortfolioSnapshot`** were added to `models.py` and
   are now recorded as required in `CONTRACT.md` §9. The engine falls back to
   `assets.normalize_base(quote.market)` when `base_asset` is empty, so an adapter that
   forgets to populate it degrades rather than breaks.
3. **`hedge_scanner.portfolio` entry points.** The CLI expects
   `build_portfolio(addresses) -> PortfolioSnapshot` and
   `quote_hedges(base_asset, side, notional_usd) -> list[Quote]`, either sync or async, and
   accepts `scan_portfolio` / `fetch_portfolio` and `quote_all` / `get_quotes` as aliases.
   A missing or non-conforming module produces a clear message and exit code 3, never a
   traceback.
4. **`rich` is a runtime dependency** of `render.py` and `cli.py` and needs adding to
   `pyproject.toml`.
