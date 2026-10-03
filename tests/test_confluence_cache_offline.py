"""Cache provenance must reflect completed EDGAR work, not a later HTTP read."""
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import run
from smartmoney.api import _StoreConfluence
from smartmoney.api_signals import (
    ConfluenceUnavailable, UnconfiguredConfluenceProvider,
    confluence_cache_metadata, make_signals_blueprint,
)
from smartmoney.crosssignal import InstitutionalSignal
from smartmoney.forms4 import Form4Client
from smartmoney.sample_confluence import sample_signals


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
    monkeypatch.setattr(provider, "_institutional", lambda: {"FIXTURE": InstitutionalSignal(ticker="FIXTURE", funds_accumulating=3)})
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

    class FixtureProvider:
        def __init__(self, *args):
            self.window = None

        def confluence(self, window):
            self.window = window
            return sample_signals(window)

        def confluence_metadata(self):
            return {"generated_at": NOW.isoformat(), "edgar_refresh_verified": not (fail_second_window and self.window == 90)}

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
