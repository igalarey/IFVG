"""
challenge_sim.py  —  FTMO challenge Monte-Carlo (pass rate, time, fees)
=======================================================================
Estimates how the strategy fares against the FTMO **two-step** evaluation, at
different per-trade risk levels, accounting for the (refundable) challenge fee.

FTMO rules modelled:
  Phase 1 (Challenge)    : target +10%,  max loss -10% total,  -5% daily
  Phase 2 (Verification) : target  +5%,  max loss -10% total,  -5% daily
  Pass BOTH -> funded. Fail either -> lose the fee, buy a new challenge.
  (No time limit — FTMO removed it.)

Method: day-block bootstrap. Each historical trade is reduced to a scale-free
R-multiple (R = net P&L / amount risked), so it can be replayed at any risk level
(account return per trade = R x risk_pct, compounding on equity). Whole trading
days are resampled with replacement — preserving the intraday clustering the
-5% DAILY rule depends on — and empty (no-signal) days are included so the
calendar pace is realistic. Each attempt runs day by day until it hits the target
(pass) or a loss limit (fail).

USAGE
    python challenge_sim.py                      # latest backtest, risk 0.5% & 1%
    python challenge_sim.py --risk 0.005 0.01 0.015 --fee 540 --account 100000
"""
import argparse
import glob
import numpy as np
import pandas as pd

BACKTEST_RISK = 0.005      # the risk the trades.csv R-multiples were realised at


def parse_args():
    ap = argparse.ArgumentParser(description="FTMO challenge Monte-Carlo")
    ap.add_argument("--folder", default=None, help="backtest folder (trades.csv)")
    ap.add_argument("--risk", type=float, nargs="+", default=[0.005, 0.01],
                    help="per-trade risk levels to test")
    ap.add_argument("--account", type=float, default=100_000.0)
    ap.add_argument("--fee", type=float, default=540.0,
                    help="challenge fee (USD), e.g. ~$540 for a $100k account")
    ap.add_argument("--n", type=int, default=20000, help="Monte-Carlo attempts")
    ap.add_argument("--seed", type=int, default=12345)
    return ap.parse_args()


def day_pool(folder):
    """List of per-trading-day R-multiple arrays over the full period, including
    empty (no-trade) business days so the calendar pace is realistic."""
    t = pd.read_csv(f"{folder}/trades.csv", parse_dates=["entry_ts", "exit_ts"])
    pnl = t["pnl_comm_usd"].values
    eq_before = 10_000.0 + np.concatenate([[0.0], np.cumsum(pnl)[:-1]])
    t["R"] = pnl / eq_before / BACKTEST_RISK
    t["day"] = t["entry_ts"].dt.normalize()
    by = {d: g["R"].values for d, g in t.groupby("day")}
    alldays = pd.bdate_range(t["day"].min(), t["day"].max())   # business days
    return [by.get(d, np.empty(0)) for d in alldays], len(t), len(alldays)


def simulate(pool, risk, target, n, rng, max_days=1500, within_days=5):
    """Monte-Carlo n attempts of one phase. Returns pass rate, median trades/days
    to pass, the fail-mode split, and the probability of passing / failing within
    `within_days` trading days (one week = 5)."""
    npool = len(pool)
    passes = 0
    fail_daily = fail_total = unresolved = 0
    pass_in = fail_in = 0           # resolved within `within_days`
    tr_pass, dy_pass, tr_all = [], [], []
    for _ in range(n):
        acct = 1.0
        ntr = 0
        outcome = None
        for d in range(max_days):
            day = pool[rng.integers(npool)]
            day_start = acct
            for R in day:
                acct *= (1.0 + R * risk)
                ntr += 1
                if acct <= 0.90:                 # -10% total loss
                    outcome = "total"; break
                if acct <= day_start * 0.95:      # -5% daily loss
                    outcome = "daily"; break
                if acct >= 1.0 + target:          # profit target
                    outcome = "pass"; break
            if outcome:
                break
        days = d + 1
        tr_all.append(ntr)
        if outcome == "pass":
            passes += 1; tr_pass.append(ntr); dy_pass.append(days)
            if days <= within_days:
                pass_in += 1
        elif outcome in ("daily", "total"):
            if outcome == "daily":
                fail_daily += 1
            else:
                fail_total += 1
            if days <= within_days:
                fail_in += 1
        else:
            unresolved += 1
    return {
        "pass": passes / n,
        "fail_daily": fail_daily / n, "fail_total": fail_total / n,
        "unresolved": unresolved / n,
        "med_trades": int(np.median(tr_pass)) if tr_pass else None,
        "med_days": int(np.median(dy_pass)) if dy_pass else None,
        "avg_trades_attempt": float(np.mean(tr_all)),
        "pass_within": pass_in / n, "fail_within": fail_in / n,
        "within_days": within_days,
    }


