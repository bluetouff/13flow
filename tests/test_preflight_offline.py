"""
Offline production preflight checks. No network, no EDGAR.
"""

import tempfile
from pathlib import Path

from smartmoney.db import Store
from smartmoney.preflight import (
    deployed_sha_from_systemd,
    run_preflight,
    _public_service_isolation_checks,
    _public_surface_checks,
    _runtime_env_checks,
)
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
    assert checks["public_surface.no_sample_badge"]["status"] == "pass"
    assert checks["public_surface.open_features"]["status"] == "pass"
    assert checks["public_api.funds"]["status"] == "pass"
    assert checks["public_api.live_status"]["status"] == "pass"
    assert checks["public_api.data_quality"]["status"] == "pass"
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


def test_deployed_sha_can_be_read_from_systemd_dropin():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "version.conf"
        path.write_text('[Service]\nEnvironment=SMARTMONEY_GIT_SHA="abc123"\n',
                        encoding="utf-8")

        assert deployed_sha_from_systemd(str(path)) == "abc123"
        assert deployed_sha_from_systemd(str(Path(d) / "missing.conf")) is None


def test_runtime_env_check_requires_db_alignment():
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / "13flow.env"
        expected = str(Path(d) / "market.db")
        env.write_text(
            "SMARTMONEY_OPEN=1\n"
            f"SMARTMONEY_DB={expected}\n"
            "SMARTMONEY_DB_READONLY=1\n",
            encoding="utf-8",
        )

        checks = {c.name: c for c in _runtime_env_checks(expected, str(env))}
        assert checks["runtime_env.db_path"].status == "pass"
        assert checks["runtime_env.db_readonly"].status == "pass"

        checks = {c.name: c for c in _runtime_env_checks("/other.db", str(env))}
        assert checks["runtime_env.db_path"].status == "fail"


def test_runtime_env_check_rejects_ingest_secrets_in_web_env():
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / "13flow-web.env"
        expected = str(Path(d) / "market.db")
        env.write_text(
            "SMARTMONEY_OPEN=1\n"
            f"SMARTMONEY_DB={expected}\n"
            "SMARTMONEY_DB_READONLY=1\n"
            "SEC_UA=13flow operator@example.com\n",
            encoding="utf-8",
        )

        checks = {c.name: c for c in _runtime_env_checks(expected, str(env))}
        assert checks["runtime_env.no_ingest_secrets"].status == "fail"


def test_public_service_isolation_rejects_pro_dropin():
    with tempfile.TemporaryDirectory() as d:
        dropin = Path(d) / "13flow.service.d"
        dropin.mkdir()
        (dropin / "pro-api.conf").write_text(
            "[Service]\n"
            "Environment=SMARTMONEY_PRO_API=1\n"
            "Environment=SMARTMONEY_PRO_DB=/var/lib/13flow-pro/13flow-pro.db\n"
            "ReadWritePaths=/var/lib/13flow-pro\n",
            encoding="utf-8",
        )

        checks = {c.name: c for c in _public_service_isolation_checks(str(dropin))}
        assert checks["runtime_env.public_no_pro_api"].status == "fail"
        assert checks["runtime_env.public_no_pro_db_write"].status == "fail"


def test_public_service_isolation_passes_without_pro_dropin():
    with tempfile.TemporaryDirectory() as d:
        dropin = Path(d) / "13flow.service.d"
        dropin.mkdir()
        (dropin / "version.conf").write_text(
            "[Service]\nEnvironment=SMARTMONEY_GIT_SHA=abc123\n",
            encoding="utf-8",
        )

        checks = {c.name: c for c in _public_service_isolation_checks(str(dropin))}
        assert checks["runtime_env.public_no_pro_api"].status == "pass"
        assert checks["runtime_env.public_no_pro_db_write"].status == "pass"


def test_preflight_fails_if_public_root_exposes_sample_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "market.db")
        _seed_stable_db(data_db)
        dash = Path(d) / "dashboard.html"
        dash.write_text("<!doctype html><html><body>SAMPLE DATA</body></html>", encoding="utf-8")

        import smartmoney.api as api_mod
        monkeypatch.setattr(api_mod, "DASHBOARD", str(dash))

        checks = {c.name: c for c in _public_surface_checks(data_db)}

    assert checks["public_surface.no_sample_badge"].status == "fail"
