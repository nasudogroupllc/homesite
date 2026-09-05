#!/usr/bin/env python3
"""
backtest.py  -  Replays the strategy over the last ~2 years of daily data and
reports how it would have performed.

WHAT IT REPORTS
    - Total trades
    - Win rate
    - Average R (average result in units of risk)
    - Max drawdown (worst peak-to-trough drop in equity)
    - Equity curve saved as equity_curve.png

HOW TO RUN
    Real data (on your VPS, ThetaData running):
        python backtest.py
    Quick self-test with fake data (no ThetaData needed, proves it works):
        python backtest.py --synthetic

NOTES / HONEST LIMITATIONS
    - The backtest uses the SAME filters and money-management rules as the live
      scanner/executor, EXCEPT it does not apply the earnings blackout (reliable
      historical earnings dates are hard to get for free). Live trading DOES
      apply it. This makes the backtest slightly optimistic, not pessimistic.
    - Fills are modeled realistically (a stop-limit that gaps past its limit
      does not fill), but real slippage/commissions are not added. Alpaca is
      commission-free; treat the numbers as a guide, not a guarantee.
"""
import sys
import math
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no screen needed; we just save a PNG file
import matplotlib.pyplot as plt

import config
import indicators

log = logging.getLogger("backtest")


# --------------------------------------------------------------- data loading
def _enrich(df, cfg):
    """Add all indicator columns to one symbol's DataFrame (vectorized)."""
    out = df.copy()
    out["sma50"] = indicators.sma(out["close"], 50)
    out["sma200"] = indicators.sma(out["close"], 200)
    out["ema20"] = indicators.ema(out["close"], 20)
    out["atr14"] = indicators.atr(out, 14)
    out["rsi14"] = indicators.rsi(out["close"], 14)
    out["adr20"] = indicators.adr(out, 20)
    out["advol"] = indicators.avg_dollar_volume(out, 20)
    out["avgvol20"] = indicators.avg_volume(out, 20)
    out["hi52"] = indicators.high_52w(out, 252)
    out["prior_high"] = out["high"].shift(1)
    lb = int(cfg["relative_strength_lookback"])
    out["ret63"] = indicators.pct_return(out["close"], lb)
    return out


def _synthetic_prices(symbols, master_dates, seed=7):
    """Generate believable fake price history (random walk with drift) so the
    engine can be tested without ThetaData."""
    rng = np.random.default_rng(seed)
    data = {}
    n = len(master_dates)
    for k, sym in enumerate(symbols):
        drift = rng.normal(0.0006, 0.0004)      # small upward drift on average
        vol = rng.uniform(0.012, 0.028)         # daily volatility
        start = rng.uniform(20, 300)
        rets = rng.normal(drift, vol, n)
        close = start * np.exp(np.cumsum(rets))
        # Build OHLC around the close path.
        daily_range = close * rng.uniform(0.01, 0.03, n)
        openp = close * (1 + rng.normal(0, 0.005, n))
        high = np.maximum(openp, close) + daily_range / 2
        low = np.minimum(openp, close) - daily_range / 2
        low = np.maximum(low, 0.5)
        volume = rng.uniform(1_000_000, 8_000_000, n) * (close / start)
        df = pd.DataFrame(
            {"open": openp, "high": high, "low": low, "close": close, "volume": volume},
            index=master_dates,
        )
        data[sym] = df
    return data


CACHE_FILE = None  # set lazily to PROJECT_DIR / "backtest_cache.pkl"


def _cache_path():
    global CACHE_FILE
    if CACHE_FILE is None:
        CACHE_FILE = config.PROJECT_DIR / "backtest_cache.pkl"
    return CACHE_FILE


def _download_raw(cfg, symbols, bench, need_days):
    """Download raw daily bars for the benchmark + every symbol. Slow (network)."""
    from datafeed import DataFeed
    feed = DataFeed()
    spy_raw = feed.get_daily_bars(bench, days=need_days)
    if spy_raw is None:
        raise RuntimeError(
            "Could not load benchmark (SPY) data. Is the market-data endpoint up? "
            "Try:  python datafeed.py test SPY   or   python backtest.py --synthetic"
        )
    raw = {}
    for i, sym in enumerate(symbols, 1):
        df = feed.get_daily_bars(sym, days=need_days)
        if df is not None and len(df) >= 260:
            raw[sym] = df
        if i % 25 == 0:
            log.info("Loaded data for %d/%d symbols...", i, len(symbols))
    return spy_raw, raw


