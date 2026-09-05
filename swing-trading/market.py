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
