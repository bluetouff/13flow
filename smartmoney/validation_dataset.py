"""
Point-in-time Confluence validation dataset builder.

The builder is deliberately offline. It reads the local 13F SQLite snapshot and, when
provided, a local adjusted-price CSV. It never fetches EDGAR or prices itself; provenance
belongs in the exported rows and in the validation manifest.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from .crosssignal import InstitutionalSignal, InsiderActivity, score_confluence
from .db import Store
from .diff import Move, diff_portfolios
from .research import (
    CONFLUENCE_VERSION,
    FEATURE_SCHEMA_VERSION,
    WEIGHT_VERSION,
    confluence_v1_spec,
    current_git_sha,
    stable_json_hash,
    utc_now_iso,
)

HORIZONS = (20, 60, 120)
PRICEABLE_TICKER_RE = re.compile(r"^[A-Z]{1,5}([./][A-Z]{1,2})?$")
NON_PRICEABLE_TITLE_HINTS = (
    "NOTE", "NOTES", "NT ", "BOND", "DEBENTURE", "DEB ",
    "CONV", "CONVERTIBLE", "PFD", "PREF", "PREFERRED",
    "WARRANT", "WRT", "RIGHT", "UNIT",
)
CURRENCY_SUFFIXES = ("USD", "EUR", "CHF", "GBP")

EXPORT_COLUMNS = [
    "as_of",
    "report_date",
    "ticker",
    "issuer_name",
    "score_version",
    "feature_schema_version",
    "weight_version",
    "parameter_hash",
    "code_commit",
    "feature_scope",
    "score",
    "quadrant",
    "institutional_score",
    "insider_score",
    "funds_accumulating",
    "funds_trimming",
    "net_funds",
    "conviction_funds",
    "avg_weight_pct",
    "total_value_usd",
    "fund_labels",
    "fund_moves",
    "open_market_buyers",
    "open_market_buy_value_usd",
    "13f_accession_hash",
    "13f_accessions",
    "form4_accession_hash",
    "form4_accessions",
    "price_source",
    "execution_timestamp",
    "adjusted_entry_price",
    "adjusted_exit_price",
    "forward_return_20d",
    "forward_return_60d",
    "forward_return_120d",
    "dollar_volume",
    "market_cap",
    "sector",
    "beta",
    "data_quality_flags",
]


@dataclass(frozen=True)
class PricePoint:
    d: date
    px: float


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def load_adjusted_prices(path: str | None) -> dict[str, list[PricePoint]]:
    """
    Load adjusted prices from CSV with columns:
      ticker,date,adj_close

    Common aliases are accepted for convenience: symbol for ticker and close/price for
    adj_close. Rows with invalid dates or non-positive prices are ignored.
    """
    if not path:
        return {}
    out: dict[str, list[PricePoint]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
            d = _parse_date(row.get("date"))
            px = _as_float(row.get("adj_close") or row.get("adjusted_close")
                           or row.get("close") or row.get("price"))
            if ticker and d and px is not None:
                out[ticker].append(PricePoint(d, px))
    return {t: sorted(points, key=lambda p: p.d) for t, points in out.items()}


def load_ticker_universe(path: str | None) -> set[str] | None:
    if not path:
        return None
    out: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            ticker = line.strip().upper()
            if ticker and not ticker.startswith("#"):
                out.add(ticker)
    return out


def _price_at_or_after(points: list[PricePoint], target: date) -> int | None:
    for i, p in enumerate(points):
        if p.d >= target:
            return i
    return None


def forward_returns(points: list[PricePoint], as_of: str,
                    *, execution_lag_days: int = 1) -> dict[str, Any]:
    d = _parse_date(as_of)
    if not d or not points:
        return {}
    start = _price_at_or_after(points, d)
    if start is None:
        return {}
    entry_idx = start + max(0, execution_lag_days)
    if entry_idx >= len(points):
        return {}
    entry = points[entry_idx]
    out: dict[str, Any] = {
        "execution_timestamp": entry.d.isoformat(),
        "adjusted_entry_price": round(entry.px, 6),
    }
    latest_exit = None
    for h in HORIZONS:
        exit_idx = entry_idx + h
        if exit_idx >= len(points):
            out[f"forward_return_{h}d"] = ""
            continue
        exit_p = points[exit_idx]
        latest_exit = exit_p
        out[f"forward_return_{h}d"] = round((exit_p.px / entry.px) - 1.0, 8)
    if latest_exit is not None:
        out["adjusted_exit_price"] = round(latest_exit.px, 6)
    return out


def _latest_filing_meta(store: Store, cik: str, report_date: str) -> dict[str, Any]:
    row = store.conn.execute(
        """SELECT f.accession, f.filing_date
           FROM latest_filings lf
           JOIN filings f ON f.accession=lf.accession
           WHERE lf.cik=? AND lf.report_date=?""",
        (cik.zfill(10), report_date),
    ).fetchone()
    return dict(row) if row else {"accession": "", "filing_date": ""}


def _quarters(store: Store) -> list[str]:
    rows = store.conn.execute(
        "SELECT DISTINCT report_date FROM latest_filings ORDER BY report_date"
    ).fetchall()
    return [r["report_date"] for r in rows]


def _ciks(store: Store) -> list[str]:
    rows = store.conn.execute("SELECT cik FROM funds ORDER BY cik").fetchall()
    return [r["cik"] for r in rows]


def _split_csv(values: Iterable[str]) -> str:
    return ";".join(v for v in values if v)


def _empty_return_fields() -> dict[str, Any]:
    out = {
        "price_source": "",
        "execution_timestamp": "",
        "adjusted_entry_price": "",
        "adjusted_exit_price": "",
    }
    for h in HORIZONS:
        out[f"forward_return_{h}d"] = ""
    return out


def validation_ticker_flags(ticker: str, issuer: str = "",
                            title_of_class: str = "") -> list[str]:
    """Return non-empty flags for rows that should not enter the default priceable universe."""
    t = str(ticker or "").upper().strip()
    title = str(title_of_class or "").upper()
    flags: list[str] = []
    if not t:
        return ["missing_ticker"]
    if any(t.endswith(sfx) and len(t) > 5 for sfx in CURRENCY_SUFFIXES):
        flags.append("currency_suffixed_ticker")
    if not PRICEABLE_TICKER_RE.match(t):
        flags.append("non_priceable_ticker")
    if any(hint in title for hint in NON_PRICEABLE_TITLE_HINTS):
        flags.append("non_common_equity_title")
    return sorted(set(flags))


def build_validation_rows(
    db_path: str,
    *,
    prices_path: str | None = None,
    start: str | None = None,
    end: str | None = None,
    execution_lag_days: int = 1,
    code_commit: str | None = None,
    include_non_priceable: bool = False,
    ticker_universe_path: str | None = None,
) -> list[dict[str, Any]]:
    prices = load_adjusted_prices(prices_path)
    price_source = f"local_csv:{os.path.basename(prices_path)}" if prices_path else ""
    ticker_universe = load_ticker_universe(ticker_universe_path)
    commit = code_commit or current_git_sha()
    spec = confluence_v1_spec(commit)
    parameter_hash = spec["parameter_hash"]
    rows: list[dict[str, Any]] = []

    with Store(db_path, read_only=True) as store:
        ciks = _ciks(store)
        for report_date in _quarters(store):
            if start and report_date < start:
                continue
            if end and report_date > end:
                continue

            by_ticker: dict[str, dict[str, Any]] = {}
            for cik in ciks:
                curr = store.load_portfolio(cik, report_date)
                if curr is None:
                    continue
                prev_q = store.previous_quarter(cik, report_date)
                prev = store.load_portfolio(cik, prev_q) if prev_q else None
                meta = _latest_filing_meta(store, cik, report_date)
                if prev is None:
                    changes = []
                    for p in curr.positions.values():
                        if not p.put_call:
                            changes.append((Move.NEW, p.cusip, p.ticker, p.issuer,
                                            p.title_of_class, p.value_usd, p.weight))
                else:
                    diff = diff_portfolios(prev, curr)
                    curr_by_cusip = {p.cusip: p for p in curr.positions.values()}
                    prev_by_cusip = {p.cusip: p for p in prev.positions.values()}
                    changes = [
                        (
                            c.move, c.cusip, c.ticker, c.issuer,
                            (curr_by_cusip.get(c.cusip) or prev_by_cusip.get(c.cusip)).title_of_class,
                            c.curr_value, c.curr_weight,
                        )
                        for c in diff.changes if not c.put_call
                    ]

                for move, _cusip, ticker, issuer, title, value_usd, weight in changes:
                    if not ticker:
                        continue
                    t = ticker.upper()
                    if ticker_universe is not None and t not in ticker_universe:
                        continue
                    row_flags = validation_ticker_flags(t, issuer, title)
                    if row_flags and not include_non_priceable:
                        continue
                    slot = by_ticker.setdefault(t, {
                        "ticker": t,
                        "issuer_name": issuer or "",
                        "title_of_class": title or "",
                        "fund_labels": [],
                        "fund_moves": [],
                        "accessions": [],
                        "filing_dates": [],
                        "quality_flags": set(),
                        "funds_accumulating": 0,
                        "funds_trimming": 0,
                        "total_value_usd": 0.0,
                        "weights": [],
                        "conviction_funds": 0,
                    })
                    slot["issuer_name"] = slot["issuer_name"] or issuer or ""
                    slot["title_of_class"] = slot["title_of_class"] or title or ""
                    slot["quality_flags"].update(row_flags)
                    slot["fund_labels"].append(curr.fund_label)
                    slot["fund_moves"].append(move.value)
                    if meta.get("accession"):
                        slot["accessions"].append(meta["accession"])
                    if meta.get("filing_date"):
                        slot["filing_dates"].append(meta["filing_date"])

                    if move in (Move.NEW, Move.ADD):
                        slot["funds_accumulating"] += 1
                        slot["total_value_usd"] += float(value_usd or 0.0)
                        slot["weights"].append(float(weight or 0.0))
                        if move == Move.NEW or (weight or 0.0) >= 0.05:
                            slot["conviction_funds"] += 1
                    elif move in (Move.EXIT, Move.TRIM):
                        slot["funds_trimming"] += 1

            for t, item in sorted(by_ticker.items()):
                as_of = max(item["filing_dates"]) if item["filing_dates"] else report_date
                avg_weight_pct = (
                    sum(item["weights"]) / len(item["weights"]) * 100.0
                    if item["weights"] else 0.0
                )
                inst = InstitutionalSignal(
                    ticker=t,
                    funds_accumulating=int(item["funds_accumulating"]),
                    funds_trimming=int(item["funds_trimming"]),
                    total_value_usd=float(item["total_value_usd"]),
                    fund_labels=tuple(item["fund_labels"]),
                    conviction_funds=int(item["conviction_funds"]),
                    avg_weight_pct=float(avg_weight_pct),
                    quarters_ago=0,
                )
                sig = score_confluence(inst, InsiderActivity(ticker=t))
                accessions = sorted(set(item["accessions"]))
                flags = sorted(set(item["quality_flags"]) | {"insider_features_not_joined"})
                row = {
                    "as_of": as_of,
                    "report_date": report_date,
                    "ticker": t,
                    "issuer_name": item["issuer_name"],
                    "score_version": CONFLUENCE_VERSION,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "weight_version": WEIGHT_VERSION,
                    "parameter_hash": parameter_hash,
                    "code_commit": commit,
                    "feature_scope": "13f_only_no_form4",
                    "score": round(sig.score, 6),
                    "quadrant": sig.quadrant,
                    "institutional_score": round(sig.breakdown.get("institutional", 0.0), 6),
                    "insider_score": round(sig.breakdown.get("insider", 0.0), 6),
                    "funds_accumulating": inst.funds_accumulating,
                    "funds_trimming": inst.funds_trimming,
                    "net_funds": inst.net_funds,
                    "conviction_funds": inst.conviction_funds,
                    "avg_weight_pct": round(inst.avg_weight_pct, 6),
                    "total_value_usd": round(inst.total_value_usd, 2),
                    "fund_labels": _split_csv(item["fund_labels"]),
                    "fund_moves": _split_csv(item["fund_moves"]),
                    "open_market_buyers": 0,
                    "open_market_buy_value_usd": 0.0,
                    "13f_accession_hash": stable_json_hash(accessions),
                    "13f_accessions": _split_csv(accessions),
                    "form4_accession_hash": stable_json_hash([]),
                    "form4_accessions": "",
                    "dollar_volume": "",
                    "market_cap": "",
                    "sector": "",
                    "beta": "",
                    "data_quality_flags": _split_csv(flags),
                }
                returns = _empty_return_fields()
                if t in prices:
                    returns.update(forward_returns(
                        prices[t], as_of, execution_lag_days=execution_lag_days))
                    returns["price_source"] = price_source
                row.update(returns)
                rows.append(row)
    return rows


def write_validation_dataset(rows: list[dict[str, Any]], path: str,
                             *, fmt: str = "csv") -> dict[str, Any]:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({c: row.get(c, "") for c in EXPORT_COLUMNS})
    else:
        raise ValueError("fmt must be csv or jsonl")
    return {
        "path": path,
        "format": fmt,
        "rows": len(rows),
        "generated_at": utc_now_iso(),
        "feature_scope": "13f_only_no_form4",
        "notes": [
            "This dataset builder is offline and currently exports 13F institutional "
            "features only. Form 4 insider features must be joined before full "
            "Confluence validation claims.",
            "Forward returns are populated only when a local adjusted-price CSV is supplied.",
        ],
    }
