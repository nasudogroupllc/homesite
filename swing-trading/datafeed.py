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
        self.api_key = config.marketdata_api_key()
        self.cfg = config.CONFIG
        self.notifier = notifier
        self._fallback_warned = False
        self._theta_alive = None  # None = untested, True/False after first check

    def _headers(self):
        """Auth header for the hosted endpoint. A local ThetaData Terminal
        needs no token, so we only add it when one is configured."""
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    # ---------------------------------------------------------------- ThetaData
    def _theta_request(self, path, params):
        """One market-data GET with retries and backoff. Returns JSON or None."""
        url = f"{self.base}{path}"
        retries = int(self.cfg.get("thetadata_max_retries", 4))
        timeout = int(self.cfg.get("thetadata_timeout", 15))
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, headers=self._headers(),
                                    timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    # Bad/missing token - retrying won't help; alert clearly.
                    log.error("Market data auth failed (HTTP %s) on %s. Check "
                              "MARKETDATA_API_KEY in your .env.",
                              resp.status_code, params)
                    return None
                if resp.status_code == 429:
                    # Rate limited - wait longer each time.
                    wait = 2 ** attempt
                    log.warning("Market data rate limit (429) on %s; waiting %ss", params, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code == 472:
                    # ThetaData "no data" for this symbol/range - not retryable.
                    log.warning("Market data: no data for %s", params)
                    return None
                log.warning(
                    "Market data HTTP %s on %s: %s",
                    resp.status_code, params, resp.text[:200],
                )
            except requests.RequestException as e:
                log.warning("Market data request error (attempt %s) for %s: %s",
                            attempt, params, e)
            time.sleep(min(2 ** attempt, 16))
        log.error("Market data failed after %s attempts for %s", retries, params)
        return None

    def theta_reachable(self):
        """Quick check: is the market-data endpoint answering at all?
        Any HTTP reply (even 401/403/404) means the host is up; only a network
        failure counts as unreachable so we fall back to Alpaca."""
        if self._theta_alive is not None:
            return self._theta_alive
        for probe in (f"{self.base}/v2/system/mdds/status", self.base):
            try:
                resp = requests.get(probe, headers=self._headers(), timeout=8)
                self._theta_alive = True
                if resp.status_code in (401, 403):
                    log.error("Market data endpoint is up but rejected the token "
                              "(HTTP %s). Check MARKETDATA_API_KEY.", resp.status_code)
                return True
            except requests.RequestException:
                continue
        self._theta_alive = False
        return False

    def _eod_params(self, symbol, days):
        """Build the request path and query params for the EOD endpoint.
        Defaults to ThetaData API v3; the path can be overridden in config.yaml
        (marketdata_eod_path) in case the endpoint changes again."""
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.8) + 400)  # calendar buffer
        path = self.cfg.get("marketdata_eod_path", "/v3/stock/history/eod")
        params = {
            "symbol": symbol,          # v3 renamed 'root' -> 'symbol'
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        }
        return path, params

    def _theta_daily(self, symbol, days):
        """Fetch daily OHLCV from the market-data EOD endpoint (v3)."""
        path, params = self._eod_params(symbol, days)
        data = self._theta_request(path, params)
        if not data:
            return None
        return self._parse_theta(data)

    @staticmethod
    def _parse_theta(data):
        """Turn the EOD JSON into a DataFrame. Handles two response shapes:
          (A) column format: {"header":{"format":[...]}, "response":[[...],...]}
          (B) list of objects: {"response":[{...},...]} or a bare [ {...}, ... ]
        and tolerates v2 (integer YYYYMMDD) or v3 (ISO string) dates."""
        try:
            recs = []

            # ---- Shape B: list of objects ----
            rows_obj = None
            if isinstance(data, list):
                rows_obj = data
            elif isinstance(data, dict) and isinstance(data.get("response"), list) \
                    and data["response"] and isinstance(data["response"][0], dict):
                rows_obj = data["response"]

            if rows_obj is not None:
                for r in rows_obj:
                    d = DataFeed._pick(r, "date", "datetime", "session", "created", "last_trade")
                    recs.append({
                        "date": DataFeed._to_date(d),
                        "open": float(DataFeed._pick(r, "open")),
                        "high": float(DataFeed._pick(r, "high")),
                        "low": float(DataFeed._pick(r, "low")),
                        "close": float(DataFeed._pick(r, "close")),
                        "volume": float(DataFeed._pick(r, "volume", "size", "count") or 0),
                    })
            else:
                # ---- Shape A: header format + array rows ----
                header = data.get("header", {}) if isinstance(data, dict) else {}
                fmt = (header.get("format") if isinstance(header, dict) else None) \
                    or (data.get("format") if isinstance(data, dict) else None)
                rows = data.get("response", []) if isinstance(data, dict) else []
                if not fmt or not rows:
                    return None
                idx = {name: i for i, name in enumerate(fmt)}
                needed = ("open", "high", "low", "close", "volume", "date")
                if not all(n in idx for n in needed):
                    return None
                for r in rows:
                    recs.append({
                        "date": DataFeed._to_date(r[idx["date"]]),
                        "open": float(r[idx["open"]]),
                        "high": float(r[idx["high"]]),
                        "low": float(r[idx["low"]]),
                        "close": float(r[idx["close"]]),
                        "volume": float(r[idx["volume"]]),
                    })

            df = pd.DataFrame(recs).dropna(subset=["date"])
            if df.empty:
                return None
            df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
            df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as e:  # noqa: BLE001
            log.error("Failed to parse market-data response: %s", e)
            return None

    @staticmethod
    def _pick(row, *names):
        """Return the first present key from a dict row (case-insensitive)."""
        lower = {str(k).lower(): v for k, v in row.items()}
        for n in names:
            if n in row:
                return row[n]
            if n.lower() in lower:
                return lower[n.lower()]
        return None

    @staticmethod
    def _to_date(value):
        """Parse a date that may be an int/str YYYYMMDD or an ISO datetime string."""
        if value is None:
            return None
        s = str(value).strip()
        # Pure YYYYMMDD (v2 style)?
        if s.isdigit() and len(s) == 8:
            return pd.to_datetime(s, format="%Y%m%d")
        # Otherwise let pandas handle ISO strings like 2024-01-02T00:00:00.000
        try:
            return pd.to_datetime(s).tz_localize(None)
        except (ValueError, TypeError):
            try:
                return pd.to_datetime(s)
            except Exception:  # noqa: BLE001
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


