"""
ifvg/position.py  —  RISK & POSITION SIZING (shared by backtest and live)
=========================================================================
One place for the money-management rules so the backtest and the live runner
size and protect trades identically:

    risk_lots()       -> how many lots to trade (fixed-fractional risk)
    breakeven_level() -> the price at which a trade reaches 1:1
    breakeven_stop()  -> the new stop (= entry) when 1:1 is reached

All constants are FIXED here (the values validated 2020-2025 on XAUUSD). The
strategy is hardcoded for XAUUSD (gold) — a multi-instrument variant was tested
on EURUSD and dropped (the edge is gold-specific).
"""
from datetime import timedelta

# ── price / contract constants (XAUUSD) ──
PIP = 0.10          # XAUUSD: 1 pip = $0.10
SL_BUFFER = 3.0     # pips placed beyond a structural extreme for the stop
OZ_PER_LOT = 100.0  # XAUUSD contract size: 1 standard lot = 100 troy oz

# ── fixed money management (FTMO-validated) ──
RISK_PCT = 0.005    # risk 0.5% of equity per trade
MIN_LOTS = 0.01     # broker minimum volume / lot step
MAX_LOTS = 5.0      # hard cap on a single position
TIME_STOP_HOURS = 4   # force-close a trade after this long: caps the break-even
                      # grind AND keeps the hold intraday (this is a low-timeframe
                      # FVG method). Shorter holds also proved more cost-robust;
                      # 4h chosen as a principled intraday cap on the stable part
                      # of the curve (not the noisy 2-3h max).
FRIDAY_FLAT_HOUR = 17  # after this hour (broker/server time) on Fridays, close any
                      # open position and stop opening new ones: FTMO regular
                      # accounts disallow weekend holding, and it avoids weekend
                      # gap risk. (17h cuts weekend-held trades 133->6 at ~-4%.)

# Overnight financing (swap). Gold swaps are a COST on BOTH sides on FTMO, so we
# model it as a flat charge per lot per night held across the broker's rollover,
# regardless of direction (the conservative/honest assumption). The Friday-flat
# rule means very few trades ever roll over; ~10% do, costing <2% of net P&L.
SWAP_USD_PER_LOT_PER_NIGHT = 6.0


def swap_cost(lots, entry_dt, exit_dt, per_night=SWAP_USD_PER_LOT_PER_NIGHT):
    """Overnight financing for a trade open across one or more broker rollovers
    (server midnight). Each midnight crossed costs `per_night` per lot; the night
    rolling INTO Thursday is charged triple (the standard weekend triple-swap
    convention). Returns a positive number = a cost to subtract from P&L."""
    if lots <= 0 or entry_dt is None or exit_dt is None:
        return 0.0
    e = entry_dt.date() if hasattr(entry_dt, "date") else entry_dt
    x = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
    nights = (x - e).days
    if nights <= 0:
        return 0.0
    weighted = sum(3 if (e + timedelta(days=i)).weekday() == 3 else 1
                   for i in range(1, nights + 1))   # Thu rollover = triple
    return lots * per_night * weighted


def risk_lots(equity: float,
              entry: float,
              sl: float,
              risk_pct: float = RISK_PCT,
              oz_per_lot: float = OZ_PER_LOT,
              min_lots: float = MIN_LOTS,
              max_lots: float = MAX_LOTS,
              lot_step: float = MIN_LOTS) -> float:
    """
    Lots to risk `risk_pct` of `equity` down to the stop:

        lots = (equity * risk_pct) / (|entry - sl| * oz_per_lot)

    A constant % of the account is at risk on every trade regardless of how far
    the stop is — what a funded/prop account needs to respect its drawdown
    limits. Rounded DOWN to `lot_step` (never exceed the risk target) and clamped
    to [min_lots, max_lots]. Returns 0.0 if the stop is non-positive (skip).
    """
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return 0.0
    raw = (equity * risk_pct) / (stop_dist * oz_per_lot)
    stepped = int(raw / lot_step) * lot_step          # round DOWN to lot step
    return max(min_lots, min(max_lots, round(stepped, 2)))


def breakeven_level(direction: str, entry: float, sl: float) -> float:
    """Price at which the trade reaches 1:1 (where the stop jumps to entry)."""
    risk = abs(entry - sl)
    return entry + risk if direction == "long" else entry - risk


def breakeven_stop(direction: str,
                   entry: float,
                   current_sl: float,
                   bar_high: float,
                   bar_low: float) -> "float | None":
    """
    Return the new stop price (= `entry`) if this bar reached the 1:1 level and
    the stop has not yet been moved to break-even; otherwise None.
    """
    be = breakeven_level(direction, entry, current_sl)
    if direction == "long" and bar_high >= be and current_sl < entry:
        return entry
    if direction == "short" and bar_low <= be and current_sl > entry:
        return entry
    return None
