"""
Offline data-quality checks. No network, no EDGAR.
"""

import tempfile
from pathlib import Path

from smartmoney.api import create_app
from smartmoney.db import Store
from smartmoney.quality import data_quality_report, quality_gate_report
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


def test_data_quality_report_flags_stale_funds_and_duplicate_labels():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "surface.db"))
        try:
            _save(store, "0000000001", "Duplicate Fund", "PM1", "A1", "13F-HR",
                  "2024-05-15", "2024-03-31",
                  [("APPLE INC", AAPL, 1_000, 100, "")])
            _save(store, "0000000002", "Duplicate Fund", "PM2", "B1", "13F-HR",
                  "2024-05-15", "2024-03-31",
                  [("COCA COLA", KO, 1_000, 50, "")])
            _save(store, "0000000002", "Duplicate Fund", "PM2", "B2", "13F-HR",
                  "2024-08-14", "2024-06-30",
                  [("COCA COLA", KO, 1_100, 50, "")])
            report = data_quality_report(store, aum_jump_threshold=100, limit=10)
        finally:
            store.close()

    assert report["summary"]["status"] == "review"
    assert report["summary"]["stale_funds"] == 1
    assert report["summary"]["duplicate_labels"] == 1
    assert report["summary"]["review_items"] == 2
    assert report["freshness_warnings"][0]["fund"]["cik"] == "0000000001"
    assert report["freshness_warnings"][0]["latest_dataset_quarter"] == "2024-06-30"
    duplicate = report["duplicate_label_warnings"][0]
    assert duplicate["label"] == "Duplicate Fund"
    assert {f["cik"] for f in duplicate["funds"]} == {"0000000001", "0000000002"}


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


def test_public_quality_surface_filters_legacy_ciks_when_registry_is_present():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "active-surface.db")
        store = Store(db)
        try:
            _save(store, "0001079114", "Greenlight", "David Einhorn", "OLD", "13F-HR",
                  "2024-02-14", "2023-12-31",
                  [("APPLE INC", AAPL, 1_000, 100, "")])
            _save(store, "0001489933", "Greenlight", "David Einhorn", "NEW", "13F-HR",
                  "2024-08-14", "2024-06-30",
                  [("MICROSOFT", MSFT, 2_000, 50, "")])
        finally:
            store.close()

        client = create_app(db, secure_cookies=False, open_mode=True).test_client()

        funds = client.get("/api/funds").get_json()
        assert [f["cik"] for f in funds] == ["0001489933"]

        quality = client.get("/api/data-quality").get_json()
        assert quality["summary"]["funds_scanned"] == 1
        assert quality["summary"]["stale_funds"] == 0
        assert quality["summary"]["duplicate_labels"] == 0


def test_quality_gate_automatically_excludes_untrusted_funds_from_signals():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "gate.db"))
        try:
            _save(store, "0000000001", "Trusted Fund", "PM1", "T1", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("APPLE INC", AAPL, 1_000, 100, "")])
            _save(store, "0000000001", "Trusted Fund", "PM1", "T2", "13F-HR",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, 1_100, 110, "")])

            _save(store, "0000000002", "Stale Fund", "PM2", "S1", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("APPLE INC", AAPL, 1_000, 100, "")])

            _save(store, "0000000003", "Partial Fund", "PM3", "P1", "13F-HR/A",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, 900, 10, "")])

            _save(store, "0000000004", "Jump Fund", "PM4", "J1", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("APPLE INC", AAPL, 1, 1, "")])
            _save(store, "0000000004", "Jump Fund", "PM4", "J2", "13F-HR",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, 200_000, 100, "")])

            gate = quality_gate_report(store)
        finally:
            store.close()

    by_label = {f["label"]: f for f in gate["funds"]}
    assert gate["summary"]["trusted_funds"] == 1
    assert gate["trusted_ciks"] == ["0000000001"]
    assert by_label["Trusted Fund"]["status"] == "trusted"
    assert by_label["Stale Fund"]["status"] == "stale"
    assert by_label["Partial Fund"]["status"] == "quarantined"
    assert by_label["Jump Fund"]["status"] == "quarantined"
    assert {r["code"] for r in by_label["Jump Fund"]["reasons"]} == {"current_aum_jump"}
