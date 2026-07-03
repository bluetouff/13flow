"""
Offline validation protocol tests. No network, no market-data dependency.
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

from smartmoney.research import (
    CONFLUENCE_VERSION,
    FEATURE_SCHEMA_VERSION,
    WEIGHT_VERSION,
    confluence_v1_spec,
)
from smartmoney.validation import (
    dataset_evidence,
    dataset_manifest,
    evidence_review,
    split_for_as_of,
    validation_report,
)


def _row(i: int, as_of: str, ticker: str, score: float, fwd60: float) -> dict:
    return {
        "as_of": as_of,
        "ticker": ticker,
        "issuer_name": f"Issuer {ticker}",
        "score_version": CONFLUENCE_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "weight_version": WEIGHT_VERSION,
        "parameter_hash": confluence_v1_spec("test")["parameter_hash"],
        "score": score,
        "quadrant": "conviction" if score >= 70 else "distribution",
        "institutional_score": score * 0.8,
        "insider_score": score * 0.6,
        "funds_accumulating": i % 6,
        "feature_scope": "13f_form4_joined",
        "open_market_buyers": 1 if i % 3 == 0 else 0,
        "open_market_buy_value_usd": 100000 if i % 3 == 0 else 0,
        "form4_accessions": f"F4-{ticker}" if i % 3 == 0 else "",
        "forward_return_20d": fwd60 / 3,
        "forward_return_60d": fwd60,
        "forward_return_120d": fwd60 * 2,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def test_split_for_as_of_uses_frozen_calendar():
    assert split_for_as_of("2019-06-30") == "train"
    assert split_for_as_of("2023-05-15T12:00:00+00:00") == "validation"
    assert split_for_as_of("2025-01-02") == "test"
    assert split_for_as_of("bad-date") == "invalid"


def test_validation_manifest_and_metrics(tmp_path):
    rows = []
    for i in range(10):
        rows.append(_row(i, "2019-06-30", f"TR{i}", 10 + i * 8, -0.05 + i * 0.015))
        rows.append(_row(i, "2023-06-30", f"VA{i}", 10 + i * 8, -0.04 + i * 0.012))
        rows.append(_row(i, "2025-06-30", f"TE{i}", 10 + i * 8, -0.03 + i * 0.01))
    path = tmp_path / "features.csv"
    _write_csv(path, rows)

    manifest = dataset_manifest(str(path))
    assert manifest["status"] == "valid_minimum_schema"
    assert manifest["row_count"] == 30
    assert manifest["split_counts"] == {"test": 10, "train": 10, "validation": 10}
    assert manifest["evidence"]["feature_scope_counts"] == {"13f_form4_joined": 30}
    assert manifest["evidence"]["rows_with_open_market_buyers"] == 12
    assert manifest["evidence"]["forward_return_coverage"]["forward_return_60d"]["coverage"] == 1.0
    assert evidence_review(manifest)["status"] == "smoke_passed_needs_larger_sample"
    assert len(manifest["sha256"]) == 64

    report = validation_report(str(path), horizon=60)
    assert report["status"] == "minimum_schema_valid_metrics_unreviewed"
    assert report["evidence_review"]["warnings"] == ["sample_below_100_rows"]
    assert report["metrics"]["train"]["full_score"]["n"] == 10
    assert report["metrics"]["train"]["full_score"]["rank_ic"] > 0.9
    assert "quadrant_only" in report["metrics"]["test"]


def test_dataset_evidence_flags_smoke_without_form4_buyers():
    rows = [
        {
            **_row(i, "2025-06-30", f"TE{i}", 20 + i, 0.01),
            "feature_scope": "13f_form4_joined",
            "open_market_buyers": 0,
            "open_market_buy_value_usd": 0,
            "form4_accessions": "",
            "data_quality_flags": "no_form4_activity_in_window",
        }
        for i in range(3)
    ]

    evidence = dataset_evidence(rows)
    assert evidence["rows_with_open_market_buyers"] == 0
    assert evidence["tickers_with_open_market_buyers"] == 0
    assert evidence["data_quality_flag_counts"] == {"no_form4_activity_in_window": 3}


def test_evidence_review_blocks_samples_without_open_market_buyers(tmp_path):
    rows = [
        {
            **_row(i, "2025-06-30", f"TE{i}", 20 + i, 0.01),
            "open_market_buyers": 0,
            "open_market_buy_value_usd": 0,
            "form4_accessions": f"F4-{i}",
        }
        for i in range(3)
    ]
    path = tmp_path / "features.csv"
    _write_csv(path, rows)

    review = evidence_review(dataset_manifest(str(path)))
    assert review["status"] == "blocked"
    assert "no_open_market_form4_buyers" in review["blockers"]


def test_validation_manifest_refuses_incomplete_jsonl(tmp_path):
    path = tmp_path / "features.jsonl"
    path.write_text(json.dumps({"as_of": "2025-01-01", "ticker": "BAD"}) + "\n",
                    encoding="utf-8")
    report = validation_report(str(path), horizon=60)
    assert report["status"] == "not_publishable"
    assert "score" in report["manifest"]["missing_required_columns"]
    assert "forward_return_120d" in report["manifest"]["missing_required_columns"]


def test_run_py_validation_dataset_json(tmp_path):
    rows = [_row(i, "2025-06-30", f"TE{i}", 20 + i * 7, -0.02 + i * 0.01)
            for i in range(10)]
    path = tmp_path / "features.csv"
    _write_csv(path, rows)

    proc = subprocess.run(
        [sys.executable, "run.py", "--validation-dataset", str(path), "--validation-json"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    body = json.loads(proc.stdout)
    assert body["protocol"] == "confluence_v1_validation"
    assert body["manifest"]["status"] == "valid_minimum_schema"
    assert body["metrics"]["test"]["full_score"]["n"] == 10
