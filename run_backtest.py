"""
run_backtest.py  —  backtest the IFVG strategy on XAUUSD
===============================================================
Pulls tick history for the symbol from a running MetaTrader 5 terminal, rebuilds
M1/M5/H1 candles, runs IFVGStrategy through backtrader, and writes results.

The strategy itself is FIXED (0.5% risk, validated constants — see
ifvg/entry_logic.py and ifvg/position.py). Only operational settings are
exposed: which symbol, which dates, starting cash, output folder, and the MT5
connection. Execution realism is fixed too: 1:100 leverage, FTMO's 0.0014%
commission, and the REAL spread measured from the ticks (×1.2 as a cushion).

USAGE
    python run_backtest.py                                   # default symbol/range
    python run_backtest.py --symbol XAUUSD2020_FTMO --start 2024-01-01 --end 2025-01-01
    python run_backtest.py --validate                        # fast 1-month smoke test

PREREQUISITE
    MetaTrader 5 installed, running and logged in, with the symbol's TICK history
    available locally (the strategy is rebuilt from ticks, not M1 bars).

Outputs land in results/ (or results_validate/): trades.csv, equity.csv,
equity_curve.png (with a metrics panel: alpha/beta, Sharpe, drawdown, …),
monte_carlo.png (return + drawdown distributions), plus a printed performance
report including the FTMO daily drawdown.
"""
import argparse
import os
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")                       # headless: save PNGs, no GUI
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import backtrader as bt

from ifvg import mt5_feed
from ifvg.position import (RISK_PCT, TIME_STOP_HOURS, FRIDAY_FLAT_HOUR,
                          SWAP_USD_PER_LOT_PER_NIGHT)
from strategy import IFVGStrategy

# ── fixed execution realism (XAUUSD, FTMO) ──
LEVERAGE = 100.0            # XAUUSD CFD; without leverage every order margin-rejects
COMMISSION_PCT = 0.0014 / 100.0   # FTMO commission: 0.0014% of notional, per side
SPREAD_MULT = 1.2          # model a slightly worse-than-median spread (cushion)

# timeframe -> (backtrader TimeFrame, compression) for correct multi-TF alignment
_BT_TF = {"m1": (bt.TimeFrame.Minutes, 1),
          "m5": (bt.TimeFrame.Minutes, 5),
          "h1": (bt.TimeFrame.Minutes, 60)}


class PandasDataSpread(bt.feeds.PandasData):
    """m1 feed that also exposes the per-bar `spread` line, so the strategy can
    charge the real (time-varying) spread on each exit instead of a constant."""
    lines = ("spread",)
    params = (("spread", -1),)        # -1 = autodetect the 'spread' column by name


def parse_args():
    ap = argparse.ArgumentParser(description="XAUUSD IFVG backtest")
    ap.add_argument("--symbol", default="XAUUSD2020_FTMO", help="MT5 symbol")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--validate", action="store_true",
                    help="Override range to the first month (fast smoke test)")
    ap.add_argument("--cash", type=float, default=10_000.0)
    # risk per trade: default 0.5% (live). Raise for a funding challenge to hit
    # the target faster, then set back to 0.5% once funded.
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT, dest="risk_pct")
    # force-close a trade after this many hours (caps the break-even grind that
    # otherwise ties up the single slot for days). Default 12h (validated);
    # pass 0 to disable.
    ap.add_argument("--time-stop-h", type=float, default=TIME_STOP_HOURS,
                    dest="time_stop_h")
    # close out + stop opening after this server-hour on Fridays (no weekend
    # holding). Default 17h; pass -1 to disable.
    ap.add_argument("--friday-flat-h", type=int, default=FRIDAY_FLAT_HOUR,
                    dest="friday_flat_h")
    # floor the per-bar spread (raw $/oz, before the x1.2 cushion). Use it to
    # normalise an ultra-tight feed (e.g. IC Markets raw ~$0.12) up to an
    # FTMO-realistic cost so a cross-source comparison isn't flattered by cheap
    # spreads. 0 = off (use the feed's own measured spread).
    ap.add_argument("--spread-floor", type=float, default=0.0, dest="spread_floor")
    # output folder. Default: backtest/<today> (auto _1/_2/... if the day already
    # has a run), so every backtest is archived by date under backtest/.
    ap.add_argument("--out", default=None)
    # MT5 connection (omit to attach to the already-open, logged-in terminal)
    ap.add_argument("--mt5-path", default=None)
    ap.add_argument("--mt5-login", type=int, default=None)
    ap.add_argument("--mt5-password", default=None)
    ap.add_argument("--mt5-server", default=None)
    ap.add_argument("--printlog", action="store_true", help="log every trade")
    return ap.parse_args()


def unique_out_dir(base):
    """First run of the day -> base; later runs -> base_1, base_2, ..."""
    cand, i = base, 1
    while os.path.exists(cand):
        cand, i = f"{base}_{i}", i + 1
    return cand


def add_feed(cerebro, df, tf_key, spread_mult=1.0, spread_floor=0.0):
    tf, comp = _BT_TF[tf_key]
    if tf_key == "m1":
        bdf = mt5_feed.to_bt_df(df, with_spread=True)
        # cushion the spread (model slightly worse than measured) and fill any
        # tickless-minute gaps with the median so the cost is never NaN. The
        # optional floor normalises an ultra-tight feed up to a realistic cost.
        med = bdf["spread"].median()
        bdf["spread"] = (bdf["spread"].fillna(med).clip(lower=spread_floor)
                         * spread_mult).clip(lower=0.0)
        data = PandasDataSpread(dataname=bdf, timeframe=tf, compression=comp,
                                open=0, high=1, low=2, close=3, volume=4,
                                openinterest=-1, spread=-1)
    else:
        data = bt.feeds.PandasData(dataname=mt5_feed.to_bt_df(df),
                                   timeframe=tf, compression=comp,
                                   open=0, high=1, low=2, close=3, volume=4,
                                   openinterest=-1)
    cerebro.adddata(data, name=tf_key)


