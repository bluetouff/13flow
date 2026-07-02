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


def make_price_provider(provider_name: str) -> PriceProvider:
    if provider_name == "massive":
        return MassiveProvider(
            os.environ.get("MASSIVE_API_KEY", ""),
            base_url=os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com"),
        )
    if provider_name == "stooq":
        return StooqProvider()
    if provider_name == "yahoo":
        return YahooChartProvider()
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
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    tickers = read_tickers(tickers_path)
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
        if sleep_sec > 0:
            sleep_func(sleep_sec)

    by_key = {
        (row["ticker"], row["date"]): row
        for row in existing_rows + new_rows
    }
    all_rows = sorted(by_key.values(), key=lambda r: (r["ticker"], r["date"]))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PRICE_COLUMNS)
        w.writeheader()
        w.writerows(all_rows)

    usable = cached + fetched
    return {
        "path": out_path,
        "provider": provider_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "tickers_requested": len(tickers),
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
        "no_data_sample": no_data[:50],
        "errors_sample": errors[:20],
        "resume_policy": "existing ticker rows are reused unless --validation-price-force is set",
    }
