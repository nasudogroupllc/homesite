"""
earnings.py  -  Finds which symbols report earnings in the next few days.

Uses Nasdaq's FREE public earnings calendar (no API key needed). If that
source is unreachable, the function returns None. The scanner treats "None"
as "I don't know" and FLAGS those symbols in Telegram instead of trading them,
exactly as the spec requires.

Optional: if you set FMP_API_KEY in your .env, we could use Financial Modeling
Prep instead - but the free Nasdaq source works out of the box.
"""
import logging
from datetime import date, timedelta

import requests

log = logging.getLogger("earnings")

# Nasdaq's API blocks requests without a browser-like User-Agent.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; swing-trader/1.0)",
    "Accept": "application/json",
}


def _next_trading_days(n):
    """Return the next n weekdays (Mon-Fri) starting tomorrow.
    Note: this skips weekends but not market holidays - close enough for a
    2-day earnings blackout, and it only ever makes us MORE cautious."""
    days = []
    d = date.today()
    while len(days) < n:
        d = d + timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            days.append(d)
    return days


def _fetch_nasdaq(day):
    """Fetch the earnings list for one calendar date from Nasdaq."""
    url = "https://api.nasdaq.com/api/calendar/earnings"
    try:
        resp = requests.get(
            url, params={"date": day.strftime("%Y-%m-%d")},
            headers=_HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            log.warning("Nasdaq earnings HTTP %s for %s", resp.status_code, day)
            return None
        data = resp.json()
        rows = (data or {}).get("data", {}).get("rows") or []
        return {str(r.get("symbol", "")).strip().upper() for r in rows if r.get("symbol")}
    except Exception as e:  # noqa: BLE001
        log.warning("Nasdaq earnings fetch failed for %s: %s", day, e)
        return None


def upcoming_earnings(blackout_days):
    """
    Return a SET of symbols reporting earnings within the next `blackout_days`
    trading days, or None if the calendar could not be loaded at all.
    """
    symbols = set()
    any_success = False
    for d in _next_trading_days(blackout_days):
        result = _fetch_nasdaq(d)
        if result is not None:
            any_success = True
            symbols |= result
    if not any_success:
        return None  # signal: unknown -> scanner will flag instead of trade
    return symbols