def build_cerebro(frames, cash, risk_pct, time_stop_h, friday_flat_h,
                  printlog=False, spread_floor=0.0):
    """Assemble a Cerebro with the 3 feeds, the strategy and the cost model.
    Shared by the backtest and the perturbation driver so they run identically.
    The spread is charged per-bar in the strategy (no broker slippage); only the
    FTMO commission lives on the broker."""
    cerebro = bt.Cerebro(stdstats=False)
    for tf in ("m1", "m5", "h1"):
        add_feed(cerebro, frames[tf], tf, spread_mult=SPREAD_MULT,
                 spread_floor=spread_floor)
    friday = None if friday_flat_h is not None and friday_flat_h < 0 \
        else friday_flat_h
    cerebro.addstrategy(IFVGStrategy, risk_pct=risk_pct, time_stop_h=time_stop_h,
                        friday_flat_h=friday, printlog=printlog)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=COMMISSION_PCT,
                                 commtype=bt.CommInfoBase.COMM_PERC, percabs=True,
                                 stocklike=True, leverage=LEVERAGE, mult=1.0)
    return cerebro


def build_report(strat, cash):
    """Trades DataFrame, equity-curve DataFrame and a metrics dict."""
    trades = pd.DataFrame(strat.closed_trades)
    if trades.empty:
        return {}, trades, pd.DataFrame(columns=["ts", "balance", "drawdown"])

    eq = pd.DataFrame(strat.equity_curve, columns=["ts", "balance"]).sort_values("ts")
    peak = eq["balance"].cummax()
    eq["drawdown"] = peak - eq["balance"]                 # in $
    eq["drawdown_pct"] = eq["drawdown"] / peak * 100      # true % vs running peak

    # trade durations (entry -> exit)
    trades["entry_ts"] = pd.to_datetime(trades["entry_ts"])
    trades["exit_ts"] = pd.to_datetime(trades["exit_ts"])
    dur_h = (trades["exit_ts"] - trades["entry_ts"]).dt.total_seconds() / 3600.0

    pnl = trades["pnl_comm_usd"]
    gross_p = pnl[pnl > 0].sum()
    gross_l = abs(pnl[pnl <= 0].sum())
    # per-trade expected value: gross (pre-cost), the cost drag, and net (post-cost).
    # Net EV in R normalises by the risk taken (risk_pct of equity-before-trade), so
    # it reads as "you make X times your risk per trade on average".
    risk_pct = getattr(strat.p, "risk_pct", 0.005) or 0.005
    eq_before = cash + np.concatenate([[0.0], np.cumsum(pnl.values)[:-1]])
    metrics = {
        "n_trades": len(trades),
        "net_pnl_usd": round(pnl.sum(), 2),
        "profit_factor": round(gross_p / gross_l, 3) if gross_l else float("inf"),
        "winrate": round((pnl > 0).mean(), 4),
        "ev_gross_usd": round(trades["pnl_usd"].mean(), 2),          # pre-cost EV
        "cost_per_trade_usd": round((trades["pnl_usd"] - pnl).mean(), 2),
        "net_ev_usd": round(pnl.mean(), 2),                          # post-cost EV
        "net_ev_r": round((pnl.values / eq_before / risk_pct).mean(), 3),
        "max_drawdown_pct": round(eq["drawdown_pct"].max(), 2),   # vs running peak
        "final_balance": round(eq["balance"].iloc[-1], 2),
        "avg_duration_h": round(dur_h.mean(), 1),
        "median_duration_h": round(dur_h.median(), 1),
        "max_duration_h": round(dur_h.max(), 1),
        "pct_held_over_24h": round((dur_h > 24).mean() * 100, 1),
    }
    metrics.update(_risk_metrics(pnl, eq, cash))
    metrics.update(mfe_mae_stats(trades))
    return metrics, trades, eq[["ts", "balance", "drawdown", "drawdown_pct"]]


def _risk_metrics(pnl, eq, cash):
    """Risk-adjusted and distribution stats from the trade P&L + equity curve."""
    e = eq.set_index("ts")["balance"]
    dret = e.resample("1D").last().dropna().pct_change().dropna()      # daily
    mret = e.resample("ME").last().dropna().pct_change().dropna()      # monthly
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    years = max(len(dret) / 252.0, 1e-9)
    cagr = (e.iloc[-1] / cash) ** (1 / years) - 1
    maxdd_usd = eq["drawdown"].max()
    sd = dret.std()
    sdn = dret[dret < 0].std()
    # equity-curve linearity: correlation of the balance with a straight line
    # (1.0 = perfectly straight equity; MT5 calls this "LR Correlation")
    bal = eq["balance"].values
    lr_corr = float(np.corrcoef(bal, np.arange(len(bal)))[0, 1]) if len(bal) > 2 else 0.0

    def streak(target):
        m = c = 0
        for v in np.sign(pnl.values):
            c = c + 1 if v == target else 0
            m = max(m, c)
        return int(m)

    return {
        "cagr_pct": round(cagr * 100, 1),
        "sharpe_ann": round(dret.mean() / sd * np.sqrt(252), 2) if sd else 0.0,
        "sortino_ann": round(dret.mean() / sdn * np.sqrt(252), 2) if sdn else 0.0,
        "calmar": round(cagr * 100 / (maxdd_usd / cash * 100), 2) if maxdd_usd else 0.0,
        "recovery_factor": round(pnl.sum() / maxdd_usd, 1) if maxdd_usd else 0.0,
        "payoff_ratio": round(wins.mean() / abs(losses.mean()), 2) if len(losses) else 0.0,
        "expectancy_usd": round(pnl.mean(), 2),
        "max_consec_losses": streak(-1),
        "max_consec_wins": streak(1),
        "pct_winning_months": round((mret > 0).mean() * 100, 0) if len(mret) else 0,
        "ret_skew": round(pnl.skew(), 2),
        "equity_lr_corr": round(lr_corr, 3),
    }