def load_data(cfg, synthetic=False, refresh=False):
    """Return (price_dict, spy_df, master_dates, sectors).

    Raw bars are cached to backtest_cache.pkl so repeated experiments with
    different config settings run in seconds instead of re-downloading.
    Pass refresh=True (or --refresh) to force a fresh download."""
    import scanner
    import pickle
    universe = scanner.load_universe()
    symbols = [u["symbol"] for u in universe]
    sectors = {u["symbol"]: u["sector"] for u in universe}
    bench = cfg["benchmark_symbol"]
    need_days = int(cfg["backtest_years"]) * 252 + 300  # extra for indicator warmup

    if synthetic:
        master_dates = pd.bdate_range(end=datetime.now(), periods=need_days)
        raw = _synthetic_prices([bench] + symbols, master_dates)
        spy_raw = raw[bench]
    else:
        cache = _cache_path()
        cached = None
        if not refresh and cache.exists():
            try:
                with open(cache, "rb") as f:
                    cached = pickle.load(f)
                # Invalidate if the universe or lookback window changed.
                if set(cached.get("symbols", [])) != set(symbols) \
                        or cached.get("need_days") != need_days:
                    log.info("Cache is stale (universe/window changed); re-downloading.")
                    cached = None
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read cache (%s); re-downloading.", e)
                cached = None

        if cached is not None:
            log.info("Using cached market data from %s (saved %s). "
                     "Delete that file or pass --refresh to re-download.",
                     cache.name, cached.get("saved"))
            spy_raw, raw = cached["spy_raw"], cached["raw"]
        else:
            spy_raw, raw = _download_raw(cfg, symbols, bench, need_days)
            try:
                with open(cache, "wb") as f:
                    pickle.dump({"symbols": symbols, "need_days": need_days,
                                 "saved": datetime.now().isoformat(timespec="seconds"),
                                 "spy_raw": spy_raw, "raw": raw}, f)
                log.info("Saved market data to %s for fast re-runs.", cache.name)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not write cache: %s", e)

        master_dates = spy_raw.index

    # Enrich and align everything to SPY's trading calendar (fast; uses cfg).
    spy = _enrich(spy_raw, cfg).reindex(master_dates)
    price = {}
    for sym in symbols:
        if sym in raw:
            price[sym] = _enrich(raw[sym], cfg).reindex(master_dates)
    return price, spy, master_dates, sectors


# ------------------------------------------------------------------ filtering
def _row_passes(row, spy_ret, cfg):
    """Apply scanner filters 1-8 to one precomputed row. Returns rs_score or None."""
    if row is None or row.isnull().any():
        # Missing indicators -> not enough history yet.
        needed = ["close", "open", "sma50", "sma200", "ema20", "atr14",
                  "rsi14", "adr20", "advol", "avgvol20", "hi52", "prior_high", "ret63"]
        if any(pd.isna(row.get(c)) for c in needed):
            return None
    price = row["close"]
    if not (cfg["price_min"] <= price <= cfg["price_max"]):
        return None
    if not (row["advol"] > cfg["min_dollar_volume"]):
        return None
    if not (price > row["sma50"] > row["sma200"]):
        return None
    if not (price >= (cfg["near_high_pct"] / 100.0) * row["hi52"]):
        return None
    near_ema = abs(price - row["ema20"]) <= row["atr14"]
    rsi_ok = cfg["rsi_low"] <= row["rsi14"] <= cfg["rsi_high"]
    if not (near_ema or rsi_ok):
        return None
    if not (price > row["open"]):
        return None
    if not (row["volume"] >= cfg["volume_ratio_min"] * row["avgvol20"]):
        return None
    if not (cfg["atr_vs_adr_max_atr_mult"] * row["atr14"]
            <= cfg["atr_vs_adr_max_adr_mult"] * row["adr20"]):
        return None
    if pd.isna(row["ret63"]):
        return None
    return (row["ret63"] - spy_ret) * 100.0


