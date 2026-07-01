"""
ifvg/entry_logic.py  —  THE STRATEGY RULE (HTF-FVG continuation)
================================================================
The single, framework-agnostic entry rule that both runtimes share:

    backtest :  strategy.IFVGStrategy.next()  -> generate_signal(...)
    live     :  run_live.py poll loop         -> generate_signal(...)   (same code)

It depends only on rolling OHLC buffers + a small mutable `state` dict, so it has
no idea whether it runs inside backtrader or against a live MT5 terminal.

THE RULE, IN PLAIN WORDS
------------------------
1.  On the 1-hour timeframe, track every Fair Value Gap as it forms (the setup
    zones) — an imbalance left by a strong impulse candle.
2.  When the current 1-minute price trades back INTO an unmitigated 1h zone,
    arm that zone once.
3.  Inside the zone, on the 5-minute timeframe, require an internal liquidity
    sweep (price takes out a recent 5m swing low for a bull zone / swing high
    for a bear zone) — the manipulation leg.
4.  Enter on the inverse 5-minute FVG from its midpoint, IN THE DIRECTION OF THE
    1h ZONE (long in a bull zone, short in a bear zone).
5.  Stop-loss beyond the swept extreme; take-profit at a fixed reward:risk.
6.  Guardrail: reject a microscopically tight stop (blows up risk-based size and
    tends to whipsaw).

EXECUTION: THE ENTRY IS A RESTING LIMIT AT THE FVG MIDPOINT
----------------------------------------------------------
`entry` is the FVG midpoint — the price the runtime must wait for, NOT a market
fill at wherever price happens to be. A market entry lands somewhere else (often
several dollars away) while SL/TP stay anchored to the midpoint, which corrupts
the reward:risk and produces wild, multi-week trades. The faithful ICT entry is a
LIMIT order resting at the midpoint: price has to retrace into the gap, so the
fill matches the level SL/TP were built from. If the retrace never comes within
ENTRY_TTL_MIN minutes the runtime cancels the order (a no-show must not block the
single position slot). Both runtimes (backtest + live) execute it this way.

EVERYTHING IS FIXED & XAUUSD-SPECIFIC
-------------------------------------
All tunables are constants below (validated 2020-2025 on gold). The strategy is
hardcoded for XAUUSD; a multi-instrument variant was tested on EURUSD and dropped
(the edge is gold-specific). A "no night trading" session filter was also tested
and removed — it is not an FTMO rule and it gutted the gold edge (much of which
is in overnight setups).

Signal dict returned to the runtime (sizing/execution is the runtime's job):
    {'direction': 'long'|'short', 'entry': float, 'sl': float, 'tp': float}
"""
import pandas as pd

from .signals import detect_fvg, find_swing_points
from .position import PIP, SL_BUFFER


# ── FIXED STRATEGY CONSTANTS (validated; not exposed as run-script options) ──
RR = 1.5                 # reward:risk — take-profit distance = RR x stop distance
FVG_BODY_MULT = 1.0      # 1h impulse filter (main activity knob; 1.0 validated)
H1_BUF = 60              # how many recent 1h candles to scan for zones
M5_SWEEP_BARS = 25       # recent 5m window used for the internal sweep + entry FVG
M5_BODY_MULT = 1.1       # impulse filter for the 5m inverse-FVG entry trigger
M5_MIN_GAP_PIPS = 0.3    # minimum 5m entry-FVG width (in pips)
MIN_STOP_PIPS = 5.0      # guardrail: reject signals with a stop tighter than this
ZONE_TTL_DAYS = 7        # forget untouched 1h zones older than this (memory bound)
MAX_ZONE_AGE_H = 1.0     # freshness filter: only enter a 1h zone if it is younger
                         # than this many hours when price returns to it (None =
                         # off). Grounding analysis (replicated on a 2nd broker)
                         # showed the edge lives in FRESH zones: <0.9h PF ~1.6-3.3,
                         # >~5h PF ~0.76 (loses). The 1h cutoff was validated by a
                         # sweep on BOTH data sources (monotonic improvement, every
                         # year +, FTMO-safe): vs off it lifts PF 1.52->1.93, cuts
                         # maxDD 5.3->4.0% and raises net, by dropping the
                         # negative-expectancy stale-zone trades. LOCKED 2026-06-28.
