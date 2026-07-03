"""
SEC Form 4 export for Confluence validation.

This module is the networked producer for the offline `--validation-form4` input.
It fetches reviewed ownership XML from SEC EDGAR, writes a small normalized CSV,
and checkpoints after each ticker so interrupted runs are resumable.
"""

from __future__ import annotations

import csv
import os
import time
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable

import requests

from .forms4 import Form4, Form4Client, Form4Transaction, parse_form4
from .validation_prices import parse_date, read_tickers

FORM4_COLUMNS = (
    "ticker",
    "issuer_cik",
    "issuer_name",
    "accession",
    "filing_date",
    "period_of_report",
    "transaction_date",
    "owner_cik",
    "owner_name",
    "officer_title",
    "is_officer",
    "is_director",
    "is_ten_percent_owner",
    "transaction_code",
    "acquired_disposed",
    "security_title",
    "shares",
    "price_per_share",
    "value_usd",
    "shares_owned_after",
    "direct",
)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def load_sec_ticker_cik_map(user_agent: str,
                            session: requests.Session | None = None) -> dict[str, str]:
    sess = session or requests.Session()
    resp = sess.get(SEC_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    out: dict[str, str] = {}
    for row in (resp.json() or {}).values():
        ticker = str(row.get("ticker") or "").upper().strip()
        cik = str(row.get("cik_str") or "").strip()
        if ticker and cik:
            out.setdefault(ticker, cik.zfill(10))
    return out


def _read_existing(path: str) -> tuple[list[dict[str, str]], set[str]]:
    if not os.path.exists(path):
        return [], set()
    rows: list[dict[str, str]] = []
    tickers: set[str] = set()
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ticker = str(row.get("ticker") or "").upper().strip()
            accession = str(row.get("accession") or "").strip()
            txn_date = str(row.get("transaction_date") or "").strip()
            if ticker and accession and txn_date:
                rows.append({c: str(row.get(c) or "") for c in FORM4_COLUMNS})
                tickers.add(ticker)
    return rows, tickers


def _row_key(row: dict[str, str]) -> tuple[str, ...]:
    return (
        row.get("ticker", ""),
        row.get("accession", ""),
        row.get("owner_cik", ""),
        row.get("transaction_date", ""),
        row.get("transaction_code", ""),
        row.get("acquired_disposed", ""),
        row.get("shares", ""),
        row.get("price_per_share", ""),
    )


def _merged_rows(existing_rows: list[dict[str, str]],
                 new_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {_row_key(row): row for row in existing_rows + new_rows}
    return sorted(by_key.values(), key=lambda r: (
        r.get("ticker", ""),
        r.get("filing_date", ""),
        r.get("accession", ""),
        r.get("owner_cik", ""),
    ))


def _write_rows(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FORM4_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in FORM4_COLUMNS})
    os.replace(tmp, path)


def validate_form4_csv(
    path: str,
    *,
    tickers_path: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    expected_tickers = read_tickers(tickers_path) if tickers_path else []
    expected_set = set(expected_tickers)
    required = set(FORM4_COLUMNS)
    rows_seen = 0
    valid_rows = 0
    invalid_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    issuer_ciks_by_ticker: dict[str, set[str]] = defaultdict(set)
    issuer_names_by_ticker: dict[str, set[str]] = defaultdict(set)
    transaction_code_counts: Counter[str] = Counter()
    acquired_disposed_counts: Counter[str] = Counter()
    row_counts_by_ticker: Counter[str] = Counter()
    transaction_dates: list[str] = []
    unexpected_tickers: set[str] = set()
    open_market_buy_tickers: set[str] = set()
    open_market_sell_tickers: set[str] = set()
    open_market_buy_rows = 0
    open_market_sell_rows = 0
    invalid_count = 0
    duplicate_count = 0

    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        missing_columns = sorted(required - set(columns))
        for idx, row in enumerate(reader, start=2):
            rows_seen += 1
            ticker = str(row.get("ticker") or "").upper().strip()
            issuer_cik = str(row.get("issuer_cik") or "").strip().zfill(10)
            issuer_name = str(row.get("issuer_name") or "").strip()
            accession = str(row.get("accession") or "").strip()
            owner_cik = str(row.get("owner_cik") or "").strip()
            raw_filing_date = str(row.get("filing_date") or "").strip()
            raw_txn_date = str(row.get("transaction_date") or "").strip()
            txn_code = str(row.get("transaction_code") or "").upper().strip()
            acquired_disposed = str(row.get("acquired_disposed") or "").upper().strip()
            shares = str(row.get("shares") or "").strip()
            price = str(row.get("price_per_share") or "").strip()
            filing_date = None
            txn_date = None
            try:
                filing_date = parse_date(raw_filing_date)
            except (TypeError, ValueError):
                pass
            try:
                txn_date = parse_date(raw_txn_date)
            except (TypeError, ValueError):
                pass

            problems = []
            if not ticker:
                problems.append("missing_ticker")
            if expected_set and ticker and ticker not in expected_set:
                problems.append("ticker_outside_universe")
                unexpected_tickers.add(ticker)
            if not issuer_cik or issuer_cik == "0000000000":
                problems.append("missing_issuer_cik")
            if not issuer_name:
                problems.append("missing_issuer_name")
            if not accession:
                problems.append("missing_accession")
            if filing_date is None:
                problems.append("invalid_filing_date")
            if txn_date is None:
                problems.append("invalid_transaction_date")
            if txn_date is not None and start and txn_date < start:
                problems.append("transaction_before_start")
            if txn_date is not None and end and txn_date > end:
                problems.append("transaction_after_end")
            if txn_code not in {"A", "C", "D", "F", "G", "I", "J", "M", "P", "S"}:
                problems.append("unexpected_transaction_code")
            if acquired_disposed not in {"A", "D"}:
                problems.append("unexpected_acquired_disposed")
            try:
                if float(shares) <= 0:
                    problems.append("non_positive_shares")
            except (TypeError, ValueError):
                problems.append("invalid_shares")
            try:
                if float(price) < 0:
                    problems.append("negative_price_per_share")
            except (TypeError, ValueError):
                problems.append("invalid_price_per_share")

            if problems:
                invalid_count += 1
                if len(invalid_rows) < 50:
                    invalid_rows.append({
                        "row": idx,
                        "ticker": ticker,
                        "accession": accession,
                        "transaction_date": raw_txn_date,
                        "errors": problems,
                    })
                continue

            key = _row_key({c: str(row.get(c) or "") for c in FORM4_COLUMNS})
            if key in seen:
                duplicate_count += 1
                if len(duplicate_rows) < 50:
                    duplicate_rows.append({
                        "row": idx,
                        "ticker": ticker,
                        "accession": accession,
                        "transaction_date": raw_txn_date,
                    })
                continue
            seen.add(key)
            valid_rows += 1
            row_counts_by_ticker[ticker] += 1
            issuer_ciks_by_ticker[ticker].add(issuer_cik)
            issuer_names_by_ticker[ticker].add(issuer_name)
            transaction_code_counts[txn_code] += 1
            acquired_disposed_counts[acquired_disposed] += 1
            if txn_date is not None:
                transaction_dates.append(txn_date.isoformat())
            if txn_code == "P" and acquired_disposed == "A":
                open_market_buy_rows += 1
                open_market_buy_tickers.add(ticker)
            if txn_code == "S" and acquired_disposed == "D":
                open_market_sell_rows += 1
                open_market_sell_tickers.add(ticker)

    mixed_issuers = []
    name_variants = []
    for ticker in sorted(issuer_ciks_by_ticker):
        ciks = sorted(issuer_ciks_by_ticker[ticker])
        names = sorted(issuer_names_by_ticker[ticker])
        if len(ciks) > 1:
            mixed_issuers.append({
                "ticker": ticker,
                "issuer_ciks": ciks,
                "issuer_names": names,
                "rows": row_counts_by_ticker[ticker],
            })
        elif len(names) > 1:
            name_variants.append({
                "ticker": ticker,
                "issuer_cik": ciks[0],
                "issuer_names": names,
                "rows": row_counts_by_ticker[ticker],
            })

    observed = set(row_counts_by_ticker)
    empty_tickers = [t for t in expected_tickers if t not in observed]
    status = "ready"
    if (missing_columns or invalid_count or duplicate_count or mixed_issuers or
            unexpected_tickers or valid_rows == 0):
        status = "review"

    return {
        "path": path,
        "status": status,
        "columns": columns,
        "missing_required_columns": missing_columns,
        "rows_total": rows_seen,
        "rows_valid": valid_rows,
        "invalid_row_count": invalid_count,
        "invalid_rows_sample": invalid_rows,
        "duplicate_row_count": duplicate_count,
        "duplicate_rows_sample": duplicate_rows,
        "ticker_universe_count": len(expected_tickers) if expected_tickers else len(observed),
        "tickers_observed": len(observed),
        "tickers_empty": len(empty_tickers),
        "empty_ticker_sample": empty_tickers[:50],
        "unexpected_ticker_count": len(unexpected_tickers),
        "unexpected_ticker_sample": sorted(unexpected_tickers)[:50],
        "mixed_issuer_ticker_count": len(mixed_issuers),
        "mixed_issuer_tickers_sample": mixed_issuers[:50],
        "issuer_name_variant_count": len(name_variants),
        "issuer_name_variant_sample": name_variants[:50],
        "open_market_buy_rows": open_market_buy_rows,
        "open_market_buy_tickers": len(open_market_buy_tickers),
        "open_market_sell_rows": open_market_sell_rows,
        "open_market_sell_tickers": len(open_market_sell_tickers),
        "transaction_code_counts": dict(sorted(transaction_code_counts.items())),
        "acquired_disposed_counts": dict(sorted(acquired_disposed_counts.items())),
        "requested_start": start.isoformat() if start else None,
        "requested_end": end.isoformat() if end else None,
        "earliest_transaction_date": min(transaction_dates) if transaction_dates else None,
        "latest_transaction_date": max(transaction_dates) if transaction_dates else None,
        "readiness_rule": (
            "ready requires required columns, valid in-window rows, no duplicate "
            "transaction rows, no tickers outside the requested universe, at least one "
            "valid row, and exactly one issuer CIK per ticker"
        ),
    }


def _txn_row(ticker: str, form: Form4, txn: Form4Transaction) -> dict[str, str]:
    return {
        "ticker": ticker.upper(),
        "issuer_cik": form.issuer_cik,
        "issuer_name": form.issuer_name,
        "accession": form.accession,
        "filing_date": form.filing_date,
        "period_of_report": form.period_of_report,
        "transaction_date": txn.txn_date,
        "owner_cik": form.owner_cik,
        "owner_name": form.owner_name,
        "officer_title": form.officer_title,
        "is_officer": "1" if form.is_officer else "0",
        "is_director": "1" if form.is_director else "0",
        "is_ten_percent_owner": "1" if form.is_ten_percent_owner else "0",
        "transaction_code": txn.code,
        "acquired_disposed": txn.acquired_disposed,
        "security_title": txn.security_title,
        "shares": f"{float(txn.shares):.8g}",
        "price_per_share": f"{float(txn.price_per_share):.8g}",
        "value_usd": f"{float(txn.value_usd):.8g}",
        "shares_owned_after": f"{float(txn.shares_owned_after):.8g}",
        "direct": "1" if txn.direct else "0",
    }


def _form_rows(ticker: str, form: Form4, *, start: date, end: date) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    filing_date = parse_date(form.filing_date)
    if filing_date < start or filing_date > end:
        return out
    for txn in form.transactions:
        try:
            txn_date = parse_date(txn.txn_date)
        except (TypeError, ValueError):
            continue
        if start <= txn_date <= end:
            out.append(_txn_row(ticker, form, txn))
    return out


def build_validation_form4_file(
    tickers_path: str,
    out_path: str,
    *,
    user_agent: str,
    start: date,
    end: date,
    sleep_sec: float = 0.0,
    max_tickers: int | None = None,
    max_filings_per_ticker: int = 200,
    force: bool = False,
    checkpoint: bool = True,
    client: Form4Client | None = None,
    ticker_cik_map: dict[str, str] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not user_agent or "@" not in user_agent:
        raise ValueError("SEC_UA with contact email is required for Form 4 export")
    all_tickers = read_tickers(tickers_path)
    tickers = all_tickers[:max_tickers] if max_tickers else all_tickers
    existing_rows, cached_tickers = ([], set()) if force else _read_existing(out_path)
    f4 = client or Form4Client(user_agent=user_agent)
    cik_map = ticker_cik_map or load_sec_ticker_cik_map(user_agent)

    new_rows: list[dict[str, str]] = []
    no_cik: list[str] = []
    no_filings: list[str] = []
    issuer_mismatches: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    cached = 0
    fetched = 0
    filings_seen = 0

    for ticker in tickers:
        if ticker in cached_tickers:
            cached += 1
            continue
        cik = cik_map.get(ticker)
        if not cik:
            no_cik.append(ticker)
            if checkpoint:
                _write_rows(out_path, _merged_rows(existing_rows, new_rows))
            continue
        try:
            metas = f4.list_form4_accessions(cik, since=start, limit=max_filings_per_ticker)
            metas = [
                m for m in metas
                if m.get("filing_date") and start <= parse_date(m["filing_date"]) <= end
            ]
            if not metas:
                no_filings.append(ticker)
            for meta in metas:
                xml = f4.fetch_ownership_xml(meta["accession"], cik)
                form = parse_form4(xml, accession=meta["accession"], filing_date=meta["filing_date"])
                if str(form.issuer_cik or "").zfill(10) != str(cik).zfill(10):
                    issuer_mismatches.append({
                        "ticker": ticker,
                        "expected_issuer_cik": str(cik).zfill(10),
                        "actual_issuer_cik": str(form.issuer_cik or "").zfill(10),
                        "actual_issuer_name": form.issuer_name,
                        "owner_cik": form.owner_cik,
                        "owner_name": form.owner_name,
                        "accession": meta["accession"],
                    })
                    continue
                rows = _form_rows(ticker, form, start=start, end=end)
                new_rows.extend(rows)
                filings_seen += 1
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - keep long export resumable
            errors.append({"ticker": ticker, "issuer_cik": cik, "error": str(exc)[:240]})
        if checkpoint:
            _write_rows(out_path, _merged_rows(existing_rows, new_rows))
        if sleep_sec > 0:
            sleep_func(sleep_sec)

    all_rows = _merged_rows(existing_rows, new_rows)
    _write_rows(out_path, all_rows)
    usable = cached + fetched
    return {
        "path": out_path,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tickers_requested": len(tickers),
        "tickers_total_in_input": len(all_tickers),
        "tickers_skipped_by_max": max(0, len(all_tickers) - len(tickers)),
        "tickers_cached": cached,
        "tickers_fetched": fetched,
        "tickers_without_cik": len(no_cik),
        "tickers_without_filings": len(no_filings),
        "issuer_mismatch_filings": len(issuer_mismatches),
        "tickers_with_errors": len(errors),
        "coverage": round(usable / len(tickers), 6) if tickers else 0.0,
        "filings_seen": filings_seen,
        "rows_existing": len(existing_rows),
        "rows_new": len(new_rows),
        "rows_total": len(all_rows),
        "rows_deduplicated": len(existing_rows) + len(new_rows) - len(all_rows),
        "checkpoint": checkpoint,
        "no_cik_sample": no_cik[:50],
        "no_filings_sample": no_filings[:50],
        "issuer_mismatch_sample": issuer_mismatches[:20],
        "errors_sample": errors[:20],
        "resume_policy": "existing ticker rows are reused unless --validation-form4-force is set",
    }
