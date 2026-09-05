#!/usr/bin/env python3
"""
sweep.py  -  Disciplined robustness test of the STOP-LOSS WIDTH.

Why this exists: the backtest showed too many trades getting stopped out
(21 of 29). The hypothesis is that the 1.5x ATR stop is too tight and is being
shaken out by normal noise. This script tests wider stops - BUT across several
time windows, not just one - so we can tell a real improvement from a lucky
stretch. It uses the SAME strategy engine as the live system (backtest.simulate),
so what you see here is what would actually trade.

It reads the cached market data (run  python backtest.py  once first to build the
cache), so it is fast. Every setting except the ATR stop multiple is held
constant at whatever is in config.yaml.

Run:   python sweep.py
"""
import copy
import logging

import config
import backtest

log = logging.getLogger("sweep")

# The stop widths to test. 1.5 is the current setting.
STOP_MULTIPLES = [1.5, 2.0, 2.5, 3.0]


def summarize(closed, start_equity, final_equity):
    n = len(closed)
    wins = sum(1 for t in closed if t["r"] > 0)
    win_rate = (wins / n * 100.0) if n else 0.0
    avg_r = (sum(t["r"] for t in closed) / n) if n else 0.0
    total_return = (final_equity / start_equity - 1.0) * 100.0
    return {"trades": n, "win_rate": win_rate, "avg_r": avg_r, "return": total_return}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = config.CONFIG
    log.info("Loading cached data (run 'python backtest.py' once if this is slow)...")
    price, spy, dates, sectors = backtest.load_data(cfg, synthetic=False)
    n_dates = len(dates)
    log.info("Loaded %d symbols over %d trading days.", len(price), n_dates)

    # Define the tradeable range, then split it into thirds for robustness.
    warmup = 210
    full_start = max(n_dates - int(cfg["backtest_years"]) * 252, warmup + 1)
    full_end = n_dates - 1
    span = full_end - full_start
    third = span // 3
    windows = [
        ("Full period", full_start, full_end),
        ("1st third", full_start, full_start + third),
        ("2nd third", full_start + third, full_start + 2 * third),
        ("3rd third", full_start + 2 * third, full_end),
    ]

    print("\n" + "=" * 68)
    print(" STOP-WIDTH SWEEP  (all other settings held at config.yaml values)")
    print(f" regime filter: {'ON' if cfg.get('market_regime_filter') else 'off'}"
          f"   time stop: {cfg['time_stop_days']} t-days"
          f"   reward: x{cfg['reward_multiple']}")
    print("=" * 68)

    results = {}  # mult -> {window_name: summary}
    for mult in STOP_MULTIPLES:
        cfg2 = copy.deepcopy(cfg)
        cfg2["atr_stop_multiple"] = mult
        results[mult] = {}
        current = " (current)" if abs(mult - float(cfg["atr_stop_multiple"])) < 1e-9 else ""
        print(f"\n--- ATR stop x{mult}{current} ---")
        print(f" {'window':<14}{'trades':>7}{'win%':>7}{'avg R':>8}{'return%':>9}")
        for name, ws, we in windows:
            closed, eq, se, fe = backtest.simulate(price, spy, dates, sectors, cfg2,
                                                   entry_start=ws, entry_end=we)
            s = summarize(closed, se, fe)
            results[mult][name] = s
            print(f" {name:<14}{s['trades']:>7}{s['win_rate']:>6.0f}%"
                  f"{s['avg_r']:>+8.2f}{s['return']:>+8.1f}%")

    # ---- Honest verdict ----
    print("\n" + "=" * 68)
    print(" READ-OUT")
    print("=" * 68)
    print(" A stop width is only worth adopting if it improves the FULL period")
    print(" AND is not negative across the sub-periods (i.e. not one lucky stretch).")
    print()
    best = None
    for mult in STOP_MULTIPLES:
        full = results[mult]["Full period"]
        subs = [results[mult][w] for w, _, _ in windows if w != "Full period"]
        pos_subs = sum(1 for s in subs if s["return"] > 0)
        avg_r = full["avg_r"]
        robust = pos_subs >= 2 and avg_r > 0
        flag = "  <-- robustly positive" if robust else ""
        print(f"  stop x{mult}: full return {full['return']:+.1f}%, "
              f"avg R {full['avg_r']:+.2f}, "
              f"{pos_subs}/3 sub-periods positive{flag}")
        if robust and (best is None or full["avg_r"] > results[best]["Full period"]["avg_r"]):
            best = mult

    print()
    if best is None:
        print(" VERDICT: No stop width produced a robust positive edge. Widening the")
        print("          stop did NOT fix the strategy. This points to the entry signal")
        print("          itself lacking an edge - not something a stop change can cure.")
    else:
        print(f" VERDICT: ATR stop x{best} looks the most promising (positive and")
        print("          reasonably consistent). Next step would be to confirm it on")
        print("          fresh data before trusting it - not to assume it will persist.")
    print()


if __name__ == "__main__":
    main()