ENTRY_TTL_MIN = 60       # limit-entry validity: cancel the resting entry if price
                         # has not retraced into the FVG within this many minutes
ENTRY_ANCHOR = "proximal"  # where in the 5m entry-FVG the limit rests:
                         #   "mid"      -> consequent encroachment (gap midpoint)
                         #   "proximal" -> the near edge price touches first (LOCKED):
                         #                 fills the clean shallow bounces mid misses.
                         #                 Cross-validated on FTMO 2020-25 AND IC Markets
                         #                 2017-19: PF 1.93->2.11 / 1.87->2.04, maxDD and
                         #                 adverse-fill PF both improved on BOTH sources.
                         #   "distal"   -> the far edge (best price, rarely filled)


def _entry_price(top, bot, direction, anchor=None):
    """Resting-limit price inside the 5m entry-FVG for the chosen anchor.

    The proximal edge is the side price touches first as it returns to the gap:
    the TOP for a long (price drops in from above), the BOT for a short (price
    rises in from below). "mid" is consequent encroachment; "distal" the far edge.
    """
    anchor = anchor or ENTRY_ANCHOR
    if anchor == "mid":
        return (top + bot) / 2.0
    if direction == "long":
        return top if anchor == "proximal" else bot
    return bot if anchor == "proximal" else top


def new_state() -> dict:
    """
    Fresh mutable state for one run. Remembers, between bars, which 1h FVG zones
    have been registered and which are still live.
        'zones'   : list of {ts, direction, top, bot, mitigated}
        'seen_ts' : set of 1h-FVG timestamps already registered (dedup)
    """
    return {"zones": [], "seen_ts": set()}


def generate_signal(now, bar, buf, state, h1_changed=True) -> "dict | None":
    """
    Evaluate the IFVG rule for the current 1-minute bar.

    Args:
        now        : current 1m bar timestamp (tz-aware UTC)
        bar        : dict with the current 1m OHLC: open/high/low/close
        buf        : dict of rolling OHLC DataFrames; keys 'h1' and 'm5'
        state      : the dict from new_state(), mutated in place across bars
        h1_changed : whether a new 1h bar has closed since the last call. When
                     False the (expensive) 1h FVG detection is skipped — the zone
                     set cannot have changed — and only the per-minute zone-entry
                     check runs. Exact: registration is idempotent (dedup by ts).
                     Defaults to True so the live runner stays correct without
                     having to track it.

    Returns one signal dict, or None. A returned signal has already passed the
    minimum-stop guardrail.
    """
    sig = _model2(now, bar, buf, state, h1_changed)
    if sig is None:
        return None
    # risk guardrail: a too-tight stop is rejected (size blow-up / whipsaw)
    if abs(sig["entry"] - sig["sl"]) < MIN_STOP_PIPS * PIP:
        return None
    return sig


def _model2(now, bar, buf, state, h1_changed=True) -> "dict | None":
    # 1) register any NEW 1h FVG zones visible in the rolling 1h buffer. This
    #    detect_fvg is the per-bar hot spot; skip it when no 1h bar has closed
    #    (the zone set is unchanged), which is 59 of every 60 one-minute bars.
    if h1_changed:
        _register_h1_zones(buf, state)

    # drop consumed / stale zones so the list stays bounded over long runs
    cutoff = now - pd.Timedelta(days=ZONE_TTL_DAYS)
    state["zones"] = [z for z in state["zones"]
                      if not z["mitigated"] and z["ts"] >= cutoff]

    return _check_zone_entry(now, bar, buf, state)


def _register_h1_zones(buf, state) -> None:
    df_htf = detect_fvg(buf["h1"].tail(H1_BUF).reset_index(drop=True),
                        body_multiplier=FVG_BODY_MULT)
    for _, row in df_htf.iterrows():
        if not (row["fvg_bull"] or row["fvg_bear"]):
            continue
        ts = row["timestamp"]
        if ts in state["seen_ts"]:
            continue
        state["seen_ts"].add(ts)
        state["zones"].append({
            "ts": ts,
            "direction": "bull" if row["fvg_bull"] else "bear",
            "top": row["fvg_top"],
            "bot": row["fvg_bot"],
            "mitigated": False,
        })