def alpha_beta(eq, m1_df):
    """Daily beta/alpha of the strategy vs buy-and-hold gold (the benchmark).
    Low beta + high alpha ⇒ the edge is timing, not riding the gold trend."""
    s = (eq.set_index("ts")["balance"].resample("1D").last().dropna()
         .pct_change().dropna())
    g = (m1_df.set_index("timestamp")["close"].resample("1D").last().dropna()
         .pct_change().dropna())
    g.index = g.index.tz_localize(None)
    j = pd.concat([s.rename("s"), g.rename("g")], axis=1).dropna()
    if len(j) < 30:
        return None
    beta, alpha = np.polyfit(j["g"], j["s"], 1)
    return {"beta": round(beta, 3), "corr": round(j["s"].corr(j["g"]), 3),
            "alpha_ann_pct": round(alpha * 252 * 100, 1)}


def monte_carlo(pnl, cash, n=10000, seed=42, n_paths=300):
    """Resample the trade sequence n times (reshuffle = same trades/new order;
    bootstrap = sample with replacement) to get the distribution of final return
    and — the order-dependent risk — max drawdown.

    Returns (summary, arrays): the printed percentiles plus the raw final-return
    / max-drawdown arrays and a sample of equity paths, for plotting."""
    rng = np.random.default_rng(seed)
    ret = (pnl.values / np.concatenate([[cash], (cash + np.cumsum(pnl.values))[:-1]]))
    L = len(ret)

    def run(replace, keep=0):
        fr = np.empty(n); md = np.empty(n); paths = []
        for i in range(n):
            idx = rng.integers(0, L, L) if replace else rng.permutation(L)
            bal = cash * np.cumprod(1 + ret[idx])
            peak = np.maximum.accumulate(bal)
            fr[i] = bal[-1] / cash - 1
            md[i] = ((peak - bal) / peak).max()
            if i < keep:
                paths.append(bal)
        return fr, md, paths

    out, arrays = {}, {}
    for name, repl in (("reshuffle", False), ("bootstrap", True)):
        fr, md, paths = run(repl, keep=n_paths if name == "bootstrap" else 0)
        out[name] = {
            "ret_p5": round(np.percentile(fr, 5) * 100),
            "ret_p50": round(np.percentile(fr, 50) * 100),
            "ret_p95": round(np.percentile(fr, 95) * 100),
            "dd_p50": round(np.percentile(md, 50) * 100, 1),
            "dd_p95": round(np.percentile(md, 95) * 100, 1),
            "dd_p99": round(np.percentile(md, 99) * 100, 1),
            "p_dd_over_10": round((md > 0.10).mean() * 100, 1),
            "p_losing": round((fr < 0).mean() * 100, 1),
        }
        arrays[name] = {"fr": fr, "md": md, "paths": paths}
    return out, arrays


def _pf(pnl):
    """Profit factor of a P&L series (inf if no losses)."""
    gp = pnl[pnl > 0].sum()
    gl = abs(pnl[pnl <= 0].sum())
    return (gp / gl) if gl else float("inf")


def _gini(x):
    """Gini coefficient of a non-negative array (0 = even, 1 = all in one)."""
    x = np.sort(np.asarray(x, dtype=float))
    n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    cum = np.cumsum(x)
    return float((n + 1 - 2 * np.sum(cum) / cum[-1]) / n)


def concentration(trades):
    """Is the edge broad or a few lucky trades / one regime / one side? Reports
    profit concentration (Gini, top-slice share), the PF/net with the single best
    month and best year removed, and the split by side and by entry hour."""
    t = trades.copy()
    t["entry_ts"] = pd.to_datetime(t["entry_ts"])
    t["exit_ts"] = pd.to_datetime(t["exit_ts"])
    pnl = t["pnl_comm_usd"]
    net = pnl.sum()
    gp = pnl[pnl > 0].sum()

    srt = np.sort(pnl.values)[::-1]                       # best first
    k5 = max(1, int(0.05 * len(srt)))
    top5_share = srt[:k5][srt[:k5] > 0].sum() / gp * 100 if gp else 0.0

    # Lorenz curve of winners (for the concentration chart)
    w = np.sort(pnl[pnl > 0].values)
    if len(w):
        cumw = np.cumsum(w) / w.sum()
        lorenz_x = np.insert(np.arange(1, len(w) + 1) / len(w), 0, 0.0)
        lorenz_y = np.insert(cumw, 0, 0.0)
    else:
        lorenz_x = lorenz_y = np.array([0.0, 1.0])

    t["ym"] = t["exit_ts"].dt.to_period("M").astype(str)
    bym = t.groupby("ym")["pnl_comm_usd"].sum()
    best_m = bym.idxmax()
    t["yr"] = t["exit_ts"].dt.year
    byy = t.groupby("yr")["pnl_comm_usd"].sum()
    best_y = byy.idxmax()

    sides = {}
    for s in ("long", "short"):
        ps = pnl[t["direction"] == s]
        sides[s] = {"n": int(len(ps)), "net": round(ps.sum(), 0),
                    "pf": round(_pf(ps), 2),
                    "wr": round((ps > 0).mean() * 100, 0) if len(ps) else 0.0}

    by_hour = t.assign(hr=t["entry_ts"].dt.hour).groupby("hr")["pnl_comm_usd"].sum()

    return {
        "net": round(net, 0),
        "gini_winners": round(_gini(pnl[pnl > 0].values), 3),
        "top5pct_share_pct": round(top5_share, 1),
        "best_month": best_m, "best_month_net": round(bym.max(), 0),
        "net_ex_best_month": round(net - bym.max(), 0),
        "pf_ex_best_month": round(_pf(pnl[t["ym"] != best_m]), 3),
        "best_year": int(best_y), "best_year_net": round(byy.max(), 0),
        "net_ex_best_year": round(net - byy.max(), 0),
        "pf_ex_best_year": round(_pf(pnl[t["yr"] != best_y]), 3),
        "by_side": sides,
        "by_hour": by_hour,
        "lorenz": (lorenz_x, lorenz_y),
        "pos_months": int((bym > 0).sum()), "tot_months": int(len(bym)),
    }


