"""Cache provenance must reflect completed EDGAR work, not a later HTTP read."""
import json
import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask

import run
from smartmoney import api
from smartmoney.api import _StoreConfluence, _sec_issuer_symbol
from smartmoney.api_signals import (
    ConfluenceUnavailable, UnconfiguredConfluenceProvider,
    confluence_cache_metadata, make_signals_blueprint,
)
from smartmoney.crosssignal import InstitutionalSignal
from smartmoney.forms4 import Form4Client, parse_form4
from smartmoney.db import Store
from smartmoney.sample_confluence import sample_signals
from tests.test_db_offline import _save
from tests.test_forms4_offline import CEO_BUY_XML


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("age_hours,status,verified", [(1, "fresh", True), (26, "fresh", True), (27, "stale", False)])
def test_cache_attestation_expires_without_changing_its_calculation_time(age_hours, status, verified):
    generated = (NOW - timedelta(hours=age_hours)).isoformat()
    payload = {"generated_at": generated, "metadata": {"edgar_refresh_verified": True}}
    health = confluence_cache_metadata(payload, now=NOW)
    assert health["cache_status"] == status
    assert health["edgar_refresh_verified"] is verified
    assert health["cache_age_seconds"] == age_hours * 3600
    assert payload["generated_at"] == generated


@pytest.mark.parametrize("generated", [None, "bad date", "2026-10-03T10:00:00", (NOW + timedelta(hours=1)).isoformat()])
def test_undated_invalid_or_future_cache_never_gets_a_freshness_attestation(generated):
    health = confluence_cache_metadata({"generated_at": generated, "metadata": {"edgar_refresh_verified": True}}, now=NOW)
    assert health["cache_status"] == "unknown"
    assert health["cache_age_seconds"] is None
    assert health["edgar_refresh_verified"] is False


def test_legacy_cache_is_served_with_explicit_unknown_age(tmp_path):
    original = {"metadata": {}, "signals": [{"ticker": "FIXTURE", "score": 12}], "kpis": {"n_signals": 1}}
    path = tmp_path / "confluence-90.json"
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    app = Flask(__name__)
    app.register_blueprint(make_signals_blueprint(UnconfiguredConfluenceProvider(), cache_dir=tmp_path))
    payload = app.test_client().get("/api/signals/confluence?window=90").get_json()
    assert payload["signals"] == original["signals"]
    assert "generated_at" not in payload
    assert payload["metadata"]["served_from_cache"] is True
    assert payload["metadata"]["cache_status"] == "unknown"
    assert payload["metadata"]["edgar_refresh_verified"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", [None, "fetch", "mapping"])
def test_live_provider_attests_only_complete_issuer_checks(monkeypatch, failure):
    provider = _StoreConfluence("unused-fixture.db", "13flow-tests test@example.com")
    monkeypatch.setattr(provider, "_institutional", lambda **kwargs: {"FIXTURE": InstitutionalSignal(ticker="FIXTURE", funds_accumulating=3)})
    monkeypatch.setattr(provider, "_issuer_index", lambda: {"OTHER" if failure == "mapping" else "FIXTURE": "0000000001"})

    class FixtureClient:
        def __init__(self, **kwargs):
            pass

        def insider_filings(self, cik, *, window_days, strict):
            assert strict is True
            if failure == "fetch":
                raise RuntimeError("fixture upstream failure")
            return []  # A successful SEC lookup can legitimately find no purchases.

    monkeypatch.setattr("smartmoney.forms4.Form4Client", FixtureClient)
    assert provider.confluence_metadata()["edgar_refresh_verified"] is False
    if failure:
        with pytest.raises(ConfluenceUnavailable):
            provider.confluence(90)
    else:
        provider.confluence(90)
    metadata = provider.confluence_metadata()
    assert metadata["edgar_refresh_verified"] is (not failure)
    if failure:
        assert metadata["generated_at"] is None
    else:
        assert datetime.fromisoformat(metadata["generated_at"]).tzinfo is not None
    assert metadata["edgar_refresh"]["issuer_failures"] == int(failure is not None)
    if failure is None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(provider.confluence_metadata).result()["edgar_refresh_verified"] is False
        assert provider.confluence_metadata()["edgar_refresh_verified"] is True


def test_strict_form4_fetch_reports_a_failed_filing(monkeypatch):
    client = Form4Client(user_agent="13flow-tests test@example.com")
    monkeypatch.setattr(client, "list_form4_accessions", lambda *args, **kwargs: [{"accession": "fixture", "filing_date": "2026-10-01"}])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args: "invalid XML")
    assert client.insider_filings("0000000001") == []
    with pytest.raises(Exception):
        client.insider_filings("0000000001", strict=True)


