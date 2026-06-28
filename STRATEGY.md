# XAUUSD IFVG Strategy — Technical & Functional Reference

A single-rule ICT/SMC strategy for gold (XAUUSD), event-driven, with the **exact
same logic running in backtest and live**. This document is the complete
technical and functional reference: what it trades, how it decides, how risk is
sized, what every file does, and what is hardcoded.

---

## 1. At a glance

| | |
|---|---|
| **Instrument** | XAUUSD (gold vs USD). Backtest uses the custom FTMO symbol `XAUUSD2020_FTMO`; live can use any broker XAUUSD. |
| **Style** | ICT/SMC — Fair Value Gap (FVG) continuation with an internal liquidity sweep. |
| **Direction** | Long and short. |
| **Setup timeframe** | **H1** — 1-hour Fair Value Gaps are the setup zones. |
| **Trigger timeframe** | **M5** — internal sweep + inverse-FVG entry. |
| **Execution clock** | **M1** — one decision per closed 1-minute bar. |
| **Positions** | One at a time (no overlapping / pyramiding). |
| **Entry order** | **Resting LIMIT at the FVG midpoint** (price must retrace into the gap), valid for 60 min then cancelled. |
| **Risk per trade** | Fixed **0.5%** of equity (drawdown-aware sizing). |
| **Reward:risk** | Fixed **1.5 : 1**, with break-even at 1:1. |
| **Time-stop** | Force-close after **4 h** (intraday cap; more cost-robust than longer holds). |
| **Zone freshness** | Enter a 1h zone only if it is **< 1 h old** when price returns — the edge lives in fresh imbalances (grounding-validated, see §7). |
| **Weekend** | **Flat by 17 h Friday** — no positions held over the weekend (FTMO rule). |
| **Costs modelled** | **Variable per-bar spread** (×1.2) + **overnight swap** + FTMO 0.0014% commission; 1:100 leverage. |
| **Engine** | `backtrader` for backtest; raw MetaTrader 5 API for live. |
| **Data** | Dukascopy ticks (FTMO profile) via Quant Data Manager, loaded as the MT5 custom symbol `XAUUSD2020_FTMO`. |
| **Result (2020–2025, compounding $10k, 0.5%, realistic costs)** | → **$62,474** · PF 1.93 · Sharpe 5.06 · max DD 4.0% · 92% winning months · 0 days over FTMO's 5% daily limit. |
| **Cross-validated** | Independent broker + unseen earlier period (IC Markets 2017–2019, via Darwinex): still strong at FTMO-level cost — PF 1.87, max DD 3.5%, 0 days ≥5%. |

> **One rule ships.** Two earlier rule variants (Judas Swing / continuation) were
> removed after multi-year validation showed they lost money and added
> drawdown; the entire edge lives in this single FVG-continuation rule.

---

## 2. How it operates (the rule, step by step)

For each closed **M1** bar, when **flat**:

1. **Track 1h setup zones.** Every Fair Value Gap that forms on H1 is registered
   as a zone (direction = bull/bear, with its top/bot price edges). A zone is a
   gap left by a strong impulse candle (body ≥ `FVG_BODY_MULT` × the trailing
   20-bar average body).
2. **Wait for a return into a *fresh* zone.** When the current M1 price trades
   back into an unmitigated H1 zone, that zone is *armed once* (never reused) —
   **but only if it is younger than `MAX_ZONE_AGE_H` (1 h)** when price returns. A
   stale zone (the imbalance already absorbed) has no edge and is skipped; this
   freshness rule is grounding-validated (§7).
3. **Require an internal liquidity sweep (M5).** Inside the armed zone, on the
   recent 5-minute window, price must take out a recent 5m swing — a swing low
   for a bull zone, a swing high for a bear zone. This is the manipulation leg.
4. **Place a resting LIMIT at the inverse 5m FVG.** The sweep creates an opposing
   5-minute FVG; a limit order rests at its **midpoint**, **in the direction of
   the H1 zone** (long in a bull zone, short in a bear zone). Price has to retrace
   *into the gap* to fill — the faithful ICT entry. The order is valid for
   `ENTRY_TTL_MIN` (60 min); if price never returns, it is cancelled and the slot
   is freed. (A market entry here filled wherever price was — often several
   dollars from the gap — which corrupted the reward:risk and produced multi-week
   trades; see §5.)