# -------------------------------------------------------------------- engine
def simulate(price, spy, dates, sectors, cfg, entry_start=None, entry_end=None):
    """Run the strategy over the data. Positions are opened only while
    entry_start <= i < entry_end (defaults: the last backtest_years, to the end),
    but existing positions are always managed so late trades can resolve.

    Returns (closed_trades, equity_curve, start_equity, final_equity).
    This is the SINGLE source of truth for the strategy logic - the normal
    backtest and the parameter sweep both call it, so they can never disagree."""
    start_equity = float(cfg["backtest_starting_equity"])
    cash = start_equity
    positions = []
    closed = []
    equity_curve = []
    peak = start_equity
    halt_armed = True        # circuit breaker is ready to fire
    cooldown_remaining = 0   # trading days left in a drawdown cooldown

    warmup = 210  # need SMA200 etc. before trading
    if entry_start is None:
        entry_start = len(dates) - int(cfg["backtest_years"]) * 252
    entry_start = max(entry_start, warmup + 1)
    if entry_end is None:
        entry_end = len(dates) - 1

    def mark_to_market(i):
        val = cash
        for p in positions:
            row = price[p["symbol"]].iloc[i]
            px = row["close"] if not pd.isna(row["close"]) else p["entry"]
            val += p["shares"] * px
        return val

    for i in range(warmup, len(dates) - 1):
        today = dates[i]

        # ---- 1) manage open positions using today's OHLC ----
        still_open = []
        for p in positions:
            row = price[p["symbol"]].iloc[i]
            if pd.isna(row["close"]):
                still_open.append(p)
                continue
            exit_price = None
            reason = None
            holding = (today.date() - p["entry_date"].date()).days      # calendar days
            td_held = i - p["entry_i"]                                   # trading days

            # Time stop first (measured in TRADING days; 0 or less disables it).
            ts_days = int(cfg["time_stop_days"])
            if ts_days > 0 and td_held >= ts_days:
                exit_price = row["open"] if not pd.isna(row["open"]) else row["close"]
                reason = "TIME_STOP"
            else:
                # Stop and target intraday. If it gaps, use the open.
                if row["low"] <= p["stop"]:
                    exit_price = min(row["open"], p["stop"]) if row["open"] < p["stop"] else p["stop"]
                    reason = "STOP"
                elif row["high"] >= p["target"]:
                    exit_price = max(row["open"], p["target"]) if row["open"] > p["target"] else p["target"]
                    reason = "TARGET"

            if reason:
                cash += p["shares"] * exit_price
                r_result = (exit_price - p["entry"]) / p["risk_per_share"]
                closed.append({
                    "symbol": p["symbol"], "entry": p["entry"], "exit": exit_price,
                    "r": r_result, "reason": reason, "holding": holding,
                    "td_held": td_held,
                    "entry_date": p["entry_date"], "exit_date": today,
                })
            else:
                still_open.append(p)
        positions = still_open

        # Only track equity / place trades inside the entry window.
        if i < entry_start:
            continue
        equity = mark_to_market(i)
        peak = max(peak, equity)
        equity_curve.append((today, equity))

        # No new entries outside the entry window (existing positions still managed).
        if i >= entry_end:
            continue

        # ---- 2) scan as of today, rank survivors ----
        spy_row = spy.iloc[i]
        spy_ret = spy_row["ret63"] if not pd.isna(spy_row["ret63"]) else 0.0

        survivors = []
        for sym, df in price.items():
            row = df.iloc[i]
            score = _row_passes(row, spy_ret, cfg)
            if score is not None:
                survivors.append((sym, score, row["prior_high"], row["atr14"]))
        survivors.sort(key=lambda x: x[1], reverse=True)
        survivors = survivors[: int(cfg["keep_top_n"])]

        # ---- 3) drawdown halt with auto-resume cooldown ----
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        halt_pct = float(cfg["drawdown_halt_pct"])
        cd_days = int(cfg.get("drawdown_cooldown_days", 10))
        # Re-arm the breaker once we've recovered back near the high.
        if dd <= 1.0:
            halt_armed = True
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            continue  # still cooling down: no new entries
        if halt_armed and dd > halt_pct:
            cooldown_remaining = cd_days   # start the pause; auto-resumes after
            halt_armed = False
            continue

        # ---- 3b) market-regime filter: skip new entries when SPY is below its
        #          own 200-day average (i.e. the broad market is in a downtrend) ----
        if bool(cfg.get("market_regime_filter", False)):
            spy_close = spy_row["close"]
            spy_sma200 = spy_row["sma200"]
            if pd.isna(spy_sma200) or pd.isna(spy_close) or spy_close <= spy_sma200:
                continue

        # ---- 4) place entries, simulated on tomorrow's bar ----
        held_syms = {p["symbol"] for p in positions}
        held_sectors = {sectors.get(p["symbol"], "Unknown") for p in positions}

        for sym, score, prior_high, atr14 in survivors:
            if len(positions) >= int(cfg["max_positions"]):
                break
            if sym in held_syms:
                continue
            sector = sectors.get(sym, "Unknown")
            if sector in held_sectors:
                continue
            if pd.isna(prior_high) or pd.isna(atr14) or atr14 <= 0:
                continue

            entry_stop = prior_high + float(cfg["entry_stop_buffer"])
            entry_limit = entry_stop * float(cfg["entry_limit_multiple"])
            stop_distance = float(cfg["atr_stop_multiple"]) * atr14

            # Simulate the stop-limit fill on tomorrow's bar.
            nxt = price[sym].iloc[i + 1]
            if pd.isna(nxt["high"]):
                continue
            if nxt["high"] < entry_stop:
                continue  # never triggered
            fill = entry_stop if nxt["open"] <= entry_stop else nxt["open"]
            if fill > entry_limit:
                continue  # gapped past our limit -> no fill

            equity_now = mark_to_market(i)
            risk_dollars = equity_now * float(cfg["account_risk_pct"]) / 100.0
            shares = math.floor(risk_dollars / stop_distance)
            if shares < 1:
                continue
            if shares * fill > float(cfg["max_position_size_pct"]) / 100.0 * equity_now:
                continue
            if shares * fill > cash:
                continue  # not enough cash

            cash -= shares * fill
            positions.append({
                "symbol": sym, "sector": sector, "shares": shares, "entry": fill,
                "stop": fill - stop_distance, "target": fill + float(cfg["reward_multiple"]) * stop_distance,
                "risk_per_share": stop_distance, "entry_date": dates[i + 1],
                "entry_i": i + 1,  # bar index of entry, for counting trading days held
            })
            held_syms.add(sym)
            held_sectors.add(sector)

    # ---- close out anything still open at the last price (for final equity) ----
    final_equity = mark_to_market(len(dates) - 1)
    return closed, equity_curve, start_equity, final_equity


