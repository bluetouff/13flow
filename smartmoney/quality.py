"""
Data-quality checks over the stored 13F time series.

These checks are deliberately read-only. They surface suspicious data points for
operator or user review instead of rewriting SEC-derived facts automatically.
"""

from __future__ import annotations

import math
from typing import Any


def _latest_rows_by_fund(store) -> dict[str, list[dict[str, Any]]]:
    rows = store.conn.execute(
        """
        SELECT f.cik, fn.label, f.accession, f.report_date, f.filing_date,
               f.form, f.total_value, f.n_positions
        FROM latest_filings lf
        JOIN filings f ON f.accession = lf.accession
        JOIN funds fn ON fn.cik = f.cik
        ORDER BY fn.label, f.report_date
        """
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


def _severity(ratio: float) -> str:
    if ratio >= 1000:
        return "critical"
    if ratio >= 250:
        return "high"
    return "review"


def aum_jump_warnings(store, threshold: float = 100.0) -> list[dict[str, Any]]:
    """Adjacent-quarter AUM discontinuities above ``threshold``.

    A warning is not a correction candidate by itself. It can be a partial
    historical series, a confidential treatment artifact, a real mandate change,
    or a unit issue that needs corroboration.
    """
    threshold = max(float(threshold), 1.0)
    warnings: list[dict[str, Any]] = []
    for cik, seq in _latest_rows_by_fund(store).items():
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


def unit_scale_candidates(
    store,
    low: float = 800.0,
    high: float = 1200.0,
    max_neighbor_ratio: float = 5.0,
) -> list[dict[str, Any]]:
    """Strict +/-1000 unit candidates with comparable neighbors.

    This is intentionally narrow. It flags a point only when both neighboring
    quarters exist, the neighbors are broadly comparable, and the middle point is
    about 1000x away from their geometric mean.
    """
    candidates: list[dict[str, Any]] = []
    for cik, seq in _latest_rows_by_fund(store).items():
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
) -> dict[str, Any]:
    by_fund = _latest_rows_by_fund(store)
    warnings = aum_jump_warnings(store, threshold=aum_jump_threshold)
    candidates = unit_scale_candidates(store)
    limit = max(1, min(int(limit), 500))
    return {
        "summary": {
            "status": "review" if warnings else "ok",
            "funds_scanned": len(by_fund),
            "series_points": sum(len(seq) for seq in by_fund.values()),
            "aum_jump_warnings": len(warnings),
            "unit_scale_candidates": len(candidates),
        },
        "parameters": {
            "aum_jump_threshold": float(aum_jump_threshold),
            "limit": limit,
        },
        "warnings": warnings[:limit],
        "unit_scale_candidates": candidates[:limit],
        "notes": [
            "Warnings are read-only data-quality signals, not automatic corrections.",
            "Unit-scale candidates require operator review before any DB repair.",
        ],
    }
