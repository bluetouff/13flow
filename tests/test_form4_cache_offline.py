"""Interrupted SEC scans resume from validated documents, with fresh filing lists."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from smartmoney.forms4 import Form4Client, parse_form4
from tests.test_forms4_offline import CEO_BUY_XML


CIK = "0001234567"
METAS = [{"accession": f"0001234567-26-{number:06d}", "filing_date": "2026-10-01"}
         for number in (1, 2, 3)]


def client_with_list(tmp_path, monkeypatch, metas=METAS):
    client = Form4Client(user_agent="13flow-tests test@example.com", cache_dir=tmp_path)
    monkeypatch.setattr(client, "list_form4_accessions", lambda *args, **kwargs: metas)
    return client


def test_failed_batch_retains_each_successful_document_and_resume_checks_new_filings(tmp_path, monkeypatch):
    first = client_with_list(tmp_path, monkeypatch, METAS[:2])
    fetched = []

    def interrupted_fetch(accession, cik):
        fetched.append(accession)
        if accession == METAS[1]["accession"]:
            raise requests.Timeout("fixture interruption")
        return CEO_BUY_XML

    monkeypatch.setattr(first, "fetch_ownership_xml", interrupted_fetch)
    with pytest.raises(requests.Timeout):
        first.insider_filings(CIK, strict=True)
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1
    first_bytes = saved[0].read_bytes()
    resumed = client_with_list(tmp_path, monkeypatch)
    listed = []

    def current_list(*args, **kwargs):
        listed.append(True)
        return METAS  # A new filing appeared after the interrupted run.

    monkeypatch.setattr(resumed, "list_form4_accessions", current_list)
    monkeypatch.setattr(resumed, "fetch_ownership_xml", lambda accession, cik: fetched.append(accession) or CEO_BUY_XML)
    forms = resumed.insider_filings(CIK, strict=True)
    assert listed == [True]
    assert fetched == [METAS[0]["accession"], METAS[1]["accession"], METAS[1]["accession"], METAS[2]["accession"]]
    assert forms == [parse_form4(CEO_BUY_XML, **meta) for meta in METAS]
    assert (resumed.filings_cached, resumed.filings_downloaded) == (1, 2)
    assert saved[0].read_bytes() == first_bytes  # Preserve the original retrieval date.
    assert not list(tmp_path.glob(".form4-*"))


@pytest.mark.parametrize("damage", ["json", "digest", "identity", "xml", "issuer", "future"])
def test_corrupt_or_mismatched_cache_is_repaired_from_sec(tmp_path, monkeypatch, damage):
    client = client_with_list(tmp_path, monkeypatch, METAS[:1])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args: CEO_BUY_XML)
    expected = client.insider_filings(CIK, strict=True)
    path = next(tmp_path.glob("*.json"))
    record = json.loads(path.read_text())
    if damage == "digest":
        record["sha256"] = "invalid"
    elif damage == "identity":
        record["identity"] = "another-filing"
    elif damage in ("xml", "issuer"):
        record["xml"] = "not XML" if damage == "xml" else CEO_BUY_XML.replace(CIK, "0000000001")
        record["sha256"] = hashlib.sha256(record["xml"].encode()).hexdigest()
    elif damage == "future":
        record["retrieved_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    path.write_text("{" if damage == "json" else json.dumps(record))
    restored = client_with_list(tmp_path, monkeypatch, METAS[:1])
    monkeypatch.setattr(restored, "fetch_ownership_xml", lambda *args: CEO_BUY_XML)
    assert restored.insider_filings(CIK, strict=True) == expected
    assert (restored.filings_cached, restored.filings_downloaded) == (0, 1)


def test_cache_keys_and_symlinks_cannot_escape_the_cache(tmp_path, monkeypatch):
    client = client_with_list(tmp_path / "cache", monkeypatch, METAS[:1])
    with pytest.raises(ValueError, match="identity"):
        client._filing_cache_path(CIK, "../../other")
    outside = tmp_path / "outside.json"
    outside.write_text("private fixture")
    path = client._filing_cache_path(CIK, METAS[0]["accession"])
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        client.insider_filings(CIK, strict=True)
    assert outside.read_text() == "private fixture"


def test_wrong_issuer_xml_never_enters_the_cache(tmp_path, monkeypatch):
    client = client_with_list(tmp_path, monkeypatch, METAS[:1])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args: CEO_BUY_XML.replace(CIK, "0000000001"))
    with pytest.raises(ValueError, match="issuer"):
        client.insider_filings(CIK, strict=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("retry_after", ["60", "Sun, 04 Oct 2099 10:00:00 GMT"])
def test_long_retry_after_stops_without_retrying_early(monkeypatch, retry_after):
    client = Form4Client(user_agent="13flow-tests test@example.com")
    calls = []
    response = SimpleNamespace(status_code=429, headers={"Retry-After": retry_after})
    monkeypatch.setattr(client._session, "get", lambda *args, **kwargs: calls.append(True) or response)
    monkeypatch.setattr(client._limiter, "wait", lambda: None)
    monkeypatch.setattr("smartmoney.forms4.time.sleep", lambda delay: pytest.fail("must not retry before SEC permits it"))
    with pytest.raises(requests.HTTPError) as failure:
        client._get("https://www.sec.gov/fixture")
    assert failure.value.response.status_code == 429
    assert calls == [True]
