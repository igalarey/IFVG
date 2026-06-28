"""
ifvg/signals.py  —  DETECTION LAYER (market structure primitives)
=================================================================
Pure, stateless, look-ahead-safe helpers that turn a window of OHLC candles
into the two structural primitives the IFVG strategy needs:

    find_swing_points()  -> local swing highs / lows  (used to locate the
                            internal liquidity sweep on the 5-minute timeframe)
    detect_fvg()         -> Fair Value Gaps           (the 1h setup zones and
                            the 5m inverse-FVG entry trigger)

Nothing else lives here. (Earlier versions also carried liquidity-pool,
manipulation-leg and daily-bias helpers; those only fed two now-removed rule
variants and have been deleted to keep the codebase to exactly what runs.)

CONVENTIONS
-----------
* Input is a pandas DataFrame with columns: timestamp, open, high, low, close,
  volume — oldest row first, newest row last, timestamps tz-aware UTC.
* "Look-ahead-safe" here means a value written to row i never depends on data
  the live runtime wouldn't have when row i closes. The one exception is
  internal to detect_fvg's three-candle pattern, which by construction is only
  *confirmed* on the third candle (so it is still safe at confirmation time).
* XAUUSD pip convention: 1 pip = $0.10 (prices are USD per troy ounce).
"""
import pandas as pd
import numpy as np


PIP = 0.10   # XAUUSD: one pip = $0.10


# ---------------------------------------------------------------------------
# Swing points
# ---------------------------------------------------------------------------

def find_swing_points(df: pd.DataFrame,
                      left: int = 3,
                      right: int = 3) -> pd.DataFrame:
    """
    Mark local swing highs and swing lows.

    A swing high at index i: high[i] is the single maximum over the window
    [i-left, i+right]. A swing low: low[i] is the single minimum over the same
    window. Ties are rejected (a flat double-top is not a clean swing).

    The rule calls this with left=2, right=2 on the recent 5-minute window to
    find the internal high/low that price must sweep before an entry is valid.

    Adds boolean columns `swing_high` and `swing_low`. Because a swing needs
    `right` future candles to confirm, the last `right` rows are always False.
    """
    df = df.copy()
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    sh = np.zeros(n, dtype=bool)
    sl = np.zeros(n, dtype=bool)

    for i in range(left, n - right):
        wh = highs[i - left: i + right + 1]
        wl = lows[i - left: i + right + 1]
        if highs[i] == wh.max() and np.sum(wh == highs[i]) == 1:
            sh[i] = True
        if lows[i] == wl.min() and np.sum(wl == lows[i]) == 1:
            sl[i] = True

    df["swing_high"] = sh
    df["swing_low"] = sl
    return df


# ---------------------------------------------------------------------------
# Fair Value Gaps (FVG)
# ---------------------------------------------------------------------------

def detect_fvg(df: pd.DataFrame,
               body_multiplier: float = 1.5,
               min_gap_pips: float = 1.0) -> pd.DataFrame:
    """
    Detect Fair Value Gaps (3-candle imbalances) on an OHLC DataFrame.

    A FVG is a price gap left by a strong middle candle (the "impulse") that
    the two neighbouring candles do not overlap:

        bullish FVG at candle i:  low[i+1]  > high[i-1]   (gap above)
        bearish FVG at candle i:  high[i+1] < low[i-1]    (gap below)

    Two quality filters:
        body_multiplier : the impulse candle i must have a body >=
                          body_multiplier x the trailing 20-candle average body.
                          This is the strategy's main activity knob on the 1h
                          timeframe — a RELATIVE filter, so in uniformly volatile
                          regimes fewer candles qualify (see STRATEGY.md).
        min_gap_pips    : the gap itself must be wider than this (in pips).

    The gap is *confirmed* on candle i+1 (when that third candle closes), so the
    result is written to row i+1.

    Added columns:
        fvg_bull / fvg_bear : bool  — a bull/bear FVG is confirmed on this row
        fvg_top / fvg_bot   : float — the upper / lower edge of the gap zone
        fvg_mitig           : bool  — True on the first later candle that trades
                                      back into the zone (the gap is "filled")
    """
    df = df.copy()
    n = len(df)

    df["fvg_bull"] = False
    df["fvg_bear"] = False
    df["fvg_top"] = np.nan
    df["fvg_bot"] = np.nan
    df["fvg_mitig"] = False

    bodies = (df["close"] - df["open"]).abs().values
    avg_body = pd.Series(bodies).rolling(20, min_periods=5).mean().values
    highs = df["high"].values
    lows = df["low"].values

    zones = []  # (confirm_idx, top, bot) to scan for mitigation afterwards

    for i in range(1, n - 1):
        # impulse-strength filter on the middle candle
        if avg_body[i] > 0 and bodies[i] < body_multiplier * avg_body[i]:
            continue

        gap_bull = lows[i + 1] - highs[i - 1]
        gap_bear = lows[i - 1] - highs[i + 1]

        if gap_bull > min_gap_pips * PIP:
            idx = i + 1
            df.at[df.index[idx], "fvg_bull"] = True
            df.at[df.index[idx], "fvg_top"] = lows[i + 1]
            df.at[df.index[idx], "fvg_bot"] = highs[i - 1]
            zones.append((idx, lows[i + 1], highs[i - 1]))
        elif gap_bear > min_gap_pips * PIP:
            idx = i + 1
            df.at[df.index[idx], "fvg_bear"] = True
            df.at[df.index[idx], "fvg_top"] = lows[i - 1]
            df.at[df.index[idx], "fvg_bot"] = highs[i + 1]
            zones.append((idx, lows[i - 1], highs[i + 1]))

    # mark the first candle after confirmation that re-enters each zone
    for (confirm_idx, top, bot) in zones:
        for j in range(confirm_idx + 1, n):
            if lows[j] <= top and highs[j] >= bot:
                df.at[df.index[j], "fvg_mitig"] = True
                break

    return df
