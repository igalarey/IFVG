"""
sweep_zone_age.py  —  validate the zone-freshness filter (MAX_ZONE_AGE_H)
=========================================================================
The grounding analysis (replicated on a 2nd broker) showed the edge lives in
FRESH 1h zones and stale zones lose money. This sweeps the freshness cutoff and
re-runs the full engine for each, so we can see the trade-off — fewer but
higher-quality trades — and check it holds per-year, NOT just in aggregate
(the discipline: pick a cutoff by robustness, never the backtest peak).

Run it on the primary data AND on the independent IC Markets cross-validation
symbol; a real improvement helps in both.

USAGE
    python sweep_zone_age.py --start 2020-01-01 --end 2025-06-27
    python sweep_zone_age.py --symbol XAUUSD2017_ICMARKETS --start 2017-01-01 \
           --end 2020-01-01 --spread-floor 0.36
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# this script lives in analysis/; add the repo root so the core modules import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ifvg import mt5_feed, entry_logic, position
import run_backtest as rb

CUTOFFS = [None, 6.0, 4.0, 3.0, 2.0, 1.5, 1.0, 0.5]   # hours; None = off (current)


def parse_args():
    ap = argparse.ArgumentParser(description="IFVG zone-freshness sweep")
    ap.add_argument("--symbol", default="XAUUSD2020_FTMO")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-27")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--spread-floor", type=float, default=0.0, dest="spread_floor")
    ap.add_argument("--mt5-path", default=None)
    ap.add_argument("--mt5-login", type=int, default=None)
    ap.add_argument("--mt5-password", default=None)
    ap.add_argument("--mt5-server", default=None)
    return ap.parse_args()


def _pf(x):
    gp = x[x > 0].sum(); gl = abs(x[x <= 0].sum())
    return gp / gl if gl else float("inf")


def run_cutoff(frames, args, cutoff):
    saved = entry_logic.MAX_ZONE_AGE_H
    entry_logic.MAX_ZONE_AGE_H = cutoff
    try:
        cerebro = rb.build_cerebro(frames, args.cash, position.RISK_PCT,
                                   position.TIME_STOP_HOURS,
                                   position.FRIDAY_FLAT_HOUR,
                                   spread_floor=args.spread_floor)
        strat = cerebro.run(runonce=False)[0]
        metrics, trades, eq = rb.build_report(strat, args.cash)
        if not metrics:
            return None
        t = trades.copy()
        t["yr"] = pd.to_datetime(t["exit_ts"]).dt.year
        yr_pf = t.groupby("yr")["pnl_comm_usd"].apply(_pf)
        yr_net = t.groupby("yr")["pnl_comm_usd"].sum()
        dd = strat.daily_dd_pct
        return {
            "n": metrics["n_trades"], "net": metrics["net_pnl_usd"],
            "pf": metrics["profit_factor"], "maxdd": metrics["max_drawdown_pct"],
            "sharpe": metrics["sharpe_ann"],
            "years_pos": int((yr_net > 0).all()), "min_yr_pf": round(yr_pf.min(), 2),
            "worst_day": round(max(dd.values()), 2) if dd else 0.0,
            "days_over5": sum(1 for v in dd.values() if v >= 5.0) if dd else 0,
        }
    finally:
        entry_logic.MAX_ZONE_AGE_H = saved


def main():
    args = parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    print(f"[sweep] fetching {args.symbol} (cached) …")
    mt5_feed.connect(path=args.mt5_path, login=args.mt5_login,
                     password=args.mt5_password, server=args.mt5_server)
    try:
        frames = mt5_feed.fetch_all_range(args.symbol, start, end)
    finally:
        mt5_feed.shutdown()

    floor = f"  (spread floor ${args.spread_floor})" if args.spread_floor else ""
    print("\n" + "=" * 74)
    print(f"  Zone-freshness sweep — {args.symbol}  {args.start}..{args.end}{floor}")
    print("=" * 74)
    print(f"  {'max age':>8} {'trades':>6} {'net $':>9} {'PF':>5} {'maxDD':>6} "
          f"{'Sharpe':>6} {'allYr+':>6} {'minYrPF':>7} {'wDay':>5} {'d>=5':>4}")
    print("  " + "-" * 70)
    for c in CUTOFFS:
        r = run_cutoff(frames, args, c)
        lbl = "off (7d)" if c is None else f"{c}h"
        if r is None:
            print(f"  {lbl:>8} {'— no trades —':>30}"); continue
        print(f"  {lbl:>8} {r['n']:>6} {r['net']:>9,.0f} {r['pf']:>5.2f} "
              f"{r['maxdd']:>5.1f}% {r['sharpe']:>6.2f} {'yes' if r['years_pos'] else 'NO':>6} "
              f"{r['min_yr_pf']:>7.2f} {r['worst_day']:>4.1f}% {r['days_over5']:>4}")
    print("  " + "-" * 70)
    print("  Trade-off: tighter cutoff -> higher PF, fewer trades, lower total net.")
    print("  Pick by robustness (every year +, daily-safe), not the highest net.")
    print("=" * 74)


if __name__ == "__main__":
    main()
