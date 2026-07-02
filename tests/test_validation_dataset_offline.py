"""
Offline point-in-time validation dataset builder tests.
"""

import csv
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from smartmoney.db import Store
from smartmoney.validation import validation_report
from smartmoney.validation_dataset import (
    build_validation_rows,
    forward_returns,
    load_adjusted_prices,
    write_validation_dataset,
)
from tests.test_db_offline import AAPL, MSFT, NVDA, _save


def _market_db(path: Path) -> None:
    store = Store(str(path))
    try:
        f1, f2 = "0000000001", "0000000002"
        _save(store, f1, "Fund One", "PM1", "A1", "13F-HR", "2024-05-01",
              "2024-03-31", [("APPLE INC", AAPL, 1000, 100, "")])
        _save(store, f2, "Fund Two", "PM2", "B1", "13F-HR", "2024-05-02",
              "2024-03-31", [("MICROSOFT", MSFT, 1000, 100, "")])
        _save(store, f1, "Fund One", "PM1", "A2", "13F-HR", "2024-08-01",
              "2024-06-30", [("APPLE INC", AAPL, 1100, 100, ""),
                              ("NVIDIA", NVDA, 500, 50, "")])
        _save(store, f2, "Fund Two", "PM2", "B2", "13F-HR", "2024-08-02",
              "2024-06-30", [("MICROSOFT", MSFT, 900, 90, ""),
                              ("NVIDIA", NVDA, 300, 30, "")])
        store.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        store.conn.execute("UPDATE holdings SET ticker='MSFT' WHERE cusip=?", (MSFT,))
        store.conn.execute("UPDATE holdings SET ticker='NVDA' WHERE cusip=?", (NVDA,))
        store.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


def _price_csv(path: Path) -> None:
    start = date(2024, 8, 1)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "date", "adj_close"])
        w.writeheader()
        for i in range(180):
            d = start + timedelta(days=i)
            w.writerow({"ticker": "NVDA", "date": d.isoformat(), "adj_close": 100 + i})
            w.writerow({"ticker": "AAPL", "date": d.isoformat(), "adj_close": 200 + i})
            w.writerow({"ticker": "MSFT", "date": d.isoformat(), "adj_close": 300 - i * 0.1})


def test_forward_returns_from_local_adjusted_price_csv(tmp_path):
    path = tmp_path / "prices.csv"
    _price_csv(path)
    prices = load_adjusted_prices(str(path))
    ret = forward_returns(prices["NVDA"], "2024-08-02", execution_lag_days=1)
    assert ret["execution_timestamp"] == "2024-08-03"
    assert ret["adjusted_entry_price"] == 102
    assert ret["forward_return_20d"] > 0
    assert ret["forward_return_120d"] > ret["forward_return_20d"]


def test_build_validation_dataset_without_prices_is_not_publishable(tmp_path):
    db = tmp_path / "market.db"
    _market_db(db)
    out = tmp_path / "features.csv"

    rows = build_validation_rows(str(db), start="2024-06-30", end="2024-06-30",
                                 code_commit="abc123")
    summary = write_validation_dataset(rows, str(out))

    assert summary["rows"] >= 3
    nvda = next(r for r in rows if r["ticker"] == "NVDA")
    assert nvda["funds_accumulating"] == 2
    assert nvda["13f_accession_hash"]
    assert nvda["forward_return_60d"] == ""

    report = validation_report(str(out))
    assert report["status"] == "not_publishable"
    assert report["manifest"]["row_error_count"] > 0


def test_build_validation_dataset_with_prices_passes_mechanical_gate(tmp_path):
    db = tmp_path / "market.db"
    prices = tmp_path / "prices.csv"
    out = tmp_path / "features.jsonl"
    _market_db(db)
    _price_csv(prices)

    rows = build_validation_rows(str(db), prices_path=str(prices),
                                 start="2024-06-30", end="2024-06-30",
                                 code_commit="abc123")
    write_validation_dataset(rows, str(out), fmt="jsonl")

    report = validation_report(str(out))
    assert report["status"] == "minimum_schema_valid_metrics_unreviewed"
    assert report["manifest"]["row_count"] >= 3
    assert report["metrics"]["validation"]["full_score"]["n"] >= 3
    assert report["manifest"]["missing_recommended_columns"] == []


def test_run_py_build_validation_dataset_json(tmp_path):
    db = tmp_path / "market.db"
    prices = tmp_path / "prices.csv"
    out = tmp_path / "features.csv"
    _market_db(db)
    _price_csv(prices)

    proc = subprocess.run(
        [
            sys.executable, "run.py",
            "--db", str(db),
            "--build-validation-dataset", str(out),
            "--validation-prices", str(prices),
            "--validation-start", "2024-06-30",
            "--validation-end", "2024-06-30",
            "--validation-json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    body = json.loads(proc.stdout)
    assert body["build"]["rows"] >= 3
    assert body["gate"]["status"] == "minimum_schema_valid_metrics_unreviewed"
