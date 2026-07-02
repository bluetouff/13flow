"""
Historical adjusted-price export for validation datasets.

The output schema is intentionally tiny and provider-neutral:
    ticker,date,adj_close

It can be joined by `run.py --build-validation-dataset --validation-prices ...`.
"""

from __future__ import annotations

import csv
import os
import time
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

from .prices import MassiveProvider, PriceProvider, StooqProvider, YahooChartProvider

PRICE_COLUMNS = ("ticker", "date", "adj_close")


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def read_tickers(path: str) -> list[str]:
    out = []
    seen = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.strip().upper()
            if not t or t.startswith("#") or t in seen:
                continue
            seen.add(t)
            out.append(t)
    return out


def provider_symbol(ticker: str, provider_name: str) -> str:
    t = ticker.upper().strip()
    if provider_name == "massive":
        return t.replace("/", ".")
    if provider_name == "stooq":
        return t
    if provider_name == "yahoo":
        return t.replace("/", "-").replace(".", "-")
    return t


def make_price_provider(provider_name: str, *, timeout: float = 10.0) -> PriceProvider:
    if provider_name == "massive":
        return MassiveProvider(
            os.environ.get("MASSIVE_API_KEY", ""),
            base_url=os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com"),
        )
    if provider_name == "stooq":
        return StooqProvider()
    if provider_name == "yahoo":
        return YahooChartProvider(timeout=timeout)
    raise ValueError("provider must be massive, stooq or yahoo")


def _read_existing(path: str) -> tuple[list[dict[str, str]], set[str]]:
    if not os.path.exists(path):
        return [], set()
    rows: list[dict[str, str]] = []
    tickers: set[str] = set()
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            t = str(row.get("ticker") or "").upper().strip()
            d = str(row.get("date") or "").strip()
            px = str(row.get("adj_close") or "").strip()
            if t and d and px:
                rows.append({"ticker": t, "date": d, "adj_close": px})
                tickers.add(t)
    return rows, tickers