def fill_perturbation(trades, cash, sigma_usd=0.10, adverse_usd=0.10,
                      n=5000, seed=7):
    """Perturbation-survival on EXECUTION: jitter every entry and exit fill by
    Gaussian price noise (±sigma_usd/oz) n times and recompute the whole run, to
    see if the edge survives imprecise fills or rests on knife-edge executions.
    Also a deterministic adverse shift (every fill adverse_usd/oz worse on BOTH
    sides) as a worst-case. Returns return/maxDD percentiles + survival rates."""
    pnl0 = trades["pnl_comm_usd"].values.astype(float)
    size = trades["lots"].values.astype(float) * 100.0      # oz
    L = len(pnl0)
    rng = np.random.default_rng(seed)

    rets = np.empty(n); dds = np.empty(n); pfs = np.empty(n)
    for i in range(n):
        dpnl = size * (rng.normal(0, sigma_usd, L) - rng.normal(0, sigma_usd, L))
        pnl = pnl0 + dpnl
        bal = cash + np.cumsum(pnl)
        peak = np.maximum.accumulate(np.maximum(bal, cash))
        dds[i] = ((peak - bal) / peak).max()
        rets[i] = bal[-1] / cash - 1
        gl = abs(pnl[pnl <= 0].sum())
        pfs[i] = (pnl[pnl > 0].sum() / gl) if gl else np.inf

    # deterministic worst-case: both fills adverse by adverse_usd
    adv_pnl = pnl0 - size * (2.0 * adverse_usd)
    adv_bal = cash + np.cumsum(adv_pnl)
    adv_peak = np.maximum.accumulate(np.maximum(adv_bal, cash))
    return {
        "sigma_usd": sigma_usd, "adverse_usd": adverse_usd, "n": n,
        "ret_p5": round(np.percentile(rets, 5) * 100),
        "ret_p50": round(np.percentile(rets, 50) * 100),
        "ret_p95": round(np.percentile(rets, 95) * 100),
        "dd_p50": round(np.percentile(dds, 50) * 100, 1),
        "dd_p95": round(np.percentile(dds, 95) * 100, 1),
        "dd_p99": round(np.percentile(dds, 99) * 100, 1),
        "p_profitable": round((rets > 0).mean() * 100, 1),
        "p_pf_over_1_2": round((pfs > 1.2).mean() * 100, 1),
        "p_dd_under_10": round((dds < 0.10).mean() * 100, 1),
        "adv_ret_pct": round((adv_bal[-1] / cash - 1) * 100),
        "adv_pf": round(_pf(pd.Series(adv_pnl)), 3),
        "adv_dd_pct": round(((adv_peak - adv_bal) / adv_peak).max() * 100, 1),
    }


