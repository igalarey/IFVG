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
import os
import time

import MetaTrader5 as mt5

from datetime import datetime, timezone, timedelta

from ifvg import mt5_feed, entry_logic
from ifvg.position import (breakeven_stop, risk_lots, RISK_PCT, MAX_LOTS,
                           TIME_STOP_HOURS, FRIDAY_FLAT_HOUR, FRIDAY_FLAT_UTC_HOUR)

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


def used_zone_fills(symbol, lookback_h=2.0):
    """(direction, entry_price) for everything we have already acted on: the open
    position, the resting limit entry, and entries closed in the last `lookback_h`
    hours. Feeds entry_logic.seed_mitigated at startup so a restart does not
    re-enter a zone we already traded (the freshness filter is 1h, so a 2h
    look-back covers every zone that could still be enterable)."""
    used = []
    pos = current_position(symbol)
    if pos is not None:
        used.append(("long" if pos.type == mt5.POSITION_TYPE_BUY else "short",
                     pos.price_open))
    pend = current_pending(symbol)
    if pend is not None:
        used.append(("long" if pend.type == mt5.ORDER_TYPE_BUY_LIMIT else "short",
                     pend.price_open))
    tick = mt5.symbol_info_tick(symbol)
    frm = datetime.fromtimestamp(tick.time - lookback_h * 3600, tz=timezone.utc)
    to = datetime.fromtimestamp(tick.time + 3600, tz=timezone.utc)
    for deal in (mt5.history_deals_get(frm, to) or ()):
        if (deal.magic == MAGIC and deal.symbol == symbol
                and deal.entry == mt5.DEAL_ENTRY_IN):
            used.append(("long" if deal.type == mt5.DEAL_TYPE_BUY else "short",
                         deal.price))
    return used


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


_offset_cache = {}   # symbol -> last trusted server-UTC offset (hours)


def broker_utc_offset(symbol):
    """Hours the broker server clock leads true UTC (auto-detected, DST-aware).

    MT5's tick.time is the LAST tick's server wall-clock encoded as a Unix epoch,
    so it only reveals the true offset while ticks are FRESH (market open). When
    the market is closed the last tick can be hours/days old and would yield a
    bogus value (e.g. -45 over a weekend — it measures tick age, not timezone).
    So we only trust + cache a fresh, plausible reading, and otherwise reuse the
    last good one (0 until we ever get one). friday_flat() only acts on fresh
    bars anyway, so by then the offset is correct."""
    tick = mt5.symbol_info_tick(symbol)
    now = datetime.now(timezone.utc).timestamp()
    if tick is not None and tick.time:
        offset = round((tick.time - now) / 3600.0)
        # real tick age once the whole-hour server offset is removed. A genuinely
        # fresh tick is ~0 regardless of the broker's timezone — so this works for
        # brokers BEHIND as well as AHEAD of UTC (the raw now-tick.time would be
        # ~+3h for a behind-UTC broker even on a live tick, the '180m ago' bug).
        real_age = (now - tick.time) + offset * 3600
        if abs(real_age) < 600 and -12 <= offset <= 14:       # fresh + plausible
            _offset_cache[symbol] = offset
            return offset
    return _offset_cache.get(symbol, 0)


def friday_flat(symbol):
    """True once past FRIDAY_FLAT_UTC_HOUR (TRUE UTC) on a Friday — no weekend
    holding. Converts the broker's server clock to real UTC via the auto-detected
    offset, so the cutoff lands at the same real-world moment on ANY prop firm
    with no per-broker editing."""
    tick = mt5.symbol_info_tick(symbol)
    server_wall = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    true_utc = server_wall - timedelta(hours=broker_utc_offset(symbol))
    return true_utc.weekday() == 4 and true_utc.hour >= FRIDAY_FLAT_UTC_HOUR


def _filling_mode(symbol):
    """Pick a filling mode the symbol supports (bitmask: FOK=1, IOC=2)."""
    fm = getattr(mt5.symbol_info(symbol), "filling_mode", 0)
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


