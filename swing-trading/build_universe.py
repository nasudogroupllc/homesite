#!/usr/bin/env python3
"""
build_universe.py  -  (OPTIONAL) Rebuild universe.csv with the FULL current
S&P 500 + Nasdaq 100 constituent lists and their sectors.

The system ships with a ready-to-use universe.csv of ~160 large, liquid names,
so you do NOT need to run this. Use it only if you want the complete ~500-name
S&P 500 list refreshed from the internet.

WHAT IT DOES
    - Downloads the S&P 500 table from Wikipedia (symbol + GICS sector)
    - Downloads the Nasdaq 100 table from Wikipedia (symbol)
    - Merges them, fills any missing sectors, and writes universe.csv

HOW TO RUN (on your VPS, in the project folder):
    python build_universe.py

If the download fails (no internet, Wikipedia layout changed), the existing
universe.csv is left untouched and an error is printed. You can always edit
universe.csv by hand in a text editor - it is just two columns: symbol,sector
"""
import sys
import csv

OUT_FILE = "universe.csv"


def main():
    try:
        import pandas as pd
    except ImportError:
        print("pandas is required. Run: pip install -r requirements.txt")
        sys.exit(1)

    # pandas.read_html needs 'lxml'. Give a friendly hint if it is missing.
    try:
        import lxml  # noqa: F401
    except ImportError:
        print("This script also needs 'lxml'. Run: pip install lxml")
        sys.exit(1)

    rows = {}  # symbol -> sector

    # ---- S&P 500 (has sectors) ----
    try:
        sp = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        )[0]
        for _, r in sp.iterrows():
            sym = str(r["Symbol"]).strip().upper().replace(".", ".")
            sector = str(r["GICS Sector"]).strip()
            if sym and sym != "NAN":
                rows[sym] = sector
        print(f"S&P 500: loaded {len(rows)} symbols")
    except Exception as e:
        print(f"Could not load S&P 500 list: {e}")

    # ---- Nasdaq 100 (may not list sectors; keep any we already have) ----
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        ndx = None
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols):
                ndx = t
                break
        if ndx is not None:
            symcol = next(
                c for c in ndx.columns
                if "ticker" in str(c).lower() or "symbol" in str(c).lower()
            )
            added = 0
            for _, r in ndx.iterrows():
                sym = str(r[symcol]).strip().upper()
                if sym and sym != "NAN" and sym not in rows:
                    rows[sym] = "Unknown"  # you can fill the sector in by hand
                    added += 1
            print(f"Nasdaq 100: added {added} extra symbols")
    except Exception as e:
        print(f"Could not load Nasdaq 100 list: {e}")

    if not rows:
        print("Nothing downloaded. Leaving universe.csv unchanged.")
        sys.exit(1)

    with open(OUT_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "sector"])
        for sym in sorted(rows):
            w.writerow([sym, rows[sym]])

    print(f"Wrote {len(rows)} symbols to {OUT_FILE}")
    unknown = [s for s, sec in rows.items() if sec == "Unknown"]
    if unknown:
        print(
            f"NOTE: {len(unknown)} symbols have sector 'Unknown'. "
            "Open universe.csv and fill in their sectors so the "
            "one-position-per-sector rule works correctly."
        )


if __name__ == "__main__":
    main()
