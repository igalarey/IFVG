"""
mt5_feed.py  (MetaTrader 5 data access)
---------------------------------------
Thin wrapper around the official `MetaTrader5` python package, used by both
run_backtest.py (pull history -> backtrader) and run_live.py (rolling buffers).

Connection model
----------------
MetaTrader5 talks to a *running* MT5 terminal on the same Windows machine:

    import MetaTrader5 as mt5
    mt5.initialize()                       # attach to the already-open terminal
    # or, to launch / log in explicitly:
    mt5.initialize(path=r"C:\\Program Files\\FTMO MT5\\terminal64.exe",
                   login=12345678, password="****", server="FTMO-Demo")

For a pure *backtest* you only need read access to history, so a bare
`mt5.initialize()` against an open, logged-in terminal is enough.  For *live*
trading you generally pass login/password/server so the script owns the session.

All timestamps are returned tz-aware UTC.  Note: MT5 bar times are in the
broker's server timezone; we label them UTC for internal consistency.  Strategy
logic only compares timestamps against each other, so a constant offset is
harmless, but keep it in mind if you align to real-world session times.
"""
import hashlib
import os
import pickle
from datetime import datetime, timezone

import pandas as pd
import MetaTrader5 as mt5

# On-disk cache for built bars. The slow part of a backtest is pulling ~250M
# ticks from MT5 and rebuilding M1/M5/H1 (minutes); the bars for a fixed
# historical range never change, so we pickle them once and reload in seconds.
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".bar_cache")


# strategy timeframe key  ->  MT5 timeframe constant
TF_MAP = {
    "m1": mt5.TIMEFRAME_M1,
    "m5": mt5.TIMEFRAME_M5,
    "m15": mt5.TIMEFRAME_M15,
    "h1": mt5.TIMEFRAME_H1,
    "h4": mt5.TIMEFRAME_H4,
    "d1": mt5.TIMEFRAME_D1,
}


def connect(path: str = None,
            login: int = None,
            password: str = None,
            server: str = None,
            timeout: int = 60_000) -> None:
    """
    Initialise the MT5 connection.  Raises RuntimeError on failure.

    Pass nothing to attach to an already-open, logged-in terminal.
    Pass path/login/password/server to launch and log in explicitly.
    """
    kwargs = {"timeout": timeout}
    if path:
        kwargs["path"] = path
    if login:
        kwargs["login"] = int(login)
    if password:
        kwargs["password"] = password
    if server:
        kwargs["server"] = server

    ok = mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")

    info = mt5.account_info()
    if info is not None:
        print(f"[mt5] connected  login={info.login}  server={info.server}  "
              f"balance={info.balance} {info.currency}")
    else:
        print("[mt5] connected (no account info — history-only session)")


def shutdown() -> None:
    mt5.shutdown()


def ensure_symbol(symbol: str) -> None:
    """Make sure the symbol exists and is visible in Market Watch."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol '{symbol}' not found on this broker.")
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Could not select symbol '{symbol}'.")


# pandas resample rule per timeframe key (higher TFs are derived from 1-min)
_RESAMPLE_RULE = {"m5": "5min", "m15": "15min", "h1": "1h", "h4": "4h", "d1": "1D"}


def _rates_to_df(rates) -> pd.DataFrame:
    """Convert an MT5 rates structured array to our standard OHLC DataFrame."""
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                     "close", "volume"])
    df = pd.DataFrame(rates)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    vol = "real_volume" if "real_volume" in df and df["real_volume"].sum() > 0 \
          else "tick_volume"
    df = df.rename(columns={vol: "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]].copy()


# --- tick-based history (the reliable path for these FTMO custom symbols) ----
#
# WHY TICKS: for custom symbols the M1 rate series often is NOT synchronised to
# the Python API (copy_rates_* returns ~nothing) even though the .hcc files
# exist.  The tick base IS exposed via copy_ticks_range, so we rebuild bars from
# ticks — exactly what the original data_loader.py + aggregation.py did.

def _ticks_to_df(ticks) -> pd.DataFrame:
    """
    MT5 tick array -> DataFrame[timestamp(UTC), mid, spread].

    Signals run on `mid` (the real mid price); `spread` (= ask - bid) is carried
    so the backtest can model the broker's *actual* execution cost instead of a
    guessed commission. XAUUSD price-filtered (drops glitchy ticks).
    """
    if ticks is None or len(ticks) == 0:
        return pd.DataFrame(columns=["timestamp", "mid", "spread"])
    df = pd.DataFrame(ticks)
    # millisecond precision when available, else seconds
    if "time_msc" in df:
        df["timestamp"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    bid, ask = df["bid"], df["ask"]
    both = (bid > 0) & (ask > 0)
    mid = ((bid + ask) / 2.0).where(both, df[["bid", "ask"]].max(axis=1))
    df["mid"] = mid
    df["spread"] = (ask - bid).where(both, 0.0).clip(lower=0.0)
    df = df[(df["mid"] >= 1_000.0) & (df["mid"] <= 5_000.0)]   # XAUUSD band
    return df[["timestamp", "mid", "spread"]]


def _ticks_to_1min(tdf: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a tick DataFrame to 1-minute OHLC (volume = tick count,
    spread = mean ask-bid that minute)."""
    if tdf.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                     "close", "volume", "spread"])
    g = tdf.set_index("timestamp")
    ohlc = g["mid"].resample("1min").ohlc()
    ohlc["volume"] = g["mid"].resample("1min").count()
    ohlc["spread"] = g["spread"].resample("1min").mean()
    bars = ohlc.dropna(subset=["open"]).reset_index()
    return bars[["timestamp", "open", "high", "low", "close", "volume", "spread"]]


