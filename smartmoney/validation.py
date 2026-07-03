"""
Offline validation utilities for Confluence v1.

This module does not fetch prices, filings, mappings or market data. It verifies and
summarises a point-in-time feature table that was built elsewhere, then computes the
minimum rank metrics required by the public validation protocol.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
from collections import Counter
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Callable, Iterable

from .backtest import hit_rate, quantile_spread, spearman_ic
from .research import (
    CONFLUENCE_VERSION,
    FEATURE_SCHEMA_VERSION,
    WEIGHT_VERSION,
    confluence_v1_spec,
)

SPLITS = confluence_v1_spec("validation")["splits"]
HORIZONS = (20, 60, 120)

IDENTITY_COLUMNS = (
    "as_of",
    "ticker",
    "score_version",
    "feature_schema_version",
    "weight_version",
    "parameter_hash",
)

REQUIRED_COLUMNS = IDENTITY_COLUMNS + (
    "score",
    "quadrant",
    "forward_return_20d",
    "forward_return_60d",
    "forward_return_120d",
)

RECOMMENDED_COLUMNS = (
    "issuer_name",
    "institutional_score",
    "insider_score",
    "funds_accumulating",
    "funds_trimming",
    "conviction_funds",
    "avg_weight_pct",
    "total_value_usd",
    "open_market_buyers",
    "open_market_buy_value_usd",
    "13f_accession_hash",
    "form4_accession_hash",
    "price_source",
    "execution_timestamp",
    "adjusted_entry_price",
    "adjusted_exit_price",
    "dollar_volume",
    "market_cap",
    "sector",
    "beta",
    "data_quality_flags",
)

BASELINE_COLUMNS = {
    "full_score": "score",
    "insider_only": "insider_score",
    "institutional_only": "institutional_score",
    "fund_count_only": "funds_accumulating",
}

QUADRANT_RANK = {
    "conviction": 4.0,
    "institutional_only": 3.0,
    "insider_only": 2.0,
    "divergent": 1.0,
    "distribution": 0.0,
}


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


def split_for_as_of(value: Any) -> str:
    d = _parse_date(value)
    if d is None:
        return "invalid"
    for name, spec in SPLITS.items():
        start = date.fromisoformat(spec["from"])
        end = date.fromisoformat(spec["to"])
        if start <= d <= end:
            return name
    return "outside"


def sha256_file(path: str) -> str:
    h = sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _split_values(value: Any) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def read_feature_rows(path: str) -> list[dict[str, Any]]:
    """Read a CSV or JSONL feature table into dictionaries."""
    if path.endswith(".jsonl"):
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def dataset_evidence(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    row_count = len(rows)
    feature_scope_counts = Counter(str(row.get("feature_scope") or "unknown") for row in rows)
    quality_flags = Counter(
        flag
        for row in rows
        for flag in _split_values(row.get("data_quality_flags"))
    )
    tickers_with_form4 = {
        str(row.get("ticker") or "").upper()
        for row in rows
        if _split_values(row.get("form4_accessions"))
    }
    tickers_with_buyers = {
        str(row.get("ticker") or "").upper()
        for row in rows
        if (_coerce_float(row.get("open_market_buyers")) or 0.0) > 0
    }
    rows_with_buyers = [
        row for row in rows
        if (_coerce_float(row.get("open_market_buyers")) or 0.0) > 0
    ]
    rows_with_form4 = [row for row in rows if _split_values(row.get("form4_accessions"))]
    total_buy_value = sum(
        _coerce_float(row.get("open_market_buy_value_usd")) or 0.0
        for row in rows
    )
    forward_return_coverage = {}
    for horizon in HORIZONS:
        col = f"forward_return_{horizon}d"
        covered = sum(1 for row in rows if _coerce_float(row.get(col)) is not None)
        forward_return_coverage[col] = {
            "rows": covered,
            "coverage": round(covered / row_count, 6) if row_count else 0.0,
        }
    return {
        "feature_scope_counts": dict(sorted(feature_scope_counts.items())),
        "rows_with_form4_accessions": len(rows_with_form4),
        "tickers_with_form4_accessions": len({t for t in tickers_with_form4 if t}),
        "rows_with_open_market_buyers": len(rows_with_buyers),
        "tickers_with_open_market_buyers": len({t for t in tickers_with_buyers if t}),
        "open_market_buy_value_usd": round(total_buy_value, 2),
        "forward_return_coverage": forward_return_coverage,
        "data_quality_flag_counts": dict(sorted(quality_flags.items())),
    }


def dataset_manifest(path: str, rows: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = list(rows if rows is not None else read_feature_rows(path))
    columns = sorted({k for row in rows for k in row.keys()})
    missing_required = [c for c in REQUIRED_COLUMNS if c not in columns]
    missing_recommended = [c for c in RECOMMENDED_COLUMNS if c not in columns]
    splits = Counter(split_for_as_of(row.get("as_of")) for row in rows)
    tickers = {str(row.get("ticker") or "").upper() for row in rows if row.get("ticker")}
    dates = [_parse_date(row.get("as_of")) for row in rows]
    dates = [d for d in dates if d is not None]

    version_mismatches = []
    row_errors = []
    expected_parameter_hash = confluence_v1_spec("validation")["parameter_hash"]
    for idx, row in enumerate(rows, start=1):
        missing_in_row = set()
        for col in REQUIRED_COLUMNS:
            value = row.get(col)
            if col in columns and (value is None or str(value).strip() == ""):
                missing_in_row.add(col)
                row_errors.append({"row": idx, "field": col, "error": "missing_value"})
        if _parse_date(row.get("as_of")) is None:
            row_errors.append({"row": idx, "field": "as_of", "error": "invalid_date",
                               "value": row.get("as_of")})
        for col in ("score", "forward_return_20d", "forward_return_60d",
                    "forward_return_120d"):
            if col in columns and col not in missing_in_row and _coerce_float(row.get(col)) is None:
                row_errors.append({"row": idx, "field": col, "error": "not_numeric",
                                   "value": row.get(col)})
        if row.get("score_version") not in (None, "", CONFLUENCE_VERSION):
            version_mismatches.append({"row": idx, "field": "score_version",
                                       "value": row.get("score_version")})
        if row.get("feature_schema_version") not in (None, "", FEATURE_SCHEMA_VERSION):
            version_mismatches.append({"row": idx, "field": "feature_schema_version",
                                       "value": row.get("feature_schema_version")})
        if row.get("weight_version") not in (None, "", WEIGHT_VERSION):
            version_mismatches.append({"row": idx, "field": "weight_version",
                                       "value": row.get("weight_version")})
        if row.get("parameter_hash") not in (None, "", expected_parameter_hash):
            version_mismatches.append({"row": idx, "field": "parameter_hash",
                                       "value": row.get("parameter_hash")})

    return {
        "path": os.path.basename(path),
        "sha256": sha256_file(path) if os.path.exists(path) else None,
        "row_count": len(rows),
        "ticker_count": len(tickers),
        "date_range": {
            "from": min(dates).isoformat() if dates else None,
            "to": max(dates).isoformat() if dates else None,
        },
        "split_counts": dict(sorted(splits.items())),
        "columns": columns,
        "evidence": dataset_evidence(rows),
        "required_columns": list(REQUIRED_COLUMNS),
        "missing_required_columns": missing_required,
        "missing_recommended_columns": missing_recommended,
        "row_errors": row_errors[:50],
        "row_error_count": len(row_errors),
        "version_mismatches": version_mismatches[:50],
        "status": "valid_minimum_schema" if rows and not missing_required and not version_mismatches
        and not row_errors
        else "not_publishable",
    }


def _baseline_scores(row: dict[str, Any]) -> dict[str, float]:
    out = {}
    for name, col in BASELINE_COLUMNS.items():
        value = _coerce_float(row.get(col))
        if value is not None:
            out[name] = value
    q = str(row.get("quadrant") or "").strip().lower()
    if q in QUADRANT_RANK:
        out["quadrant_only"] = QUADRANT_RANK[q]
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _bootstrap_ci(scores: list[float], returns: list[float],
                  metric: Callable[[list[float], list[float]], float],
                  *, samples: int = 250, seed: int = 13) -> tuple[float, float]:
    n = len(scores)
    if n < 8:
        return (0.0, 0.0)
    rng = random.Random(seed)
    vals = []
    for _ in range(samples):
        idx = [rng.randrange(n) for _ in range(n)]
        vals.append(metric([scores[i] for i in idx], [returns[i] for i in idx]))
    vals.sort()
    lo = vals[int(0.025 * (len(vals) - 1))]
    hi = vals[int(0.975 * (len(vals) - 1))]
    return (round(lo, 6), round(hi, 6))


def _permutation_pvalue(scores: list[float], returns: list[float], *,
                        samples: int = 500, seed: int = 17) -> float:
    if len(scores) < 8:
        return 1.0
    observed = abs(spearman_ic(scores, returns))
    rng = random.Random(seed)
    count = 0
    shuffled = list(returns)
    for _ in range(samples):
        rng.shuffle(shuffled)
        if abs(spearman_ic(scores, shuffled)) >= observed:
            count += 1
    return round((count + 1) / (samples + 1), 6)


def _metrics(scores: list[float], returns: list[float], q: int = 5) -> dict[str, Any]:
    if not scores or len(scores) != len(returns):
        return {"n": 0, "status": "empty"}
    ic = spearman_ic(scores, returns)
    spread = quantile_spread(scores, returns, q)
    hit = hit_rate(scores, returns, q)
    ic_ci = _bootstrap_ci(scores, returns, spearman_ic)
    spread_ci = _bootstrap_ci(scores, returns, lambda s, r: quantile_spread(s, r, q))
    return {
        "n": len(scores),
        "rank_ic": round(ic, 6),
        "rank_ic_ci95": ic_ci,
        "rank_ic_permutation_p": _permutation_pvalue(scores, returns),
        "top_bottom_spread": round(spread, 6),
        "top_bottom_spread_ci95": spread_ci,
        "hit_rate": round(hit, 6),
        "mean_forward_return": round(_mean(returns), 6),
    }


def validation_report(path: str, *, horizon: int = 60,
                      rows: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    rows = list(rows if rows is not None else read_feature_rows(path))
    manifest = dataset_manifest(path, rows)
    ret_col = f"forward_return_{horizon}d"
    by_split: dict[str, dict[str, tuple[list[float], list[float]]]] = {}

    for row in rows:
        ret = _coerce_float(row.get(ret_col))
        if ret is None:
            continue
        split = split_for_as_of(row.get("as_of"))
        if split in ("invalid", "outside"):
            continue
        for name, score in _baseline_scores(row).items():
            by_split.setdefault(split, {}).setdefault(name, ([], []))
            by_split[split][name][0].append(score)
            by_split[split][name][1].append(ret)

    split_reports = {}
    for split in ("train", "validation", "test"):
        split_reports[split] = {}
        for name, pair in sorted(by_split.get(split, {}).items()):
            split_reports[split][name] = _metrics(pair[0], pair[1])

    evidence = manifest["evidence"]
    notes = [
        "Metrics are descriptive until the dataset builder, price source, costs, "
        "liquidity rules and no-lookahead controls are independently reviewed.",
        "The test split is frozen; do not tune parameters after reading it.",
    ]
    if evidence["feature_scope_counts"].get("13f_form4_joined", 0) == 0:
        notes.append("No joined Form 4 rows are present; this artifact cannot support a "
                     "complete 13F + Form 4 Confluence claim.")
    if evidence["rows_with_open_market_buyers"] == 0:
        notes.append("No row has open-market Form 4 buyers; this artifact only tests "
                     "negative/neutral insider evidence.")
    if manifest["row_count"] < 100:
        notes.append("Sample size is below 100 rows; treat this as a pipeline smoke test, "
                     "not validation evidence.")

    return {
        "protocol": "confluence_v1_validation",
        "status": "not_publishable" if manifest["status"] != "valid_minimum_schema"
        else "minimum_schema_valid_metrics_unreviewed",
        "horizon_days": horizon,
        "manifest": manifest,
        "metrics": split_reports,
        "notes": notes,
    }
