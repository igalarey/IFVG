"""
analyze_adverse_selection.py  —  bound the live cost of limit-fill adverse selection
====================================================================================
Adverse selection cannot be MEASURED before live (it is about which of your
resting limits actually fill given the real queue/flow). But it can be BOUNDED.

Mechanism: our entry is a limit at the FVG midpoint (a mean-reversion fill). On
the bar it fills, price either just *tags* the level and bounces (shallow
penetration — a clean trade you tend to MISS live, because price never cleared
your queue) or *slices through* it (deep penetration — a continuation you'd surely
be filled on). If shallow fills are disproportionately winners, then live you miss
winners and keep losers — that is the adverse-selection haircut.

This reads the per-trade `fill_penetration` (logged by strategy.py) and:
  1. shows win-rate by penetration quartile (does shallow = winners?), and
  2. sweeps a penetration buffer — "live you only fill trades that penetrate >=
     buffer" — to bracket how far the edge degrades. buffer 0 = the backtest
     (fill every tag, optimistic); higher buffer = ever more adverse selection.
Reality sits between; the sweep is the bracket.

USAGE
    python analyze_adverse_selection.py [backtest_folder]
"""
import sys
import glob
import numpy as np
import pandas as pd

RISK = 0.005


def _pf(x):
    gp = x[x > 0].sum(); gl = abs(x[x <= 0].sum())
    return gp / gl if gl else float("inf")


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("backtest/2*"))[-1]
    t = pd.read_csv(f"{folder}/trades.csv")
    if "fill_penetration" not in t.columns:
        print("No fill_penetration column — re-run the backtest first."); return
    t = t[t["fill_penetration"].notna()].copy()
    net = t["pnl_comm_usd"].values
    eqb = 10_000.0 + np.concatenate([[0.0], np.cumsum(net)[:-1]])
    t["R"] = net / eqb / RISK
    pen = t["fill_penetration"].values
    n0 = len(t)

    print("=" * 70)
    print(f"  Adverse-selection proxy — {folder}  ({n0} filled trades)")
    print("=" * 70)
    print(f"  fill penetration ($/oz):  median {np.median(pen):.2f}   "
          f"p25 {np.percentile(pen,25):.2f}   p75 {np.percentile(pen,75):.2f}   "
          f"max {pen.max():.2f}")

    t["q"] = pd.qcut(t["fill_penetration"].rank(method="first"), 4,
                     labels=["Q1 shallow", "Q2", "Q3", "Q4 deep"])
    print("\n  Win rate by penetration quartile (mechanism check —")
    print("  shallow 'clean tag' should win MORE if adverse selection is real):")
    for q, g in t.groupby("q", observed=True):
        print(f"    {q:<10} pen ${g.fill_penetration.min():.2f}-{g.fill_penetration.max():>5.2f}  "
              f"n {len(g):>4}  WR {(g.pnl_comm_usd>0).mean()*100:>3.0f}%  "
              f"meanR {g.R.mean():+.3f}  PF {_pf(g.pnl_comm_usd):.2f}")

    print("\n  Bracket — if live you only fill trades penetrating >= buffer")
    print("  (dropping the clean taps you would miss):")
    print(f"    {'buffer':>7} {'kept':>6} {'%kept':>6} {'WR':>4} {'netEV':>7} "
          f"{'PF':>5} {'recompound':>11} {'vs base':>8}")
    base = None
    for b in [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]:
        k = t[t["fill_penetration"] >= b]
        if len(k) < 10:
            continue
        R = k["R"].values
        ret = (np.prod(1.0 + R * RISK) - 1.0) * 100      # recompounded at 0.5%
        if base is None:
            base = ret
        print(f"    ${b:>5.2f} {len(k):>6} {len(k)/n0*100:>5.0f}% "
              f"{(k.pnl_comm_usd>0).mean()*100:>3.0f}% ${k.pnl_comm_usd.mean():>6.2f} "
              f"{_pf(k.pnl_comm_usd):>5.2f} {ret:>9.0f}% {ret/base*100:>6.0f}%")
    print("=" * 70)
    print("  buffer $0.00 = the backtest (fills every tag, optimistic). Reality is")
    print("  somewhere along the sweep — the worse your live fills, the higher the")
    print("  buffer. The PF / netEV columns are the adverse-selection-bounded edge.")
    print("=" * 70)


if __name__ == "__main__":
    main()
