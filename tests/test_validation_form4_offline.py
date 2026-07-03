"""
Offline tests for normalized Form 4 validation export.
"""

from __future__ import annotations

import csv
from pathlib import Path

from smartmoney.validation_form4 import build_validation_form4_file
from smartmoney.validation_prices import parse_date

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2024-08-01</periodOfReport>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000420</rptOwnerCik>
      <rptOwnerName>Sample CEO</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-08-01</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>100</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

REPORTING_OWNER_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2024-09-01</periodOfReport>
  <issuer>
    <issuerCik>0001680247</issuerCik>
    <issuerName>PROPETRO HOLDING CORP.</issuerName>
    <issuerTradingSymbol>PUMP</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001045810</rptOwnerCik>
      <rptOwnerName>NVIDIA CORP</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>0</isOfficer>
      <isTenPercentOwner>1</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-09-01</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>12</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>0</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


class FakeForm4Client:
    def list_form4_accessions(self, issuer_cik, *, since=None, limit=100):
        if issuer_cik == "0001045810":
            return [
                {"accession": "0001045810-24-000004", "filing_date": "2024-08-02"},
                {"accession": "0001680247-24-000009", "filing_date": "2024-09-03"},
                {"accession": "0001045810-25-000099", "filing_date": "2025-01-03"},
            ][:limit]
        return []

    def fetch_ownership_xml(self, accession, cik):
        if accession == "0001680247-24-000009":
            return REPORTING_OWNER_XML
        return FORM4_XML


def test_build_validation_form4_file_exports_normalized_rows_without_network(tmp_path):
    tickers = tmp_path / "tickers.txt"
    out = tmp_path / "form4.csv"
    tickers.write_text("NVDA\nAAPL\n", encoding="utf-8")

    summary = build_validation_form4_file(
        str(tickers),
        str(out),
        user_agent="13FLOW test@example.com",
        start=parse_date("2024-01-01"),
        end=parse_date("2024-12-31"),
        max_tickers=None,
        client=FakeForm4Client(),
        ticker_cik_map={"NVDA": "0001045810"},
        sleep_func=lambda _s: None,
    )

    assert summary["tickers_requested"] == 2
    assert summary["tickers_fetched"] == 1
    assert summary["tickers_without_cik"] == 1
    assert summary["filings_seen"] == 1
    assert summary["issuer_mismatch_filings"] == 1
    assert summary["issuer_mismatch_sample"][0]["actual_issuer_cik"] == "0001680247"
    assert summary["issuer_mismatch_sample"][0]["owner_cik"] == "0001045810"
    assert summary["rows_total"] == 1

    with out.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "NVDA"
    assert row["accession"] == "0001045810-24-000004"
    assert row["filing_date"] == "2024-08-02"
    assert row["transaction_code"] == "P"
    assert row["acquired_disposed"] == "A"
    assert row["value_usd"] == "1000000"


def test_build_validation_form4_file_reuses_cached_tickers(tmp_path):
    tickers = tmp_path / "tickers.txt"
    out = tmp_path / "form4.csv"
    tickers.write_text("NVDA\n", encoding="utf-8")

    first = build_validation_form4_file(
        str(tickers),
        str(out),
        user_agent="13FLOW test@example.com",
        start=parse_date("2024-01-01"),
        end=parse_date("2024-12-31"),
        client=FakeForm4Client(),
        ticker_cik_map={"NVDA": "0001045810"},
        sleep_func=lambda _s: None,
    )
    second = build_validation_form4_file(
        str(tickers),
        str(out),
        user_agent="13FLOW test@example.com",
        start=parse_date("2024-01-01"),
        end=parse_date("2024-12-31"),
        client=FakeForm4Client(),
        ticker_cik_map={"NVDA": "0001045810"},
        sleep_func=lambda _s: None,
    )

    assert first["rows_total"] == 1
    assert second["tickers_cached"] == 1
    assert second["rows_new"] == 0
    assert second["rows_total"] == 1
