"""
Offline test for the long-tail resolver. No network.

Exercises the full chain on a portfolio where:
  - AAPL resolves via (fake) OpenFIGI
  - an Apple share-class CUSIP (same 037833 prefix) resolves via CUSIP-prefix
  - 'BERKSHIRE HATHAWAY' resolves via SEC name match
  - a true unknown stays unresolved (but keeps its issuer name)
Plus: confidence/provenance, retryable negative cache, coverage(), and Store.apply_resolution.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from smartmoney.figi import FigiMatch
from smartmoney.resolver import (CusipResolver, ResolutionCache, Resolution,
                                 resolve_portfolio, coverage, build_sec_index, normalize_name)
from smartmoney.db import Store
from smartmoney.edgar import Filing
from tests.test_offline import _table

AAPL = "037833100"
AAPL_CLASS = "037833200"     # same issuer prefix 037833 -> prefix resolver
BRK = "084670108"            # Berkshire; resolve via SEC name match
UNKNOWN = "999999999"


class FakeFigi:
    """Only knows AAPL; everything else is a miss (the tail)."""
    def map_cusips(self, cusips, cache=None):
        return {c: (FigiMatch(c, "AAPL", "APPLE INC", "BBG", "US", "Common Stock", "Equity")
                    if c == AAPL else None) for c in cusips}


def _resolver(cache=None):
    sec = build_sec_index({"0": {"cik_str": 1067983, "ticker": "BRK-B",
                                 "title": "BERKSHIRE HATHAWAY INC"}})
    return CusipResolver(openfigi=FakeFigi(), sec_index=sec, cache=cache)


def test_chain_sources_and_confidence():
    res = _resolver().resolve([
        (AAPL, "APPLE INC"),
        (AAPL_CLASS, "APPLE INC"),              # same name -> high-confidence prefix match
        (BRK, "BERKSHIRE HATHAWAY INC"),
        (UNKNOWN, "WEIRD PRIVATE NOTE 2029"),
    ])
    assert res[AAPL].ticker == "AAPL" and res[AAPL].source == "openfigi"
    assert res[AAPL_CLASS].ticker == "AAPL" and res[AAPL_CLASS].source == "cusip_prefix"
    assert res[AAPL_CLASS].confidence >= 0.85          # issuer names match -> high
    assert res[BRK].ticker == "BRK-B" and res[BRK].source == "sec_name"
    assert res[UNKNOWN].ticker is None and res[UNKNOWN].source == "none"
    assert res[UNKNOWN].name == "WEIRD PRIVATE NOTE 2029"   # issuer name retained


def test_negative_cache_is_retryable():
    with tempfile.TemporaryDirectory() as d:
        cache = ResolutionCache(Path(d) / "rc.json", negative_ttl_days=30)
        now = datetime.now(timezone.utc)
        # Fresh miss -> don't requery yet.
        cache.put(Resolution(UNKNOWN, None, "X", None, "none", 0.0, now.isoformat(timespec="seconds")))
        assert cache.needs_query(UNKNOWN, now) is False
        # Stale miss -> requery.
        old = (now - timedelta(days=40)).isoformat(timespec="seconds")
        cache.put(Resolution(UNKNOWN, None, "X", None, "none", 0.0, old))
        assert cache.needs_query(UNKNOWN, now) is True
        # Solid hit -> never requery.
        cache.put(Resolution(AAPL, "AAPL", "APPLE INC", "B", "openfigi", 0.95, old))
        assert cache.needs_query(AAPL, now) is False


def test_coverage_and_db_sweep():
    pf = build_portfolio("0001067983", "Berkshire Hathaway", "2024-06-30", "13F-HR",
        parse_info_table(_table([
            ("APPLE INC", AAPL, 800000, 1000, ""),
            ("APPLE INC CLASS X", AAPL_CLASS, 100000, 100, ""),
            ("WEIRD NOTE", UNKNOWN, 50000, 10, ""),
        ])))
    resolve_portfolio(pf, _resolver())
    cov = coverage(pf)
    assert cov["n_total"] == 3 and cov["n_resolved"] == 2
    assert abs(cov["value_share"] - (900000 / 950000)) < 1e-9     # only the note is unresolved
    assert cov["tail"][0]["cusip"] == UNKNOWN
    assert "openfigi" in cov["by_source_value"] and "cusip_prefix" in cov["by_source_value"]

    # Persist a partially-resolved portfolio, then sweep the tail in the DB.
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        # Save a version where nothing is resolved, to exercise the sweep end to end.
        raw = build_portfolio("0001067983", "Berkshire Hathaway", "2024-06-30", "13F-HR",
            parse_info_table(_table([("WEIRD NOTE", UNKNOWN, 50000, 10, "")])))
        store.save_portfolio(raw, Filing(cik="0001067983", accession="a1", form="13F-HR",
                             filing_date="2024-08-14", report_date="2024-06-30", primary_doc="p"))
        assert store.coverage()["overall_value_share"] == 0.0
        tail = store.unresolved_holdings()
        assert tail and tail[0]["cusip"] == UNKNOWN
        # Manually resolve and back-fill.
        n = store.apply_resolution(UNKNOWN, "ZZZZ", "Weird Note Co", "manual", 1.0)
        assert n == 1
        assert store.coverage()["overall_value_share"] == 1.0


def test_normalize_name():
    assert normalize_name("Apple Inc.") == normalize_name("APPLE INCORPORATED") == "APPLE"
    assert normalize_name("Berkshire Hathaway Inc. Class B") == "BERKSHIRE HATHAWAY"


if __name__ == "__main__":
    test_chain_sources_and_confidence()
    test_negative_cache_is_retryable()
    test_coverage_and_db_sweep()
    test_normalize_name()
    print("All resolver offline tests passed.")