@pytest.mark.parametrize("fail_second_window", [False, True])
def test_precompute_publishes_complete_windows_atomically_and_preserves_failed_runs(tmp_path, monkeypatch, fail_second_window):
    monkeypatch.setenv("SMARTMONEY_CACHE_DIR", str(tmp_path))
    paths = [tmp_path / f"confluence-{window}.json" for window in (30, 90)]
    for path in paths:
        path.write_text('{"previous":true}')
    calculated = []

    class FixtureProvider:
        def __init__(self, *args, **kwargs):
            self.window = None

        def confluence(self, window):
            calculated.append(window)
            self.window = window
            return sample_signals(window)

        def confluence_metadata(self):
            return {"generated_at": NOW.isoformat(), "edgar_refresh_verified": not (fail_second_window and self.window == 30)}

    monkeypatch.setattr("smartmoney.api._StoreConfluence", FixtureProvider)
    if fail_second_window:
        with pytest.raises(ConfluenceUnavailable):
            run.cmd_confluence("unused-fixture.db", "13flow-tests test@example.com", [30, 90])
        assert all(path.read_text() == '{"previous":true}' for path in paths)
        assert not (tmp_path / "confluence-history.jsonl").exists()
    else:
        run.cmd_confluence("unused-fixture.db", "13flow-tests test@example.com", [30, 90])
        for path in paths:
            payload = json.loads(path.read_text())
            assert payload["generated_at"] == NOW.isoformat()
            assert payload["metadata"]["edgar_refresh_verified"] is True
            assert payload["signals"]
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert (tmp_path / "confluence-history.jsonl").is_file()
    assert not list(tmp_path.glob(".confluence-*"))
    assert calculated == [90, 30]


@pytest.mark.parametrize("ticker,expected", [
    ("BRK/B", "BRK-B"), ("brk.b", "BRK-B"), ("HEI/A", "HEI-A"),
    ("AAPL", "AAPL"), ("NO/A", None), ("QQQ", None),
    ("AAPL 1 12/01/28", None), ("BRK/B 1 12/01/28", None),
])
def test_sec_aliases_require_a_confirmed_whole_share_class_symbol(ticker, expected):
    assert _sec_issuer_symbol(ticker, {"AAPL": "1", "BRK-B": "2", "HEI-A": "3"}) == expected
    assert _sec_issuer_symbol("BRK/B", {"BRK/B": "1", "BRK-B": "2"}) == "BRK/B"