def _merged_rows(existing_rows: list[dict[str, str]],
                 new_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {
        (row["ticker"], row["date"]): row
        for row in existing_rows + new_rows
    }
    return sorted(by_key.values(), key=lambda r: (r["ticker"], r["date"]))


def _write_rows(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PRICE_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _as_positive_float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def validate_price_csv(
    path: str,
    *,
    tickers_path: str | None = None,
    start: date | None = None,
    end: date | None = None,
    max_gap_days: int = 10,
) -> dict[str, Any]:
    expected_tickers = read_tickers(tickers_path) if tickers_path else []
    required = set(PRICE_COLUMNS)
    rows_seen = 0
    valid_rows = 0
    invalid_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    by_ticker: dict[str, list[date]] = {}

    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        missing_columns = sorted(required - set(columns))
        for idx, row in enumerate(reader, start=2):
            rows_seen += 1
            ticker = str(row.get("ticker") or "").upper().strip()
            raw_date = str(row.get("date") or "").strip()
            d = None
            px = _as_positive_float(row.get("adj_close"))
            try:
                d = parse_date(raw_date)
            except (TypeError, ValueError):
                pass
            problems = []
            if not ticker:
                problems.append("missing_ticker")
            if d is None:
                problems.append("invalid_date")
            if px is None:
                problems.append("invalid_adj_close")
            if problems:
                if len(invalid_rows) < 50:
                    invalid_rows.append({
                        "row": idx,
                        "ticker": ticker,
                        "date": raw_date,
                        "errors": problems,
                    })
                continue
            key = (ticker, d.isoformat())
            if key in seen:
                if len(duplicate_rows) < 50:
                    duplicate_rows.append({"row": idx, "ticker": ticker, "date": d.isoformat()})
                continue
            seen.add(key)
            valid_rows += 1
            by_ticker.setdefault(ticker, []).append(d)

    universe = expected_tickers or sorted(by_ticker)
    empty_tickers = []
    partial_history = []
    major_gaps = []
    first_dates = []
    last_dates = []
    max_gap_days = max(1, int(max_gap_days))

    for ticker in universe:
        dates = sorted(set(by_ticker.get(ticker, [])))
        if not dates:
            empty_tickers.append(ticker)
            continue
        first_dates.append(dates[0].isoformat())
        last_dates.append(dates[-1].isoformat())
        missing_start = bool(start and dates[0] > start)
        missing_end = bool(end and dates[-1] < end)
        if missing_start or missing_end:
            partial_history.append({
                "ticker": ticker,
                "rows": len(dates),
                "from": dates[0].isoformat(),
                "to": dates[-1].isoformat(),
                "missing_start": missing_start,
                "missing_end": missing_end,
            })
        if len(dates) > 1:
            gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
            max_gap = max(gaps)
            if max_gap > max_gap_days:
                gap_idx = gaps.index(max_gap)
                major_gaps.append({
                    "ticker": ticker,
                    "gap_days": max_gap,
                    "from": dates[gap_idx].isoformat(),
                    "to": dates[gap_idx + 1].isoformat(),
                })

    status = "ready"
    if (missing_columns or invalid_rows or duplicate_rows or empty_tickers or
            partial_history or major_gaps):
        status = "review"

    return {
        "path": path,
        "status": status,
        "columns": columns,
        "missing_required_columns": missing_columns,
        "rows_total": rows_seen,
        "rows_valid": valid_rows,
        "invalid_row_count": len(invalid_rows),
        "invalid_rows_sample": invalid_rows,
        "duplicate_row_count": len(duplicate_rows),
        "duplicate_rows_sample": duplicate_rows,
        "ticker_universe_count": len(universe),
        "tickers_observed": len(by_ticker),
        "tickers_empty": len(empty_tickers),
        "empty_ticker_sample": empty_tickers[:50],
        "tickers_partial_history": len(partial_history),
        "partial_history_sample": partial_history[:50],
        "major_gap_count": len(major_gaps),
        "major_gap_sample": major_gaps[:50],
        "max_gap_days": max_gap_days,
        "requested_start": start.isoformat() if start else None,
        "requested_end": end.isoformat() if end else None,
        "earliest_price_date": min(first_dates) if first_dates else None,
        "latest_price_date": max(last_dates) if last_dates else None,
        "readiness_rule": (
            "ready requires required columns, positive prices, no duplicate ticker/date, "
            "all requested tickers present, full requested date coverage, and no major "
            "calendar gaps above max_gap_days"
        ),
    }


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return int(status) if status is not None else None


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(str(raw))
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_with_retries(
    provider: PriceProvider,
    symbol: str,
    start: date,
    end: date,
    *,
    retry_attempts: int,
    retry_base_sleep: float,
    retry_max_sleep: float,
    sleep_func: Callable[[float], None],
) -> tuple[dict[date, float], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    max_attempts = max(1, retry_attempts + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            return provider.daily_closes(symbol, start, end), events
        except requests.HTTPError as exc:
            status = _http_status(exc)
            retryable = status == 429 or (status is not None and 500 <= status <= 599)
            if not retryable or attempt >= max_attempts:
                raise
            retry_after = _retry_after_seconds(exc)
            delay = retry_after if retry_after is not None else retry_base_sleep * (2 ** (attempt - 1))
            delay = min(max(0.0, delay), retry_max_sleep)
            events.append({
                "attempt": attempt,
                "status": status,
                "sleep_sec": round(delay, 3),
                "source": "retry-after" if retry_after is not None else "exponential_backoff",
            })
            if delay > 0:
                sleep_func(delay)


def _history_coverage(rows: list[dict[str, str]],
                      tickers: list[str],
                      start: date,
                      end: date) -> dict[str, Any]:
    by_ticker: dict[str, list[str]] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row["date"])

    complete = 0
    partial: list[dict[str, Any]] = []
    empty: list[str] = []
    first_dates = []
    last_dates = []
    requested_start = start.isoformat()
    requested_end = end.isoformat()

    for ticker in tickers:
        dates = sorted(set(by_ticker.get(ticker, [])))
        if not dates:
            empty.append(ticker)
            continue
        first_dates.append(dates[0])
        last_dates.append(dates[-1])
        missing_start = dates[0] > requested_start
        missing_end = dates[-1] < requested_end
        if not missing_start and not missing_end:
            complete += 1
        else:
            partial.append({
                "ticker": ticker,
                "rows": len(dates),
                "from": dates[0],
                "to": dates[-1],
                "missing_start": missing_start,
                "missing_end": missing_end,
            })

    return {
        "requested_start": requested_start,
        "requested_end": requested_end,
        "tickers_complete_history": complete,
        "tickers_partial_history": len(partial),
        "tickers_without_rows": len(empty),
        "earliest_price_date": min(first_dates) if first_dates else None,
        "latest_price_date": max(last_dates) if last_dates else None,
        "partial_history_sample": partial[:50],
        "empty_ticker_sample": empty[:50],
    }


def build_validation_price_file(
    tickers_path: str,
    out_path: str,
    provider: PriceProvider,
    *,
    provider_name: str,
    start: date,
    end: date,
    sleep_sec: float = 0.0,
    force: bool = False,
    retry_attempts: int = 5,
    retry_base_sleep: float = 30.0,
    retry_max_sleep: float = 300.0,
    max_tickers: int | None = None,
    checkpoint: bool = True,
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    all_requested_tickers = read_tickers(tickers_path)
    tickers = all_requested_tickers[:max_tickers] if max_tickers else all_requested_tickers
    existing_rows, cached_tickers = ([], set()) if force else _read_existing(out_path)
    new_rows: list[dict[str, str]] = []
    no_data: list[str] = []
    errors: list[dict[str, str]] = []
    retry_events: list[dict[str, Any]] = []
    fetched = 0
    cached = 0

    for ticker in tickers:
        if ticker in cached_tickers:
            cached += 1
            continue
        symbol = provider_symbol(ticker, provider_name)
        try:
            closes, events = _fetch_with_retries(
                provider,
                symbol,
                start,
                end,
                retry_attempts=retry_attempts,
                retry_base_sleep=retry_base_sleep,
                retry_max_sleep=retry_max_sleep,
                sleep_func=sleep_func,
            )
            for event in events:
                retry_events.append({"ticker": ticker, "source_symbol": symbol, **event})
        except Exception as e:  # noqa: BLE001 - report and continue; validation wants coverage stats
            errors.append({"ticker": ticker, "source_symbol": symbol, "error": str(e)[:240]})
            if checkpoint:
                _write_rows(out_path, _merged_rows(existing_rows, new_rows))
            continue
        if not closes:
            no_data.append(ticker)
        else:
            fetched += 1
            for d, px in sorted(closes.items()):
                if start <= d <= end and px > 0:
                    new_rows.append({
                        "ticker": ticker,
                        "date": d.isoformat(),
                        "adj_close": f"{float(px):.8g}",
                    })
        if checkpoint:
            _write_rows(out_path, _merged_rows(existing_rows, new_rows))
        if sleep_sec > 0:
            sleep_func(sleep_sec)

    all_rows = _merged_rows(existing_rows, new_rows)
    _write_rows(out_path, all_rows)

    usable = cached + fetched
    return {
        "path": out_path,
        "provider": provider_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tickers_requested": len(tickers),
        "tickers_total_in_input": len(all_requested_tickers),
        "tickers_skipped_by_max": max(0, len(all_requested_tickers) - len(tickers)),
        "tickers_cached": cached,
        "tickers_fetched": fetched,
        "tickers_with_no_data": len(no_data),
        "tickers_with_errors": len(errors),
        "coverage": round(usable / len(tickers), 6) if tickers else 0.0,
        "rows_existing": len(existing_rows),
        "rows_new": len(new_rows),
        "rows_total": len(all_rows),
        "rows_deduplicated": len(existing_rows) + len(new_rows) - len(all_rows),
        "history_coverage": _history_coverage(all_rows, tickers, start, end),
        "retry_policy": {
            "retry_attempts": retry_attempts,
            "retry_base_sleep": retry_base_sleep,
            "retry_max_sleep": retry_max_sleep,
            "retry_statuses": [429, "5xx"],
        },
        "retry_event_count": len(retry_events),
        "retry_events_sample": retry_events[:50],
        "checkpoint": checkpoint,
        "no_data_sample": no_data[:50],
        "errors_sample": errors[:20],
        "resume_policy": "existing ticker rows are reused unless --validation-price-force is set",
    }
