"""
Offline production preflight checks. No network, no EDGAR.
"""

import tempfile
from pathlib import Path

from smartmoney.db import Store
from smartmoney.preflight import run_preflight
from smartmoney.pro import ProAPIStore
from tests.test_db_offline import AAPL, KO, MSFT, _save


def _seed_stable_db(path):
    store = Store(path)
    _save(store, "0000000001", "Stable Operator Fund", "PM", "A1", "13F-HR",
          "2024-05-15", "2024-03-31",
          [("APPLE INC", AAPL, 1_000, 100, "")])
    _save(store, "0000000001", "Stable Operator Fund", "PM", "A2", "13F-HR",
          "2024-08-14", "2024-06-30",
          [("COCA COLA", KO, 1_100, 100, "")])
    _save(store, "0000000001", "Stable Operator Fund", "PM", "A3", "13F-HR",
          "2024-11-14", "2024-09-30",
          [("MICROSOFT", MSFT, 1_050, 100, "")])
    store.close()


def test_preflight_passes_for_stable_market_and_pro_dbs():
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "market.db")
        pro_db = str(Path(d) / "pro.db")
        _seed_stable_db(data_db)
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key("QA Institution")
            pro.audit(key.key_id, "GET", "/api/pro/v1/status", 200, "127.0.0.1", "pytest")

        report = run_preflight(
            data_db,
            pro_db_path=pro_db,
            require_pro=True,
            expected_sha="abc123",
            current_sha="abc123",
            api_token=token,
        )

    assert report["status"] == "pass"
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["deploy.sha"]["status"] == "pass"
    assert checks["market_db.rejects_writes"]["status"] == "pass"
    assert checks["market_db.data_quality"]["data"]["summary"]["unit_scale_candidates"] == 0
    assert checks["pro_api.unauth_challenge"]["status"] == "pass"
    assert checks["pro_api.cache_headers"]["status"] == "pass"
    assert checks["pro_api.rate_limits_configured"]["status"] == "pass"
    assert checks["pro_db.active_keys"]["data"]["active"] == 1
    assert checks["pro_db.audit_recent"]["status"] == "pass"


def test_preflight_fails_on_sha_mismatch_and_missing_required_pro_db():
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "market.db")
        _seed_stable_db(data_db)

        report = run_preflight(
            data_db,
            pro_db_path=str(Path(d) / "missing-pro.db"),
            require_pro=True,
            expected_sha="release-sha",
            current_sha="old-sha",
        )

    assert report["status"] == "fail"
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["deploy.sha"]["status"] == "fail"
    assert checks["pro_db.exists"]["status"] == "fail"