if __name__ == "__main__":
    # Connectivity test:  python datafeed.py test AAPL
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    symbol = sys.argv[2].upper() if len(sys.argv) > 2 else "AAPL"

    feed = DataFeed()
    print(f"Market-data endpoint: {feed.base}")
    print(f"Bearer token set:     {'yes' if feed.api_key else 'NO'}")
    print(f"Endpoint reachable:   {'yes' if feed.theta_reachable() else 'NO (network)'}")
    print(f"\nFetching daily bars for {symbol} ...")
    df = feed._theta_daily(symbol, int(feed.cfg.get("history_days", 300)))
    if df is not None and len(df):
        print(f"OK - got {len(df)} daily bars from the market-data endpoint. Most recent 3:")
        print(df.tail(3).to_string())
        sys.exit(0)

    # Parsing failed or no data: show the RAW response so we can see the shape.
    print("\nCould not parse bars from the market-data endpoint. Raw response below")
    print("(copy this and send it back so the parser can be matched exactly):")
    print("-" * 60)
    import json
    path, params = feed._eod_params(symbol, 60)
    try:
        resp = requests.get(f"{feed.base}{path}", params=params,
                            headers=feed._headers(), timeout=20)
        print(f"GET {feed.base}{path}?symbol={symbol}&...")
        print(f"HTTP {resp.status_code}")
        body = resp.text
        try:
            parsed = resp.json()
            print(json.dumps(parsed, indent=2)[:1500])
        except ValueError:
            print(body[:1500])
    except Exception as e:  # noqa: BLE001
        print(f"Raw request also failed: {e}")
    print("-" * 60)
    print("\n(If the raw response above looks like valid price data, send it to me. "
          "Otherwise check MARKETDATA_API_KEY. The system will use Alpaca data as a "
          "fallback in the meantime.)")
    sys.exit(1)
