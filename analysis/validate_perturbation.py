"""
validate_perturbation.py  —  structural perturbation-survival test
==================================================================
Re-runs the FULL backtest with each fixed strategy constant jittered a little,
to check the edge is not balanced on one exact parameter value (the fingerprint
of an overfit). A robust edge degrades gracefully as the knobs move; a fragile
one collapses the moment you nudge a threshold.

This complements:
  • the per-trade FILL perturbation in run_backtest (execution robustness),
  • the time-stop / RR sweeps already done (single-knob curves),
by perturbing the SIGNAL-DETECTION constants the sweeps never touched
(FVG impulse filters, min-gap, min-stop, entry anchor) and re-running the
whole engine for each.

Uses the exact same engine and cost model as run_backtest (build_cerebro), so
the numbers are directly comparable. Bars are pulled once from MT5 (cached), then
every perturbation reruns over the same bars.

USAGE
    python validate_perturbation.py --start 2020-01-01 --end 2025-06-27
"""
import argparse
import os
import sys
from datetime import datetime, timezone

# this script lives in analysis/; add the repo root so the core modules import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ifvg import mt5_feed, entry_logic, position
import run_backtest as rb

EL, PO = entry_logic, position


def parse_args():
    ap = argparse.ArgumentParser(description="IFVG structural perturbation survival")
    ap.add_argument("--symbol", default="XAUUSD2020_FTMO")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-06-27")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--mt5-path", default=None)
    ap.add_argument("--mt5-login", type=int, default=None)
    ap.add_argument("--mt5-password", default=None)
    ap.add_argument("--mt5-server", default=None)
    return ap.parse_args()


# (label, {(module, attr): value, ...}, time_stop_h) — baseline first.
# Each numeric knob is nudged both ways; the anchor and time-stop are nudged too.
def grid():
    return [
        ("baseline                 ", {}, 4),
        ("FVG_BODY_MULT 1.0->0.9    ", {(EL, "FVG_BODY_MULT"): 0.9}, 4),
        ("FVG_BODY_MULT 1.0->1.1    ", {(EL, "FVG_BODY_MULT"): 1.1}, 4),
        ("M5_BODY_MULT 1.1->1.0     ", {(EL, "M5_BODY_MULT"): 1.0}, 4),
        ("M5_BODY_MULT 1.1->1.2     ", {(EL, "M5_BODY_MULT"): 1.2}, 4),
        ("M5_MIN_GAP_PIPS 0.3->0.2  ", {(EL, "M5_MIN_GAP_PIPS"): 0.2}, 4),
        ("M5_MIN_GAP_PIPS 0.3->0.4  ", {(EL, "M5_MIN_GAP_PIPS"): 0.4}, 4),
        ("MIN_STOP_PIPS 5->4        ", {(EL, "MIN_STOP_PIPS"): 4.0}, 4),
        ("MIN_STOP_PIPS 5->6        ", {(EL, "MIN_STOP_PIPS"): 6.0}, 4),
        ("RR 1.5->1.4               ", {(EL, "RR"): 1.4}, 4),
        ("RR 1.5->1.6               ", {(EL, "RR"): 1.6}, 4),
        ("ENTRY_ANCHOR mid->proximal", {(EL, "ENTRY_ANCHOR"): "proximal"}, 4),
        ("time_stop 4h->3h          ", {}, 3),
        ("time_stop 4h->5h          ", {}, 5),
    ]


def run_config(frames, cash, patches, time_stop_h):
    """Apply the constant patches, run the full engine, restore the constants."""
    saved = {k: getattr(k[0], k[1]) for k in patches}
    for (mod, attr), val in patches.items():
        setattr(mod, attr, val)
    try:
        cerebro = rb.build_cerebro(frames, cash, PO.RISK_PCT, time_stop_h,
                                   PO.FRIDAY_FLAT_HOUR)
        strat = cerebro.run(runonce=False)[0]
        metrics, _, _ = rb.build_report(strat, cash)
        if not metrics:
            return None
        return {"net": metrics["net_pnl_usd"], "pf": metrics["profit_factor"],
                "maxdd": metrics["max_drawdown_pct"], "n": metrics["n_trades"]}
    finally:
        for (mod, attr), val in saved.items():
            setattr(mod, attr, val)


def main():
    args = parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    print(f"[perturb] Connecting to MT5, fetching {args.symbol} (cached) …")
    mt5_feed.connect(path=args.mt5_path, login=args.mt5_login,
                     password=args.mt5_password, server=args.mt5_server)
    try:
        frames = mt5_feed.fetch_all_range(args.symbol, start, end)
    finally:
        mt5_feed.shutdown()

    print("\n" + "=" * 64)
    print("  Structural perturbation survival  (baseline knobs nudged)")
    print("=" * 64)
    print(f"  {'config':<26} {'trades':>6} {'net $':>9} {'PF':>6} {'maxDD%':>7}")
    print("  " + "-" * 60)

    base = None
    survivors = 0
    total = 0
    for label, patches, ts in grid():
        r = run_config(frames, args.cash, patches, ts)
        if r is None:
            print(f"  {label:<26} {'— no trades —':>30}")
            continue
        if base is None:
            base = r
        total += 1
        # "survives" = still clearly tradeable: profitable and PF >= 1.2
        ok = r["net"] > 0 and r["pf"] >= 1.2
        survivors += ok
        flag = "" if ok else "  <-- FRAGILE"
        d_net = (r["net"] / base["net"] - 1) * 100 if base["net"] else 0
        print(f"  {label:<26} {r['n']:>6} {r['net']:>9,.0f} {r['pf']:>6.2f} "
              f"{r['maxdd']:>6.1f}%  ({d_net:+.0f}% vs base){flag}")

    print("  " + "-" * 60)
    print(f"  Survivors (profitable & PF>=1.2): {survivors}/{total}")
    print("  A robust edge survives every nudge; collapses = overfit to a knob.")
    print("=" * 64)


if __name__ == "__main__":
    main()