5. **Stop & target.** Stop-loss goes just beyond the swept extreme (plus a small
   buffer); take-profit is at **1.5 × the stop distance**. Because the fill lands
   at the gap, both are anchored to the price actually entered.
6. **Guardrail.** If the resulting stop is closer than `MIN_STOP_PIPS`, the
   signal is rejected (tiny stops blow up risk-based size and tend to whipsaw).

While **in a trade** (managed every M1 bar):

7. **Break-even.** Once price reaches 1:1, the stop is moved to entry.
8. **Time-stop.** If the trade has been open `TIME_STOP_HOURS` (4 h) it is
   force-closed, regardless of price. This caps the *break-even grind* (a trade
   that reaches 1:1 then hovers for hours, tying up the single slot) and keeps the
   hold **intraday**, which suits a low-timeframe FVG method. Shorter holds also
   proved more robust to transaction costs.
9. **Exit.** When the bar's range touches the stop or the target, the position
   is closed with a single market order (see §5 on why this is manual).

Only **H1, M5 and the current M1 bar** are consulted. There is no daily bias,
no higher-timeframe trend filter, no session window, and no news filter.

---

## 3. Risk & money management

* **Fixed-fractional risk.** Position size is computed so that hitting the stop
  loses exactly `RISK_PCT` (0.5%) of *current* equity:

  ```
  lots = (equity × 0.005) / (|entry − stop| × 100 oz)
  ```

  Tight stop → larger size, wide stop → smaller size; the **dollar risk is
  constant**, which is what keeps a funded/prop account inside its drawdown
  limits. Lots are rounded down to the broker's volume step and capped at
  `MAX_LOTS`.
* **Equity is tracked from realised P&L** (not the broker's leveraged
  `value()`), so sizing and the reported drawdowns are deterministic.
* **Two drawdown views are reported:** total max drawdown (peak-to-trough of the
  realised equity curve) and **daily drawdown** — the worst intraday equity loss
  below the day's opening realised balance, which is the metric FTMO enforces
  (~5%/day). The report also counts days that breached 5%.

**Two-phase risk (prop-firm playbook).** `RISK_PCT` (0.5%) is the *funded*-phase
setting — the funded account is the asset, so it is sized conservatively to never
breach FTMO's limits (validated: max DD 7.4%, 0 days over 5% across 5+ years). For
the *challenge* phase the asset is just the (refundable) entry fee, so a higher
`--risk-pct` can be worth it to reach +10% faster, accepting that some attempts
blow up.

A Monte-Carlo challenge simulation (`challenge_sim.py`, 20k day-block-bootstrap
attempts of FTMO's **two phases** — P1 +10%, P2 +5%, both with −10% total / −5%
daily — on the current locked config) quantifies the tradeoff:

| Risk | P(funded)/challenge | Time to fund | Loses 1st try | Fee spend |
|---|---|---|---|---|
| **0.50%** | ~100% | ~22 wk | ~0% | $540 |
| **1.00%** | ~100% | **~11 wk** | ~0% | $541 |
| 1.50% | 98% | ~8 wk | ~2% | $550 |
| 2.00% | 92% | ~6 wk | ~8% | $589 |

Because the worst day is ~1.65% and max DD ~4%, the −5% daily / −10% total limits
are **almost never hit** below ~1.5% risk — so the pass rate is near-certain and
**the lever is speed, not survival** (only at 2% do fails appear: ~3% daily, ~2%
total). **Recommended: 1.0% for the challenge** (near-certain funding in ~11 weeks,
2× faster than 0.5%), then **drop back to 0.5% once funded**.

> These pass rates use the **backtest** R-distribution and are an **upper bound** —
> live the edge is thinner (cost/fills), which lowers EV, the pass rate and the
> speed. Treat the *relative* picture (more risk = faster, more fails) as robust,
> the absolutes as optimistic. Per-trade economics: gross EV $36.87, cost $6.84,
> **net EV $30.04 = 0.192 R** (you net ~19% of the amount risked, per trade).

---

## 4. File structure — what each file does

