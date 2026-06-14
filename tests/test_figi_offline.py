"""
Offline test for the OpenFIGI enrichment — no network.

A fake session records POST bodies and returns canned v3 responses so we can assert:
  - batching respects batch_size
  - v3 'warning' (no match) is treated as None, not crashed on
  - misses retry once WITHOUT exchCode and can resolve on the second pass
  - the disk cache prevents re-querying and persists negatives
  - enrich_portfolio attaches tickers to positions
"""

import json
import tempfile
from pathlib import Path

from smartmoney.figi import OpenFigiClient, TickerCache, enrich_portfolio
from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from tests.test_offline import _table  # reuse the synthetic 13F builder


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Maps CUSIP -> ticker, but ONLY answers when exchCode is absent for 'TRICKY'
    so we can exercise the no-exchCode fallback path. Records every request."""

    KNOWN = {
        "037833100": "AAPL",
        "191216100": "KO",
        "67066G104": "NVDA",
    }
    TRICKY = {"88160R101": "TSLA"}  # only resolves when exchCode omitted

    def __init__(self):
        self.headers = {}
        self.calls: list[list[dict]] = []

    def post(self, url, data=None, timeout=None):
        jobs = json.loads(data)
        self.calls.append(jobs)
        out = []
        for job in jobs:
            cusip = job["idValue"]
            has_exch = "exchCode" in job
            ticker = None
            if cusip in self.KNOWN:
                ticker = self.KNOWN[cusip]
            elif cusip in self.TRICKY and not has_exch:
                ticker = self.TRICKY[cusip]
            if ticker:
                out.append({"data": [{
                    "ticker": ticker, "name": f"{ticker} INC", "compositeFIGI": f"FIGI{ticker}",
                    "exchCode": "US", "securityType": "Common Stock", "marketSector": "Equity",
                }]})
            else:
                out.append({"warning": "No identifier found."})
        return _Resp(out)


def test_batching_and_warning_and_fallback():
    sess = FakeSession()
    client = OpenFigiClient(session=sess, batch_size=2, requests_per_min=100000)
    cusips = ["037833100", "191216100", "67066G104", "88160R101", "000000000"]
    res = client.map_cusips(cusips)

    assert res["037833100"].ticker == "AAPL"
    assert res["67066G104"].ticker == "NVDA"
    assert res["88160R101"].ticker == "TSLA"   # resolved on the no-exch fallback pass
    assert res["000000000"] is None            # genuine miss stays None

    # batch_size=2 over 5 unique CUSIPs => 3 first-pass batches.
    first_pass_batches = [c for c in sess.calls if any("exchCode" in j for j in c)]
    assert len(first_pass_batches) == 3
    # fallback pass ran on the misses (TSLA + the bad cusip), without exchCode.
    fallback_batches = [c for c in sess.calls if all("exchCode" not in j for j in c)]
    assert fallback_batches, "expected a no-exchCode fallback pass"


def test_cache_round_trip_and_negative_caching():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cache.json"
        sess = FakeSession()
        client = OpenFigiClient(session=sess, batch_size=10, requests_per_min=100000)
        cache = TickerCache(path)

        client.map_cusips(["037833100", "000000000"], cache=cache)
        calls_after_first = len(sess.calls)
        assert "037833100" in cache and "000000000" in cache  # negative cached too

        # Second run hits cache only -> no new POSTs.
        res2 = client.map_cusips(["037833100", "000000000"], cache=cache)
        assert len(sess.calls) == calls_after_first
        assert res2["037833100"].ticker == "AAPL"
        assert res2["000000000"] is None

        # Persisted to disk.
        reloaded = TickerCache(path)
        assert reloaded.get("037833100").ticker == "AAPL"


def test_enrich_portfolio():
    sess = FakeSession()
    client = OpenFigiClient(session=sess, batch_size=10, requests_per_min=100000)
    pf = build_portfolio("0", "T", "2024-12-31", "13F-HR", parse_info_table(_table([
        ("APPLE INC", "037833100", 1000, 100, ""),
        ("NVIDIA", "67066G104", 500, 50, ""),
    ])))
    enrich_portfolio(pf, client)
    assert pf.positions[("037833100", "")].ticker == "AAPL"
    assert pf.positions[("67066G104", "")].ticker == "NVDA"


def test_429_retry():
    class Throttled(FakeSession):
        def __init__(self):
            super().__init__()
            self._first = True

        def post(self, url, data=None, timeout=None):
            if self._first:
                self._first = False
                return _Resp([], status=429, headers={"Retry-After": "0"})
            return super().post(url, data=data, timeout=timeout)

    sess = Throttled()
    client = OpenFigiClient(session=sess, batch_size=10, requests_per_min=100000)
    res = client.map_cusips(["037833100"])
    assert res["037833100"].ticker == "AAPL"


if __name__ == "__main__":
    test_batching_and_warning_and_fallback()
    test_cache_round_trip_and_negative_caching()
    test_enrich_portfolio()
    test_429_retry()
    print("All OpenFIGI offline tests passed.")
