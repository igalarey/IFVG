"""
run_live.py  —  live / paper execution of the IFVG strategy on MT5
==================================================================
The bridge from backtest to live. It does NOT use backtrader; it drives the
exact same rule (ifvg.entry_logic.generate_signal) used by the backtest, feeding
it rolling buffers pulled live from MT5 and sending real orders.

    backtest :  backtrader next()  -> generate_signal()
    live     :  this poll loop      -> generate_signal()      (identical rule)

The strategy is FIXED (0.5% risk — see ifvg/entry_logic.py and
ifvg/position.py). Only the connection / account settings are configurable.
Sizing uses the same fixed-fractional risk model as the backtest, off the LIVE
account equity, snapped to the symbol's volume step. SL/TP go on the order; the
break-even rule is applied each new bar by modifying the position's stop.

The entry is a resting LIMIT at the FVG midpoint (sig['entry']) — the same
faithful ICT entry the backtest uses — expiring after ENTRY_TTL_MIN minutes, NOT
a market fill. While that limit is pending (or a position is open) no new signal
is taken, so there is never more than one order/position at a time.

SAFETY: refuses to run on a non-demo account unless --allow-real is given.
This is a clean, reviewable skeleton — check broker fill rules before funding.

USAGE
    python run_live.py --symbol XAUUSD --login 123456 --password "***" --server "FTMO-Demo"
    python run_live.py --symbol XAUUSD            # attach to the open terminal
"""
import argparse
import time

import MetaTrader5 as mt5

from datetime import datetime, timezone

from ifvg import mt5_feed, entry_logic
from ifvg.position import (breakeven_stop, risk_lots, RISK_PCT, MAX_LOTS,
                           TIME_STOP_HOURS, FRIDAY_FLAT_HOUR)

# recent bars kept per timeframe (cover entry_logic's tail() needs + margin)
BUFFER_BARS = {"m1": 5, "m5": 90, "h1": 90}
MAGIC = 20240624


def parse_args():
    ap = argparse.ArgumentParser(description="XAUUSD IFVG live runner")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--poll", type=float, default=5.0, help="poll seconds")
    # risk per trade: 0.5% is the funded default; raise (e.g. 0.01) for the
    # challenge phase to reach the target faster, then drop back once funded.
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT, dest="risk_pct")
    ap.add_argument("--allow-real", action="store_true",
                    help="permit running on a real (non-demo) account")
    ap.add_argument("--mt5-path", default=None)
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    return ap.parse_args()


def fetch_buffers(symbol):
    """Rolling buffers the rule needs: m1 (current bar), m5 and h1."""
    return {tf: mt5_feed.fetch_recent(symbol, tf, n) for tf, n in BUFFER_BARS.items()}


def current_position(symbol):
    for pos in (mt5.positions_get(symbol=symbol) or ()):
        if pos.magic == MAGIC:
            return pos
    return None


def current_pending(symbol):
    """Our resting limit entry, if one is still waiting for a fill."""
    for o in (mt5.orders_get(symbol=symbol) or ()):
        if o.magic == MAGIC:
            return o
    return None


def position_lots(symbol, sig, risk_pct):
    """Fixed-fractional risk off LIVE equity, snapped to the symbol's volume."""
    info = mt5.symbol_info(symbol)
    step = getattr(info, "volume_step", 0.01) or 0.01
    vmin = getattr(info, "volume_min", 0.01) or 0.01
    vmax = getattr(info, "volume_max", 100.0) or 100.0
    contract = getattr(info, "trade_contract_size", 100.0) or 100.0
    acct = mt5.account_info()
    equity = acct.equity if acct else 0.0
    if equity <= 0:                      # no/booted account info -> don't trade
        return 0.0
    return risk_lots(equity, sig["entry"], sig["sl"], risk_pct=risk_pct,
                     oz_per_lot=contract, min_lots=vmin,
                     max_lots=min(vmax, MAX_LOTS), lot_step=step)


def friday_flat(symbol):
    """True once past FRIDAY_FLAT_HOUR (server time) on a Friday — no weekend
    holding. Uses the symbol's last tick time (broker server clock)."""
    tick = mt5.symbol_info_tick(symbol)
    t = datetime.fromtimestamp(tick.time, tz=timezone.utc)   # server epoch
    return t.weekday() == 4 and t.hour >= FRIDAY_FLAT_HOUR


def _filling_mode(symbol):
    """Pick a filling mode the symbol supports (bitmask: FOK=1, IOC=2)."""
    fm = getattr(mt5.symbol_info(symbol), "filling_mode", 0)
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def send_limit_order(symbol, direction, lots, entry, sl, tp):
    """Rest a LIMIT entry at the FVG midpoint, expiring after ENTRY_TTL_MIN.

    Price has to retrace into the gap to fill, so the fill matches the level
    SL/TP were built from (the backtest does the same). Expiration is in broker
    server time (off the last tick), so a no-show retrace cancels itself.
    """
    tick = mt5.symbol_info_tick(symbol)
    otype = (mt5.ORDER_TYPE_BUY_LIMIT if direction == "long"
             else mt5.ORDER_TYPE_SELL_LIMIT)
    expiration = int(tick.time + entry_logic.ENTRY_TTL_MIN * 60)
    req = {
        "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": float(lots),
        "type": otype, "price": float(entry), "sl": float(sl), "tp": float(tp),
        "deviation": 20, "magic": MAGIC, "comment": "ifvg-m2",
        "type_time": mt5.ORDER_TIME_SPECIFIED, "expiration": expiration,
        "type_filling": _filling_mode(symbol),
    }
    res = mt5.order_send(req)
    print(f"[live] LIMIT {direction} {lots}@{entry} sl={sl} tp={tp} "
          f"valid<={entry_logic.ENTRY_TTL_MIN}min -> {res.retcode}")
    return res