# MT5 order_send retcodes you actually meet live -> short labels (for the log)
_RETCODES = {
    10009: "done", 10008: "placed", 10010: "done (partial)", 10004: "requote",
    10006: "rejected", 10013: "invalid request", 10014: "invalid volume",
    10015: "invalid price", 10016: "invalid stops", 10018: "market closed",
    10019: "not enough money", 10027: "AutoTrading disabled — enable Algo Trading",
    10030: "unsupported filling mode", 10031: "no connection to server",
}


def _rc(res):
    """Human-readable order_send result (decodes the numeric retcode)."""
    if res is None:
        return "FAILED (no response)"
    code = res.retcode
    label = _RETCODES.get(code, "")
    tag = "OK" if code in (10009, 10008, 10010) else "FAILED"
    return f"{tag} ({code}{': ' + label if label else ''})"


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
    _log(f"order: LIMIT {direction} {lots} {symbol} @{entry} sl={sl} tp={tp} "
         f"ttl<={entry_logic.ENTRY_TTL_MIN}m -> {_rc(res)}")
    return res


def modify_sl(position, new_sl):
    res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                          "position": position.ticket, "sl": float(new_sl),
                          "tp": float(position.tp)})
    _log(f"break-even: SL -> {new_sl} -> {_rc(res)}")
    return res


def close_position(position, reason="manual"):
    """Market-close an open position (time-stop / weekend / manual)."""
    tick = mt5.symbol_info_tick(position.symbol)
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
        "volume": float(position.volume), "position": position.ticket,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if is_buy else tick.ask, "deviation": 20,
        "magic": MAGIC, "comment": f"ifvg-{reason}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling_mode(position.symbol),
    }
    res = mt5.order_send(req)
    _log(f"close ({reason}): {'long' if is_buy else 'short'} {position.volume} "
         f"{position.symbol} -> {_rc(res)}")
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


def _close_summary(symbol, rt):
    """Describe how the just-closed position ended: price, net P&L and reason
    (the time-stop/weekend reason we set, else SL/TP inferred from the price)."""
    reason, pnl, price = rt.get("closing"), None, None
    try:
        now = mt5.symbol_info_tick(symbol).time
        frm = datetime.fromtimestamp(now - 24 * 3600, tz=timezone.utc)
        to = datetime.fromtimestamp(now + 3600, tz=timezone.utc)
        outs = [d for d in (mt5.history_deals_get(frm, to) or ())
                if d.magic == MAGIC and d.position_id == rt.get("pos_ticket")
                and d.entry == mt5.DEAL_ENTRY_OUT]
        if outs:
            o = outs[-1]
            pnl = o.profit + o.swap + o.commission
            price = o.price
    except Exception:
        pass
    if reason is None and price is not None:        # infer SL vs TP from the fill
        sl, tp = rt.get("pos_sl"), rt.get("pos_tp")
        if tp and abs(price - tp) <= abs(price - (sl if sl else price)):
            reason = "TP"
        elif sl:
            reason = "SL"
    pstr = f"{pnl:+.2f}" if pnl is not None else "?"
    return (f"{rt.get('pos_dir') or '?'} @ {price if price is not None else '?'}  "
            f"P&L {pstr}  ({reason or 'manual/other'})")


def monitor(symbol, rt):
    """Run every poll: log fills / closes / expiries by diffing the broker's
    position & order state against the last seen (rt). This is what makes the log
    show 'FILLED'/'CLOSED' even though entries/exits happen at the broker."""
    try:
        pos = current_position(symbol)
        pend = current_pending(symbol)
        if pos is not None and pos.ticket != rt.get("pos_ticket"):     # a fill
            d = "long" if pos.type == mt5.POSITION_TYPE_BUY else "short"
            _log(f"FILLED: {d} {pos.volume} {symbol} @ {pos.price_open} "
                 f"sl={pos.sl} tp={pos.tp}")
            rt.update(pos_ticket=pos.ticket, pos_dir=d, pos_entry=pos.price_open,
                      pos_sl=pos.sl, pos_tp=pos.tp, closing=None)
        if pos is None and rt.get("pos_ticket") is not None:           # a close
            _log("CLOSED: " + _close_summary(symbol, rt))
            rt.update(pos_ticket=None, pos_dir=None, pos_sl=None, pos_tp=None,
                      closing=None)
        if pos is not None:
            rt["pos_sl"] = pos.sl       # track break-even moves for close inference
        # pending vanished with no resulting position -> it expired/was cancelled
        if pend is None and rt.get("pend_ticket") is not None and pos is None:
            _log("limit expired/cancelled (no fill)")
        rt["pend_ticket"] = pend.ticket if pend is not None else None
    except Exception as exc:
        _log(f"monitor error: {exc!r}")


