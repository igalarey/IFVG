# CLAUDE.md

Two parts: **A — general coding behaviour** (how to work on any task in this repo) and
**B — trading-bot methodology** (the domain playbook, and the base for future bots).

---

# Part A — Behavioural guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## A1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## A2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## A3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## A4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# Part B — Trading-bot playbook

A distilled, **strategy-agnostic** foundation for building algorithmic trading bots,
extracted from the IFVG XAUUSD project. Copy this file as the seed `CLAUDE.md` of a
new bot repo. It is the *methodology* — the specific rules of any one strategy live
in that project's `STRATEGY.md`.

The golden rule running through everything: **be honest, not optimistic.** A backtest
is a hypothesis, not a result. The only verdict that counts is forward/live data.
Your job is to find reasons the edge is *fake* before the market does.

---

## 1. Mindset (read this first)

- **Overfitting / self-deception is the #1 enemy**, not a weak backtest. Every knob
  you tune, every config you compare, spends statistical credibility.
- **Lock the config by a robustness rule, not the backtest maximum.** Pick a value on
  the stable, monotonic part of a curve — never the noisy peak. If two independent
  measurements (e.g. time-stop and a freshness filter) both point the same way, that's
  signal; a lone spike is overfit.
- **Costs and fills are the real live risk**, almost always more than the edge being
  "wrong". Model them honestly and assume live is worse.
- **Separate two different questions:** *is the edge real?* (answered by cross-
  validation on independent data) vs *will it persist?* (answered by an economic/
  mechanistic rationale — usually absent in pattern strategies, so monitor live).
- **State caveats up front, not hidden.** If a test is a no-op, a metric is misleading,
  or a result is an upper bound — say so plainly. "I'd rather tell you than slip it past
  you."
- **The forward demo is the judge.** Build toward it; don't keep re-optimizing the
  backtest in its place.

---

## 2. The architecture pattern (the single most reusable idea)

**Write the strategy rule once, as a framework-agnostic pure function, and share it
between backtest and live.** No divergence, no "it worked in backtest but the live
code is different".

```
rule(now, bar, buffers, state) -> signal | None      # knows nothing about the engine
    backtest:  backtrader next()   -> rule(...)
    live:      MT5 poll loop        -> rule(...)       # identical call
```

Layered package (rename to your strategy):
- `signals.py`   — pure detection primitives (patterns, swings, levels). Stateless,
                   look-ahead-safe. No engine, no broker imports.
- `entry_logic.py` — THE rule as `generate_signal(...)` + **all tunables as FIXED
                   module constants** at the top. Returns `{direction, entry, sl, tp, ...}`.
- `position.py`  — risk sizing, break-even, swap; shared by backtest and live so they
                   size and protect trades identically.
- `feed.py`      — the ONLY file that imports the broker/data API (e.g. MetaTrader5).
- `strategy.py`  — the backtrader bridge (feeds → rule → sizing → manual exits → metrics).
- `run_backtest.py`, `run_live.py` — thin entry points. Only operational settings are
  configurable (symbol, dates, cash, risk, connection); the strategy itself is fixed.

Keep research/validation scripts in `analysis/` (with a `sys.path` bootstrap to the
repo root if they import the core).

---

## 3. Repo layout template

```
bot/
├── README.md            overview + quickstart
├── STRATEGY.md          full technical & functional reference for THIS strategy
├── DEPLOY.md            24/7 VPS deployment guide
├── CLAUDE.md            this playbook
├── .gitignore           ignore artifacts: outputs/, .cache/, __pycache__/, *.log
├── <core>/              framework-agnostic rule (signals, entry_logic, position, feed)
├── strategy.py          backtrader bridge
├── run_backtest.py      → backtest/<date>/ (csv + charts + printed report)
├── run_live.py          live runner (same rule, real orders, hardened)
├── run_live.bat         24/7 auto-restart wrapper
└── analysis/            validation tools (run from repo root)
```

Outputs, tick caches and logs are **regenerated artifacts → git-ignore them.** Only
code + docs get committed.

---

## 4. Data & execution model

- **Build bars from ticks**, don't trust a broker's pre-built rate series (for custom/
  prop symbols it is often unsynced). Ticks give accurate per-bar high/low — which is
  what actually decides whether SL/TP was touched.
- **Cache built bars to disk** (pickle, keyed by symbol+range+timeframes). The tick
  fetch + bar build is the slow part; caching turns a ~45-min run into minutes.
- **Decide per closed bar, not per tick.** The structure is bar-based; intra-bar the
  setup doesn't change. A resting **limit** entry fills at tick precision intrabar
  anyway, so you get fill accuracy without a tick loop.
- **Entries = resting LIMIT at the level**, not market. A market fill lands wherever
  price is (often far from the planned level) while SL/TP stay anchored to the level —
  this corrupts the realized reward:risk and spawns monstrous trades. A limit fills at
  the level the stops were built from. Give it a TTL and cancel if unfilled.
