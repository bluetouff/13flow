"""
Data-quality checks over the stored 13F time series.

These checks are deliberately read-only. They surface suspicious data points for
operator or user review instead of rewriting SEC-derived facts automatically.
"""

from __future__ import annotations

import math
from typing import Any


def _active_clause(active_ciks: set[str] | None, prefix: str = "") -> tuple[str, tuple[str, ...]]:
    if not active_ciks:
        return "", ()
    column = f"{prefix}cik" if prefix else "cik"
    placeholders = ",".join("?" for _ in active_ciks)
    return f" AND {column} IN ({placeholders})", tuple(sorted(active_ciks))


def _latest_rows_by_fund(store, active_ciks: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    active_sql, active_args = _active_clause(active_ciks, "f.")
    rows = store.conn.execute(
        f"""
        SELECT f.cik, fn.label, f.accession, f.report_date, f.filing_date,
               f.form, f.total_value, f.n_positions
        FROM latest_filings lf
        JOIN filings f ON f.accession = lf.accession
        JOIN funds fn ON fn.cik = f.cik
        WHERE 1=1 {active_sql}
        ORDER BY fn.label, f.report_date
        """,
        active_args,
    ).fetchall()
    by_fund: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        r = dict(row)
        by_fund.setdefault(r["cik"], []).append(r)
    return by_fund


def _filing_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_date": row["report_date"],
        "filing_date": row["filing_date"],
        "accession": row["accession"],
        "form": row["form"],
        "total_value": row["total_value"],
        "n_positions": row["n_positions"],
    }


def _fund_rows(store, active_ciks: set[str] | None = None) -> list[dict[str, Any]]:
    active_sql, active_args = _active_clause(active_ciks)
    rows = store.conn.execute(
        f"SELECT cik, label, manager FROM funds WHERE 1=1 {active_sql} ORDER BY label, cik",
        active_args,
    ).fetchall()
    return [dict(r) for r in rows]


def _current_rows_by_fund(store, active_ciks: set[str] | None = None) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for cik, seq in _latest_rows_by_fund(store, active_ciks).items():
        if seq:
            current[cik] = seq[-1]
    return current


def _severity(ratio: float) -> str:
    if ratio >= 1000:
        return "critical"
    if ratio >= 250:
        return "high"
    return "review"


