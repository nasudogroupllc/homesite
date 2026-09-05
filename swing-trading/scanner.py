#!/usr/bin/env python3
"""
scanner.py  -  Runs at 5:00 PM ET every trading day.

WHAT IT DOES
  1. Loads your stock list from universe.csv.
  2. Downloads daily price history for each (ThetaData first, Alpaca fallback).
  3. Applies ALL of the scanner filters from the spec.
  4. Ranks survivors by relative strength vs SPY and keeps the top ones.
  5. Writes the winners to candidates.json (which executor.py reads next morning).
  6. Sends you a Telegram message listing the candidates.

RUN IT BY HAND (for testing):
    python scanner.py
"""
import json
import logging
import traceback
from datetime import datetime

import config
import journal
import notifier
import indicators
import earnings
from datafeed import DataFeed

log = logging.getLogger("scanner")


def load_universe():
    """Read universe.csv into a list of {symbol, sector} dicts."""
    import csv
    rows = []
    with open(config.UNIVERSE_FILE, "r", newline="") as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                rows.append({"symbol": sym, "sector": (r.get("sector") or "Unknown").strip()})
    return rows


def passes_filters(snap, cfg, spy_ret):
    """Return (True, rs_score) if the snapshot passes filters 1-8, else (False, reason)."""
    price = snap["price"]

    # 1) Price range
    if not (cfg["price_min"] <= price <= cfg["price_max"]):
        return False, "price out of range"

    # 2) Dollar volume
    if not (snap["avg_dollar_volume"] > cfg["min_dollar_volume"]):
        return False, "dollar volume too low"

    # 3) Trend: close > SMA50 > SMA200
    if not (price > snap["sma50"] > snap["sma200"]):
        return False, "not in uptrend"

    # 4) Within X% of 52-week high
    if not (price >= (cfg["near_high_pct"] / 100.0) * snap["high_52w"]):
        return False, "too far below 52w high"

    # 5) Near EMA20 (within 1 ATR) OR RSI in pullback band
    near_ema = abs(price - snap["ema20"]) <= snap["atr14"]
    rsi_ok = cfg["rsi_low"] <= snap["rsi14"] <= cfg["rsi_high"]
    if not (near_ema or rsi_ok):
        return False, "not near EMA20 and RSI not in band"

    # 6) Up day: close > open
    if not (price > snap["open"]):
        return False, "down day"

    # 7) Volume confirmation
    if not (snap["volume"] >= cfg["volume_ratio_min"] * snap["avg_volume20"]):
        return False, "volume too light"

    # 8) 3*ATR <= 10*ADR (not unusually volatile vs its normal range)
    if not (cfg["atr_vs_adr_max_atr_mult"] * snap["atr14"]
            <= cfg["atr_vs_adr_max_adr_mult"] * snap["adr20"]):
        return False, "ATR too high vs ADR"

    # Relative strength score (filter 10 ranking): stock 63d return minus SPY's
    rs_score = (snap["ret_63"] - spy_ret) * 100.0 if snap["ret_63"] is not None else -999
    return True, rs_score


def run():
    journal.setup_logging()
    import market
    if not market.is_trading_day():
        log.info("Not a trading day; skipping scan.")
        return {"candidates": [], "flagged_earnings_unknown": []}
    log.info("===== SCAN START %s =====", datetime.now().isoformat())
    feed = DataFeed(notifier=notifier)
    cfg = config.CONFIG

    # --- Benchmark: SPY 63-day return, used for the relative-strength ranking.
    spy_ret = 0.0
    spy_df = feed.get_daily_bars(cfg["benchmark_symbol"])
    if spy_df is not None:
        spy_snap = indicators.compute_snapshot(spy_df, cfg)
        if spy_snap and spy_snap["ret_63"] is not None:
            spy_ret = spy_snap["ret_63"]
    log.info("SPY %sd return: %.2f%%", cfg["relative_strength_lookback"], spy_ret * 100)

    universe = load_universe()
    log.info("Scanning %d symbols...", len(universe))

    survivors = []
    for row in universe:
        sym = row["symbol"]
        try:
            df = feed.get_daily_bars(sym)
            if df is None:
                log.warning("No data for %s; skipping.", sym)
                continue
            snap = indicators.compute_snapshot(df, cfg)
            if snap is None:
                continue
            ok, result = passes_filters(snap, cfg, spy_ret)
            if ok:
                survivors.append({
                    "symbol": sym,
                    "sector": row["sector"],
                    "price": round(snap["price"], 2),
                    "prior_high": round(snap["prior_high"], 2),
                    "atr14": round(snap["atr14"], 4),
                    "rs_score": round(result, 2),
                })
        except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the scan
            log.error("Error scanning %s: %s", sym, e)

    # --- Filter 9: earnings blackout ---
    flagged = []
    earn = earnings.upcoming_earnings(int(cfg["earnings_blackout_days"]))
    if earn is None:
        # Calendar unavailable: flag everyone instead of trading blindly.
        flagged = [s["symbol"] for s in survivors]
        log.warning("Earnings calendar unavailable; flagging %d symbols.", len(flagged))
    else:
        kept = []
        for s in survivors:
            if s["symbol"] in earn:
                log.info("%s excluded: earnings within blackout window.", s["symbol"])
            else:
                kept.append(s)
        survivors = kept

    # --- Filter 10: rank by relative strength, keep top N ---
    survivors.sort(key=lambda s: s["rs_score"], reverse=True)
    top = survivors[: int(cfg["keep_top_n"])]
    for i, s in enumerate(top, start=1):
        s["rank"] = i

    payload = {
        "generated_at": datetime.now().isoformat(),
        "spy_ret_63": round(spy_ret * 100, 2),
        "earnings_source_ok": earn is not None,
        "candidates": top,
        "flagged_earnings_unknown": flagged,
    }
    with open(config.CANDIDATES_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    log.info("Scan complete: %d candidates written to %s",
             len(top), config.CANDIDATES_FILE)
    notifier.notify_candidates(top, flagged=flagged if flagged else None)
    log.info("===== SCAN END =====")
    return payload


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        journal.setup_logging()
        log.error("SCAN CRASHED: %s\n%s", e, traceback.format_exc())
        notifier.notify_error("scanner", e)
        raise
