"""Regression for Alphabet's Form 4 filed as an Ethos reporting owner."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartmoney.forms4 import Form4Client, parse_form4
from tests.test_forms4_offline import CEO_BUY_XML


ALPHABET = "0001652044"
ACCESSION = "0001168404-26-000041"
XML = (Path(__file__).parent / "fixtures/form4-alphabet-reporting-owner.xml").read_text()


def test_real_joint_filing_identifies_alphabet_as_seventh_owner():
    filing = parse_form4(XML, accession=ACCESSION)
    assert filing.issuer_cik == "0001788451"
    assert filing.owner_cik == "0001845038"
    assert len(filing.reporting_owner_ciks) == 7
    assert filing.reporting_owner_ciks[-1] == ALPHABET
    assert Form4Client._is_issuer_filing(filing, ALPHABET) is False
    assert Form4Client._is_issuer_filing(filing, "0001788451") is True


def test_non_issuer_filing_is_disclosed_cached_and_never_scored(tmp_path, monkeypatch):
    meta = {"accession": ACCESSION, "filing_date": "2026-07-29"}
    fetched = []
    original = None
    for run in range(2):
        client = Form4Client(user_agent="fixture test@example.com", cache_dir=tmp_path)
        monkeypatch.setattr(client, "list_form4_accessions", lambda *a, **kw: [meta])
        monkeypatch.setattr(client, "fetch_ownership_xml", lambda *a: fetched.append(a) or XML)
        assert client.insider_filings(ALPHABET, strict=True) == []
        assert client.non_issuer_filings == [{**meta, "requested_cik": ALPHABET,
            "issuer_cik": "0001788451", "reason": "requested_company_is_reporting_owner_only"}]
        assert (client.filings_downloaded, client.filings_cached) == ((1, 0) if run == 0 else (0, 1))
        saved = next(tmp_path.glob("*.json")).read_bytes()
        if original is not None:
            assert saved == original
        original = saved
    assert len(fetched) == 1


@pytest.mark.parametrize("xml", [XML.replace(ALPHABET, "0000000001"),
                                 XML.replace("<issuerCik>0001788451</issuerCik>", ""),
                                 XML.replace("<issuerCik>0001788451</issuerCik>", "<issuerCik>0</issuerCik>"),
                                 XML.replace("<documentType>4</documentType>", "<documentType>3</documentType>"),
                                 XML.replace("ownershipDocument", "unrelatedDocument")])
def test_unexplained_mismatch_or_wrong_document_still_aborts_without_caching(tmp_path, monkeypatch, xml):
    client = Form4Client(user_agent="fixture test@example.com", cache_dir=tmp_path)
    monkeypatch.setattr(client, "list_form4_accessions", lambda *a, **kw: [
        {"accession": ACCESSION, "filing_date": "2026-07-29"}])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *a: xml)
    with pytest.raises(ValueError):
        client.insider_filings(ALPHABET, strict=True)
    assert not list(tmp_path.iterdir())
    assert client.non_issuer_filings == []


@pytest.mark.parametrize("document", ["ownership.xml", "xslF345X06/ownership.xml"])
def test_submissions_primary_document_uses_one_request_without_directory_guessing(monkeypatch, document):
    client = Form4Client(user_agent="fixture test@example.com")
    requested = []
    def get(url):
        requested.append(url)
        if url.endswith(".json"):
            return SimpleNamespace(json=lambda: {"filings": {"recent": {
                "form": ["4"], "accessionNumber": [ACCESSION], "filingDate": ["2026-07-29"],
                "primaryDocument": [document]}}})
        return SimpleNamespace(text=XML)
    monkeypatch.setattr(client, "_get", get)
    metas = client.list_form4_accessions(ALPHABET, since=date(2026, 1, 1), strict=True)
    assert metas[0]["primary_document"] == document
    monkeypatch.setattr(client, "list_form4_accessions", lambda *a, **kw: metas)
    assert client.insider_filings(ALPHABET, strict=True) == []
    assert requested == ["https://data.sec.gov/submissions/CIK0001652044.json",
        f"https://www.sec.gov/Archives/edgar/data/1652044/{ACCESSION.replace('-', '')}/ownership.xml"]


@pytest.mark.parametrize("name", ["../ownership.xml", "xslF345X06/../ownership.xml", "https://other.test/x.xml",
                                  "//other.test/x.xml", "xslOther/a.xml", "a.xml?key=private", "a%2f.xml", "a\\b.xml", "", 7])
def test_untrusted_document_path_is_rejected_before_network(monkeypatch, name):
    client = Form4Client(user_agent="fixture test@example.com")
    monkeypatch.setattr(client, "_get", lambda *a: pytest.fail("invalid name must not be fetched"))
    with pytest.raises(ValueError):
        client.fetch_ownership_xml(ACCESSION, ALPHABET, primary_document=name)


def test_directory_fallback_refuses_ambiguous_xml(monkeypatch):
    client = Form4Client(user_agent="fixture test@example.com")
    monkeypatch.setattr(client, "_index_json", lambda *a: {"directory": {"item": [
        {"name": "a.xml", "type": "text.gif"}, {"name": "b.xml", "type": "text.gif"}]}})
    monkeypatch.setattr(client, "_get", lambda *a: pytest.fail("must not guess between XML files"))
    with pytest.raises(LookupError):
        client.fetch_ownership_xml(ACCESSION, ALPHABET)


def test_mismatch_error_identifies_accession_and_validated_ciks_without_xml_content():
    filing = parse_form4(CEO_BUY_XML, accession=ACCESSION)
    with pytest.raises(ValueError, match=ACCESSION) as failure:
        Form4Client._is_issuer_filing(filing, ALPHABET)
    assert "0001234567" in str(failure.value) and ALPHABET in str(failure.value)
    assert "Atlantic" not in str(failure.value)