def seed_mitigated(state, buf, used) -> int:
    """Restart safety: rebuild the zone set from `buf` and mark mitigated every
    zone we have already acted on, so a restart does not re-enter a zone we still
    hold or just traded (the in-memory `mitigated` flag is otherwise lost on
    restart, while the broker-side position/history survives).

    `used` is an iterable of (direction, price) — the open position, the resting
    limit entry and recent fills, read from the broker by the live runner. A zone
    matches when the fill price falls within its band (small tolerance) and the
    direction agrees (bull zone -> long, bear zone -> short). Returns the number
    of zones marked, for logging.
    """
    _register_h1_zones(buf, state)
    tol = SL_BUFFER * PIP
    n = 0
    for z in state["zones"]:
        if z["mitigated"]:
            continue
        zdir = "long" if z["direction"] == "bull" else "short"
        for d, px in used:
            if d == zdir and z["bot"] - tol <= px <= z["top"] + tol:
                z["mitigated"] = True
                n += 1
                break
    return n


def _check_zone_entry(now, bar, buf, state) -> "dict | None":
    # has the current 1m bar entered an unmitigated zone?
    for z in state["zones"]:
        if z["mitigated"] or z["ts"] >= now:
            continue
        if not (bar["low"] <= z["top"] and bar["high"] >= z["bot"]):
            continue

        z["mitigated"] = True   # arm the zone once; never reuse it

        # freshness filter: a stale (already-absorbed) zone has no edge — skip it.
        # Age = formation -> first return; measured here, the moment price returns.
        if MAX_ZONE_AGE_H is not None and \
                (now - z["ts"]).total_seconds() / 3600.0 > MAX_ZONE_AGE_H:
            continue

        # 3) internal liquidity sweep on the recent 5m window
        recent = buf["m5"][buf["m5"]["timestamp"] <= now].tail(M5_SWEEP_BARS)
        recent = recent.reset_index(drop=True)
        if len(recent) < 5:
            continue
        recent = find_swing_points(recent, left=2, right=2)

        if z["direction"] == "bull":
            swings = recent[recent["swing_low"]]["low"]
            if swings.empty or not (recent["low"].min() <= swings.min()):
                continue
            fvg = detect_fvg(recent, body_multiplier=M5_BODY_MULT,
                             min_gap_pips=M5_MIN_GAP_PIPS)
            hits = fvg[fvg["fvg_bull"]]
            if hits.empty:
                continue
            last = hits.iloc[-1]
            entry = _entry_price(last["fvg_top"], last["fvg_bot"], "long")
            sl = recent["low"].min() - SL_BUFFER * PIP
            tp = entry + abs(entry - sl) * RR
            direction = "long"
            sweep_depth = float(swings.min() - recent["low"].min())  # overshoot $
        else:
            swings = recent[recent["swing_high"]]["high"]
            if swings.empty or not (recent["high"].max() >= swings.max()):
                continue
            fvg = detect_fvg(recent, body_multiplier=M5_BODY_MULT,
                             min_gap_pips=M5_MIN_GAP_PIPS)
            hits = fvg[fvg["fvg_bear"]]
            if hits.empty:
                continue
            last = hits.iloc[-1]
            entry = _entry_price(last["fvg_top"], last["fvg_bot"], "short")
            sl = recent["high"].max() + SL_BUFFER * PIP
            tp = entry - abs(entry - sl) * RR
            direction = "short"
            sweep_depth = float(recent["high"].max() - swings.max())  # overshoot $

        return {
            "direction": direction,
            "entry": round(float(entry), 2),
            "sl": round(float(sl), 2),
            "tp": round(float(tp), 2),
            # diagnostic features (for grounding analysis; ignored by execution).
            # The ICT narrative predicts a stronger edge with a deeper sweep, a
            # cleaner/wider FVG and a fresher zone — these let us test that.
            "fvg_width": round(float(last["fvg_top"] - last["fvg_bot"]), 3),
            "sweep_depth": round(sweep_depth, 3),
            "zone_age_h": round((now - z["ts"]).total_seconds() / 3600.0, 1),
            "zone_width": round(float(z["top"] - z["bot"]), 3),
        }

    return None