def test_unmapped_securities_are_disclosed_without_scoring_or_mutating_holdings(tmp_path, monkeypatch):
    path = tmp_path / "market.db"
    tickers = ["AAPL", "BRK/B", "QQQ", "AAPL 1 12/01/28", "MISSING"]
    ciks = [f"{n:010d}" for n in range(1, 4)]
    with Store(str(path)) as store:
        for cik in ciks:
            _save(store, cik, f"Fund {cik}", "Fixture", f"{cik}-26-000001", "13F-HR",
                  "2026-05-14", "2026-03-31", [("Previous", "000000099", 100, 10, "")])
            _save(store, cik, f"Fund {cik}", "Fixture", f"{cik}-26-000002", "13F-HR",
                  "2026-08-14", "2026-06-30",
                  [("Security fixture", f"{n:09d}", 100, 10, "") for n in range(1, 6)])
        for n, ticker in enumerate(tickers, 1):
            store.conn.execute("UPDATE holdings SET ticker=? WHERE cusip=?", (ticker, f"{n:09d}"))
    monkeypatch.setattr(api, "_trusted_active_ciks", lambda store: (ciks, {}))
    monkeypatch.setenv("SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS", "3")
    provider = _StoreConfluence(str(path), "13flow-tests test@example.com")
    monkeypatch.setattr(provider, "_issuer_index", lambda: {"AAPL": "0000000001", "BRK-B": "0000000002"})
    fetched = []
    enriched = []
    enrich = provider._institutional_enrichment

    def capture_enrichment(rows, *args):
        enriched.extend({row["ticker"] for row in rows})
        return enrich(rows, *args)

    monkeypatch.setattr(provider, "_institutional_enrichment", capture_enrichment)

    class FixtureClient:
        def __init__(self, **kwargs):
            pass

        def insider_filings(self, cik, **kwargs):
            fetched.append(cik)
            return []

    monkeypatch.setattr("smartmoney.forms4.Form4Client", FixtureClient)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    signals = provider.confluence(90)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert {signal.ticker for signal in signals} == {"AAPL", "BRK/B"}
    assert set(enriched) == {"AAPL", "BRK/B"}
    assert sorted(fetched) == ["0000000001", "0000000002"]
    metadata = provider.confluence_metadata()
    scope = metadata["universe_coverage"]
    assert (scope["candidate_tickers"], scope["eligible_tickers"]) == (5, 2)
    assert scope["ticker_aliases"] == {"BRK/B": "BRK-B"}
    assert scope["excluded_tickers"] == [
        {"ticker": ticker, "reason": "not_mapped_to_sec_company_index"}
        for ticker in sorted(tickers[2:])
    ]
    assert metadata["edgar_refresh_verified"] is True


@pytest.mark.parametrize("index", [{}, {"OTHER": "0000000001"}])
def test_no_eligible_universe_cannot_be_attested_as_an_empty_screen(monkeypatch, index):
    provider = _StoreConfluence("unused-fixture.db", "13flow-tests test@example.com")
    monkeypatch.setattr(provider, "_issuer_index", lambda: index)
    monkeypatch.setattr(provider, "_institutional", lambda **kwargs: {})
    with pytest.raises(ConfluenceUnavailable):
        provider.confluence(90)
    assert provider.confluence_metadata()["edgar_refresh_verified"] is False


@pytest.mark.parametrize("precompute", [True, False])
def test_only_cli_batches_reuse_filings_and_filter_by_filing_date(monkeypatch, precompute):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return NOW.date()

    monkeypatch.setattr(api, "date", FixedDate)
    provider = _StoreConfluence("unused-fixture.db", "13flow-tests test@example.com", precompute=precompute)
    institutional_calls = []

    def institutional(**kwargs):
        institutional_calls.append(True)
        return {"FIXTURE": InstitutionalSignal(ticker="FIXTURE", funds_accumulating=3)}

    monkeypatch.setattr(provider, "_institutional", institutional)
    monkeypatch.setattr(provider, "_issuer_index", lambda: {"FIXTURE": "0001234567"})
    # All transactions are old: eligibility still follows the filing date, including
    # late disclosures, exactly as a fresh Form4Client lookup would.
    xml = CEO_BUY_XML.replace("2026-05-29", "2025-01-01")
    forms = [parse_form4(xml, accession=str(age), filing_date=(NOW.date() - timedelta(days=age)).isoformat())
             for age in (0, 30, 31, 90, 91, 180)]
    lookups = []
    aggregated = {}

    class FixtureClient:
        def __init__(self, **kwargs):
            pass

        def insider_filings(self, cik, *, window_days, strict, max_filings=60):
            assert strict is True and max_filings == 60
            lookups.append(window_days)
            return [form for form in forms if int(form.accession) <= window_days]

    from smartmoney.crosssignal import aggregate_insider_activity

    def capture(ticker, forms, *, window_days):
        aggregated[window_days] = [int(form.accession) for form in forms]
        return aggregate_insider_activity(ticker, forms, window_days=window_days)

    monkeypatch.setattr("smartmoney.forms4.Form4Client", FixtureClient)
    monkeypatch.setattr("smartmoney.crosssignal.aggregate_insider_activity", capture)
    receipts = []
    for window in (180, 90, 30):
        assert provider.confluence(window)
        metadata = provider.confluence_metadata()
        assert metadata["edgar_refresh_verified"] is True
        receipts.append(metadata["edgar_refresh"])
    assert aggregated == {180: [0, 30, 31, 90, 91, 180], 90: [0, 30, 31, 90], 30: [0, 30]}
    assert lookups == ([180] if precompute else [180, 90, 30])
    assert len(institutional_calls) == (1 if precompute else 3)
    assert [receipt["issuers_reused"] for receipt in receipts] == ([0, 1, 1] if precompute else [0, 0, 0])
    if precompute:
        assert len({receipt["started_at"] for receipt in receipts}) == 1