def modify_sl(position, new_sl):
    res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                          "position": position.ticket, "sl": float(new_sl),
                          "tp": float(position.tp)})
    print(f"[live] break-even SL -> {new_sl}  -> {res.retcode}")
    return res


def close_position(position):
    """Market-close an open position (used by the time-stop)."""
    tick = mt5.symbol_info_tick(position.symbol)
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
        "volume": float(position.volume), "position": position.ticket,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if is_buy else tick.ask, "deviation": 20,
        "magic": MAGIC, "comment": "ifvg-timestop",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling_mode(position.symbol),
    }
    res = mt5.order_send(req)
    print(f"[live] TIME-STOP close -> {res.retcode}")
    return res


def _log(msg):
    print(f"[live {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def _connected():
    ti = mt5.terminal_info()
    return ti is not None and ti.connected


def _connect(args):
    """(Re)connect to the MT5 terminal; return True on success, never raises."""
    try:
        mt5.shutdown()
    except Exception:
        pass
    try:
        mt5_feed.connect(path=args.mt5_path, login=args.login,
                         password=args.password, server=args.server)
        mt5_feed.ensure_symbol(args.symbol)
        return _connected()
    except Exception as exc:
        _log(f"connect failed: {exc!r}")
        return False


def _step(args, state):
    """One poll iteration. Returns the latest M1 timestamp acted on, or None."""
    m1 = mt5_feed.fetch_recent(args.symbol, "m1", 2)
    if m1.empty:
        return None
    bar_ts = m1["timestamp"].iloc[-1]

    buf = fetch_buffers(args.symbol)
    now = buf["m1"]["timestamp"].iloc[-1]
    last = buf["m1"].iloc[-1]
    bar = {"open": last["open"], "high": last["high"],
           "low": last["low"], "close": last["close"]}

    weekend = friday_flat(args.symbol)
    pos = current_position(args.symbol)
    if pos is not None:
        age_h = (mt5.symbol_info_tick(args.symbol).time - pos.time) / 3600.0
        # close on the time-stop OR before the weekend (no holding over it)
        if weekend or (TIME_STOP_HOURS and age_h >= TIME_STOP_HOURS):
            close_position(pos)
        else:
            direction = "long" if pos.type == mt5.POSITION_TYPE_BUY else "short"
            new_sl = breakeven_stop(direction, pos.price_open, pos.sl,
                                    bar["high"], bar["low"])
            if new_sl is not None and abs(new_sl - pos.sl) > 1e-6:
                modify_sl(pos, new_sl)
    elif not weekend and current_pending(args.symbol) is None:
        sig = entry_logic.generate_signal(now, bar, buf, state)
        if sig is not None:
            lots = position_lots(args.symbol, sig, args.risk_pct)
            if lots > 0:
                send_limit_order(args.symbol, sig["direction"], lots,
                                 sig["entry"], sig["sl"], sig["tp"])
    return bar_ts


def main():
    args = parse_args()

    # startup: wait for the terminal (it may not be up yet under a restarter)
    while not (_connected() or _connect(args)):
        _log("waiting for MT5 terminal …")
        time.sleep(5)

    acct = mt5.account_info()
    is_demo = acct is not None and acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    if not is_demo and not args.allow_real:
        mt5_feed.shutdown()
        raise SystemExit("[live] Refusing to run on a non-demo account without "
                         "--allow-real.")

    state = entry_logic.new_state()
    _log(f"{args.symbol}  risk={args.risk_pct*100:.2f}%  time-stop={TIME_STOP_HOURS}h"
         f"  friday-flat={FRIDAY_FLAT_HOUR}h  ({'DEMO' if is_demo else 'REAL'})")

    last_bar, last_beat = None, 0.0
    try:
        while True:
            try:
                # keep the connection alive (auto-reconnect if the terminal drops)
                if not (_connected() or _connect(args)):
                    _log("disconnected — retrying")
                    time.sleep(args.poll)
                    continue
                # hourly heartbeat so the log shows the bot is alive when idle
                if time.time() - last_beat > 3600:
                    a = mt5.account_info()
                    p = current_position(args.symbol)
                    _log(f"heartbeat  equity={getattr(a, 'equity', '?')}  "
                         f"position={'open' if p else 'flat'}")
                    last_beat = time.time()
                # act once per newly closed M1 bar
                m1 = mt5_feed.fetch_recent(args.symbol, "m1", 2)
                if not m1.empty and m1["timestamp"].iloc[-1] != last_bar:
                    last_bar = _step(args, state) or last_bar
                time.sleep(args.poll)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                _log(f"loop error: {exc!r} — recovering")
                time.sleep(args.poll)
    except KeyboardInterrupt:
        _log("stopped by user.")
    finally:
        try:
            mt5_feed.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