def _step(args, state, rt):
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
            reason = "weekend" if weekend else "time-stop"
            rt["closing"] = reason          # so monitor logs the right close reason
            close_position(pos, reason)
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
            else:
                _log(f"signal {sig['direction']} @{sig['entry']} but size=0 "
                     f"(equity too low / stop too tight) — skipped")
    return bar_ts


def _health(symbol):
    """Snapshot of operational health + a list of PROBLEMS (empty = all good).
    Only unambiguous misconfigurations are flagged as problems (a stale tick on a
    closed weekend market is normal, so it is reported but not flagged)."""
    warns = []
    ti = mt5.terminal_info()
    connected = ti is not None and getattr(ti, "connected", False)
    if not connected:
        warns.append("not connected to the MT5 terminal")
    algo = bool(getattr(ti, "trade_allowed", False))
    if not algo:
        warns.append("Algo Trading is OFF — orders will be rejected (enable it in MT5)")
    acct = mt5.account_info()
    if acct is None:
        warns.append("no account info (terminal not logged in?)")
    info = mt5.symbol_info(symbol)
    if info is None:
        warns.append(f"symbol {symbol} not found in Market Watch")
        tradeable = False
    else:
        tradeable = getattr(info, "trade_mode", 0) != 0
        if not tradeable:
            warns.append(f"{symbol} is not tradeable (trading disabled for it)")
    tick = mt5.symbol_info_tick(symbol)
    now = datetime.now(timezone.utc).timestamp()
    # real tick age: remove the broker's whole-hour offset, else a behind-UTC
    # broker shows a fake "180m ago" on a live tick (see broker_utc_offset)
    offset = broker_utc_offset(symbol)
    tick_age = ((now - tick.time) + offset * 3600) if tick and tick.time else None
    spread = (tick.ask - tick.bid) if tick else None
    return {
        "connected": connected, "algo": algo, "tradeable": tradeable,
        "demo": acct is not None and acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
        "equity": getattr(acct, "equity", None), "spread": spread,
        "tick_age": tick_age, "offset": offset,
    }, warns


def log_health(symbol, tag="ping"):
    """Emit one HEALTH line (parsed by the dashboard). Returns True if all good."""
    h, warns = _health(symbol)
    sp = f"{h['spread']:.2f}" if h["spread"] is not None else "?"
    age = f"{h['tick_age'] / 60:.0f}m ago" if h["tick_age"] is not None else "?"
    line = (f"HEALTH [{tag}] ok={not warns} | conn={h['connected']} "
            f"algo={'ON' if h['algo'] else 'OFF'} {'DEMO' if h['demo'] else 'REAL'} "
            f"tradeable={h['tradeable']} eq={h['equity']} spread={sp} "
            f"last_tick={age} (server UTC{h['offset']:+d})")
    if warns:
        line += " | WARN: " + "; ".join(warns)
    _log(line)
    return not warns