- **Exits = ONE market close per bar**, checked manually (SL/TP vs bar high/low, break-
  even, time-stop). Resting OCO stop+limit pairs can BOTH fill inside one bar (esp.
  after break-even) and flip the position into an unprotected, stale state. A single
  close cannot double-fill. Cost: fills at next bar's open (slightly pessimistic, fully
  robust).
- **Track equity from realized P&L**, not the leveraged broker `value()` — deterministic
  sizing and drawdowns.
- **Two drawdown views:** total (peak-to-trough) and **daily** (worst intraday loss vs
  the day's opening balance) — the latter is what prop firms enforce.

---

## 5. Cost modeling (do it honestly — it decides live viability)

- **Spread is variable, not a constant.** Measure it per bar from the ticks. A resting
  limit is a *maker* (pays ~no spread); a market exit is a *taker* (pays half-spread of
  its own bar). News-aware by construction. Add a cushion (e.g. ×1.2).
- **Don't double-count.** Either use the broker's slippage OR deduct spread explicitly
  per trade — not both.
- **Swap/financing** for any trade held over the broker rollover (triple one weekday).
- **Commission** per side.
- **Cross-source spread normalization.** Different brokers have very different raw
  spreads (an ECN can be ~3× tighter than a prop firm). To compare a strategy across
  data sources fairly, floor/normalize the spread to a common realistic level — else
  the cheaper feed looks better for the wrong reason.
- **Cost-sensitivity is usually the dominant live risk.** Quote the edge as a fraction
  of cost (e.g. "cost = 19% of gross EV") and scale cost ×2/×3 to see how PF compresses.
- **Variation vs level:** an edge can be *robust to spread variation* (a "trade only
  when spread < X" filter does nothing) yet *sensitive to the cost level* (worse average
  execution thins it). They are different axes — don't conflate them.

---

## 6. Engine & broker gotchas (hard-won)

**backtrader**
- `notify_order` receives a **CLONE** of the order → `order is self.entry_order` is
  ALWAYS false. **Match by `order.ref`.** (Symptom of missing this: cancelled limits
  never free the slot and the strategy silently stalls — a full run gives 0 trades while
  one month gives 2.)
- The order `valid=` expiry is unreliable → enforce the entry TTL manually in `next()`.
- A limit **never fills worse than its price**, so "adverse limit fill" stress via
  `slip_limit` is a no-op — model fill risk a different way (see adverse selection).
- **Leverage is required** or every CFD order margin-rejects (1 lot of gold ≈ $200k
  notional). Leverage affects margin only, not P&L — so 1:25–1:100 give identical
  results as long as nothing is margin-rejected.

**MT5 / data**
- Prop "custom" symbols often expose ticks (`copy_ticks_range`) but not a synced rate
  series — build from ticks.
- Watch for hardcoded price-band filters when generalizing across instruments (a gold
  `1000<price<5000` guard silently drops every EURUSD tick).
- Account currency ≠ quote currency slightly skews realized risk (e.g. EUR account,
  USD prices → real risk ~0.45% not 0.5%).
- No economic-calendar API in MT5 Python — a news filter needs an external dataset.

---

## 7. The validation battery (what to run, and what each actually tests)

Run these in roughly this order. Each answers a *different* question; together they make
the difference between "a pretty backtest" and "a defensible strategy".

| Test | Question it answers |
|---|---|
| **Per-year consistency** | Is it profitable *every* year, or carried by one regime? |
| **Walk-forward (IS/OOS)** | Does the chosen parameter generalize out-of-sample, in both halves independently? |
| **Deflated Sharpe / PSR** (Bailey & López de Prado) | Does the Sharpe survive correcting for how many configs you tried? |
| **Monte Carlo** (reshuffle + bootstrap) | Path/sequence risk: P(maxDD>limit), P(loss). First **check trade autocorrelation** — if ≈0, i.i.d. bootstrap is valid; if not, use a block bootstrap. |
| **Concentration** | Is the edge broad or a few lucky trades? Gini, remove best month/year, split by side and by hour. **Risk-normalize to R** — $ concentration is inflated by compounding. |
| **Perturbation — structural** | Re-run with each fixed constant nudged. Robust = all survive & degrade gracefully; overfit = a knob collapses it. |
| **Perturbation — execution (fills)** | Random fill jitter (washes out over many trades) + a systematic adverse shift (the honest sensitivity). |
| **Cross-validation** | Re-run unchanged on a **second broker's data AND an unseen earlier period.** The strongest test — replication across source *and* time. Normalize costs first. |
| **Cost-sensitivity** | Scale total cost ×N; where does the edge die? |
| **Adverse-selection bound** | Limit fills can't be *measured* pre-live, but bound them: log per-trade fill penetration, then "only fill if price penetrates ≥ buffer" (drops the clean taps you'd miss live). |
| **MFE/MAE** | Stop/target efficiency: edge ratio, heat winners take, % of favourable move captured. |
| **Challenge simulation** (if prop-firm) | Day-block-bootstrap the 2-phase rules with the fee; pass rate, time-to-fund, fees. |

