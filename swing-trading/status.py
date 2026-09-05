#!/usr/bin/env python3
"""
status.py  -  A quick "how are things?" report you can run any time.

    python status.py

Shows: account equity, high-water mark and drawdown, open positions, open
orders, tonight's candidates, and the last few journal entries. It never
places or changes any orders - it only reads and prints.
"""
import json
from datetime import date

import config
import state


def _hr(title):
    print("\n" + "=" * 56)
    print(f" {title}")
    print("=" * 56)


def main():
    _hr("SWING TRADING SYSTEM - STATUS")
    print(f" Date: {date.today().isoformat()}")
    print(f" Mode: {'PAPER (fake money)' if config.is_paper() else 'LIVE MONEY'}")

    # ---- Account (needs Alpaca) ----
    equity = None
    try:
        import broker
        b = broker.Broker()
        equity = b.equity()
        print(f" Account equity: ${equity:,.2f}")
        hwm = state.get_high_water_mark(default=equity)
        print(f" High-water mark: ${hwm:,.2f}")
        print(f" Current drawdown: {state.drawdown_pct(equity):.1f}%"
              f"  (halt at {config.CONFIG['drawdown_halt_pct']}%)")

        _hr("OPEN POSITIONS")
        pos = b.positions()
        if not pos:
            print(" (none)")
        for p in pos:
            pl = ""
            if p["current_price"]:
                chg = (p["current_price"] - p["avg_entry_price"]) * p["qty"]
                pl = f"  P/L ${chg:+,.2f}"
            print(f"  {p['symbol']:6} {p['qty']:>5} sh @ ${p['avg_entry_price']:.2f}"
                  f"  now ${p['current_price'] or 0:.2f}{pl}")

        _hr("OPEN ORDERS")
        orders = b.open_orders()
        if not orders:
            print(" (none)")
        for o in orders:
            print(f"  {o.symbol:6} {str(o.side).split('.')[-1]:4} {str(o.type).split('.')[-1]:11}"
                  f" qty {o.qty}  status {str(o.status).split('.')[-1]}")
    except Exception as e:  # noqa: BLE001
        print(f"\n (Could not reach Alpaca: {e})")

    # ---- Candidates ----
    _hr("LATEST CANDIDATES (candidates.json)")
    try:
        data = json.load(open(config.CANDIDATES_FILE))
        print(f" Generated: {data.get('generated_at', '?')}")
        cands = data.get("candidates", [])
        if not cands:
            print(" (no candidates in file)")
        for c in cands[:15]:
            print(f"  {c.get('rank','?'):>2}. {c['symbol']:6} ${c['price']:.2f}"
                  f"  RS {c['rs_score']:+.1f}%  ({c.get('sector','?')})")
        if data.get("flagged_earnings_unknown"):
            print(" Flagged (earnings unknown): " + ", ".join(data["flagged_earnings_unknown"]))
    except FileNotFoundError:
        print(" (candidates.json not created yet - run scanner.py)")
    except Exception as e:  # noqa: BLE001
        print(f" (could not read candidates.json: {e})")

    # ---- Recent journal ----
    _hr("RECENT JOURNAL ENTRIES (journal.csv)")
    try:
        import csv
        rows = list(csv.DictReader(open(config.JOURNAL_FILE)))
        if not rows:
            print(" (no trades recorded yet)")
        for r in rows[-8:]:
            print(f"  {r['date']} {r['symbol']:6} {r['status']:9}"
                  f" entry {r['entry']:>7} exit {r.get('exit_price','') or '-':>7}"
                  f" R {r.get('r_result','') or '-':>6}")
    except FileNotFoundError:
        print(" (journal.csv not created yet)")

    print()


if __name__ == "__main__":
    main()
