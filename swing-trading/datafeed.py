"""
datafeed.py  -  Gets daily price history for a symbol.

Primary source:  ThetaData Terminal REST API (running locally on port 25510).
Fallback source: Alpaca's market-data API (used only if ThetaData is down).

If we ever fall back to Alpaca, a one-time Telegram warning is sent so you know
the data came from the backup source.

The public function you call is:  get_daily_bars(symbol, days)
It returns a pandas DataFrame (columns: open, high, low, close, volume;
oldest row first) or None if both sources fail.
"""
import time
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests

import config

log = logging.getLogger("datafeed")


class DataFeed:
    def __init__(self, notifier=None):
        self.base = config.thetadata_base_url().rstrip("/")
        self.cfg = config.CONFIG
        self.notifier = notifier
        self._fallback_warned = False
        self._theta_alive = None  # None = untested, True/False after first check

    # ---------------------------------------------------------------- ThetaData
    def _theta_request(self, path, params):
        """One ThetaData GET with retries and backoff. Returns JSON or None."""
        url = f"{self.base}{path}"
        retries = int(self.cfg.get("thetadata_max_retries", 4))
        timeout = int(self.cfg.get("thetadata_timeout", 15))
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    # Rate limited - wait longer each time.
                    wait = 2 ** attempt
                    log.warning("ThetaData rate limit (429) on %s; waiting %ss", params, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code == 472:
                    # ThetaData "no data" for this symbol/range - not retryable.
                    log.warning("ThetaData: no data for %s", params)
                    return None
                log.warning(
                    "ThetaData HTTP %s on %s: %s",
                    resp.status_code, params, resp.text[:200],
                )
            except requests.RequestException as e:
                log.warning("ThetaData request error (attempt %s) for %s: %s",
                            attempt, params, e)
            time.sleep(min(2 ** attempt, 16))
        log.error("ThetaData failed after %s attempts for %s", retries, params)
        return None

    def theta_reachable(self):
        """Quick check: is the ThetaData Terminal answering at all?"""
        if self._theta_alive is not None:
            return self._theta_alive
        try:
            # A lightweight status-ish call; any HTTP answer means it's alive.
            resp = requests.get(f"{self.base}/v2/system/mdds/status", timeout=5)
            self._theta_alive = resp.status_code < 500
        except requests.RequestException:
            try:
                resp = requests.get(self.base, timeout=5)
                self._theta_alive = True
            except requests.RequestException:
                self._theta_alive = False
        return self._theta_alive

    def _theta_daily(self, symbol, days):
        """Fetch daily OHLCV from ThetaData's stock EOD endpoint."""
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.8) + 400)  # calendar buffer
        params = {
            "root": symbol.replace(".", "/"),  # e.g. BRK.B -> BRK/B for ThetaData
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        }
        data = self._theta_request("/v2/hist/stock/eod", params)
        if not data:
            return None
        return self._parse_theta(data)

    @staticmethod
    def _parse_theta(data):
        """Turn ThetaData's JSON (header format + rows) into a DataFrame."""
        try:
            header = data.get("header", {})
            fmt = header.get("format") or data.get("format")
            rows = data.get("response", [])
            if not fmt or not rows:
                return None
            idx = {name: i for i, name in enumerate(fmt)}
            needed = ("open", "high", "low", "close", "volume", "date")
            if not all(n in idx for n in needed):
                return None
            recs = []
            for r in rows:
                recs.append({
                    "date": pd.to_datetime(str(int(r[idx["date"]])), format="%Y%m%d"),
                    "open": float(r[idx["open"]]),
                    "high": float(r[idx["high"]]),
                    "low": float(r[idx["low"]]),
                    "close": float(r[idx["close"]]),
                    "volume": float(r[idx["volume"]]),
                })
            df = pd.DataFrame(recs)
            if df.empty:
                return None
            df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
            df = df.sort_values("date").set_index("date")
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as e:  # noqa: BLE001
            log.error("Failed to parse ThetaData response: %s", e)
            return None

    # ------------------------------------------------------------------- Alpaca
    def _alpaca_daily(self, symbol, days):
        """Fallback: fetch daily bars from Alpaca's market-data API."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
        except ImportError:
            log.error("alpaca-py not installed; cannot use fallback data source.")
            return None

        key, secret, _ = config.alpaca_keys()
        client = StockHistoricalDataClient(key, secret)
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.8) + 400)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        try:
            bars = client.get_stock_bars(req)
        except Exception as e:  # noqa: BLE001
            log.error("Alpaca data error for %s: %s", symbol, e)
            return None
        df = bars.df
        if df is None or df.empty:
            return None
        # Alpaca returns a multi-index (symbol, timestamp); flatten it.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df.rename(columns={
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume",
        })
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    def _warn_fallback(self):
        if not self._fallback_warned:
            self._fallback_warned = True
            msg = "ThetaData was unreachable - using Alpaca market data as a fallback."
            log.warning(msg)
            if self.notifier:
                self.notifier.send(msg, kind="warning")

    # -------------------------------------------------------------------- public
    def get_daily_bars(self, symbol, days=None):
        """Get daily bars, trying ThetaData first, then Alpaca."""
        days = days or int(self.cfg.get("history_days", 300))

        if self.theta_reachable():
            df = self._theta_daily(symbol, days)
            if df is not None and len(df) >= 210:
                return df.tail(days + 60)
            # ThetaData is up but returned nothing usable for this symbol;
            # try Alpaca for just this symbol without a global fallback warning.

        # Fallback path.
        self._warn_fallback()
        df = self._alpaca_daily(symbol, days)
        if df is not None and len(df) >= 210:
            return df.tail(days + 60)
        return None
