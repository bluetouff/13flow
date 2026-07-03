"""
Offline tests for normalized Form 4 validation export.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

from smartmoney.validation_form4 import FORM4_COLUMNS, build_validation_form4_file, validate_form4_csv
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


def _write_form4_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FORM4_COLUMNS)
        writer.writeheader()
        for row in rows:
            complete = {column: "" for column in FORM4_COLUMNS}
            complete.update(row)
            writer.writerow(complete)


def test_validate_form4_csv_ready_report(tmp_path):
    tickers = tmp_path / "tickers.txt"
    form4 = tmp_path / "form4.csv"
    tickers.write_text("NVDA\nAAPL\n", encoding="utf-8")
    base = {
        "ticker": "NVDA",
        "issuer_cik": "0001045810",
        "issuer_name": "NVIDIA CORP",
        "accession": "0001045810-24-000004",
        "filing_date": "2024-08-02",
        "period_of_report": "2024-08-01",
        "transaction_date": "2024-08-01",
        "owner_cik": "0000000420",
        "owner_name": "Sample CEO",
        "officer_title": "Chief Executive Officer",
        "is_officer": "1",
        "is_director": "0",
        "is_ten_percent_owner": "0",
        "security_title": "Common Stock",
        "shares": "10000",
        "price_per_share": "100",
        "value_usd": "1000000",
        "shares_owned_after": "50000",
        "direct": "1",
    }
    _write_form4_rows(form4, [
        {**base, "transaction_code": "P", "acquired_disposed": "A"},
        {
            **base,
            "accession": "0001045810-24-000005",
            "transaction_code": "S",
            "acquired_disposed": "D",
            "price_per_share": "110",
        },
    ])

    report = validate_form4_csv(
        str(form4),
        tickers_path=str(tickers),
        start=parse_date("2024-07-01"),
        end=parse_date("2024-12-31"),
    )

    assert report["status"] == "ready"
    assert report["rows_valid"] == 2
    assert report["tickers_empty"] == 1
    assert report["mixed_issuer_ticker_count"] == 0
    assert report["open_market_buy_rows"] == 1
    assert report["open_market_sell_rows"] == 1


def test_validate_form4_csv_flags_mixed_issuer_identity(tmp_path):
    tickers = tmp_path / "tickers.txt"
    form4 = tmp_path / "form4.csv"
    tickers.write_text("XOM\n", encoding="utf-8")
    common = {
        "ticker": "XOM",
        "filing_date": "2026-05-22",
        "period_of_report": "2026-05-20",
        "transaction_date": "2026-05-20",
        "owner_cik": "0000034088",
        "owner_name": "EXXON MOBIL CORP",
        "transaction_code": "S",
        "acquired_disposed": "D",
        "security_title": "Common Stock",
        "shares": "100",
        "price_per_share": "16.66",
        "value_usd": "1666",
        "shares_owned_after": "0",
        "direct": "0",
    }
    _write_form4_rows(form4, [
        {
            **common,
            "issuer_cik": "0000034088",
            "issuer_name": "EXXON MOBIL CORP",
            "accession": "0000034088-26-000052",
        },
        {
            **common,
            "issuer_cik": "0001680247",
            "issuer_name": "ProPetro Holding Corp.",
            "accession": "0001193125-26-236842",
        },
    ])

    report = validate_form4_csv(
        str(form4),
        tickers_path=str(tickers),
        start=parse_date("2026-01-01"),
        end=parse_date("2026-12-31"),
    )

    assert report["status"] == "review"
    assert report["mixed_issuer_ticker_count"] == 1
    assert report["mixed_issuer_tickers_sample"][0]["ticker"] == "XOM"
    assert report["mixed_issuer_tickers_sample"][0]["issuer_ciks"] == [
        "0000034088",
        "0001680247",
    ]


def test_run_py_help_exposes_validation_form4_tools():
    proc = subprocess.run(
        [sys.executable, "run.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--build-validation-form4" in proc.stdout
    assert "--validation-form4-out" in proc.stdout
    assert "--validate-form4-csv" in proc.stdout