def _resample_1min(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a 1-minute OHLC frame to a higher timeframe."""
    s = df1m.set_index("timestamp")
    agg = s.resample(rule).agg(open=("open", "first"), high=("high", "max"),
                               low=("low", "min"), close=("close", "last"),
                               volume=("volume", "sum")).dropna(subset=["open"])
    return agg.reset_index()


def _cache_path(symbol, start, end, tf_keys) -> str:
    key = f"{symbol}|{start.isoformat()}|{end.isoformat()}|{','.join(tf_keys)}"
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{symbol}_{h}.pkl")


def fetch_all_range(symbol: str,
                    start: datetime, end: datetime,
                    tf_keys=("m1", "m5", "h1"),
                    use_cache: bool = True) -> dict:
    """
    Build the timeframes the strategy needs from MT5 TICKS. -> {tf_key: df}

    The strategy uses only m1 (execution clock), m5 (internal sweep / entry FVG)
    and h1 (setup zones), so only those are built by default.

    Ticks are pulled month-by-month (bounded memory), aggregated to 1-minute
    OHLC, then the higher timeframes are resampled from that single 1-min source.

    Built bars are cached to disk (CACHE_DIR) keyed by symbol+range+timeframes;
    a repeat call for the same range reloads in seconds instead of re-pulling
    every tick. Pass use_cache=False to force a fresh fetch.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    cache_file = _cache_path(symbol, start, end, tf_keys)
    if use_cache and os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            out = pickle.load(f)
        print(f"[mt5] bars from cache: " +
              "  ".join(f"{tf}={len(out[tf]):,}" for tf in tf_keys) +
              f"  ({os.path.basename(cache_file)})")
        return out

    ensure_symbol(symbol)

    # monthly chunk boundaries (bounded memory; one copy_ticks_range per month)
    bounds = [pd.Timestamp(start)]
    bounds += list(pd.date_range(start, end, freq="MS", tz="UTC"))
    bounds += [pd.Timestamp(end)]
    bounds = sorted(set(b for b in bounds if start <= b <= end))

    parts = []
    for a, b in zip(bounds, bounds[1:]):
        if a >= b:
            continue
        ticks = mt5.copy_ticks_range(symbol, a.to_pydatetime(),
                                     b.to_pydatetime(), mt5.COPY_TICKS_ALL)
        tdf = _ticks_to_df(ticks)
        bars = _ticks_to_1min(tdf)
        if not bars.empty:
            parts.append(bars)
        print(f"[mt5] {symbol} ticks {a:%Y-%m} : {len(tdf):>9,} ticks "
              f"-> {len(bars):>6,} m1 bars")

    if not parts:
        raise RuntimeError(
            f"No ticks for {symbol} in {start.date()}..{end.date()}. "
            f"Check the symbol name and that its tick history is present in MT5.")

    df1m = (pd.concat(parts, ignore_index=True)
            .drop_duplicates("timestamp")
            .sort_values("timestamp")
            .reset_index(drop=True))

    out = {"m1": df1m}
    for tf in tf_keys:
        if tf == "m1":
            continue
        out[tf] = _resample_1min(df1m, _RESAMPLE_RULE[tf])
    print(f"[mt5] built bars: " +
          "  ".join(f"{tf}={len(out[tf]):,}" for tf in tf_keys))
    result = {tf: out[tf] for tf in tf_keys}

    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[mt5] cached bars -> {os.path.basename(cache_file)}")
    return result


def fetch_recent(symbol: str, tf_key: str, count: int) -> pd.DataFrame:
    """
    Fetch the last `count` closed bars of `tf_key` for the live loop.

    Tries the rate series first (fast); if the terminal doesn't expose it
    (custom-symbol sync quirk), rebuilds the needed bars from recent ticks.
    """
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[tf_key], 1, count)
    df = _rates_to_df(rates)
    if len(df) >= max(3, count // 2):
        return df

    # fallback: rebuild from the last few days of ticks
    end = datetime.now(timezone.utc)
    span_days = {"m1": 2, "m5": 4, "m15": 8, "h1": 20, "h4": 60, "d1": 200}
    start = end - pd.Timedelta(days=span_days.get(tf_key, 5)).to_pytimedelta()
    ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
    df1m = _ticks_to_1min(_ticks_to_df(ticks))
    if tf_key == "m1":
        return df1m.tail(count).reset_index(drop=True)
    return _resample_1min(df1m, _RESAMPLE_RULE[tf_key]).tail(count).reset_index(drop=True)


def to_bt_df(df: pd.DataFrame, with_spread: bool = False) -> pd.DataFrame:
    """
    Convert our standard OHLC DataFrame into the shape backtrader's
    PandasData expects: a naive (tz-removed) DatetimeIndex named 'datetime'
    with open/high/low/close/volume columns. With `with_spread`, the per-bar
    `spread` column is carried too (the m1 feed uses it to charge a variable,
    per-bar spread cost instead of a single constant).
    """
    out = df.copy()
    out["datetime"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_localize(None)
    cols = ["open", "high", "low", "close", "volume"]
    if with_spread and "spread" in out.columns:
        cols = cols + ["spread"]
    return out.set_index("datetime")[cols]