@pytest.mark.parametrize("last_row", [{"ticker": "BAD", "cik_str": 0}, {"ticker": "AAPL", "cik_str": 2}])
def test_invalid_index_never_leaves_partially_accepted_company_mappings(monkeypatch, last_row):
    provider = _StoreConfluence("unused-fixture.db", "13flow-tests test@example.com")
    response = SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"0": {"ticker": "AAPL", "cik_str": 1}, "1": last_row})
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: response)
    assert provider._issuer_index() == {}


@pytest.mark.parametrize("recent", [None, {}, {"form": [], "accessionNumber": []},
                                       {"form": ["4"], "accessionNumber": [], "filingDate": []},
                                       {"form": "4", "accessionNumber": [], "filingDate": []},
                                       {"form": ["4"], "accessionNumber": ["0000000001-26-000001"], "filingDate": [""]}])
def test_strict_sec_submissions_reject_missing_invalid_or_truncated_columns(monkeypatch, recent):
    client = Form4Client(user_agent="13flow-tests test@example.com")
    response = SimpleNamespace(json=lambda: {"filings": {"recent": recent}})
    monkeypatch.setattr(client, "_get", lambda *args: response)
    with pytest.raises((KeyError, TypeError, ValueError)):
        client.insider_filings("0000000001", strict=True)


def test_strict_form4_accepts_an_empty_valid_feed_and_rejects_another_xml_issuer(monkeypatch):
    client = Form4Client(user_agent="13flow-tests test@example.com")
    response = SimpleNamespace(json=lambda: {"filings": {"recent": {
        "form": [], "accessionNumber": [], "filingDate": [],
    }}})
    monkeypatch.setattr(client, "_get", lambda *args: response)
    assert client.insider_filings("0000000001", strict=True) == []
    monkeypatch.setattr(client, "list_form4_accessions", lambda *args, **kwargs: [
        {"accession": "0000000001-26-000001", "filing_date": "2026-10-01"},
    ])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args: CEO_BUY_XML)
    with pytest.raises(ValueError, match="issuer"):
        client.insider_filings("0000000001", strict=True)


def test_upstream_failure_stops_before_requesting_other_issuers(monkeypatch):
    import requests
    provider = _StoreConfluence("unused-fixture.db", "13flow-tests test@example.com")
    monkeypatch.setattr(provider, "_institutional", lambda **kwargs: {
        ticker: InstitutionalSignal(ticker=ticker, funds_accumulating=3) for ticker in ("FIRST", "LATER")
    })
    monkeypatch.setattr(provider, "_issuer_index", lambda: {"FIRST": "0000000001", "LATER": "0000000002"})
    checked = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def insider_filings(self, cik, **kwargs):
            checked.append(cik)
            raise requests.HTTPError("private upstream detail", response=SimpleNamespace(status_code=403))

    monkeypatch.setattr("smartmoney.forms4.Form4Client", Client)
    with pytest.raises(ConfluenceUnavailable, match="HTTP 403") as failure:
        provider.confluence(180)
    assert "private upstream detail" not in str(failure.value)
    assert checked == ["0000000001"]
    assert provider.confluence_metadata()["edgar_refresh_verified"] is False
