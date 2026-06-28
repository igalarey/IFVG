"""
ifvg  —  framework-agnostic IFVG strategy logic for XAUUSD
=========================================================
The same rule code is reused by the backtrader backtest (run_backtest.py) and by
the live MT5 runner (run_live.py). Layers:

    signals.py     detection primitives — swing points and Fair Value Gaps.
                   Pure, stateless, look-ahead-safe.

    entry_logic.py THE rule (HTF-FVG continuation). Incremental
                   generate_signal(now, bar, buf, state) -> signal | None.
                   All tunables are FIXED constants here.

    position.py    risk sizing (risk_lots) + break-even logic, shared by both
                   runtimes. Money-management constants are FIXED here.

    mt5_feed.py    MetaTrader 5 access — builds M1/M5/H1 candles from ticks for
                   the backtest and rolling buffers for live. (The only module
                   that imports MetaTrader5.)

Nothing in signals/entry_logic/position imports backtrader or MetaTrader5, which
is what lets the exact same rule run in both the backtest and live.
"""
from . import signals
from . import entry_logic
from . import position

__all__ = ["signals", "entry_logic", "position"]
