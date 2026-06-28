"""
strategy.py  —  IFVGStrategy: the backtrader bridge for the IFVG rule
=====================================================================
Wraps the framework-agnostic rule in ifvg/entry_logic.py into a backtrader
Strategy. It is responsible for everything backtrader-specific:

    * reading the multi-timeframe feeds and exposing recent bars as DataFrames
    * calling generate_signal() once per closed 1-minute bar
    * sizing the trade (risk_lots), entering, and managing the exit
    * MANUAL exit management (SL / TP / break-even) — see _manage_exits
    * recording closed trades, the equity curve and the daily drawdown

MULTI-TIMEFRAME WIRING (done in run_backtest.py)
------------------------------------------------
Three feeds are added, in this order, by name:
        m1  (= self.datas[0], the system clock)   m5   h1
next() fires once per closed 1-minute bar; the 5m and 1h feeds only advance when
their own candle closes, which makes the rule look-ahead-safe for free.
(Two earlier rule variants — and the m15/h4/d1 feeds and daily-bias they needed —
were removed: the validated edge is entirely in this one.)

HOW ENTRIES WORK
----------------
The entry is a resting LIMIT at the FVG midpoint (sig['entry']), valid for
ENTRY_TTL_MIN minutes, NOT a market order. Price must retrace into the gap, so
the fill matches the level SL/TP and risk sizing were built from. A market entry
filled wherever price was — often dollars from the planned level — which warped
the reward:risk and produced multi-week trades that blocked the single slot.
Unfilled limits self-cancel via `valid` (notify_order frees the slot).

WHY EXITS ARE MANUAL
--------------------
Resting Stop+Limit (even as a native OCO pair) can BOTH fill inside the same
1-minute bar — especially after break-even, where the stop sits right next to
price — flipping the position into an unprotected, stale state that then runs
for months. That bug was caught here. The fix: no resting exit orders at all.
Each bar we check the bar's high/low against the stop/target and, when touched,
send ONE market close. A single close can never double-fill or flip the
position. Cost: the close fills at the next bar's open rather than exactly at
SL/TP (slightly pessimistic, fully robust).

All strategy numbers are FIXED (see ifvg.entry_logic and ifvg.position
constants). The only Strategy param is `printlog` for debugging.
"""
from datetime import timedelta

import backtrader as bt
import pandas as pd

from ifvg import entry_logic
from ifvg.position import (risk_lots, breakeven_stop, swap_cost, RISK_PCT,
                           MIN_LOTS, MAX_LOTS, OZ_PER_LOT, TIME_STOP_HOURS,
                           FRIDAY_FLAT_HOUR)

# rolling buffer depth handed to the rule (must cover entry_logic's tail() needs)
H1_BUF_BARS = 80
M5_BUF_BARS = 80


