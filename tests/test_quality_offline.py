"""
Offline data-quality checks. No network, no EDGAR.
"""

import tempfile
from pathlib import Path

from smartmoney.api import create_app
from smartmoney.db import Store
from smartmoney.quality import data_quality_report
from tests.test_db_offline import AAPL, KO, MSFT, _save


def _seed_quality_db(path):
    store = Store(path)
    _save(store, "0000000001", "Unit Bug Fund", "PM1", "A1", "13F-HR",
          "2024-05-15", "2024-03-31",
          [("APPLE INC", AAPL, 1_000_000, 100, "")])
    _save(store, "0000000001", "Unit Bug Fund", "PM1", "A2", "13F-HR",
          "2024-08-14", "2024-06-30",
          [("APPLE INC", AAPL, 1_000, 100, "")])
    _save(store, "0000000001", "Unit Bug Fund", "PM1", "A3", "13F-HR",
          "2024-11-14", "2024-09-30",
          [("APPLE INC", AAPL, 1_100_000, 100, "")])

    _save(store, "0000000002", "Stable Fund", "PM2", "B1", "13F-HR",
          "2024-05-15", "2024-03-31",
          [("COCA COLA", KO, 1_000, 50, "")])
    _save(store, "0000000002", "Stable Fund", "PM2", "B2", "13F-HR",
          "2024-08-14", "2024-06-30",
          [("MICROSOFT", MSFT, 1_100, 50, "")])
    return store


def test_data_quality_report_flags_aum_jumps_and_strict_unit_candidates():
    with tempfile.TemporaryDirectory() as d:
        store = _seed_quality_db(str(Path(d) / "quality.db"))
        try:
            report = data_quality_report(store, aum_jump_threshold=100, limit=10)
        finally:
            store.close()

    assert report["summary"]["funds_scanned"] == 2
    assert report["summary"]["aum_jump_warnings"] == 2
    assert report["summary"]["unit_scale_candidates"] == 1
    assert {w["fund"]["label"] for w in report["warnings"]} == {"Unit Bug Fund"}

    candidate = report["unit_scale_candidates"][0]
    assert candidate["action"] == "MULTIPLY_1000"
    assert candidate["current"]["accession"] == "A2"
    assert candidate["status"] == "operator_review_required"


def test_data_quality_endpoint_is_public_read_only_and_validates_params():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "quality-api.db")
        store = _seed_quality_db(db)
        store.close()
        client = create_app(db, secure_cookies=False, open_mode=True).test_client()

        r = client.get("/api/data-quality?threshold=100&limit=5")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["summary"]["status"] == "review"
        assert payload["summary"]["unit_scale_candidates"] == 1
        assert len(payload["warnings"]) == 2

        bad = client.get("/api/data-quality?threshold=abc")
        assert bad.status_code == 400 and bad.is_json
