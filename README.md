# IFVG — XAUUSD ICT/SMC strategy

A single-rule, event-driven gold strategy with the **exact same logic running in
backtest and live**. Setup = 1h Fair Value Gap; trigger = 5m liquidity sweep +
inverse-FVG; entry = resting limit at the gap; one position at a time; fixed 0.5%
risk; intraday (4h time-stop, 1h zone-freshness filter, flat by Friday).

- **[STRATEGY.md](STRATEGY.md)** — the full technical & functional reference (how
  it decides, risk sizing, fixed parameters, the validation battery, caveats).
- **[DEPLOY.md](DEPLOY.md)** — running it 24/7 on a Windows VPS.

## Layout

```
ifvg/                  core, framework-agnostic strategy logic (no backtrader / no MT5*)
  ├── signals.py       market-structure detection (swings, FVGs)
  ├── entry_logic.py   THE RULE: generate_signal() + fixed constants
  ├── position.py      risk sizing, break-even, swap (shared backtest+live)
  └── mt5_feed.py      MT5 access: ticks -> OHLC, live buffers  (* the only MT5 import)
strategy.py            backtrader bridge (feeds, sizing, manual exits, costs, metrics)
run_backtest.py        backtest entry point  -> backtest_<date>/ (csv + charts + report)
run_live.py            live/paper runner (same rule, real MT5 orders; auto-reconnect)
run_live.bat           24/7 auto-restart wrapper for the live runner
app/                   local web dashboard: configure / start / stop / watch the bot(s)
  └── webapp.py        Flask panel; supervises one run_live.py per account (multi-account)
run_app.bat            launches the dashboard (open http://127.0.0.1:8765)
analysis/              research & validation tools (run from the repo root)
  ├── validate_perturbation.py     structural perturbation-survival (overfit test)
  ├── sweep_zone_age.py            zone-freshness cutoff sweep (validates MAX_ZONE_AGE_H)
  ├── analyze_grounding.py         edge by session + signal-feature quartiles
  ├── analyze_adverse_selection.py bounds the limit-fill adverse-selection haircut
  └── challenge_sim.py             FTMO 2-phase challenge Monte-Carlo (pass rate/time/fees)
```

All run outputs land under `backtest/` (date-stamped runs + named cross-vals);
that folder, the tick cache (`.bar_cache/`) and logs are regenerated artifacts and
are git-ignored.

## Quickstart

```bash
pip install -r requirements.txt    # live deps; uncomment backtrader+matplotlib for backtest
# MT5 terminal must be open + logged in

python run_backtest.py --start 2020-01-01 --end 2025-06-27   # full backtest
python analysis/challenge_sim.py                              # FTMO challenge odds
python run_live.py --symbol XAUUSD --risk-pct 0.01            # forward demo (1% for a challenge)
python app/webapp.py                                          # web dashboard (configure/start/stop/watch)
```

> Status: fully backtested, cross-validated (2nd broker + earlier period) and
> documented. The remaining step is the **forward demo** — the decisive test of the
> two unmeasured live risks (limit-fill adverse selection and the true cost level).
> See STRATEGY.md §9.