def aum_jump_warnings(
    store,
    threshold: float = 100.0,
    active_ciks: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Adjacent-quarter AUM discontinuities above ``threshold``.

    A warning is not a correction candidate by itself. It can be a partial
    historical series, a confidential treatment artifact, a real mandate change,
    or a unit issue that needs corroboration.
    """
    threshold = max(float(threshold), 1.0)
    warnings: list[dict[str, Any]] = []
    for cik, seq in _latest_rows_by_fund(store, active_ciks).items():
        if len(seq) < 2:
            continue
        for prev, curr in zip(seq, seq[1:]):
            pv = prev["total_value"] or 0
            cv = curr["total_value"] or 0
            if pv <= 0 or cv <= 0:
                continue
            ratio = max(cv / pv, pv / cv)
            if ratio < threshold:
                continue
            warnings.append({
                "type": "aum_jump",
                "severity": _severity(ratio),
                "fund": {"cik": cik, "label": curr["label"]},
                "direction": "up" if cv > pv else "down",
                "ratio": ratio,
                "from": _filing_payload(prev),
                "to": _filing_payload(curr),
                "status": "review_required",
            })
    warnings.sort(key=lambda w: w["ratio"], reverse=True)
    return warnings


def stale_fund_warnings(store, active_ciks: set[str] | None = None) -> list[dict[str, Any]]:
    """Funds whose latest selected filing lags the dataset's latest 13F quarter."""
    dates = [
        r["report_date"] for r in store.conn.execute(
            "SELECT DISTINCT report_date FROM latest_filings ORDER BY report_date"
        ).fetchall()
    ]
    if not dates:
        return []
    latest_date = dates[-1]
    date_rank = {d: i for i, d in enumerate(dates)}
    current = _current_rows_by_fund(store, active_ciks)
    warnings: list[dict[str, Any]] = []
    for fund in _fund_rows(store, active_ciks):
        row = current.get(fund["cik"])
        if row is None:
            warnings.append({
                "type": "stale_fund",
                "severity": "high",
                "fund": {
                    "cik": fund["cik"],
                    "label": fund["label"],
                    "manager": fund.get("manager"),
                },
                "latest_dataset_quarter": latest_date,
                "latest_fund_quarter": None,
                "quarters_behind": len(dates),
                "filing": None,
                "status": "review_required",
            })
            continue
        if row["report_date"] == latest_date:
            continue
        warnings.append({
            "type": "stale_fund",
            "severity": "high",
            "fund": {
                "cik": fund["cik"],
                "label": fund["label"],
                "manager": fund.get("manager"),
            },
            "latest_dataset_quarter": latest_date,
            "latest_fund_quarter": row["report_date"],
            "quarters_behind": date_rank[latest_date] - date_rank.get(row["report_date"], -1),
            "filing": _filing_payload(row),
            "status": "review_required",
        })
    return warnings


def duplicate_label_warnings(store, active_ciks: set[str] | None = None) -> list[dict[str, Any]]:
    """Labels that map to several CIKs on the public surface."""
    current = _current_rows_by_fund(store, active_ciks)
    groups: dict[str, list[dict[str, Any]]] = {}
    for fund in _fund_rows(store, active_ciks):
        key = " ".join(str(fund["label"] or "").lower().split())
        if not key:
            continue
        groups.setdefault(key, []).append(fund)

    warnings: list[dict[str, Any]] = []
    for key, funds in groups.items():
        if len(funds) < 2:
            continue
        latest_quarters = [
            current[f["cik"]]["report_date"] for f in funds if f["cik"] in current
        ]
        warnings.append({
            "type": "duplicate_label",
            "severity": "high",
            "label": funds[0]["label"],
            "normalized_label": key,
            "funds": [
                {
                    "cik": f["cik"],
                    "label": f["label"],
                    "manager": f.get("manager"),
                    "filing": (
                        _filing_payload(current[f["cik"]])
                        if f["cik"] in current else None
                    ),
                }
                for f in funds
            ],
            "latest_quarters": sorted(set(latest_quarters)),
            "status": "review_required",
        })
    warnings.sort(key=lambda w: (w["label"], w["normalized_label"]))
    return warnings


def unit_scale_candidates(
    store,
    low: float = 800.0,
    high: float = 1200.0,
    max_neighbor_ratio: float = 5.0,
    active_ciks: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Strict +/-1000 unit candidates with comparable neighbors.

    This is intentionally narrow. It flags a point only when both neighboring
    quarters exist, the neighbors are broadly comparable, and the middle point is
    about 1000x away from their geometric mean.
    """
    candidates: list[dict[str, Any]] = []
    for cik, seq in _latest_rows_by_fund(store, active_ciks).items():
        if len(seq) < 3:
            continue
        for i in range(1, len(seq) - 1):
            prev, curr, nxt = seq[i - 1], seq[i], seq[i + 1]
            pv = prev["total_value"] or 0
            cv = curr["total_value"] or 0
            nv = nxt["total_value"] or 0
            if pv <= 0 or cv <= 0 or nv <= 0:
                continue
            neighbor_ratio = max(pv / nv, nv / pv)
            if neighbor_ratio > max_neighbor_ratio:
                continue
            baseline = math.sqrt(pv * nv)
            ratio = cv / baseline
            if 1 / high <= ratio <= 1 / low:
                action = "MULTIPLY_1000"
            elif low <= ratio <= high:
                action = "DIVIDE_1000"
            else:
                continue
            candidates.append({
                "type": "unit_scale_candidate",
                "action": action,
                "fund": {"cik": cik, "label": curr["label"]},
                "ratio_to_neighbor_geomean": ratio,
                "neighbor_ratio": neighbor_ratio,
                "previous": _filing_payload(prev),
                "current": _filing_payload(curr),
                "next": _filing_payload(nxt),
                "status": "operator_review_required",
            })
    candidates.sort(key=lambda c: abs(math.log(c["ratio_to_neighbor_geomean"])), reverse=True)
    return candidates


def data_quality_report(
    store,
    aum_jump_threshold: float = 100.0,
    limit: int = 100,
    active_ciks: set[str] | None = None,
) -> dict[str, Any]:
    by_fund = _latest_rows_by_fund(store, active_ciks)
    warnings = aum_jump_warnings(store, threshold=aum_jump_threshold, active_ciks=active_ciks)
    stale = stale_fund_warnings(store, active_ciks=active_ciks)
    duplicates = duplicate_label_warnings(store, active_ciks=active_ciks)
    candidates = unit_scale_candidates(store, active_ciks=active_ciks)
    limit = max(1, min(int(limit), 500))
    review_items = len(warnings) + len(stale) + len(duplicates) + len(candidates)
    return {
        "summary": {
            "status": "review" if review_items else "ok",
            "funds_scanned": len(by_fund),
            "series_points": sum(len(seq) for seq in by_fund.values()),
            "aum_jump_warnings": len(warnings),
            "stale_funds": len(stale),
            "duplicate_labels": len(duplicates),
            "unit_scale_candidates": len(candidates),
            "review_items": review_items,
        },
        "parameters": {
            "aum_jump_threshold": float(aum_jump_threshold),
            "limit": limit,
        },
        "warnings": warnings[:limit],
        "freshness_warnings": stale[:limit],
        "duplicate_label_warnings": duplicates[:limit],
        "unit_scale_candidates": candidates[:limit],
        "notes": [
            "Warnings are read-only data-quality signals, not automatic corrections.",
            "Stale funds and duplicate labels are public-surface quality issues for Pro review.",
            "Unit-scale candidates require operator review before any DB repair.",
        ],
    }
