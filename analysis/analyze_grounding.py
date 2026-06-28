"""
analyze_grounding.py  —  does the edge concentrate where the ICT story predicts?
=================================================================================
A price-pattern edge has no economic "why" by itself (see STRATEGY.md §9). This
script looks for *partial* grounding: if the ICT/FVG narrative is the real
mechanism, the edge should be **stronger when the structural signature is
stronger** — a deeper liquidity sweep, a wider/cleaner FVG, a fresher zone — and
in the **higher-flow sessions**. If instead the edge is flat across all of these,
it is more likely a generic statistical pattern than the ICT mechanism.

It reads a backtest folder's trades.csv (which now carries per-trade features:
fvg_width, sweep_depth, zone_age_h, zone_width) and reports the edge — in
risk-multiples R, so neither compounding nor trade count distorts it — bucketed by
session and by each feature quartile, plus a Spearman rank correlation (feature vs
R) that tests for a monotonic relationship.

USAGE
    python analyze_grounding.py [backtest_folder]   # default: latest backtest_*
"""
import sys
import glob
import numpy as np
import pandas as pd

CASH = 10_000.0
RISK = 0.005          # 1R = the 0.5% risked per trade


def _pf(x):
    gp = x[x > 0].sum(); gl = abs(x[x <= 0].sum())
    return gp / gl if gl else float("inf")


def _spearman(x, y):
    """Rank correlation (monotonic association), scipy-free."""
    m = (~pd.isna(x)) & (~pd.isna(y))
    if m.sum() < 10:
        return float("nan")
    rx = pd.Series(x[m]).rank().values
    ry = pd.Series(y[m]).rank().values
    return float(np.corrcoef(rx, ry)[0, 1])


def load(folder):
    t = pd.read_csv(f"{folder}/trades.csv", parse_dates=["entry_ts"])
    pnl = t["pnl_comm_usd"].values
    eq_before = CASH + np.concatenate([[0], np.cumsum(pnl)[:-1]])
    t["R"] = pnl / eq_before / RISK        # risk-multiples (compounding-free)
    t["hr"] = t["entry_ts"].dt.hour
    return t


def by_session(t):
    def sess(h):
        if 0 <= h < 8:  return "1 Asia       (srv 00-07)"
        if 8 <= h < 13: return "2 London     (srv 08-12)"
        if 13 <= h < 18: return "3 LDN-NY ovl (srv 13-17)"
        return "4 NY late    (srv 18-23)"
    g = t.assign(s=t["hr"].map(sess)).groupby("s")
    print(f"\n  {'session':<26} {'n':>5} {'meanR':>7} {'PF':>6} {'WR':>5}")
    for s, d in g:
        print(f"  {s:<26} {len(d):>5} {d.R.mean():>7.3f} {_pf(d.R):>6.2f} "
              f"{(d.R > 0).mean() * 100:>4.0f}%")


def by_feature(t, col, label, unit):
    """Quartile buckets of `col` vs the per-trade edge, + Spearman(col, R)."""
    d = t[~pd.isna(t[col])].copy()
    if d[col].nunique() < 4:
        print(f"\n  {label}: not enough distinct values"); return
    # rank-based quartiles -> 4 equal-N groups even with heavy ties (e.g. many
    # zero-overshoot sweeps), so the buckets are always comparable
    d["q"] = pd.qcut(d[col].rank(method="first"), 4,
                     labels=["Q1 low", "Q2", "Q3", "Q4 high"])
    rho = _spearman(d[col].values, d["R"].values)
    print(f"\n  {label}  (Spearman vs R = {rho:+.3f})")
    print(f"    {'quartile':<9} {'range '+unit:>16} {'n':>5} {'meanR':>7} {'PF':>6} {'WR':>5}")
    for q, g in d.groupby("q", observed=True):
        lo, hi = g[col].min(), g[col].max()
        print(f"    {str(q):<9} {f'{lo:.2f}–{hi:.2f}':>16} {len(g):>5} "
              f"{g.R.mean():>7.3f} {_pf(g.R):>6.2f} {(g.R > 0).mean() * 100:>4.0f}%")


def main():
    if len(sys.argv) > 1:
        folder = sys.argv[1]
    else:
        cands = sorted(glob.glob("backtest/2*"))
        folder = cands[-1] if cands else "backtest"
    t = load(folder)
    print("=" * 60)
    print(f"  Grounding analysis — {folder}  ({len(t)} trades)")
    print(f"  Edge measured in R (1R = {RISK*100:.1f}% risked); overall "
          f"meanR={t.R.mean():.3f} PF={_pf(t.R):.2f}")
    print("=" * 60)
    print("\n  Does the edge concentrate in higher-flow sessions?")
    by_session(t)
    print("\n  Does the edge grow with the structural signature?")
    print("  (ICT narrative => deeper sweep / wider FVG / fresher zone = stronger)")
    by_feature(t, "sweep_depth", "Liquidity-sweep depth", "$")
    by_feature(t, "fvg_width", "5m entry-FVG width", "$")
    by_feature(t, "zone_width", "1h zone width", "$")
    by_feature(t, "zone_age_h", "1h zone age at entry", "h")
    print("\n" + "=" * 60)
    print("  Positive Spearman + rising meanR across quartiles = the feature")
    print("  the narrative points to really does carry more edge (partial")
    print("  grounding). Flat/negative = that part of the story is not the driver.")
    print("=" * 60)


if __name__ == "__main__":
    main()
