"""
Price / fundamentals providers.

A 13F reports value at QUARTER-END. To get *current* weights and the implied P&L
since the filing, we need live prices keyed to a ticker — which is exactly why the
OpenFIGI CUSIP->ticker step had to come first.

Providers are pluggable behind a small interface so you can swap data sources:
  - StooqProvider     — free, no key, works out of the box. The sane default.
  - MassiveProvider   — Massive Market Data (Polygon-shaped REST); your preferred
                        source. Gives market cap + shares outstanding too. Needs a key.
  - YahooChartProvider — no-key fallback for research validation price history.

Historical daily closes are immutable, so they're memoized per run; add a disk cache
later if you value large books frequently.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import requests


@dataclass
class Fundamentals:
    ticker: str
    market_cap: Optional[float]
    shares_outstanding: Optional[float]


def _us_trading_window(d: date, lookback: int) -> tuple[date, date]:
    return (d - timedelta(days=lookback), d)


class PriceProvider:
    """Base: implement daily_closes() (and optionally fundamentals())."""

    def __init__(self):
        self._memo: dict[tuple, dict[date, float]] = {}

    def daily_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        raise NotImplementedError

    def fundamentals(self, ticker: str) -> Optional[Fundamentals]:
        return None

    # -- convenience built on daily_closes --------------------------------
    def _cached_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        key = (self.__class__.__name__, ticker.upper(), start, end)
        if key not in self._memo:
            self._memo[key] = self.daily_closes(ticker, start, end)
        return self._memo[key]

    def close_on_or_before(self, ticker: str, d: date, lookback: int = 7) -> Optional[tuple[date, float]]:
        """Quarter-ends fall on weekends/holidays; take the last close <= d."""
        start, end = _us_trading_window(d, lookback)
        closes = self._cached_closes(ticker, start, end)
        eligible = [dt for dt in closes if dt <= d]
        if not eligible:
            return None
        best = max(eligible)
        return best, closes[best]

    def latest_close(self, ticker: str, lookback: int = 10) -> Optional[tuple[date, float]]:
        today = datetime.now(timezone.utc).date()
        start, end = _us_trading_window(today, lookback)
        closes = self._cached_closes(ticker, start, end)
        if not closes:
            return None
        d = max(closes)
        return d, closes[d]


# ---------------------------------------------------------------------------
class StooqProvider(PriceProvider):
    """Free CSV endpoints, no key. US tickers map to '<sym>.us' (dots/slashes -> dashes)."""

    BASE = "https://stooq.com/q/d/l/"

    def __init__(self, session: Optional[requests.Session] = None, suffix: str = ".us"):
        super().__init__()
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "13flow-validation/1.0")
        self._suffix = suffix

    def _symbol(self, ticker: str) -> str:
        return ticker.lower().replace(".", "-").replace("/", "-") + self._suffix

    def daily_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        params = {
            "s": self._symbol(ticker),
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
        resp = self._session.get(self.BASE, params=params, timeout=30)
        resp.raise_for_status()
        return self._parse_csv(resp.text)

    @staticmethod
    def _parse_csv(text: str) -> dict[date, float]:
        out: dict[date, float] = {}
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "Close" not in reader.fieldnames:
            return out  # 'No data' / rate-limited / unknown symbol
        for row in reader:
            try:
                out[date.fromisoformat(row["Date"])] = float(row["Close"])
            except (ValueError, KeyError, TypeError):
                continue
        return out


# ---------------------------------------------------------------------------
class YahooChartProvider(PriceProvider):
    """
    No-key Yahoo chart adapter for research validation exports.

    This is a fallback when the primary vendor account cannot serve enough history.
    It should be disclosed as a research data source, not as the institutional-grade
    production price feed.
    """

    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, session: Optional[requests.Session] = None):
        super().__init__()
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "13flow-validation/1.0")

    @staticmethod
    def _symbol(ticker: str) -> str:
        return ticker.upper().replace("/", "-").replace(".", "-")

    @staticmethod
    def _epoch(d: date) -> int:
        return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp())

    def daily_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        params = {
            "period1": str(self._epoch(start)),
            # Yahoo's period2 is exclusive; include the requested end date.
            "period2": str(self._epoch(end + timedelta(days=1))),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
        resp = self._session.get(self.BASE + self._symbol(ticker),
                                 params=params, timeout=30)
        resp.raise_for_status()
        return self._parse_chart(resp.json())

    @staticmethod
    def _parse_chart(data: dict) -> dict[date, float]:
        chart = data.get("chart") or {}
        if chart.get("error"):
            raise ValueError(str(chart["error"]))
        results = chart.get("result") or []
        if not results:
            return {}
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        adj = ((indicators.get("adjclose") or [{}])[0].get("adjclose") or [])
        close = ((indicators.get("quote") or [{}])[0].get("close") or [])
        values = adj if adj else close
        out: dict[date, float] = {}
        for ts, px in zip(timestamps, values):
            if ts is None or px is None:
                continue
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            out[d] = float(px)
        return out


# ---------------------------------------------------------------------------
class MassiveProvider(PriceProvider):
    """
    Massive Market Data adapter (Polygon-shaped REST, JSON).
      - daily closes: GET /v2/aggs/ticker/{T}/range/1/day/{start}/{end}?adjusted=true
      - fundamentals: GET /v3/reference/tickers/{T}  (market_cap, *_shares_outstanding)

    Confirm your exact base URL + auth from your Massive account; defaults below follow
    the documented endpoints. Auth is sent Polygon-style as an apiKey query param.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        if not api_key:
            raise ValueError("MassiveProvider needs an API key.")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        # Send the key as a header, not a query param: query strings leak into access
        # logs, proxies, and Referer headers. (Polygon-shaped APIs accept Bearer auth.)
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._session.get(self._base + path, params=dict(params or {}), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def daily_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        path = (f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/"
                f"{start.isoformat()}/{end.isoformat()}")
        data = self._get(path, {"adjusted": "true", "limit": 5000})
        out: dict[date, float] = {}
        for r in data.get("results") or []:
            ts = r.get("t")
            c = r.get("c")
            if ts is None or c is None:
                continue
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            out[d] = float(c)
        return out

    def fundamentals(self, ticker: str) -> Optional[Fundamentals]:
        data = self._get(f"/v3/reference/tickers/{ticker.upper()}")
        res = data.get("results") or {}
        if not res:
            return None
        shares = res.get("weighted_shares_outstanding") or res.get("share_class_shares_outstanding")
        return Fundamentals(
            ticker=ticker.upper(),
            market_cap=res.get("market_cap"),
            shares_outstanding=shares,
        )