def plot_concentration(conc, path, symbol="IFVG"):
    """Lorenz curve of winners (concentration), net P&L by entry hour, by side."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle(f"{symbol} IFVG — Edge concentration",
                 fontsize=13, fontweight="bold")

    ax = axes[0]
    by_hour = conc["by_hour"]
    lx, ly = conc["lorenz"]
    ax.set_title(f"Profit concentration (Gini {conc['gini_winners']})")
    ax.set_xlabel("Cumulative share of winning trades")
    ax.set_ylabel("Cumulative share of gross profit")
    ax.plot([0, 1], [0, 1], color="#999", ls="--", label="perfectly even")
    ax.plot(lx, ly, color="#2196F3", lw=1.6, label="Lorenz (winners)")
    ax.fill_between(lx, ly, lx, alpha=0.10, color="#2196F3")
    ax.text(0.05, 0.80, f"top 5% of trades =\n{conc['top5pct_share_pct']}% of gross profit",
            transform=ax.transAxes, fontsize=10)
    ax.legend(loc="upper left"); ax.grid(alpha=0.25)

    ax = axes[1]
    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in by_hour.values]
    ax.bar(by_hour.index, by_hour.values, color=colors)
    ax.set_title("Net P&L by entry hour (server)"); ax.set_xlabel("Hour")
    ax.set_ylabel("Net P&L (USD)"); ax.grid(alpha=0.25)

    ax = axes[2]
    s = conc["by_side"]
    names = ["long", "short"]
    vals = [s["long"]["net"], s["short"]["net"]]
    ax.bar(names, vals, color=["#2196F3", "#FF9800"])
    for i, (nm, v) in enumerate(zip(names, vals)):
        ax.text(i, v, f"PF {s[nm]['pf']}\nWR {s[nm]['wr']:.0f}%\n{s[nm]['n']}tr",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=10)
    ax.set_title("Net P&L by side"); ax.set_ylabel("Net P&L (USD)")
    ax.grid(alpha=0.25)

    plt.tight_layout(); plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def _profit_r(trades):
    """Per-trade profit in R-multiples (net P&L / dollars risked to the stop)."""
    risk = (trades["entry"] - trades["sl"]).abs() * trades["lots"] * 100.0
    return trades["pnl_comm_usd"] / risk.replace(0, np.nan)


def mfe_mae_stats(trades):
    """Trade-efficiency stats from max favourable/adverse excursion (in R):
    are stops well placed, and how much of the favourable move is captured?"""
    if not {"mfe_r", "mae_r"}.issubset(trades.columns):
        return {}
    pr = _profit_r(trades)
    win = trades["pnl_comm_usd"] > 0
    mae_mean = trades["mae_r"].mean()
    cap = (pr[win] / trades.loc[win, "mfe_r"].replace(0, np.nan)).mean()
    return {
        "avg_mfe_r": round(trades["mfe_r"].mean(), 2),
        "avg_mae_r": round(mae_mean, 2),
        "edge_ratio": round(trades["mfe_r"].mean() / mae_mean, 2) if mae_mean else 0.0,
        "winners_mae_r": round(trades.loc[win, "mae_r"].mean(), 2),   # heat winners take
        "losers_mfe_r": round(trades.loc[~win, "mfe_r"].mean(), 2),   # how far losers ran first
        "mfe_capture_pct": round(cap * 100, 0) if cap == cap else 0.0,
    }


def plot_mfe_mae(trades, path, symbol="IFVG"):
    """Two scatters: max favourable excursion vs profit kept, and max adverse
    excursion (heat taken) vs profit — both in R. Reveals stop/target quality."""
    if not {"mfe_r", "mae_r"}.issubset(trades.columns):
        return
    pr = _profit_r(trades)
    win = (trades["pnl_comm_usd"] > 0).values
    mfe, mae = trades["mfe_r"].values, trades["mae_r"].values
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5.5))
    fig.suptitle(f"{symbol} IFVG — MFE / MAE  (trade efficiency, in R)",
                 fontsize=13, fontweight="bold")

    lim = max(np.nanpercentile(mfe, 99), 2.0)
    ax1.plot([0, lim], [0, lim], color="#999", ls="--", lw=0.8, label="profit = MFE (kept it all)")
    ax1.scatter(mfe[win], pr.values[win], s=8, c="#4CAF50", alpha=0.4, label="winner")
    ax1.scatter(mfe[~win], pr.values[~win], s=8, c="#F44336", alpha=0.4, label="loser")
    ax1.set_xlim(0, lim); ax1.set_xlabel("MFE (R — max favourable)")
    ax1.set_ylabel("Profit (R)"); ax1.set_title("Favourable excursion vs profit kept")
    ax1.legend(fontsize=8); ax1.grid(alpha=0.25)

    ax2.axvline(-1.0, color="#D32F2F", ls="--", lw=1.0, label="−1R (initial stop)")
    ax2.scatter(-mae[win], pr.values[win], s=8, c="#4CAF50", alpha=0.4, label="winner")
    ax2.scatter(-mae[~win], pr.values[~win], s=8, c="#F44336", alpha=0.4, label="loser")
    ax2.set_xlabel("MAE (R — max adverse / heat)"); ax2.set_ylabel("Profit (R)")
    ax2.set_title("Adverse excursion vs profit"); ax2.legend(fontsize=8); ax2.grid(alpha=0.25)

    plt.tight_layout(); plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def plot_returns_heatmap(eq, path, symbol="IFVG", cash=10_000.0):
    """Year × month returns heatmap (%), with each year's compounded total."""
    e = eq.set_index("ts")["balance"]
    m = e.resample("ME").last().dropna()
    if len(m) < 2:
        return
    prev = m.shift(1); prev.iloc[0] = cash          # first month's base = start cash
    ret = (m / prev - 1.0) * 100.0
    d = pd.DataFrame({"r": ret.values, "year": ret.index.year, "month": ret.index.month})
    piv = d.pivot_table(index="year", columns="month", values="r").reindex(columns=range(1, 13))
    ytot = (d.assign(g=1 + d["r"] / 100).groupby("year")["g"].prod() - 1) * 100
    arr, years = piv.values, piv.index.tolist()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    fig, ax = plt.subplots(figsize=(13, 0.55 * len(years) + 2))
    norm = matplotlib.colors.TwoSlopeNorm(vmin=min(np.nanmin(arr), -0.1),
                                          vcenter=0.0, vmax=max(np.nanmax(arr), 0.1))
    ax.imshow(arr, cmap="RdYlGn", norm=norm, aspect="auto")
    ax.set_xticks(range(12)); ax.set_xticklabels(months)
    ax.set_yticks(range(len(years))); ax.set_yticklabels(years)
    for i in range(len(years)):
        for j in range(12):
            v = arr[i, j]
            if v == v:
                ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=8)
        ax.text(12.6, i, f"{ytot.get(years[i], float('nan')):+.0f}%",
                ha="left", va="center", fontsize=9, fontweight="bold")
    ax.text(12.6, -0.75, "Year", ha="left", fontsize=9, fontweight="bold")
    ax.set_xlim(-0.5, 14.0)
    ax.set_title(f"{symbol} IFVG — Monthly returns (%)", fontsize=13, fontweight="bold")
    plt.tight_layout(); plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def plot_seasonality(trades, path, symbol="IFVG"):
    """Entries and net P&L by hour, day-of-week and month (the FXBlue-style grid)."""
    t = trades.copy()
    t["entry_ts"] = pd.to_datetime(t["entry_ts"])
    t["hour"] = t["entry_ts"].dt.hour
    t["dow"] = t["entry_ts"].dt.dayofweek
    t["moy"] = t["entry_ts"].dt.month
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    moys = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    specs = [("hour", list(range(24)), [str(h) for h in range(24)], "hour"),
             ("dow", list(range(7)), dows, "day of week"),
             ("moy", list(range(1, 13)), moys, "month")]

    fig, axes = plt.subplots(2, 3, figsize=(16, 8))
    fig.suptitle(f"{symbol} IFVG — Seasonality (entries & net P&L)",
                 fontsize=13, fontweight="bold")
    for col, (key, idx, labels, name) in enumerate(specs):
        cnt = t.groupby(key).size().reindex(idx, fill_value=0)
        net = t.groupby(key)["pnl_comm_usd"].sum().reindex(idx, fill_value=0.0)
        axes[0, col].bar(range(len(idx)), cnt.values, color="#2196F3")
        axes[0, col].set_title(f"Entries by {name}")
        bar_c = ["#4CAF50" if v >= 0 else "#F44336" for v in net.values]
        axes[1, col].bar(range(len(idx)), net.values, color=bar_c)
        axes[1, col].set_title(f"Net P&L by {name}"); axes[1, col].set_xlabel(name)
        for r in (0, 1):
            axes[r, col].set_xticks(range(len(idx)))
            axes[r, col].set_xticklabels(labels, fontsize=7)
            axes[r, col].grid(alpha=0.25)
    axes[0, 0].set_ylabel("Entries"); axes[1, 0].set_ylabel("Net P&L (USD)")
    plt.tight_layout(); plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def plot_trades(trades, m5, path, symbol="IFVG", n=6):
    """A grid of example trades on 5-minute candles — the 5m FVG entry band, the
    SL/TP, and the entry/exit markers — so the mechanics are visible."""
    if "fvg_width" not in trades.columns or m5 is None or len(m5) == 0:
        return
    bars = m5.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"]).dt.tz_localize(None)
    t = trades.copy()
    t["entry_ts"] = pd.to_datetime(t["entry_ts"]); t["exit_ts"] = pd.to_datetime(t["exit_ts"])
    t["dur_h"] = (t["exit_ts"] - t["entry_ts"]).dt.total_seconds() / 3600.0
    risk = (t["entry"] - t["sl"]).abs() * t["lots"] * 100.0
    t["R"] = t["pnl_comm_usd"] / risk.replace(0, np.nan)
    t = t[(t["dur_h"] < 8) & t["R"].notna()]                    # clean, normal trades
    wins = t[t["pnl_comm_usd"] > 0].sort_values("R", ascending=False)
    loss = t[t["pnl_comm_usd"] <= 0].sort_values("R")
    picks = [wins.iloc[int(i)] for i in
             np.linspace(0, len(wins) - 1, min(4, len(wins))).astype(int)] if len(wins) else []
    picks += [loss.iloc[k * max(1, len(loss) // 2)] for k in range(min(2, len(loss)))]
    picks = picks[:n]
    if not picks:
        return

    rows = (len(picks) + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(15, 4.0 * rows), squeeze=False)
    fig.suptitle(f"{symbol} IFVG — example trades (5m candles)",
                 fontsize=13, fontweight="bold")
    for ax, tr in zip(axes.flat, picks):
        e, x = tr["entry_ts"], tr["exit_ts"]
        w = bars[(bars["timestamp"] >= e - pd.Timedelta(hours=3)) &
                 (bars["timestamp"] <= x + pd.Timedelta(hours=1))].reset_index(drop=True)
        if w.empty:
            ax.axis("off"); continue
        col = np.where(w["close"] >= w["open"], "#26A69A", "#EF5350")
        ax.vlines(range(len(w)), w["low"], w["high"], color=col, lw=0.6)
        ax.bar(range(len(w)), (w["close"] - w["open"]).abs().clip(lower=0.01),
               bottom=w[["open", "close"]].min(axis=1), width=0.6, color=col)
        half = (tr.get("fvg_width") or 0.0) / 2.0
        if half:
            ax.axhspan(tr["entry"] - half, tr["entry"] + half, color="#1E88E5", alpha=0.10)
        ax.axhline(tr["entry"], color="#1E88E5", lw=1.0)            # entry / FVG mid
        ax.axhline(tr["sl"], color="#E53935", lw=1.0, ls="--")     # stop
        ax.axhline(tr["tp"], color="#43A047", lw=1.0, ls="--")     # target
        ei = int((w["timestamp"] - e).abs().values.argmin())
        xi = int((w["timestamp"] - x).abs().values.argmin())
        ax.scatter([ei], [tr["entry"]], marker="^" if tr["direction"] == "long" else "v",
                   color="#1E88E5", s=90, zorder=5, edgecolors="white")
        ax.scatter([xi], [tr["exit"]], marker="o", s=70, zorder=5, edgecolors="white",
                   color="#43A047" if tr["pnl_comm_usd"] > 0 else "#E53935")
        res = "WIN" if tr["pnl_comm_usd"] > 0 else "LOSS"
        ax.set_title(f"{tr['direction'].upper()}  {res} {tr['R']:+.2f}R  "
                     f"{tr['dur_h']:.1f}h  {e:%Y-%m-%d %H:%M}", fontsize=9)
        ax.set_xticks([]); ax.grid(alpha=0.2)
    for ax in axes.flat[len(picks):]:
        ax.axis("off")
    fig.text(0.5, 0.005, "blue line/band = entry (5m FVG)   ·   red dashed = stop   "
             "·   green dashed = target   ·   triangle = entry   ·   circle = exit",
             ha="center", fontsize=9, color="#555")
    plt.tight_layout(rect=[0, 0.02, 1, 1])
    plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def build_monthly(trades, eq, cash):
    """Per-month: trades, net P&L, win rate, and worst drawdown reached."""
    t = trades.copy()
    t["month"] = pd.to_datetime(t["exit_ts"]).dt.to_period("M").astype(str)
    by = t.groupby("month").agg(
        trades=("pnl_comm_usd", "count"),
        net=("pnl_comm_usd", "sum"),
        wr=("pnl_comm_usd", lambda x: (x > 0).mean() * 100),
    )
    e = eq.copy()
    e["month"] = pd.to_datetime(e["ts"]).dt.to_period("M").astype(str)
    by["maxDD_pct"] = e.groupby("month")["drawdown_pct"].max()   # vs running peak
    by["net"] = by["net"].round(0)
    by["wr"] = by["wr"].round(0)
    by["maxDD_pct"] = by["maxDD_pct"].round(1)
    return by.reset_index()


def _stats_lines(m, ab):
    """Format the metrics dict (+ alpha/beta) into aligned text lines for the
    side panel of the equity chart."""
    def g(k, suf=""):
        v = m.get(k)
        return f"{v}{suf}" if v is not None else "—"
    lines = [
        "PERFORMANCE",
        f"  Net P&L     ${g('net_pnl_usd')}",
        f"  Final bal   ${g('final_balance')}",
        f"  CAGR        {g('cagr_pct', '%')}",
        f"  Profit fac  {g('profit_factor')}",
        f"  Win rate    {m['winrate'] * 100:.1f}%" if 'winrate' in m else "  Win rate    —",
        f"  EV gross/tr ${g('ev_gross_usd')}",
        f"  Cost/trade  -${g('cost_per_trade_usd')}",
        f"  Net EV/tr   ${g('net_ev_usd')}  ({g('net_ev_r')}R)",
        f"  Payoff      {g('payoff_ratio')}",
        "",
        "RISK",
        f"  Max DD      {g('max_drawdown_pct', '%')}",
        f"  Sharpe      {g('sharpe_ann')}",
        f"  Sortino     {g('sortino_ann')}",
        f"  Calmar      {g('calmar')}",
        f"  Recovery    {g('recovery_factor')}",
        f"  Max cons L  {g('max_consec_losses')}",
        f"  Win months  {g('pct_winning_months', '%')}",
        f"  Skew        {g('ret_skew')}",
        f"  Equity lin  {g('equity_lr_corr')}",
        "",
        "TRADES",
        f"  N trades    {g('n_trades')}",
        f"  Avg dur     {g('avg_duration_h', 'h')}",
        f"  Med dur     {g('median_duration_h', 'h')}",
        f"  >24h held   {g('pct_held_over_24h', '%')}",
    ]
    if ab:
        lines += ["", "VS GOLD (B&H)",
                  f"  Beta        {ab['beta']}",
                  f"  Corr        {ab['corr']}",
                  f"  Alpha       {ab['alpha_ann_pct']}%/yr"]
    return lines


def plot_equity(eq, path, symbol="IFVG", metrics=None, ab=None):
    if eq.empty:
        return
    fig = plt.figure(figsize=(16, 7))
    gs = fig.add_gridspec(2, 2, width_ratios=[4, 1], height_ratios=[3, 1],
                          hspace=0.08, wspace=0.03)
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
    axT = fig.add_subplot(gs[:, 1]); axT.axis("off")
    fig.suptitle(f"{symbol} IFVG — Equity Curve", fontsize=13, fontweight="bold")
    ax1.plot(eq["ts"], eq["balance"], color="#2196F3", lw=1.2, label="Balance")
    ax1.fill_between(eq["ts"], eq["balance"], alpha=0.08, color="#2196F3")
    ax1.set_ylabel("Balance (USD)"); ax1.legend(loc="upper left"); ax1.grid(alpha=0.25)
    ax2.fill_between(eq["ts"], -eq["drawdown"], color="#F44336", alpha=0.5, label="Drawdown")
    ax2.set_ylabel("Drawdown (USD)"); ax2.set_xlabel("Date")
    ax2.legend(loc="lower left"); ax2.grid(alpha=0.25)
    if metrics:
        axT.text(0.0, 1.0, "\n".join(_stats_lines(metrics, ab)), va="top", ha="left",
                 family="monospace", fontsize=9, transform=axT.transAxes)
    plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def plot_monte_carlo(arrays, cash, path, symbol="IFVG"):
    """Three panels from the bootstrap resamples: a fan of sample equity paths,
    the final-return distribution, and the (order-dependent) max-drawdown one."""
    if not arrays:
        return
    b = arrays["bootstrap"]
    fr, md, paths = b["fr"] * 100, b["md"] * 100, b["paths"]
    n = len(fr)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle(f"{symbol} IFVG — Monte Carlo (bootstrap, {n:,} resamples)",
                 fontsize=13, fontweight="bold")

    ax = axes[0]
    for bal in paths:
        ax.plot(bal, color="#2196F3", lw=0.5, alpha=0.07)
    ax.axhline(cash, color="#555", ls="--", lw=0.9)
    ax.set_title("Sample equity paths"); ax.set_xlabel("Trade #")
    ax.set_ylabel("Balance (USD)"); ax.grid(alpha=0.25)

    ax = axes[1]
    ax.hist(fr, bins=60, color="#2196F3", alpha=0.85)
    for p, c, ls in [(5, "#F44336", "--"), (50, "#000", "-"), (95, "#4CAF50", "--")]:
        v = np.percentile(fr, p)
        ax.axvline(v, color=c, ls=ls, lw=1.2, label=f"p{p}: {v:.0f}%")
    ax.set_title("Final return distribution"); ax.set_xlabel("Return (%)")
    ax.set_ylabel("Frequency"); ax.legend(); ax.grid(alpha=0.25)

    ax = axes[2]
    ax.hist(md, bins=60, color="#F44336", alpha=0.85)
    for p, c in [(50, "#000"), (95, "#FF9800"), (99, "#9C27B0")]:
        v = np.percentile(md, p)
        ax.axvline(v, color=c, ls="--", lw=1.2, label=f"p{p}: {v:.1f}%")
    ax.axvline(10, color="#D32F2F", ls="-", lw=1.6, label="FTMO 10%")
    ax.set_title("Max drawdown distribution"); ax.set_xlabel("Max drawdown (%)")
    ax.set_ylabel("Frequency"); ax.legend(); ax.grid(alpha=0.25)

    plt.tight_layout(); plt.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def main():
    args = parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    if args.validate:
        end = (pd.Timestamp(start) + pd.DateOffset(months=1)).to_pydatetime()
        print(f"[run] VALIDATION mode — {start.date()} -> {end.date()}")

    # 1) history from MT5 (built from ticks)
    print(f"[run] Connecting to MT5 and fetching {args.symbol} ticks …")
    mt5_feed.connect(path=args.mt5_path, login=args.mt5_login,
                     password=args.mt5_password, server=args.mt5_server)
    try:
        frames = mt5_feed.fetch_all_range(args.symbol, start, end)
    finally:
        mt5_feed.shutdown()

    # 2) backtrader: m1 first so it is the system clock. The m1 feed carries the
    # real per-bar spread (cushioned x SPREAD_MULT); the strategy charges half of
    # it on each market exit — a variable, news-aware cost, not a flat constant.
    raw_med = float(frames["m1"]["spread"].clip(lower=args.spread_floor).median())
    med_spread = raw_med * SPREAD_MULT
    floor_note = f", floor ${args.spread_floor:.2f}" if args.spread_floor else ""
    print(f"[run] cash=${args.cash:,.0f}  spread~${med_spread:.3f} median "
          f"(~{med_spread/0.10:.1f} pips, x{SPREAD_MULT}{floor_note}, variable per-bar)  "
          f"comm={COMMISSION_PCT*100:.4f}%  swap=${SWAP_USD_PER_LOT_PER_NIGHT}/lot/night"
          f"  leverage={LEVERAGE:.0f}  risk={args.risk_pct*100:.2f}%")
    cerebro = build_cerebro(frames, args.cash, args.risk_pct, args.time_stop_h,
                            args.friday_flat_h, printlog=args.printlog,
                            spread_floor=args.spread_floor)
    strat = cerebro.run(runonce=False)[0]

    # 3) report + export
    metrics, trades, eq = build_report(strat, args.cash)
    ab = alpha_beta(eq, frames["m1"]) if metrics else None
    mc, mc_arrays = (monte_carlo(trades["pnl_comm_usd"], args.cash)
                     if metrics else (None, None))
    if args.out is not None:
        out = args.out + ("_validate" if args.validate else "")
    else:
        base = os.path.join("backtest", datetime.now().strftime("%Y-%m-%d"))
        out = unique_out_dir(base + ("_validate" if args.validate else ""))
    os.makedirs(out, exist_ok=True)
    trades.to_csv(f"{out}/trades.csv", index=False)
    eq.to_csv(f"{out}/equity.csv", index=False)
    plot_equity(eq, f"{out}/equity_curve.png", args.symbol, metrics, ab)
    plot_monte_carlo(mc_arrays, args.cash, f"{out}/monte_carlo.png", args.symbol)
    plot_mfe_mae(trades, f"{out}/mfe_mae.png", args.symbol)
    plot_returns_heatmap(eq, f"{out}/returns_heatmap.png", args.symbol, args.cash)
    plot_seasonality(trades, f"{out}/seasonality.png", args.symbol)
    plot_trades(trades, frames["m5"], f"{out}/trade_examples.png", args.symbol)

    print("\n" + "=" * 50)
    print(f"  {args.symbol} IFVG — Performance Report")
    print("=" * 50)
    if metrics:
        for k, v in metrics.items():
            print(f"  {k:<18}: {v}")
        # longest trade detail
        idx = (pd.to_datetime(trades["exit_ts"]) - pd.to_datetime(trades["entry_ts"])).idxmax()
        lt = trades.loc[idx]
        print(f"  longest trade     : {metrics['max_duration_h']:.0f}h  "
              f"{lt['entry_ts']:%Y-%m-%d %H:%M} -> {lt['exit_ts']:%Y-%m-%d %H:%M} "
              f"({lt['direction']}, ${lt['pnl_comm_usd']:.0f})")
        # transaction costs actually charged (commission is in pnl_comm already;
        # spread + swap are the explicit per-trade deductions)
        if {"spread_usd", "swap_usd"}.issubset(trades.columns):
            tot_spread = trades["spread_usd"].sum()
            tot_swap = trades["swap_usd"].sum()
            n_roll = int((trades["swap_usd"] > 0).sum())
            print(f"\n  Transaction costs charged:")
            print(f"    spread (variable): -${tot_spread:,.0f}")
            print(f"    swap (overnight) : -${tot_swap:,.0f}  "
                  f"({n_roll} trades rolled over)")
        dd = strat.daily_dd_pct
        if dd:
            worst_day = max(dd, key=dd.get)
            over5 = sum(1 for v in dd.values() if v >= 5.0)
            print(f"\n  Daily drawdown (FTMO ~5% limit):")
            print(f"    worst day      : {dd[worst_day]:.2f}%  ({worst_day})")
            print(f"    days >= 5%      : {over5}")
        # monthly breakdown -> CSV + printed table
        monthly = build_monthly(trades, eq, args.cash)
        monthly.to_csv(f"{out}/monthly.csv", index=False)
        print(f"\n  Monthly (net $ / trades / WR / worst DD%):")
        for _, r in monthly.iterrows():
            print(f"    {r['month']}  {r['net']:>8,.0f}  {int(r['trades']):>4}tr  "
                  f"{r['wr']:>3.0f}%  DD {r['maxDD_pct']:>4.1f}%")
        # alpha/beta vs gold benchmark (computed above, reused here)
        if ab:
            print(f"\n  Alpha/Beta vs buy-and-hold gold:")
            print(f"    beta {ab['beta']} (corr {ab['corr']})   "
                  f"alpha {ab['alpha_ann_pct']}%/yr (return not from the gold trend)")
        # Monte Carlo: distribution over resampled trade sequences (computed above)
        print(f"\n  Monte Carlo (10k resamples — return p5/50/95, maxDD p50/95/99):")
        for name in ("reshuffle", "bootstrap"):
            m = mc[name]
            print(f"    {name:>9}: ret {m['ret_p5']}/{m['ret_p50']}/{m['ret_p95']}%  "
                  f"maxDD {m['dd_p50']}/{m['dd_p95']}/{m['dd_p99']}%  "
                  f"P(DD>10%) {m['p_dd_over_10']}%  P(loss) {m['p_losing']}%")

        # Concentration: is the edge broad, or a few trades / one month / one side?
        conc = concentration(trades)
        plot_concentration(conc, f"{out}/concentration.png", args.symbol)
        sl, ss = conc["by_side"]["long"], conc["by_side"]["short"]
        print(f"\n  Edge concentration (broad = robust):")
        print(f"    Gini (winners)   : {conc['gini_winners']}  "
              f"(top 5% of trades = {conc['top5pct_share_pct']}% of gross profit)")
        print(f"    ex best month    : net ${conc['net_ex_best_month']:,.0f} "
              f"(PF {conc['pf_ex_best_month']})  [removed {conc['best_month']} "
              f"${conc['best_month_net']:,.0f}]")
        print(f"    ex best year     : net ${conc['net_ex_best_year']:,.0f} "
              f"(PF {conc['pf_ex_best_year']})  [removed {conc['best_year']} "
              f"${conc['best_year_net']:,.0f}]")
        print(f"    by side          : long  ${sl['net']:>8,.0f}  PF {sl['pf']}  "
              f"WR {sl['wr']:.0f}%  ({sl['n']}tr)")
        print(f"                       short ${ss['net']:>8,.0f}  PF {ss['pf']}  "
              f"WR {ss['wr']:.0f}%  ({ss['n']}tr)")

        # Perturbation survival: does the edge survive imprecise fills?
        fp = fill_perturbation(trades, args.cash)
        print(f"\n  Perturbation survival — ±${fp['sigma_usd']}/oz fill jitter "
              f"({fp['n']} runs):")
        print(f"    return p5/50/95  : {fp['ret_p5']}/{fp['ret_p50']}/{fp['ret_p95']}%  "
              f"maxDD p50/95/99 {fp['dd_p50']}/{fp['dd_p95']}/{fp['dd_p99']}%")
        print(f"    survival         : P(profit) {fp['p_profitable']}%  "
              f"P(PF>1.2) {fp['p_pf_over_1_2']}%  P(DD<10%) {fp['p_dd_under_10']}%")
        print(f"    adverse worst-case ({fp['adverse_usd']}/oz worse both sides): "
              f"ret {fp['adv_ret_pct']}%  PF {fp['adv_pf']}  maxDD {fp['adv_dd_pct']}%")
    else:
        print("  No trades generated in this period.")
    print("=" * 50)
    print(f"\n[run] Saved trades / equity / chart to: {out}/")


if __name__ == "__main__":
    main()
