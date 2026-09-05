"""
market.py  -  Small helper to answer "is today a trading day?"

Cron already restricts jobs to Monday-Friday, but this also skips market
holidays. It asks Alpaca's calendar; if Alpaca can't be reached it falls back
to a simple weekday check (so the system keeps working even if the check fails).
"""
import logging
from datetime import date

log = logging.getLogger("market")


def is_trading_day(today=None):
    today = today or date.today()
    # Weekend? Never a trading day.
    if today.weekday() >= 5:
        return False
    try:
        import config
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest

        key, secret, base = config.alpaca_keys()
        client = TradingClient(key, secret, paper="paper" in base.lower())
        cal = client.get_calendar(GetCalendarRequest(start=today, end=today))
        return len(cal) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("Could not check market calendar (%s); assuming weekday=open.", e)
        return True  # already known to be a weekday


def trading_days_between(start_date, end_date):
    """Number of trading (market) sessions AFTER start_date up to and including
    end_date. Used for the time stop. Falls back to a business-day count
    (weekdays, ignoring holidays) if the market calendar can't be reached."""
    if end_date <= start_date:
        return 0
    try:
        import config
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest

        key, secret, base = config.alpaca_keys()
        client = TradingClient(key, secret, paper="paper" in base.lower())
        cal = client.get_calendar(GetCalendarRequest(start=start_date, end=end_date))
        # sessions include the start day if it was a trading day; days held after
        # entry is the number of sessions minus that entry session.
        return max(0, len(cal) - 1)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not count trading days (%s); using business-day estimate.", e)
        import numpy as np
        return int(np.busday_count(start_date, end_date))
