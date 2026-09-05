"""
journal.py  -  Keeps your trade records.

Two things live here:
  1) trades.log  - a running text log of everything the system does.
  2) journal.csv - one row per trade with the numbers you care about:
        date, symbol, entry, stop, target, shares, r_risked,
        exit_date, exit_price, r_result, holding_days, status

An OPEN trade is written when an entry fills. When the trade later exits
(target, stop, or time-stop), the same row is UPDATED with the exit details.
"""
import csv
import logging
from datetime import datetime, date

import config

log = logging.getLogger("journal")

FIELDS = [
    "date", "symbol", "entry", "stop", "target", "shares", "r_risked",
    "exit_date", "exit_price", "r_result", "holding_days", "status",
]


def setup_logging(level=logging.INFO):
    """Configure logging to write to BOTH the screen and trades.log."""
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Avoid adding duplicate handlers if called twice.
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(config.LOG_FILE)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _read_all():
    rows = []
    if config.JOURNAL_FILE.exists():
        with open(config.JOURNAL_FILE, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    return rows


def _write_all(rows):
    with open(config.JOURNAL_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def record_entry(symbol, entry, stop, target, shares, r_risked):
    """Add a new OPEN trade row when an entry fills."""
    rows = _read_all()
    rows.append({
        "date": date.today().isoformat(),
        "symbol": symbol,
        "entry": f"{entry:.2f}",
        "stop": f"{stop:.2f}",
        "target": f"{target:.2f}",
        "shares": str(int(shares)),
        "r_risked": f"{r_risked:.2f}",
        "exit_date": "",
        "exit_price": "",
        "r_result": "",
        "holding_days": "",
        "status": "OPEN",
    })
    _write_all(rows)
    log.info("Journal: opened %s %s sh @ %.2f (stop %.2f, target %.2f)",
             symbol, int(shares), entry, stop, target)


def record_exit(symbol, exit_price, reason="EXIT"):
    """Fill in exit details on the most recent OPEN row for this symbol."""
    rows = _read_all()
    for r in reversed(rows):
        if r["symbol"] == symbol and r["status"] == "OPEN":
            entry = float(r["entry"])
            stop = float(r["stop"])
            shares = int(r["shares"])
            risk_per_share = max(entry - stop, 0.01)
            r_result = (exit_price - entry) / risk_per_share
            try:
                d0 = datetime.fromisoformat(r["date"]).date()
                holding = (date.today() - d0).days
            except Exception:  # noqa: BLE001
                holding = ""
            r["exit_date"] = date.today().isoformat()
            r["exit_price"] = f"{exit_price:.2f}"
            r["r_result"] = f"{r_result:.2f}"
            r["holding_days"] = str(holding)
            r["status"] = reason
            _write_all(rows)
            log.info("Journal: closed %s @ %.2f (%s, %.2fR)",
                     symbol, exit_price, reason, r_result)
            return
    log.warning("Journal: no OPEN row found for %s to close.", symbol)


def open_symbols():
    """Symbols currently marked OPEN in the journal."""
    return {r["symbol"] for r in _read_all() if r["status"] == "OPEN"}


def weekly_stats(equity=None):
    """Compute this week's stats for the Friday summary."""
    from datetime import timedelta
    rows = _read_all()
    week_ago = date.today() - timedelta(days=7)
    closed = []
    for r in rows:
        if r["status"] in ("OPEN", ""):
            continue
        if not r.get("exit_date"):
            continue
        try:
            ed = datetime.fromisoformat(r["exit_date"]).date()
        except Exception:  # noqa: BLE001
            continue
        if ed >= week_ago:
            closed.append(r)

    trades = len(closed)
    wins = sum(1 for r in closed if _to_float(r.get("r_result")) > 0)
    total_r = sum(_to_float(r.get("r_result")) for r in closed)
    win_rate = (wins / trades * 100.0) if trades else 0.0

    if equity is None:
        equity = _current_equity_safe()

    return {
        "trades": trades,
        "win_rate": win_rate,
        "total_r": total_r,
        "equity": equity,
    }


def _to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _current_equity_safe():
    """Ask Alpaca for equity, but never crash the summary if it fails."""
    try:
        import broker
        return broker.Broker().equity()
    except Exception:  # noqa: BLE001
        return 0.0
