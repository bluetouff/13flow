"""
Offline Pro API tests: API-key auth, scopes, persistent rate limits, and audit.
"""

import sqlite3
import tempfile
from pathlib import Path

from smartmoney.api import create_app
from smartmoney.pro import APIKeyError, ProAPIStore
from tests.test_quality_offline import _seed_quality_db


def _client(monkeypatch, tmpdir, *, scopes=("funds:read", "quality:read"),
            rate_per_min=120):
    data_db = str(Path(tmpdir) / "data.db")
    pro_db = str(Path(tmpdir) / "pro.db")
    store = _seed_quality_db(data_db)
    store.close()
    with ProAPIStore(pro_db) as pro:
        token, key = pro.create_key("Test Institution", scopes=scopes,
                                    rate_per_min=rate_per_min, rate_per_day=10000)
    monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
    monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
    app = create_app(data_db, secure_cookies=False, open_mode=True)
    return app.test_client(), token, key, pro_db


def test_pro_api_requires_api_key_and_scope(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d, scopes=("quality:read",))

        assert c.get("/api/pro/v1/data-quality").status_code == 401

        r = c.get("/api/pro/v1/data-quality", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        assert r.get_json()["report"]["summary"]["unit_scale_candidates"] == 1

        forbidden = c.get("/api/pro/v1/funds", headers={"Authorization": "Bearer " + token})
        assert forbidden.status_code == 403
        assert forbidden.get_json()["error"] == "insufficient_scope"

        rows = sqlite3.connect(pro_db).execute("SELECT status FROM api_audit").fetchall()
        assert [r[0] for r in rows] == [401, 200, 403]


def test_pro_api_funds_payload_includes_quality_summary(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)
        r = c.get("/api/pro/v1/funds", headers={"X-13FLOW-Key": token})
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["api"] == "13flow-pro"
        assert payload["quality_summary"]["aum_jump_warnings"] == 2
        assert len(payload["funds"]) == 2
        assert any(f["quality_warnings"] for f in payload["funds"])


def test_pro_api_rate_limit_is_persistent(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d, rate_per_min=1)
        hdr = {"Authorization": "Bearer " + token}
        assert c.get("/api/pro/v1/status", headers=hdr).status_code == 200
        limited = c.get("/api/pro/v1/status", headers=hdr)
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"

        rows = sqlite3.connect(pro_db).execute(
            "SELECT status FROM api_audit ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [200, 429]


def test_pro_api_key_revocation_is_immediate():
    with tempfile.TemporaryDirectory() as d:
        pro_db = str(Path(d) / "pro.db")
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key("Revoke Me")
            assert pro.authenticate(token, "funds:read").key_id == key.key_id
            assert pro.revoke_key(key.key_id) is True
            try:
                pro.authenticate(token, "funds:read")
                assert False, "revoked key must not authenticate"
            except APIKeyError:
                pass
