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
from typing import Any

from .prices import MassiveProvider, PriceProvider, StooqProvider

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
    return t


def make_price_provider(provider_name: str) -> PriceProvider:
    if provider_name == "massive":
        return MassiveProvider(
            os.environ.get("MASSIVE_API_KEY", ""),
            base_url=os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com"),
        )
    if provider_name == "stooq":
        return StooqProvider()
    raise ValueError("provider must be massive or stooq")


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
) -> dict[str, Any]:
    tickers = read_tickers(tickers_path)
    existing_rows, cached_tickers = ([], set()) if force else _read_existing(out_path)
    new_rows: list[dict[str, str]] = []
    no_data: list[str] = []
    errors: list[dict[str, str]] = []
    fetched = 0
    cached = 0

    for ticker in tickers:
        if ticker in cached_tickers:
            cached += 1
            continue
        symbol = provider_symbol(ticker, provider_name)
        try:
            closes = provider.daily_closes(symbol, start, end)
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
            time.sleep(sleep_sec)

    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda r: (r["ticker"], r["date"]))
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
        "no_data_sample": no_data[:50],
        "errors_sample": errors[:20],
        "resume_policy": "existing ticker rows are reused unless --validation-price-force is set",
    }