def _one_phase(pool, risk, target, rng, max_days):
    """Run a single phase; return (outcome, trading_days_used)."""
    npool = len(pool)
    acct = 1.0
    for d in range(max_days):
        day = pool[rng.integers(npool)]
        day_start = acct
        for R in day:
            acct *= (1.0 + R * risk)
            if acct <= 0.90 or acct <= day_start * 0.95:
                return "fail", d + 1
            if acct >= 1.0 + target:
                return "pass", d + 1
    return "unresolved", max_days


def simulate_funded(pool, risk, n, rng, windows, max_days=1500):
    """Full funding pipeline per attempt: Phase 1 (+10%) then, if passed, Phase 2
    (+5%) on a fresh account. Tracks TOTAL trading days to the resolution, so we
    can ask P(funded within W trading days)."""
    funded = np.zeros(n, dtype=bool)
    res_day = np.zeros(n, dtype=int)
    for i in range(n):
        o1, d1 = _one_phase(pool, risk, 0.10, rng, max_days)
        if o1 != "pass":
            res_day[i] = d1; continue
        o2, d2 = _one_phase(pool, risk, 0.05, rng, max_days)
        funded[i] = (o2 == "pass")
        res_day[i] = d1 + d2
    out = {"p_funded": funded.mean(),
           "med_days_funded": int(np.median(res_day[funded])) if funded.any() else None}
    for w in windows:
        out[f"fund_{w}"] = float((funded & (res_day <= w)).mean())
        out[f"fail_{w}"] = float(((~funded) & (res_day <= w)).mean())
    return out


def main():
    args = parse_args()
    folder = args.folder or sorted(glob.glob("backtest/2*"))[-1]
    pool, n_trades, n_days = day_pool(folder)
    rng = np.random.default_rng(args.seed)
    trades_per_week = n_trades / (n_days / 5.0)      # 5 business days per week

    print("=" * 72)
    print(f"  FTMO challenge Monte-Carlo — {folder}  ({args.n:,} attempts/phase)")
    print(f"  account ${args.account:,.0f}  fee ${args.fee:,.0f}  "
          f"(~{trades_per_week:.1f} trades/week)")
    print("=" * 72)

    for risk in args.risk:
        p1 = simulate(pool, risk, 0.10, args.n, rng)
        p2 = simulate(pool, risk, 0.05, args.n, rng)
        p_fund = p1["pass"] * p2["pass"]
        exp_attempts = 1.0 / p_fund if p_fund else float("inf")
        exp_fees = exp_attempts * args.fee
        # rough calendar time to fund: trades across all paid attempts / rate
        exp_trades_to_fund = exp_attempts * (
            p1["avg_trades_attempt"] + p1["pass"] * p2["avg_trades_attempt"])
        exp_weeks = exp_trades_to_fund / trades_per_week if trades_per_week else 0

        def wk(d):
            return f"~{d/5:.0f} wk" if d else "—"
        print(f"\n  RISK {risk*100:.1f}%")
        print(f"    Phase 1 (+10%): pass {p1['pass']*100:4.1f}%   "
              f"median {p1['med_trades']} trades ({wk(p1['med_days'])})   "
              f"fail: daily {p1['fail_daily']*100:.0f}% / total {p1['fail_total']*100:.0f}%")
        still = (1 - p1["pass_within"] - p1["fail_within"]) * 100
        print(f"      within 1 week (5 td): pass {p1['pass_within']*100:4.1f}%  "
              f"blow-up {p1['fail_within']*100:4.1f}%  still going {still:4.1f}%")
        print(f"    Phase 2 (+5%) : pass {p2['pass']*100:4.1f}%   "
              f"median {p2['med_trades']} trades ({wk(p2['med_days'])})   "
              f"fail: daily {p2['fail_daily']*100:.0f}% / total {p2['fail_total']*100:.0f}%")
        fnd = simulate_funded(pool, risk, args.n, rng, [10, 15])
        print(f"    Funded (P1+P2) within 2 wk: {fnd['fund_10']*100:4.1f}% "
              f"(failed {fnd['fail_10']*100:.0f}%)   within 3 wk: {fnd['fund_15']*100:4.1f}% "
              f"(failed {fnd['fail_15']*100:.0f}%)   median to fund "
              f"{fnd['med_days_funded']} td (~{(fnd['med_days_funded'] or 0)/5:.0f} wk)")
        print(f"    => P(funded) per paid challenge = {p_fund*100:.1f}%")
        print(f"       expected paid challenges to fund: {exp_attempts:.1f}  "
              f"(~{exp_attempts-1:.1f} lost first)")
        print(f"       expected fee spend to fund: ${exp_fees:,.0f}")
        print(f"       expected time to fund: ~{exp_weeks:.0f} weeks "
              f"({exp_trades_to_fund:.0f} trades)")
    print("\n" + "=" * 72)
    print("  Note: per-trade R is held fixed across risk levels (scale-free); at")
    print("  higher risk the daily/total-loss limits bite more often -> lower pass.")
    print("=" * 72)


if __name__ == "__main__":
    main()