Reporting helpers worth building once and reusing: equity curve with a metrics panel,
returns heatmap (year×month), seasonality (by hour/day/month), example-trade charts,
and per-trade EV (gross → cost → net, in $ and in R).

---

## 8. Grounding & overfitting discipline

- **Instrument per-trade features** (the setup's measurable properties) and bucket the
  **risk-normalized** edge by each. If the edge concentrates where the theory predicts
  (e.g. fresher/bigger signal = stronger) *and that replicates on independent data*,
  you have partial mechanistic grounding — much better than "a pattern that works".
- A relationship that is **monotonic + cross-validated** is a real, exploitable lever
  (worth a rule-based filter). A **sharp in-sample peak that doesn't replicate** is
  overfit (e.g. an entry-anchor that scores +43% but was never reproduced — do NOT
  chase it).
- **Beware look-ahead features.** A variable that's dramatic but is actually an *outcome*
  (e.g. "the trade reached break-even") cannot be a pre-trade filter — it knows the
  future. The classic meta-labeling trap.
- **Prefer a simple rule to ML on small data.** ~thousands of trades is tiny for ML and
  explodes researcher degrees of freedom. The only defensible ML here is **meta-labeling**
  (a filter on top of the existing rule, with purged/embargoed CV) — and only *after* the
  base rule is proven live.

---

## 9. Live hardening (production)

- **Same rule as backtest** (the agnostic core) — this is non-negotiable.
- Auto-reconnect, **per-iteration try/except** (one bad tick must not kill the bot),
  startup wait-for-terminal, hourly heartbeat in the log.
- Restart wrapper (`.bat` loop or NSSM service); RDP **disconnect, don't log off.**
- **Demo-account guard** — refuse to run on a real account without an explicit flag.
- Restart-safe: read open positions back from the broker by magic number so a restart
  resumes managing them.
- **Two-phase risk** (prop-firm): higher risk for the challenge (reach target faster,
  fee is the only thing at stake), then drop to conservative once the funded account —
  the real asset — is live.

---

## 10. Honest caveats to always state (and re-state)

- **Cost/fill sensitivity** — the #1 live risk; the demo is the decisive test.
- **Adverse selection on limit fills** — you fill the continuations (losers) and miss
  the clean bounces (winners); not measurable pre-live, only boundable.
- **Single-source / single-asset data** until cross-validated; in-sample on the symbol.
- **Optimistic fills** — backtest assumes a limit fills whenever price touches the level.
- **No economic rationale** for pattern edges — they can decay with regime; monitor live.
- Expect **live PF meaningfully below backtest.** Plan for it.

---

## 11. Workflow conventions & lessons

- **Run heavy backtests in the background**; cache bars so reruns are fast.
- **After any multi-line edit (esp. dict/call literals), run a STANDALONE syntax check**
  (`python -c "import ast; ast.parse(open('f.py').read())"`). A dropped `)` once made a
  chained `ast.parse && run` short-circuit so the backtest *silently didn't run* and a
  stale output looked current — **caught only by checking the file mtime.** Verify the
  run actually ran (mtime / fresh log), don't trust exit-code 0 of a chained command.
- **Date-stamp output folders** under `backtest/` (`<date>`, `<date>_1`, …); git-ignore
  the lot. Keep one canonical run.
- **Keep `STRATEGY.md` and a project memory continuously updated** — numbers, locked
  decisions (with the *why*), and the next step. Future-you will thank present-you.
- When a result contradicts your hypothesis, **the data wins** — say "I was wrong" and
  move on.

---

## 12. New-bot starter checklist

1. Copy this `CLAUDE.md`; scaffold the layout (§3). Write the rule as the agnostic core
   (§2). Hardcode constants; expose only operational knobs.
2. Wire `run_backtest.py` with **honest costs** (§5) and the manual-exit / limit-entry
   model (§4, §6). Build bars from ticks; cache them.
3. Get ONE clean multi-year run. Then **stop adding return and start attacking it** with
   the battery (§7).
4. **Lock the config by robustness** (§1, §8), not the max. Document every locked
   decision and its why in `STRATEGY.md`.
5. **Cross-validate on a second data source + unseen period** (§7). If it doesn't
   replicate, you don't have an edge — you have a backtest.
6. Harden `run_live.py` (§9). Deploy to a demo. **The demo is the judge.**
7. Only after the demo holds: consider a real challenge / funded account, conservative
   risk, and keep monitoring for decay.

> Reference implementation: the IFVG XAUUSD repo this playbook was distilled from —
> single-rule ICT/SMC FVG strategy, full validation suite, same code backtest+live.
