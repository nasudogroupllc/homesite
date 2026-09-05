"""
indicators.py  -  All the technical-analysis math in one place.

Every function takes a pandas DataFrame of daily bars with these columns:
    open, high, low, close, volume
sorted oldest-first (row 0 is the oldest day, the last row is the most recent).

These are pure calculations - no internet, no orders - so they are easy to
test and reuse in both the scanner and the backtest.
"""
import numpy as np
import pandas as pd


def sma(series, window):
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series, window):
    """Exponential moving average."""
    return series.ewm(span=window, adjust=False).mean()


def true_range(df):
    """True Range for each day (the building block of ATR)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df, window=14):
    """Average True Range using Wilder's smoothing (the classic ATR)."""
    tr = true_range(df)
    # Wilder's smoothing == exponential mean with alpha = 1/window.
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def rsi(series, window=14):
    """Relative Strength Index using Wilder's smoothing."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # If there were no losses at all, RSI is 100.
    out = out.where(avg_loss != 0.0, 100.0)
    return out


def adr(df, window=20):
    """Average Daily Range in dollars = average of (high - low) over N days."""
    return (df["high"] - df["low"]).rolling(window=window, min_periods=window).mean()


def avg_dollar_volume(df, window=20):
    """Average of (close x volume) over N days - i.e. dollars traded per day."""
    return (df["close"] * df["volume"]).rolling(window=window, min_periods=window).mean()


def avg_volume(df, window=20):
    """Average share volume over N days."""
    return df["volume"].rolling(window=window, min_periods=window).mean()


def high_52w(df, window=252):
    """Highest high over the last ~52 weeks (252 trading days)."""
    return df["high"].rolling(window=window, min_periods=1).max()


def pct_return(series, lookback):
    """Percentage return over 'lookback' trading days, as a fraction (0.1 = 10%)."""
    return series / series.shift(lookback) - 1.0


def compute_snapshot(df, cfg):
    """
    Compute every indicator the scanner needs for the MOST RECENT day.
    Returns a dictionary of numbers, or None if there is not enough history.
    'cfg' is the loaded config.yaml dictionary.
    """
    if df is None or len(df) < 210:
        # Need enough bars for SMA200 to exist.
        return None

    close = df["close"]
    last = df.iloc[-1]

    sma50 = sma(close, 50).iloc[-1]
    sma200 = sma(close, 200).iloc[-1]
    ema20 = ema(close, 20).iloc[-1]
    atr14 = atr(df, 14).iloc[-1]
    rsi14 = rsi(close, 14).iloc[-1]
    adr20 = adr(df, 20).iloc[-1]
    dollar_vol = avg_dollar_volume(df, 20).iloc[-1]
    vol_avg20 = avg_volume(df, 20).iloc[-1]
    hi52 = high_52w(df, 252).iloc[-1]

    lookback = int(cfg["relative_strength_lookback"])
    ret_63 = None
    if len(close) > lookback:
        ret_63 = float(close.iloc[-1] / close.iloc[-1 - lookback] - 1.0)

    snap = {
        "price": float(last["close"]),
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "volume": float(last["volume"]),
        "prior_high": float(df["high"].iloc[-2]),
        "sma50": _f(sma50),
        "sma200": _f(sma200),
        "ema20": _f(ema20),
        "atr14": _f(atr14),
        "rsi14": _f(rsi14),
        "adr20": _f(adr20),
        "avg_dollar_volume": _f(dollar_vol),
        "avg_volume20": _f(vol_avg20),
        "high_52w": _f(hi52),
        "ret_63": ret_63,
    }
    # If any core indicator is missing (not enough data), reject the symbol.
    for key in ("sma50", "sma200", "ema20", "atr14", "rsi14", "adr20",
                "avg_dollar_volume", "avg_volume20", "high_52w"):
        if snap[key] is None or (isinstance(snap[key], float) and np.isnan(snap[key])):
            return None
    return snap


def _f(x):
    """Turn a possibly-NaN value into a clean float or None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(x):
        return None
    return x
