"""
CUSIP -> ticker enrichment via the OpenFIGI v3 mapping API.

Why this is the load-bearing module of the whole product: 13F filings carry only
CUSIPs (CUSIP is a licensed identifier with no free official lookup), so without a
mapping layer you have portfolios you can't price, chart, or link to a ticker.
OpenFIGI (run by Bloomberg) maps CUSIP -> FIGI -> ticker for free.

Key facts baked into the defaults below (verified against openfigi.com/api/documentation):
  - POST https://api.openfigi.com/v3/mapping
  - body is an ARRAY of jobs; response is an array aligned by index
  - batch size: 100 jobs/request WITH an API key, 5 WITHOUT
  - a no-match job returns a "warning" key in v3 (it was "error" in v2 — easy to miss)
  - 429 when you exceed the rate window; we honor Retry-After and back off

Get a free key at openfigi.com to lift the rate limits, then:
    export OPENFIGI_APIKEY="..."

Because CUSIPs are effectively immutable, results are cached to disk; in steady
state you only ever pay for newly-appearing CUSIPs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

MAPPING_URL = "https://api.openfigi.com/v3/mapping"


@dataclass
class FigiMatch:
    cusip: str
    ticker: Optional[str]
    name: Optional[str]
    figi: Optional[str]
    exch_code: Optional[str]
    security_type: Optional[str]
    market_sector: Optional[str]


# ---------------------------------------------------------------------------
# Disk cache (CUSIP -> match or explicit miss). Negative results are cached too,
# so we don't re-hammer the API for CUSIPs OpenFIGI genuinely can't resolve.
# ---------------------------------------------------------------------------
class TickerCache:
    def __init__(self, path: str | os.PathLike | None = None):
        if path is None:
            path = Path(os.environ.get("SMARTMONEY_CACHE_DIR") or ".") / ".smartmoney_cusip_cache.json"
        self.path = Path(path)
        self._data: dict[str, Optional[dict]] = {}
        self._dirty = False
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def __contains__(self, cusip: str) -> bool:
        return cusip.upper() in self._data

    def get(self, cusip: str) -> Optional[FigiMatch]:
        rec = self._data.get(cusip.upper())
        return FigiMatch(**rec) if rec else None

    def put(self, cusip: str, match: Optional[FigiMatch]) -> None:
        self._data[cusip.upper()] = asdict(match) if match else None
        self._dirty = True

    def save(self) -> None:
        if self._dirty:
            self.path.write_text(json.dumps(self._data, indent=0))
            self._dirty = False


# ---------------------------------------------------------------------------
# Rate limiter: sliding 60s window on REQUEST count. Defaults differ by whether
# you have a key (keyed: 250 req/min x 100 jobs = 25k jobs/min, the documented
# ceiling; unkeyed: deliberately conservative).
# ---------------------------------------------------------------------------
class _MinuteWindow:
    def __init__(self, max_per_min: int):
        self.max = max_per_min
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) >= self.max:
                sleep_for = 60.0 - (now - self._times[0]) + 0.01
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
            self._times.append(time.monotonic())


class OpenFigiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        batch_size: Optional[int] = None,
        requests_per_min: Optional[int] = None,
        exch_code: str = "US",
        timeout: int = 30,
        max_retries: int = 4,
    ):
        self.api_key = api_key or os.environ.get("OPENFIGI_APIKEY")
        self._session = session or requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if self.api_key:
            self._session.headers["X-OPENFIGI-APIKEY"] = self.api_key

        # Defaults follow OpenFIGI's keyed/unkeyed tiers.
        self.batch_size = batch_size or (100 if self.api_key else 5)
        self._limiter = _MinuteWindow(requests_per_min or (250 if self.api_key else 20))
        self.exch_code = exch_code
        self._timeout = timeout
        self._max_retries = max_retries

    # --- single batch POST with 429 backoff -------------------------------
    def _post(self, jobs: list[dict]) -> list[dict]:
        attempt = 0
        while True:
            self._limiter.wait()
            resp = self._session.post(MAPPING_URL, data=json.dumps(jobs), timeout=self._timeout)
            if resp.status_code == 429 and attempt < self._max_retries:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(max(retry_after, 1.0))
                attempt += 1
                continue
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _select(data: list[dict]) -> dict:
        """Pick the most representative match: prefer Equity / Common Stock."""
        if not data:
            return {}
        equities = [d for d in data if (d.get("marketSector") == "Equity")]
        pool = equities or data
        commons = [d for d in pool if "Common" in (d.get("securityType") or "")]
        return (commons or pool)[0]

    def _jobs_for(self, cusips: list[str], with_exch: bool) -> list[dict]:
        out = []
        for c in cusips:
            job = {"idType": "ID_CUSIP", "idValue": c}
            if with_exch and self.exch_code:
                job["exchCode"] = self.exch_code
            out.append(job)
        return out

    def _map_pass(self, cusips: list[str], with_exch: bool) -> dict[str, Optional[FigiMatch]]:
        results: dict[str, Optional[FigiMatch]] = {}
        for i in range(0, len(cusips), self.batch_size):
            chunk = cusips[i : i + self.batch_size]
            jobs = self._jobs_for(chunk, with_exch)
            resp = self._post(jobs)
            for cusip, item in zip(chunk, resp):
                # v3: a miss carries a "warning" key, not "data".
                data = item.get("data") if isinstance(item, dict) else None
                if data:
                    best = self._select(data)
                    results[cusip] = FigiMatch(
                        cusip=cusip,
                        ticker=best.get("ticker"),
                        name=best.get("name"),
                        figi=best.get("compositeFIGI") or best.get("figi"),
                        exch_code=best.get("exchCode"),
                        security_type=best.get("securityType"),
                        market_sector=best.get("marketSector"),
                    )
                else:
                    results[cusip] = None
        return results

    def map_cusips(
        self, cusips: list[str], cache: Optional[TickerCache] = None
    ) -> dict[str, Optional[FigiMatch]]:
        """
        Resolve a list of CUSIPs to FigiMatch (or None). Dedupes, uses the cache,
        and retries cache/exch misses once WITHOUT exchCode (catches names that
        don't carry a US composite under the requested exchange).
        """
        uniq = sorted({c.upper() for c in cusips if c})
        out: dict[str, Optional[FigiMatch]] = {}
        to_query: list[str] = []
        for c in uniq:
            if cache is not None and c in cache:
                out[c] = cache.get(c)
            else:
                to_query.append(c)

        if to_query:
            first = self._map_pass(to_query, with_exch=True)
            misses = [c for c, m in first.items() if m is None]
            second = self._map_pass(misses, with_exch=False) if misses else {}
            for c in to_query:
                match = second.get(c) or first.get(c)
                out[c] = match
                if cache is not None:
                    cache.put(c, match)
            if cache is not None:
                cache.save()
        return out


def enrich_portfolio(pf, client: OpenFigiClient, cache: Optional[TickerCache] = None) -> None:
    """Attach ticker + canonical name to each position in a Portfolio, in place."""
    cusips = [p.cusip for p in pf.positions.values()]
    mapping = client.map_cusips(cusips, cache=cache)
    for p in pf.positions.values():
        m = mapping.get(p.cusip.upper())
        if m:
            p.ticker = m.ticker
            p.figi_name = m.name