```
mt5/
├── README.md             ← project overview / quickstart
├── STRATEGY.md           ← this document
├── DEPLOY.md             VPS deployment guide (MT5 setup, NSSM, monitoring)
│
├── ifvg/                 ← framework-agnostic strategy logic (no backtrader / no MT5*)
│   ├── __init__.py       package overview
│   ├── signals.py        DETECTION: find_swing_points(), detect_fvg()
│   ├── entry_logic.py    THE RULE: generate_signal() + fixed constants
│   ├── position.py       RISK: risk_lots(), break-even, swap + fixed constants
│   └── mt5_feed.py       MT5 access: ticks→OHLC for backtest, live buffers (*imports MT5)
│
├── strategy.py           backtrader bridge: IFVGStrategy (feeds, sizing, manual exits, costs, DD)
├── run_backtest.py       backtest entry point → backtest_<date>/ (csv + equity/monte_carlo/concentration.png)
├── run_live.py           live/paper runner (same rule, real MT5 orders; auto-reconnect)
├── run_live.bat          24/7 auto-restart wrapper for the live runner
│
└── analysis/             research & validation tools (run from the repo root)
    ├── validate_perturbation.py     structural perturbation-survival (overfit test)
    ├── sweep_zone_age.py            zone-freshness cutoff sweep (validates MAX_ZONE_AGE_H)
    ├── analyze_grounding.py         edge by session + signal-feature quartiles
    ├── analyze_adverse_selection.py bounds the limit-fill adverse-selection haircut
    └── challenge_sim.py             FTMO 2-phase challenge Monte-Carlo (pass rate/time/fees)
```

All run outputs land under `backtest/` (date-stamped runs + named cross-vals); that
folder, the tick cache (`.bar_cache/`) and logs are regenerated artifacts and are
git-ignored (see `.gitignore`).

**Detail per module:**

* **`ifvg/signals.py`** — pure market-structure primitives. `find_swing_points`
  marks local swing highs/lows (used for the M5 internal sweep). `detect_fvg`
  finds 3-candle Fair Value Gaps with an impulse-strength filter and marks when
  each gap is later mitigated. Stateless and look-ahead-safe.
* **`ifvg/entry_logic.py`** — the IFVG rule as one incremental function,
  `generate_signal(now, bar, buf, state)`, returning at most one signal per bar.
  Tracks 1h zones across bars in `state`. **All tunables are fixed constants at
  the top of the file.**
* **`ifvg/position.py`** — `risk_lots` (fixed-fractional sizing) and the
  break-even helpers, shared identically by backtest and live. Money-management
  constants (`RISK_PCT`, `MAX_LOTS`, etc.) live here.
* **`ifvg/mt5_feed.py`** — the only module that imports `MetaTrader5`. Builds
  M1/M5/H1 candles from **ticks** (`copy_ticks_range`) for the backtest and
  rolling buffers for live. Tick-based because the FTMO custom symbols expose
  ticks but not a synced M1 rate series.
* **`strategy.py`** — adapts the rule to backtrader: reads the three feeds,
  calls `generate_signal`, sizes via `risk_lots`, places a **resting limit entry**
  at the FVG (cancelled after 60 min if unfilled), and **manages exits manually**
  (SL/TP/break-even/time-stop checked each bar, closed with one market order).
  Records trades, equity and daily drawdown. Caches the rolling buffers and only
  rebuilds/rescans them when a 5m/1h bar closes (big speedup, exact).
* **`run_backtest.py`** — pulls history from MT5 (cached to `.bar_cache/`), wires
  backtrader, runs, prints a rich report (risk-adjusted metrics, alpha/beta vs
  gold, Monte Carlo) and writes the CSVs and chart. Fixed realism (1:100 leverage,
  FTMO 0.0014% commission, real spread ×1.2).
* **`run_live.py`** — polls MT5 once per new M1 bar, runs the same rule, rests a
  **pending limit** entry at the FVG (with expiration), and on the open position
  applies the break-even rule and the 4 h time-stop — identical behaviour to the
  backtest.

---

## 5. Execution model