class IFVGStrategy(bt.Strategy):
    # risk_pct can be raised for a funding challenge (reach target faster) and
    # set back to RISK_PCT (0.5%) once funded; everything else is fixed (XAUUSD).
    # time_stop_h: force-close a trade after this many hours (None = disabled).
    # Caps the break-even grind: a trade that reaches 1:1 can otherwise hover
    # between entry and target for days, holding the single slot and starving new
    # signals (>24h trades occupy ~35% of all slot-time). 12h is the validated
    # default (raised return $21.6k->$35.3k and cut maxDD 11.9%->7.4%).
    # friday_flat_h: after this hour (broker/server time) on Fridays, close any
    # open position and stop opening new ones, so nothing is held over the
    # weekend (FTMO regular accounts disallow weekend holding; also avoids
    # uncompensated weekend gap risk). None = disabled.
    # use_breakeven: move the stop to entry at 1:1. On = fewer losers but some
    # winners get scratched at entry before reaching TP; off = let trades run to
    # SL/TP/time-stop. Tested both ways.
    params = dict(risk_pct=RISK_PCT, time_stop_h=TIME_STOP_HOURS,
                  friday_flat_h=FRIDAY_FLAT_HOUR, use_breakeven=True,
                  printlog=False)

    # -- setup ---------------------------------------------------------------

    def __init__(self):
        # feeds resolved by name (added in run_backtest.py: m1, m5, h1)
        d = {data._name: data for data in self.datas}
        self.d1m = d["m1"]      # execution clock + current bar
        self.d5m = d["m5"]      # internal sweep + entry FVG
        self.d1h = d["h1"]      # setup zones (1h FVGs)

        self.state = entry_logic.new_state()

        # cached rolling buffers: the h1/m5 OHLC frames only change when a new
        # h1/m5 bar CLOSES, but next() fires every 1m bar (~2M times). Rebuilding
        # both DataFrames every bar dominated runtime; we now rebuild only when
        # the feed advances. Exact (closed bars are immutable between closes).
        self._h1_len = self._m5_len = -1
        self._h1_buf = self._m5_buf = None

        # order / open-trade bookkeeping (manual exits -> no resting orders)
        self.entry_order = None
        self.exit_order = None   # the market close order while one is in flight
        self.cur = None          # dict describing the currently open trade

        # outputs consumed by run_backtest.py
        self.closed_trades = []  # -> trades.csv
        self.equity_curve = []   # [(datetime, balance)] -> equity.csv

        # equity tracked from realised pnl (deterministic; the leveraged broker
        # value() is unreliable). Drives risk sizing and the drawdown metrics.
        self.account_equity = self.broker.startingcash

        # daily drawdown (the FTMO ~5%/day metric), measured intraday against
        # each day's opening realised balance.
        self._day = None
        self._day_start_eq = self.broker.startingcash
        self.daily_dd_pct = {}   # date -> worst intraday loss % that day

    # -- helpers -------------------------------------------------------------

    def _buf(self, data, size):
        """Last `size` closed bars of a feed -> standard OHLC DataFrame (UTC)."""
        n = min(size, len(data))
        if n == 0:
            return pd.DataFrame(columns=["timestamp", "open", "high",
                                         "low", "close", "volume"])
        ts, o, h, l, c, v = [], [], [], [], [], []
        for ago in range(n - 1, -1, -1):   # oldest first
            ts.append(bt.num2date(data.datetime[-ago]))
            o.append(data.open[-ago]); h.append(data.high[-ago])
            l.append(data.low[-ago]); c.append(data.close[-ago])
            v.append(data.volume[-ago])
        df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v})
        df["timestamp"] = pd.to_datetime(ts, utc=True)
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def _log(self, msg):
        if self.p.printlog:
            print(f"{bt.num2date(self.d1m.datetime[0]):%Y-%m-%d %H:%M}  {msg}")

    def _exit_spread(self):
        """The current m1 bar's spread (price units), used to charge a variable
        spread cost on the taker exit. 0.0 if the feed carries no spread line."""
        try:
            s = self.d1m.spread[0]
            return s if (s == s and s > 0) else 0.0   # s==s rejects NaN
        except (AttributeError, IndexError):
            return 0.0

    def _track_daily_dd(self, now):
        """
        Worst intraday equity loss BELOW the day's opening *realised* balance
        (the FTMO daily metric). Using realised balance as the reference means
        merely giving back unrealised open profit is not counted as drawdown,
        while a genuine loss (realised + adverse floating) is.
        """
        mtm = self.account_equity
        if self.position and self.cur is not None and self.cur.get("entry_px"):
            px = self.d1m.close[0]
            entry = self.cur["entry_px"]
            sz = self.cur.get("size", 0.0)
            mtm += ((px - entry) if self.cur["direction"] == "long"
                    else (entry - px)) * sz
        d = now.date()
        if d != self._day:
            self._day = d
            self._day_start_eq = self.account_equity
        if self._day_start_eq > 0:
            loss_pct = (self._day_start_eq - mtm) / self._day_start_eq * 100.0
            if loss_pct > self.daily_dd_pct.get(d, 0.0):
                self.daily_dd_pct[d] = loss_pct

    # -- main event ----------------------------------------------------------

    def next(self):
        now = pd.Timestamp(bt.num2date(self.d1m.datetime[0]), tz="UTC")
        self._track_daily_dd(now)

        # manage an open trade; never overlap trades (one position at a time)
        if self.position:
            if self.cur is None:                 # residual/phantom -> flatten
                if self.exit_order is None:
                    self.exit_order = self.close(data=self.d1m)
                return
            if self.cur["entry_px"] is None:     # first bar we see the fill
                self.cur["entry_px"] = self.position.price
                self.cur["entry_dt"] = bt.num2date(self.d1m.datetime[0])
                self.entry_order = None
                # fill-bar penetration past the limit (for the adverse-selection
                # proxy): how far price moved THROUGH the entry level on the fill
                # bar. Shallow = a clean tag-and-bounce (a winner you might MISS
                # live); deep = price sliced through (a loser you'd surely fill).
                px = self.cur["entry_px"]
                pen = (px - self.d1m.low[0] if self.cur["direction"] == "long"
                       else self.d1m.high[0] - px)
                self.cur["fill_penetration"] = round(max(float(pen), 0.0), 3)
            if self.exit_order is not None:      # close already sent; await fill
                return
            self._manage_exits()
            return
        if self.entry_order is not None:         # limit resting, awaiting retrace
            # Cancel it once it overstays ENTRY_TTL_MIN (deterministic; we do not
            # rely on the broker's `valid`, which proved unreliable here). An
            # un-cancelled limit holds the single slot hostage for the whole run.
            placed = self.cur.get("placed_dt") if self.cur else None
            if placed is not None and (now - placed) >= pd.Timedelta(
                    minutes=entry_logic.ENTRY_TTL_MIN):
                self.cancel(self.entry_order)
            return

        # flat: look for a fresh signal — but not into the Friday weekend close
        if self.p.friday_flat_h is not None and now.weekday() == 4 \
                and now.hour >= self.p.friday_flat_h:
            return
        bar = {"open": self.d1m.open[0], "high": self.d1m.high[0],
               "low": self.d1m.low[0], "close": self.d1m.close[0]}
        # rebuild a buffer only when its feed has advanced (a bar closed)
        h1_changed = len(self.d1h) != self._h1_len
        if h1_changed:
            self._h1_buf = self._buf(self.d1h, H1_BUF_BARS)
            self._h1_len = len(self.d1h)
        if len(self.d5m) != self._m5_len:
            self._m5_buf = self._buf(self.d5m, M5_BUF_BARS)
            self._m5_len = len(self.d5m)
        buf = {"h1": self._h1_buf, "m5": self._m5_buf}
        try:
            # h1_changed lets the rule skip the costly 1h FVG re-detection when
            # no 1h bar has closed (exact: the zone set cannot have changed)
            sig = entry_logic.generate_signal(now, bar, buf, self.state,
                                              h1_changed=h1_changed)
        except Exception as exc:    # one bad bar must not kill a multi-year run
            self._log(f"signal error: {exc}")
            return
        if sig is not None:
            self._enter(sig)

    # -- entry / exit --------------------------------------------------------

    def _enter(self, sig):
        """
        Size by fixed-fractional risk and rest a LIMIT entry at the FVG midpoint.

        A market fill lands wherever price happens to be — often several dollars
        from the planned FVG level — while SL/TP stay anchored to that level,
        which corrupts the reward:risk and spawns multi-week trades that block the
        single slot. A limit at the midpoint is the faithful ICT entry: price must
        retrace into the gap, so the fill matches the level SL/TP were built from
        (and risk sizing, which also uses sig['entry'], becomes accurate). The
        order self-cancels after ENTRY_TTL_MIN minutes (`valid`) so a retrace that
        never comes does not hold the slot hostage.
        """
        lots = risk_lots(self.account_equity, sig["entry"], sig["sl"],
                         risk_pct=self.p.risk_pct, oz_per_lot=OZ_PER_LOT,
                         min_lots=MIN_LOTS, max_lots=MAX_LOTS)
        if lots <= 0:
            return
        size = lots * OZ_PER_LOT
        now = pd.Timestamp(bt.num2date(self.d1m.datetime[0]), tz="UTC")
        self.cur = {
            "direction": sig["direction"], "sl": sig["sl"], "tp": sig["tp"],
            "initial_sl": sig["sl"], "size": size, "be_moved": False,
            "entry_dt": None, "entry_px": None, "plan_entry": sig["entry"],
            "placed_dt": now,   # when the limit was placed (for TTL cancellation)
            # diagnostic signal features (grounding analysis; not used by exits)
            "fvg_width": sig.get("fvg_width"), "sweep_depth": sig.get("sweep_depth"),
            "zone_age_h": sig.get("zone_age_h"), "zone_width": sig.get("zone_width"),
            "fill_penetration": None,   # set at fill (adverse-selection proxy)
            "mfe": 0.0, "mae": 0.0,     # max favorable / adverse excursion ($)
        }
        valid = bt.num2date(self.d1m.datetime[0]) + timedelta(
            minutes=entry_logic.ENTRY_TTL_MIN)
        if sig["direction"] == "long":
            self.entry_order = self.buy(data=self.d1m, size=size,
                                        exectype=bt.Order.Limit,
                                        price=sig["entry"], valid=valid)
        else:
            self.entry_order = self.sell(data=self.d1m, size=size,
                                         exectype=bt.Order.Limit,
                                         price=sig["entry"], valid=valid)
        self._log(f"LIMIT {sig['direction']} {lots:.2f} lots @ {sig['entry']}  "
                  f"sl={sig['sl']} tp={sig['tp']}  valid<= {valid:%H:%M}")

    def _manage_exits(self):
        """
        Manual SL / TP / break-even for the open trade. Break-even moves the
        stop to entry once price reaches 1:1; when the bar touches the stop or
        target, send ONE market close (fills next open). No resting orders ->
        no intra-bar double-fill / phantom position.
        """
        d = self.cur
        hi, lo = self.d1m.high[0], self.d1m.low[0]
        now_dt = bt.num2date(self.d1m.datetime[0])

        # track max favorable / adverse excursion (MFE/MAE) for the trade analysis
        e = d.get("entry_px")
        if e:
            if d["direction"] == "long":
                fav, adv = hi - e, e - lo
            else:
                fav, adv = e - lo, hi - e
            d["mfe"] = max(d.get("mfe", 0.0), fav, 0.0)
            d["mae"] = max(d.get("mae", 0.0), adv, 0.0)

        # Friday flat: close before the weekend so nothing is held over it
        if self.p.friday_flat_h is not None and now_dt.weekday() == 4 \
                and now_dt.hour >= self.p.friday_flat_h:
            self.exit_order = self.close(data=self.d1m)
            self._log("friday-flat -> close")
            return

        # time-stop: cut a trade that has overstayed, freeing the slot
        # (falsy time_stop_h — None or 0 — disables it)
        if self.p.time_stop_h and d.get("entry_dt") is not None:
            age_h = (bt.num2date(self.d1m.datetime[0])
                     - d["entry_dt"]).total_seconds() / 3600.0
            if age_h >= self.p.time_stop_h:
                self.exit_order = self.close(data=self.d1m)
                self._log(f"time-stop {age_h:.0f}h -> close")
                return

        if self.p.use_breakeven and not d["be_moved"]:
            new_sl = breakeven_stop(d["direction"], d["entry_px"], d["sl"], hi, lo)
            if new_sl is not None:
                d["sl"] = new_sl
                d["be_moved"] = True
                self._log(f"break-even -> SL {new_sl}")

        sl, tp = d["sl"], d["tp"]
        if d["direction"] == "long":
            hit = lo <= sl or hi >= tp     # pessimistic: SL assumed if both touched
        else:
            hit = hi >= sl or lo <= tp
        if hit:
            self.exit_order = self.close(data=self.d1m)

    # -- notifications -------------------------------------------------------

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        # NB: backtrader delivers a CLONE of the order to notify_order, so
        # `order is self.entry_order` is always False — identity cannot be used.
        # Match by ref (unique and preserved across the clone) instead.
        is_entry = (self.entry_order is not None
                    and order.ref == self.entry_order.ref)
        is_exit = (self.exit_order is not None
                   and order.ref == self.exit_order.ref)
        if order.status == order.Completed:
            if is_exit:                        # entry fill captured in next()
                self.exit_order = None
            return
        # A resting limit that did not fill ends as Canceled (our TTL cancel) or
        # Expired (broker `valid`). Either way we must clear entry_order, else the
        # dead order holds the single slot hostage for the rest of the run.
        if order.status in (order.Canceled, order.Expired,
                            order.Margin, order.Rejected):
            if is_entry:
                self.entry_order = None
                self.cur = None
            if is_exit:
                self.exit_order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        # a close with no plan is a residual flatten: keep equity right, no log row
        if self.cur is None:
            self.account_equity += trade.pnlcomm
            return
        d = self.cur
        exit_dt = bt.num2date(self.d1m.datetime[0])
        entry_px = d.get("entry_px") or 0.0
        size = d.get("size") or 0.0
        # risk distance (entry -> initial stop) to express MFE/MAE in R-multiples
        _risk_px = abs(entry_px - (d.get("initial_sl") or entry_px)) or 1.0
        # exact exit price from gross pnl (independent of fill-notification order):
        #   long : pnl = (exit - entry) * size  ->  exit = entry + pnl/size
        #   short: pnl = (entry - exit) * size  ->  exit = entry - pnl/size
        if entry_px and size:
            exit_px = (entry_px + trade.pnl / size if d["direction"] == "long"
                       else entry_px - trade.pnl / size)
        else:
            exit_px = 0.0

        # explicit, realistic costs on top of backtrader's commission:
        #  • spread: the LIMIT entry is a maker (no spread); the market exit is a
        #    taker, so it pays half the spread of THIS bar (the exit bar — we are
        #    in notify_trade at the exact fill bar). Variable/news-aware, not flat.
        #  • swap: overnight financing for the rare trade that rolls over midnight.
        spread_cost = self._exit_spread() / 2.0 * size
        swp = swap_cost(size / OZ_PER_LOT, d.get("entry_dt"), exit_dt)
        net = trade.pnlcomm - spread_cost - swp

        self.account_equity += net
        self.closed_trades.append({
            "entry_ts": d.get("entry_dt"), "exit_ts": exit_dt,
            "direction": d.get("direction"),
            "lots": round(size / OZ_PER_LOT, 2),
            "plan_entry": d.get("plan_entry"),
            "entry": round(entry_px, 2), "exit": round(exit_px, 2),
            "sl": d.get("initial_sl"), "tp": d.get("tp"),
            "be_moved": d.get("be_moved"),
            "pnl_usd": round(trade.pnl, 2),
            "spread_usd": round(spread_cost, 2),
            "swap_usd": round(swp, 2),
            "pnl_comm_usd": round(net, 2),   # net of commission + spread + swap
            "fvg_width": d.get("fvg_width"), "sweep_depth": d.get("sweep_depth"),
            "zone_age_h": d.get("zone_age_h"), "zone_width": d.get("zone_width"),
            "fill_penetration": d.get("fill_penetration"),
            # max favorable / adverse excursion, in R (risk = entry->initial stop)
            "mfe_r": round((d.get("mfe") or 0.0) / _risk_px, 3),
            "mae_r": round((d.get("mae") or 0.0) / _risk_px, 3),
        })
        self.equity_curve.append((exit_dt, self.account_equity))
        self.cur = None
        self.exit_order = None