def main():
    args = parse_args()
    # the web app passes the broker password via env (not argv, so it stays out
    # of the process list); the --password flag still works for manual runs.
    if not args.password:
        args.password = os.environ.get("MT5_PASSWORD")

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

    # startup: identity line + a health line that flags any misconfiguration
    _log(f"connected: acct #{getattr(acct, 'login', '?')} "
         f"{getattr(acct, 'server', '?')} ({'DEMO' if is_demo else 'REAL'})  "
         f"bal={getattr(acct, 'balance', '?')} eq={getattr(acct, 'equity', '?')} "
         f"{getattr(acct, 'currency', '')}  lev=1:{getattr(acct, 'leverage', '?')}")
    log_health(args.symbol, "startup")

    state = entry_logic.new_state()
    # restart safety: mark zones we already hold / just traded as mitigated so a
    # restart never re-enters them (broker state survives; in-memory state doesn't)
    try:
        seeded = entry_logic.seed_mitigated(state, fetch_buffers(args.symbol),
                                            used_zone_fills(args.symbol))
        if seeded:
            _log(f"restart: marked {seeded} already-traded zone(s) mitigated")
    except Exception as exc:
        _log(f"seed_mitigated skipped: {exc!r}")
    offset = broker_utc_offset(args.symbol)
    if args.symbol in _offset_cache:             # fresh tick -> offset is known
        tz = (f"server UTC{offset:+d} -> "
              f"{(FRIDAY_FLAT_UTC_HOUR + offset) % 24:02d}h server")
    else:                                        # market closed -> resolves later
        tz = "server offset pending (market closed; resolves on first tick)"
    _log(f"{args.symbol}  risk={args.risk_pct*100:.2f}%  time-stop={TIME_STOP_HOURS}h"
         f"  friday-flat={FRIDAY_FLAT_UTC_HOUR}h UTC ({tz})"
         f"  ({'DEMO' if is_demo else 'REAL'})")

    # runtime state for the lifecycle logger; seed it from the broker so a resumed
    # position/order is reported as 'resuming', not as a fresh fill
    rt = {"pos_ticket": None, "pos_dir": None, "pos_sl": None, "pos_tp": None,
          "pend_ticket": None, "closing": None}
    p0 = current_position(args.symbol)
    if p0 is not None:
        rt.update(pos_ticket=p0.ticket,
                  pos_dir="long" if p0.type == mt5.POSITION_TYPE_BUY else "short",
                  pos_sl=p0.sl, pos_tp=p0.tp)
        _log(f"resuming open {rt['pos_dir']} {p0.volume} {args.symbol} "
             f"@{p0.price_open} P&L {p0.profit:+.2f}")
    pend0 = current_pending(args.symbol)
    rt["pend_ticket"] = pend0.ticket if pend0 is not None else None

    ping_file = os.environ.get("IFVG_PING_FILE")   # dashboard touches it -> HEALTH
    last_bar, last_beat = None, 0.0
    try:
        while True:
            try:
                # keep the connection alive (auto-reconnect if the terminal drops)
                if not (_connected() or _connect(args)):
                    _log("disconnected — retrying")
                    time.sleep(args.poll)
                    continue
                # every poll: log fills / closes / expiries promptly (~poll seconds)
                monitor(args.symbol, rt)
                # on-demand ping: the dashboard touches IFVG_PING_FILE -> reply HEALTH
                if ping_file and os.path.exists(ping_file):
                    try:
                        os.remove(ping_file)
                    except OSError:
                        pass
                    log_health(args.symbol, "ping")
                # hourly heartbeat so the log shows the bot is alive when idle
                if time.time() - last_beat > 3600:
                    a = mt5.account_info()
                    p = current_position(args.symbol)
                    if p is not None:
                        d = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
                        age_h = (mt5.symbol_info_tick(args.symbol).time - p.time) / 3600.0
                        _log(f"heartbeat: in {d} {p.volume} @{p.price_open} "
                             f"P&L {p.profit:+.2f} age={age_h:.1f}h "
                             f"eq={getattr(a, 'equity', '?')}")
                    else:
                        live = sum(1 for z in state["zones"] if not z["mitigated"])
                        _log(f"heartbeat: flat  eq={getattr(a, 'equity', '?')} "
                             f"bal={getattr(a, 'balance', '?')}  {live} live zone(s)")
                    last_beat = time.time()
                # act once per newly closed M1 bar
                m1 = mt5_feed.fetch_recent(args.symbol, "m1", 2)
                if not m1.empty and m1["timestamp"].iloc[-1] != last_bar:
                    last_bar = _step(args, state, rt) or last_bar
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