* **Entries are resting LIMIT orders at the FVG midpoint**, valid for
  `ENTRY_TTL_MIN` (60 min). Price must retrace into the gap to fill, so the fill
  matches the level the stop/target/sizing were built from. *Why this matters:* a
  market entry fills wherever price happens to be when the signal fires — often
  several dollars from the planned gap — while the stop/target stay anchored to
  the gap. That corrupts the realised reward:risk (median was fine but a long tail
  ran to 70:1) and produced trades that lasted **weeks**. The limit fixes it; the
  4 h time-stop then caps the residual break-even grind.
* **Exits are managed manually**: each bar the stop/target/time-stop are checked
  and, when hit, the position is closed with one market order (filling at the next
  bar's open). This is deliberate — resting Stop+Limit orders (even OCO) can both
  fill inside one bar after break-even and flip the position into an unprotected,
  stale state. A single market close cannot double-fill. The cost is slightly
  pessimistic fills (next open vs exact SL/TP).
* **backtrader order matching.** Two gotchas were fixed: (1) `notify_order`
  receives a *clone* of the order, so identity checks (`order is entry_order`)
  never match — orders are matched by `order.ref`; without this, a cancelled
  limit never freed the slot and the strategy stalled. (2) The broker's `valid`
  expiry proved unreliable, so the 60-min entry timeout is enforced manually.
  `slip_limit=False` so a limit fills at its price or better, never worse.
* **Costs.** Three components, all charged honestly:
  * **Spread — variable, per bar.** The real spread is measured from the ticks for
    *every* M1 bar (median ≈ $0.36) and the limit **entry is a maker** (no spread);
    the **market exit is a taker** and pays half the spread *of its own exit bar*,
    **×1.2** as a cushion. This is news-aware: exits in a wide-spread minute cost
    more than in a calm one — not a single flat constant. (No broker slippage is
    used; the spread is deducted explicitly per trade so it is never double-counted.)
  * **Swap — overnight financing.** Trades open across the broker's rollover pay a
    flat `$SWAP_USD_PER_LOT_PER_NIGHT` per lot per night (triple Wed→Thu), modelled
    as a cost on both sides. ~10% of trades roll over; total ≈ −$520 over 5 years
    (the Friday-flat rule keeps this small).
  * **Commission** — FTMO's **0.0014%** of notional per side.
  NB on cost sensitivity: the cost is a meaningful slice of a modest edge — $6.84
  per trade = **19% of the $36.87 gross EV**. The edge is robust to spread
  *variation* (per-trade outcome is flat across spread quartiles — a spread filter
  does nothing), but sensitive to the cost *level*: scaling the whole cost up gives
  PF 1.93 (×1) → **1.65 (×2)** → 1.42 (×3). So execution quality is the single
  biggest live risk (see §9) — not spread spikes, but the average level of
  slippage and especially limit-fill adverse selection.
* **Leverage.** 1:100 — required, or every order is margin-rejected (1 lot of
  gold ≈ $200k notional).
* **Live.** SL/TP are attached to the order; the break-even rule modifies the
  position's stop on each new bar.

---

## 6. Fixed parameters (hardcoded — not run-script options)

These were validated across 2020–2025 and are intentionally not exposed. Change
them in the source if you ever re-tune.

| Constant | Value | Where | Meaning |
|---|---|---|---|
| `RR` | 1.5 | entry_logic | take-profit = 1.5 × stop distance |
| `ENTRY_ANCHOR` | "mid" | entry_logic | where the limit rests in the FVG (mid / proximal / distal) |
| `ENTRY_TTL_MIN` | 60 | entry_logic | cancel the resting limit if unfilled after this long |
| `FVG_BODY_MULT` | 1.0 | entry_logic | 1h impulse filter (main activity knob) |
| `H1_BUF` | 60 | entry_logic | recent 1h candles scanned for zones |
| `M5_SWEEP_BARS` | 25 | entry_logic | recent 5m window for sweep + entry FVG |
| `M5_BODY_MULT` | 1.1 | entry_logic | 5m entry-FVG impulse filter |
| `M5_MIN_GAP_PIPS` | 0.3 | entry_logic | minimum 5m entry-FVG width |
| `MIN_STOP_PIPS` | 5.0 | entry_logic | reject signals with a tighter stop |
| `MAX_ZONE_AGE_H` | 1.0 | entry_logic | **freshness filter** — only enter a 1h zone younger than this (grounding-validated) |
| `ZONE_TTL_DAYS` | 7 | entry_logic | forget untouched zones after this |
| `RISK_PCT` | 0.005 | position | 0.5% of equity risked per trade |
| `TIME_STOP_HOURS` | 4 | position | force-close a trade after this long (intraday cap) |
| `FRIDAY_FLAT_HOUR` | 17 | position | flat after this server-hour on Fridays (no weekend hold) |
| `MAX_LOTS` | 5.0 | position | hard cap on a single position |
| `SL_BUFFER` | 3.0 pips | position | buffer beyond the swept extreme |
| `OZ_PER_LOT` | 100 | position | XAUUSD contract size |
| `LEVERAGE` | 100 | run_backtest | broker leverage |
| `COMMISSION_PCT` | 0.0014% | run_backtest | FTMO commission, % of notional/side |
| `SPREAD_MULT` | 1.2 | run_backtest | spread cushion (applied to the per-bar spread) |
| `SWAP_USD_PER_LOT_PER_NIGHT` | 6.0 | position | overnight financing, per lot per night (triple Wed→Thu) |

Operational settings that **remain configurable**: symbol, date range, starting
cash, output folder, MT5 connection, and (live) demo/real guard.

---

## 7. Validation (out-of-sample, every year profitable)

Full 2020–2025 run, **one compounding $10,000 account**, final config (limit
entry, **1 h zone-freshness filter**, **4 h** time-stop, **17 h-Friday flat**, 0.5%
risk, **realistic costs**: variable per-bar spread ×1.2 + overnight swap + 0.0014%
commission). **$10,000 → $72,474** (net +$62,474) — CAGR 52%, PF 1.93, Sharpe 5.06,
Sortino 10.24, max DD 4.0%, worst day 1.65%, **0 days over FTMO's 5% daily limit**,
**92% winning months**, return skew +0.93, expectancy $30/trade.

| Year | Trades | Net P&L | Profit Factor | Win rate | Max DD |
|---|---|---|---|---|---|
| 2020 | 373 | +$3,150 | 1.75 | 51% | 3.1% |
| 2021 | 367 | +$4,669 | 1.80 | 55% | 2.7% |
| 2022 | 393 | +$8,885 | 2.03 | 56% | 4.0% |
| 2023 | 340 | +$11,805 | 2.03 | 56% | 2.3% |
| 2024 | 410 | +$25,133 | 2.28 | 56% | 2.4% |
| 2025 H1 | 197 | +$8,832 | 1.50 | 52% | 3.6% |

Profitable **every year** (6/6), PF 1.50–2.28, max DD ≤ 4.0% in every year. The 1 h
freshness filter lifts PF from 1.52 to 1.93 and cuts max DD from 5.3% to 4.0% vs
the no-filter config (§ "Zone freshness" below) while removing the
negative-expectancy stale-zone trades.

**Market-neutral.** Beta to buy-and-hold gold ≈ **0.00** (correlation ≈ 0): the
+42%/yr is **alpha**, not a long-gold tilt — the short holds capture the post-FVG
bounce in both directions.

**Walk-forward (the key overfitting test).** Swept the time-stop separately on
in-sample 2020–2022 and out-of-sample 2023–2025: *shorter is better in BOTH halves
independently* (not a period artefact). IS picks 2 h, OOS picks 4 h — both short;
the IS-chosen stop applied blind to OOS gives PF 1.60. **4 h is the OOS optimum and
strong in-sample (PF 1.32),** so it was locked as a principled intraday cap on the
stable part of the curve (the 2–3 h "maximum" is a noisy peak we deliberately did
not chase).

**Monte Carlo (10k resampled trade sequences).** Bootstrap (resample with
replacement) return p5/50/95 = 441/624/867%; max-drawdown p50/95/99 = 3.8 / 5.5 /
6.6%. **P(max DD > 10%) = 0%**, P(losing over the period) = **0%**. The i.i.d.
resampling is valid here: trade-to-trade autocorrelation is ≈ 0 at every lag (no
streak structure), so a block bootstrap would add nothing. Both the order-only
reshuffle (degenerate return, drawdown-only) and the with-replacement bootstrap
are reported; `monte_carlo.png` plots the equity-path fan and both distributions
with the FTMO-10% line.

**Trade efficiency (MFE/MAE).** From each trade's max favourable/adverse excursion
(`mfe_mae.png`): edge ratio (avg MFE / avg MAE) = **2.6**, winners take only **0.17 R**
of heat before resolving (well-timed entries, ample stop buffer), and ~**62%** of the
favourable move is captured (the uncaptured part is the known cost of the break-even
rule). Losers cluster at −1 R (the stop); the break-even mechanism is visible as the
MFE=1 R cutoff on losers. Equity-curve linearity (LR correlation) = **0.95**.

**Deflated Sharpe.** Adjusting the Sharpe for how many configurations were tried
(Bailey & López de Prado): PSR(true Sharpe > 0) = 100%, and the **DSR stays > 99%
even assuming 1,000 trials** — the result is not an artefact of multiple testing.

**Edge concentration (broad, not a few lucky trades / one regime).** Gini of the
winners = 0.55 (top 5% of trades = 35% of gross profit — moderate, normal for a
continuation method). Removing the single best month leaves PF 1.91; **removing the
entire best year (2024, ~40% of profit) still leaves PF 1.78** over the other 4.5
years. **Both sides are profitable** (long PF 2.09, short PF 1.74). `concentration.png`
plots the Lorenz curve, net by hour and net by side.

**Perturbation survival — execution.** Jittering every fill by ±$0.10/oz of
Gaussian noise (5,000 runs) leaves the result essentially unchanged (return
622/625/627%, **P(profit)=P(PF>1.2)=P(DD<10%)=100%**): random fill noise washes out
over 2,000+ trades, so the edge is not knife-edge on exact executions. A
*systematic* adverse shift ($0.10/oz worse on both sides) costs ~14% of the return
but still holds (PF 1.75, max DD 6.0%) — the honest sensitivity to persistently
worse fills.

**Perturbation survival — structure (overfit test).** Re-running the full engine
with each fixed signal constant nudged (`FVG_BODY_MULT`, `M5_BODY_MULT`,
`M5_MIN_GAP_PIPS`, `MIN_STOP_PIPS`, `RR`, `ENTRY_ANCHOR`, time-stop): **14/14
perturbations stay profitable with PF ≥ 1.2** (PF range 1.45–1.61, measured on the
pre-freshness-filter config — the filter only raises these), degrading gracefully
in every direction. The edge is not balanced on one exact parameter value. (Run it
with `python analysis/validate_perturbation.py`.)

**Zone freshness — partial mechanistic grounding (and a validated improvement).**
A pure price-pattern has no "why"; to look for one, the signal now logs per-trade
features (sweep depth, FVG width, **zone age**) and `analyze_grounding.py` buckets
the risk-normalised edge by each. The striking, **cross-validated** result is zone
age: the edge lives almost entirely in **fresh** zones and **decays to negative**
in stale ones — and this replicates near-identically on the independent IC Markets
data:

| 1h-zone age at entry | Dukascopy 2020–25 | IC Markets 2017–19 |
|---|---|---|
| < 0.5 h (fresh) | PF **3.31** (meanR 0.34) | PF **3.09** (meanR 0.33) |
| 0.9–5 h | PF ~1.0 | PF ~0.75 |
| > 5 h (stale) | PF **0.78** (loses) | PF **0.76** (loses) |

This is exactly what the ICT mechanism predicts (a fresh imbalance reacts, a
mitigated one does not), and it replicates out-of-sample — so it is real, not a
sample artefact. (By contrast the narrative's "deeper sweep = stronger" is
**refuted** in both sources: shallow sweeps fare better.) A sweep of the freshness
cutoff (`sweep_zone_age.py`) on **both** sources is monotonic — tighter = higher PF,
lower DD, every year still profitable — so **`MAX_ZONE_AGE_H = 1 h` was locked**:
it raises PF 1.52→1.93, cuts max DD 5.3→4.0% and *raises* net by dropping the
negative-expectancy stale trades. Picked by robustness across both sources (not the
backtest peak); 0.5 h scores higher PF but halves the trade count.

**Cross-validation on an independent source + unseen period.** The whole strategy,
unchanged, was re-run on **IC Markets** gold ticks **2017–2019** (via Darwinex) — a
*different broker's liquidity* and a period **before** any data used to build it.
**Normalised to the same FTMO-level spread** (`--spread-floor 0.36`) it gives
**PF 1.87, +118% (CAGR ~40%), max DD 3.5%, 0 days ≥5%, both sides profitable**. The
pattern generalises across broker *and* time, not just Dukascopy 2020–2025.

| Test | Source / period | Spread | Net | PF | Max DD |
|---|---|---|---|---|---|
| Primary | Dukascopy 2020–2025 | $0.44 (FTMO) | +$62,474 | 1.93 | 4.0% |
| **Cross-val (FTMO cost)** | IC Markets 2017–2019 | $0.43 (floored) | **+$11,848** | **1.87** | **3.5%** |

**The honest caveat — cost sensitivity.** The per-trade edge is still modest
relative to costs. At the modelled cost the PF is 1.93; the freshness filter
widened the margin, but a systematic cost increase still compresses it (the
pre-filter cross-val dropped from PF 1.58 at IC Markets' raw $0.14 spread to 1.31
at FTMO-level $0.43). So real-world performance hinges on execution quality, and
the live demo (§9) is the decisive test — expect live to run below backtest.

**The edge is gold-specific.** A multi-instrument variant was tested on EURUSD
(full 2020–2025): it did not transfer — profit factor ~1.0–1.1, two losing years,
and drawdowns over 10%. The strategy is therefore hardcoded for XAUUSD. A
"no night trading" session filter was also tested and removed: it is not an FTMO
rule and it gutted the edge (much of gold's edge is in overnight setups).

---

## 8. How to run

```bash
# install
pip install backtrader MetaTrader5 pandas matplotlib

# backtest (MT5 terminal must be open + logged in, with tick history)
python run_backtest.py                                   # XAUUSD2020_FTMO, 2024
python run_backtest.py --start 2020-01-01 --end 2025-06-27
python run_backtest.py --validate                        # fast 1-month smoke test
python run_backtest.py --risk-pct 0.02                   # higher risk (challenge phase)
python run_backtest.py --time-stop-h 0                   # disable the time-stop
python run_backtest.py --symbol XAUUSD2017_ICMARKETS --start 2017-01-01 \
       --end 2020-01-01 --spread-floor 0.36 --out backtest/crossval  # cross-validation
python analysis/validate_perturbation.py                 # structural overfit test
python analysis/challenge_sim.py                          # FTMO challenge odds

# live / paper (refuses a real account unless --allow-real)
python run_live.py --symbol XAUUSD --risk-pct 0.01    # 1% for a challenge
# 24/7 on a VPS: use run_live.bat (auto-restart) — see DEPLOY.md
```

Backtest outputs go to a date-stamped folder `backtest/<today>` (a second run the
same day is `backtest/<today>_1`, `_2`, …; override with `--out`): `trades.csv`
(with per-trade `spread_usd`/`swap_usd` columns), `equity.csv`, `monthly.csv`,
`equity_curve.png` (balance + drawdown, with a side panel of every metric —
P&L/CAGR/PF, Sharpe/Sortino/Calmar/maxDD, durations, alpha/beta vs gold),
`monte_carlo.png` (sample equity paths + return and drawdown distributions with the
FTMO-10% line), `concentration.png` (Lorenz curve + net by hour + net by side),
`mfe_mae.png` (max favourable/adverse excursion vs profit, in R — stop/target
efficiency), `returns_heatmap.png` (year × month returns %), `seasonality.png`
(entries & net P&L by hour / day-of-week / month), `trade_examples.png` (a grid of
example trades on 5m candles — FVG entry band, SL/TP, entry/exit), and a printed
report with the monthly breakdown, the daily drawdown, the cost breakdown, the
concentration stats and the fill-perturbation survival.

**Speed / caching.** The slow part is pulling ~250M ticks from MT5 and rebuilding
the bars. Built bars are cached to `.bar_cache/` (keyed by symbol + range), so the
first run for a range takes a few minutes and every rerun loads in seconds. With
the cache warm a full 2020–2025 backtest runs in **~7 minutes** (the strategy also
caches its rolling buffers and skips the 1h FVG re-scan on bars where no 1h candle
closed). Pass `use_cache=False` to `fetch_all_range` to force a refresh.

**Operational knobs** (everything else is hardcoded — see §6): `--symbol`,
`--start` / `--end`, `--cash`, `--risk-pct` (raise for a funding challenge, then
drop back to 0.5% once funded), `--time-stop-h`, `--friday-flat-h`, `--spread-floor`
(normalise a tight feed to a realistic cost for cross-source comparison), `--out`,
MT5 connection.

---

## 9. Caveats

* **Cost sensitivity is the #1 live risk — but it is a *level*, not a *variation*,
  effect.** The cost is $6.84/trade = 19% of the $36.87 gross EV. The edge is
  **robust to spread variation** — per-trade outcome is flat across spread
  quartiles, even news-spike bars (Spearman ≈ 0), so a "trade only when spread < X"
  filter does nothing. What bites is the cost *level* applied to every trade:
  scaling the whole cost gives PF 1.93 (×1) → 1.65 (×2) → 1.42 (×3). So if real
  execution runs worse than modelled — higher average slippage on the market exits,
  and above all **adverse selection on the limit entries** (not spread spikes) — the
  edge thins. **Discount the backtest and treat the demo as the real test.**
* **Optimistic limit fills (adverse selection) — bounded.** The backtest assumes a
  limit fills whenever price *touches* the level. Live, you tend to fill the
  continuations (losers) and miss the clean tag-and-bounce (winners). It can't be
  *measured* pre-live, but `analyze_adverse_selection.py` *bounds* it from the
  per-trade fill penetration: the mechanism is real but mild — shallow "clean tag"
  fills win more (PF 2.37) than deep ones (PF 1.52), yet even deep fills stay
  profitable. Bracketing it (drop the shallowest fills you'd most likely miss) puts
  the **per-trade edge at PF ~1.6–1.7 / net-EV haircut ~15–20%** (PF stays ≥1.5 even
  under aggressive assumptions). The larger live cost is on **volume** — missing
  fills means fewer trades, so slower funding and lower absolute return, more than a
  per-trade-quality hit. Penetrations are tiny (median $0.08, <¼ of the spread), so
  the real fill outcome is queue-dependent — only the live demo settles it.
* **Data source — single asset, now two brokers.** The primary data is Dukascopy
  ticks (FTMO profile, via Quant Data Manager) loaded as the custom symbol. The
  single-source risk is **substantially addressed**: the strategy was cross-
  validated unchanged on **IC Markets** ticks **2017–2019** (different broker, a
  period it never saw) and stays profitable and FTMO-safe at matched cost (§7). It
  is still **one asset (gold)** — the edge is gold-specific. The only fully
  forward, never-modelled test remains a live demo run (`run_live.py`).
* **No economic rationale (but partial mechanistic grounding).** This is a
  price-pattern (ICT/FVG) edge, not a risk premium — there is no structural "who
  pays you", so it can decay or change with regime; monitor it live. It is *not*
  a blind pattern, though: the edge behaves as the mechanism predicts (it lives in
  **fresh** imbalances) and that behaviour **replicates** on independent data (§7),
  which is meaningfully better grounding than "a pattern that happens to work".
* **Limit entries skip setups.** Only signals where price retraces into the gap
  within 60 min fill (~half of them); the rest are cancelled. That is by design —
  fewer but cleaner trades — but it does make the strategy lower-frequency than the
  old market-entry version and dependent on the fill actually happening live.
* **Pessimistic manual fills.** Exits fill at the next bar's open, not exactly at
  SL/TP — robust, but it makes backtest results slightly conservative.
* **Regime sensitivity.** The 1h impulse filter is relative to local volatility;
  `FVG_BODY_MULT = 1.0` keeps activity consistent across calm and volatile years,
  but the rule is, by design, an FVG-continuation method and will behave like one.
* **Tick history required.** Backtests are rebuilt from MT5 ticks; you need the
  symbol's tick history loaded in the terminal (custom symbols are populated from
  Quant Data Manager exports — Dukascopy for the primary symbol, Darwinex/IC
  Markets for the cross-validation symbol).