def run(synthetic=False, refresh=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = config.CONFIG
    log.info("Loading data (%s)...", "synthetic" if synthetic else "market data")
    price, spy, dates, sectors = load_data(cfg, synthetic=synthetic, refresh=refresh)
    log.info("Loaded %d symbols over %d trading days.", len(price), len(dates))

    closed, equity_curve, start_equity, final_equity = simulate(
        price, spy, dates, sectors, cfg)

    # SPY buy-and-hold over the same window, for an honest benchmark.
    spy_bh = None
    try:
        warmup = 210
        rs = max(len(dates) - int(cfg["backtest_years"]) * 252, warmup + 1)
        c0 = spy["close"].iloc[rs]
        c1 = spy["close"].iloc[-1]
        if c0 and not pd.isna(c0) and not pd.isna(c1):
            spy_bh = (c1 / c0 - 1.0) * 100.0
    except Exception:  # noqa: BLE001
        pass

    _report(closed, equity_curve, start_equity, final_equity, spy_bh=spy_bh)
    return closed, equity_curve


def _report(closed, equity_curve, start_equity, final_equity, spy_bh=None):
    n = len(closed)
    if n == 0:
        print("\nNo trades were generated in the backtest window.")
        print("(With synthetic random data this can happen; with real data you")
        print(" should see trades. Try loosening filters in config.yaml to check.)")
    wins = sum(1 for t in closed if t["r"] > 0)
    win_rate = (wins / n * 100.0) if n else 0.0
    avg_r = (sum(t["r"] for t in closed) / n) if n else 0.0
    total_r = sum(t["r"] for t in closed)

    # Max drawdown from the equity curve.
    max_dd = 0.0
    peak = start_equity
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    total_return = (final_equity / start_equity - 1.0) * 100.0

    print("\n" + "=" * 52)
    print(" BACKTEST RESULTS")
    print("=" * 52)
    print(f" Total trades:      {n}")
    print(f" Win rate:          {win_rate:.1f}%")
    print(f" Average R:         {avg_r:+.2f}")
    print(f" Total R:           {total_r:+.2f}")
    print(f" Start equity:      ${start_equity:,.2f}")
    print(f" Final equity:      ${final_equity:,.2f}")
    print(f" Total return:      {total_return:+.1f}%")
    print(f" Max drawdown:      {max_dd:.1f}%")
    if spy_bh is not None:
        verdict = "BEAT" if total_return > spy_bh else "LAGGED"
        print(f" SPY buy & hold:    {spy_bh:+.1f}%   (strategy {verdict} the market)")
    print("=" * 52)

    # ---- Diagnostics: where does the money come from / go? ----
    if n:
        print("\n WHERE TRADES ENDED (exit reason breakdown)")
        print(" " + "-" * 50)
        print(f" {'reason':<12}{'count':>7}{'avg R':>9}{'total R':>10}{'avg t-days':>12}")
        for reason in ("TARGET", "STOP", "TIME_STOP", "EXIT"):
            grp = [t for t in closed if t["reason"] == reason]
            if not grp:
                continue
            c = len(grp)
            ar = sum(t["r"] for t in grp) / c
            tr = sum(t["r"] for t in grp)
            ad = sum(t.get("td_held", t["holding"]) for t in grp) / c
            print(f" {reason:<12}{c:>7}{ar:>9.2f}{tr:>10.2f}{ad:>12.1f}")
        avg_hold = sum(t.get("td_held", t["holding"]) for t in closed) / n
        print(f"\n Average holding time: {avg_hold:.1f} trading days")

        # Benchmark: buy-and-hold SPY over the same window, for context.
        try:
            first_eq_date = equity_curve[0][0]
            last_eq_date = equity_curve[-1][0]
            print(f" Backtest window: {first_eq_date.date()} to {last_eq_date.date()}")
        except Exception:  # noqa: BLE001
            pass

        # Echo the key settings that produced THIS run, so runs are comparable.
        cfg = config.CONFIG
        print("\n SETTINGS USED FOR THIS RUN")
        print(" " + "-" * 50)
        print(f"   risk/trade {cfg['account_risk_pct']}%   ATR stop x{cfg['atr_stop_multiple']}"
              f"   reward x{cfg['reward_multiple']}")
        print(f"   time stop {cfg['time_stop_days']} days (0=off)"
              f"   market-regime filter: {'ON' if cfg.get('market_regime_filter') else 'off'}")
        print(f"   max positions {cfg['max_positions']}   max/sector {cfg['max_sector_positions']}")

    if equity_curve:
        dates = [d for d, _ in equity_curve]
        eqs = [e for _, e in equity_curve]
        plt.figure(figsize=(11, 5))
        plt.plot(dates, eqs, linewidth=1.6, color="#1f77b4")
        plt.title("Backtest Equity Curve")
        plt.xlabel("Date")
        plt.ylabel("Account Equity ($)")
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        out = config.PROJECT_DIR / "equity_curve.png"
        plt.savefig(out, dpi=110)
        print(f" Equity curve saved to: {out}")


if __name__ == "__main__":
    synthetic = "--synthetic" in sys.argv
    refresh = "--refresh" in sys.argv
    run(synthetic=synthetic, refresh=refresh)
