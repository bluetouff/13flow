"""
Offline Pro API tests: API-key auth, scopes, persistent rate limits, and audit.
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartmoney.api import create_app
from smartmoney.db import Store
from smartmoney.pro import APIKeyError, ProAPIStore
from tests.test_db_offline import AAPL, MSFT, _save
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


def test_pro_api_responses_are_not_cacheable(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)

        unauth = c.get("/api/pro/v1/status")
        assert unauth.status_code == 401
        assert unauth.headers["WWW-Authenticate"] == 'Bearer realm="13flow-pro"'

        r = c.get("/api/pro/v1/status", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "private, no-store, max-age=0"
        assert r.headers["Pragma"] == "no-cache"
        assert r.headers["Expires"] == "0"
        vary = {v.strip() for v in r.headers["Vary"].split(",")}
        assert {"Authorization", "X-13FLOW-Key"} <= vary


def test_pro_api_fund_detail_is_self_describing(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)
        hdr = {"Authorization": "Bearer " + token}

        r = c.get("/api/pro/v1/fund/1", headers=hdr)
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["methodology"]["source"].startswith("SEC EDGAR")
        assert payload["fund"]["cik"] == "0000000001"
        assert payload["filing"]["accession"] == "A3"
        assert payload["previous_filing"]["accession"] == "A2"
        assert payload["portfolio"]["report_date"] == "2024-09-30"
        assert payload["portfolio"]["positions"][0]["cusip"]
        assert payload["moves"]["previous_report_date"] == "2024-06-30"
        assert payload["moves"]["counts"]["HOLD"] == 1
        assert payload["quality"]["summary"]["fund_warnings"] == 2
        assert "global_stale_funds" in payload["quality"]["summary"]
        assert "global_duplicate_labels" in payload["quality"]["summary"]

        historical = c.get("/api/pro/v1/fund/1?basis=2024-06-30", headers=hdr)
        assert historical.status_code == 200
        assert historical.get_json()["filing"]["accession"] == "A2"

        assert c.get("/api/pro/v1/fund/1?basis=bad-date", headers=hdr).status_code == 400
        assert c.get("/api/pro/v1/fund/not-a-cik", headers=hdr).status_code == 400


def test_pro_api_fund_detail_payload_controls(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)
        hdr = {"Authorization": "Bearer " + token}
        r = c.get("/api/pro/v1/fund/1?include_holds=0&limit_positions=1&limit_moves=2",
                  headers=hdr)
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["request"]["include_holds"] is False
        assert payload["portfolio"]["positions_total"] >= payload["portfolio"]["positions_returned"]
        assert payload["portfolio"]["positions_returned"] == 1
        assert len(payload["portfolio"]["positions"]) == 1
        assert payload["moves"]["changes_total"] >= payload["moves"]["changes_returned"]
        assert payload["moves"]["changes_returned"] <= 2
        assert len(payload["moves"]["changes"]) <= 2
        assert all(c["move"] != "HOLD" for c in payload["moves"]["changes"])

        assert c.get("/api/pro/v1/fund/1?include_holds=wat", headers=hdr).status_code == 400


def test_pro_openapi_document_is_available_when_pro_enabled(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)
        r = c.get("/api/pro/v1/openapi.json")
        assert r.status_code == 200
        doc = r.get_json()
        assert doc["openapi"].startswith("3.")
        assert "/api/pro/v1/fund/{cik}" in doc["paths"]
        assert "/api/pro/v1/watchlist" in doc["paths"]
        assert "/api/pro/v1/watchlist/discover" in doc["paths"]
        assert "/api/pro/v1/workspace/overview" in doc["paths"]
        assert "/api/pro/v1/workspace/activity" in doc["paths"]
        assert "/api/pro/v1/workspace/alerts" in doc["paths"]
        assert "/api/pro/v1/workspace/alerts/{alert_id}" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/preview" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/snapshot" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history" in doc["paths"]


def test_pro_watchlist_feed_uses_ticker_flow(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "watchlist-data.db")
        pro_db = str(Path(d) / "watchlist-pro.db")
        s = Store(data_db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 1_000, 100, "")])
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1_300, 120, ""), ("MICROSOFT", MSFT, 500, 10, "")])
        _save(s, "0001336528", "Pershing Square", "Bill Ackman",
              "PS-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 700, 35, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.execute("UPDATE holdings SET ticker='MSFT' WHERE cusip=?", (MSFT,))
        s.conn.commit()
        s.close()
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key("Watchlist Institution", scopes=("funds:read",),
                                        rate_per_min=120, rate_per_day=10000)
        monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
        monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
        c = create_app(data_db, secure_cookies=False, open_mode=True).test_client()

        r = c.get("/api/pro/v1/watchlist?tickers=AAPL,MSFT", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["api"] == "13flow-pro"
        assert payload["watchlist"]["metadata"]["version"] == "watchlist_preview_v1"
        assert payload["watchlist"]["summary"]["alerts"] >= 1
        tickers = {item["ticker"] for item in payload["watchlist"]["items"]}
        assert tickers == {"AAPL", "MSFT"}

        r = c.get("/api/pro/v1/watchlist/discover?limit=5", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["api"] == "13flow-pro"
        assert payload["watchlist"]["metadata"]["version"] == "watchlist_discovery_v1"
        assert payload["watchlist"]["metadata"]["human_review_required_for_routine_publication"] is False
        assert payload["watchlist"]["metadata"]["quality_gate"]["trusted_funds"] == 2
        assert "quality_gate_detail" in payload["watchlist"]["metadata"]
        discovered = {item["ticker"] for item in payload["watchlist"]["items"]}
        assert {"AAPL", "MSFT"} <= discovered
        assert all("excluded_funds" not in item["quality_gate"] for item in payload["watchlist"]["items"])

        r = c.get(
            "/api/pro/v1/watchlist/discover?limit=5&action=alert&min_score=30&move=NEW&min_buyers=1",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["watchlist"]["metadata"]["filters"]["action"] == ["alert"]
        assert payload["watchlist"]["metadata"]["filters"]["move"] == ["NEW"]
        assert payload["watchlist"]["items"]
        assert all(item["action"] == "alert" for item in payload["watchlist"]["items"])
        assert all("NEW" in item["movement_codes"] for item in payload["watchlist"]["items"])


def test_pro_workspace_watchlists_are_saved_per_api_key(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "workspace-data.db")
        pro_db = str(Path(d) / "workspace-pro.db")
        s = Store(data_db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 1_000, 100, "")])
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1_300, 120, ""), ("MICROSOFT", MSFT, 500, 10, "")])
        _save(s, "0001336528", "Pershing Square", "Bill Ackman",
              "PS-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 700, 35, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.execute("UPDATE holdings SET ticker='MSFT' WHERE cusip=?", (MSFT,))
        s.conn.commit()
        s.close()
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key("Workspace Institution",
                                        scopes=("funds:read", "workspace:write"),
                                        rate_per_min=120, rate_per_day=10000)
            other_token, other_key = pro.create_key("Other Workspace",
                                                    scopes=("funds:read", "workspace:write"),
                                                    rate_per_min=120, rate_per_day=10000)
            readonly_token, _ = pro.create_key("Read Only Institution", scopes=("funds:read",),
                                               rate_per_min=120, rate_per_day=10000)
        monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
        monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
        c = create_app(data_db, secure_cookies=False, open_mode=True).test_client()

        readonly = c.get(
            "/api/pro/v1/workspace/watchlists",
            headers={"Authorization": "Bearer " + readonly_token},
        )
        assert readonly.status_code == 403

        create = c.post(
            "/api/pro/v1/workspace/watchlists",
            headers={"Authorization": "Bearer " + token},
            json={
                "name": "Core tech monitor",
                "tickers": ["AAPL", "MSFT"],
                "filters": {"action": "alert", "min_score": 30, "move": "NEW"},
                "alert_policy": {"enabled": True, "frequency": "daily"},
                "notes": "Pilot desk watchlist",
            },
        )
        assert create.status_code == 201
        created = create.get_json()["watchlist"]
        watchlist_id = created["id"]
        assert created["tickers"] == ["AAPL", "MSFT"]
        assert created["filters"]["action"] == ["alert"]
        assert created["alert_policy"] == {"enabled": True, "frequency": "daily"}

        activity = c.get(
            "/api/pro/v1/workspace/activity?limit=5",
            headers={"Authorization": "Bearer " + token},
        )
        assert activity.status_code == 200
        payload = activity.get_json()
        assert payload["activity"][0]["event_type"] == "watchlist.created"
        assert payload["activity"][0]["entity_id"] == watchlist_id
        assert payload["activity"][0]["detail"]["tickers"] == ["AAPL", "MSFT"]

        listed = c.get(
            "/api/pro/v1/workspace/watchlists",
            headers={"Authorization": "Bearer " + token},
        ).get_json()
        assert listed["meta"]["ui_exposed"] is False
        assert [w["id"] for w in listed["watchlists"]] == [watchlist_id]

        other_get = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert other_get.status_code == 404

        preview = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/preview",
            headers={"Authorization": "Bearer " + token},
        )
        assert preview.status_code == 200
        payload = preview.get_json()
        assert payload["meta"]["saved_watchlist_id"] == watchlist_id
        assert payload["watchlist"]["metadata"]["version"] == "watchlist_preview_v1"
        assert {item["ticker"] for item in payload["watchlist"]["items"]} == {"AAPL", "MSFT"}

        signals = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals",
            headers={"Authorization": "Bearer " + token},
        )
        assert signals.status_code == 200
        payload = signals.get_json()
        assert payload["meta"]["saved_watchlist_id"] == watchlist_id
        assert payload["signals"]["metadata"]["version"] == "saved_watchlist_signals_v1"
        assert payload["signals"]["metadata"]["filters"]["action"] == ["alert"]
        assert payload["signals"]["metadata"]["filters"]["move"] == ["NEW"]
        assert payload["signals"]["metadata"]["filtered_count"] >= len(payload["signals"]["items"])
        assert payload["signals"]["items"]
        assert all(item["action"] == "alert" for item in payload["signals"]["items"])
        assert all("NEW" in item["movement_codes"] for item in payload["signals"]["items"])

        snapshot = c.post(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/snapshot",
            headers={"Authorization": "Bearer " + token},
        )
        assert snapshot.status_code == 201
        payload = snapshot.get_json()
        assert payload["meta"]["saved_watchlist_id"] == watchlist_id
        assert payload["meta"]["history_retention_snapshots"] == 100
        assert payload["snapshot"]["watchlist_id"] == watchlist_id
        assert payload["snapshot"]["summary"]["alerts"] >= 1
        assert payload["snapshot"]["tickers"]
        assert "signals" not in payload["snapshot"]
        assert payload["alerts"]["candidates"] >= 1
        assert payload["alerts"]["open"] >= 1
        assert payload["delta"]["baseline_snapshot_id"] is None
        assert payload["delta"]["previous_count"] == 0
        assert payload["delta"]["current_count"] == len(payload["snapshot"]["tickers"])
        assert payload["delta"]["added_tickers"] == sorted(payload["snapshot"]["tickers"])

        second_snapshot = c.post(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/snapshot",
            headers={"Authorization": "Bearer " + token},
        )
        assert second_snapshot.status_code == 201
        payload = second_snapshot.get_json()
        assert payload["delta"]["baseline_snapshot_id"] == snapshot.get_json()["snapshot"]["id"]
        assert payload["delta"]["added_tickers"] == []
        assert payload["delta"]["removed_tickers"] == []
        assert payload["delta"]["changed_actions"] == []

        snapshot_activity = c.get(
            "/api/pro/v1/workspace/activity?event_type=signals.snapshot",
            headers={"Authorization": "Bearer " + token},
        )
        assert snapshot_activity.status_code == 200
        payload = snapshot_activity.get_json()
        assert len(payload["activity"]) == 2
        assert payload["activity"][0]["detail"]["signal_count"] >= 1
        assert payload["activity"][0]["detail"]["alerts"]["candidates"] >= 1

        alerts = c.get(
            "/api/pro/v1/workspace/alerts?limit=10",
            headers={"Authorization": "Bearer " + token},
        )
        assert alerts.status_code == 200
        payload = alerts.get_json()
        assert payload["summary"]["by_status"]["open"] >= 1
        assert payload["alerts"]
        first_alert = payload["alerts"][0]
        assert first_alert["watchlist_id"] == watchlist_id
        assert first_alert["status"] == "open"
        assert first_alert["action"] in {"alert", "watch"}
        assert first_alert["reason"]["movement_codes"]

        acknowledged = c.patch(
            f"/api/pro/v1/workspace/alerts/{first_alert['id']}",
            headers={"Authorization": "Bearer " + token},
            json={"status": "acknowledged"},
        )
        assert acknowledged.status_code == 200
        payload = acknowledged.get_json()
        assert payload["alert"]["status"] == "acknowledged"
        assert payload["alert"]["acknowledged_at"]

        acknowledged_list = c.get(
            "/api/pro/v1/workspace/alerts?status=acknowledged",
            headers={"Authorization": "Bearer " + token},
        )
        assert acknowledged_list.status_code == 200
        payload = acknowledged_list.get_json()
        assert first_alert["id"] in {a["id"] for a in payload["alerts"]}

        alert_activity = c.get(
            "/api/pro/v1/workspace/activity?event_type=alert.acknowledged",
            headers={"Authorization": "Bearer " + token},
        )
        assert alert_activity.status_code == 200
        payload = alert_activity.get_json()
        assert payload["activity"][0]["entity_id"] == first_alert["id"]
        assert payload["activity"][0]["detail"]["ticker"] == first_alert["ticker"]

        overview = c.get(
            "/api/pro/v1/workspace/overview",
            headers={"Authorization": "Bearer " + token},
        )
        assert overview.status_code == 200
        payload = overview.get_json()
        assert payload["meta"]["automation"] == "manual_snapshot_only"
        assert payload["summary"]["watchlists"] == 1
        assert payload["summary"]["signal_snapshots"] == 2
        assert payload["summary"]["alerts"]["by_status"]["acknowledged"] >= 1
        assert payload["summary"]["activity_events"] >= 4
        assert payload["recent_activity"]
        assert payload["watchlists"][0]["id"] == watchlist_id

        other_alerts = c.get(
            "/api/pro/v1/workspace/alerts?status=all",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert other_alerts.status_code == 200
        assert other_alerts.get_json()["alerts"] == []

        history = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history?limit=1",
            headers={"Authorization": "Bearer " + token},
        )
        assert history.status_code == 200
        payload = history.get_json()
        assert payload["meta"]["include_signals"] is False
        assert len(payload["history"]) == 1
        assert "signals" not in payload["history"][0]

        history_with_signals = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history?limit=2&include_signals=1",
            headers={"Authorization": "Bearer " + token},
        )
        assert history_with_signals.status_code == 200
        payload = history_with_signals.get_json()
        assert payload["meta"]["include_signals"] is True
        assert len(payload["history"]) == 2
        assert payload["history"][0]["signals"]["metadata"]["version"] == "saved_watchlist_signals_v1"

        other_history = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert other_history.status_code == 404

        update = c.put(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}",
            headers={"Authorization": "Bearer " + token},
            json={
                "name": "MSFT only",
                "tickers": ["MSFT"],
                "filters": {"action": "watch"},
                "alert_policy": {"enabled": False, "frequency": "weekly"},
                "notes": "",
            },
        )
        assert update.status_code == 200
        updated = update.get_json()["watchlist"]
        assert updated["name"] == "MSFT only"
        assert updated["tickers"] == ["MSFT"]
        assert updated["alert_policy"] == {"enabled": False, "frequency": "weekly"}

        watchlist_activity = c.get(
            "/api/pro/v1/workspace/activity?entity_type=watchlist&limit=10",
            headers={"Authorization": "Bearer " + token},
        )
        assert watchlist_activity.status_code == 200
        assert "watchlist.updated" in {
            event["event_type"] for event in watchlist_activity.get_json()["activity"]
        }

        deleted = c.delete(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}",
            headers={"Authorization": "Bearer " + token},
        )
        assert deleted.status_code == 200
        assert deleted.get_json()["deleted"] is True
        gone = c.get(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}",
            headers={"Authorization": "Bearer " + token},
        )
        assert gone.status_code == 404

        activity_after_delete = c.get(
            "/api/pro/v1/workspace/activity?entity_type=watchlist&limit=10",
            headers={"Authorization": "Bearer " + token},
        )
        assert activity_after_delete.status_code == 200
        event_types = {event["event_type"] for event in activity_after_delete.get_json()["activity"]}
        assert {"watchlist.created", "watchlist.updated", "watchlist.deleted"} <= event_types

        other_activity = c.get(
            "/api/pro/v1/workspace/activity",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert other_activity.status_code == 200
        assert other_activity.get_json()["activity"] == []


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


def test_pro_api_audit_uses_trusted_proxy_xff(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d)
        hdr = {
            "Authorization": "Bearer " + token,
            "User-Agent": "audit-test",
            "X-Forwarded-For": "198.51.100.10, 203.0.113.99",
        }
        assert c.get("/api/pro/v1/status", headers=hdr).status_code == 200

        row = sqlite3.connect(pro_db).execute(
            "SELECT ip, user_agent FROM api_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row == ("203.0.113.99", "audit-test")


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


def test_pro_api_audit_retention_prunes_only_old_rows():
    with tempfile.TemporaryDirectory() as d:
        pro_db = str(Path(d) / "pro.db")
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds")
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        with ProAPIStore(pro_db) as pro:
            with pro.conn:
                pro.conn.execute(
                    """INSERT INTO api_audit(key_id,method,route,status,ip,user_agent,at)
                       VALUES (?,?,?,?,?,?,?)""",
                    ("old", "GET", "/api/pro/v1/status", 200, "127.0.0.1", "old", old),
                )
                pro.conn.execute(
                    """INSERT INTO api_audit(key_id,method,route,status,ip,user_agent,at)
                       VALUES (?,?,?,?,?,?,?)""",
                    ("recent", "GET", "/api/pro/v1/status", 200, "127.0.0.1", "recent", recent),
                )

            result = pro.prune_audit(30)
            rows = pro.conn.execute(
                "SELECT key_id FROM api_audit ORDER BY id"
            ).fetchall()

    assert result["before"] == 2
    assert result["deleted"] == 1
    assert result["after"] == 1
    assert [r[0] for r in rows] == ["recent"]
