"""
Offline Pro API tests: API-key auth, scopes, persistent rate limits, and audit.
"""

import csv
import io
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartmoney.api import create_app
from smartmoney.db import Store
from smartmoney.pro import APIKeyError, APIKeyExpired, ProAPIStore
from tests.test_db_offline import AAPL, MSFT, _save
from tests.test_quality_offline import _seed_quality_db


def _client(monkeypatch, tmpdir, *, scopes=("funds:read", "quality:read"),
            rate_per_min=120, max_watchlists=None):
    data_db = str(Path(tmpdir) / "data.db")
    pro_db = str(Path(tmpdir) / "pro.db")
    store = _seed_quality_db(data_db)
    store.close()
    with ProAPIStore(pro_db) as pro:
        token, key = pro.create_key("Test Institution", scopes=scopes,
                                    rate_per_min=rate_per_min, rate_per_day=10000)
    monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
    monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
    if max_watchlists is not None:
        monkeypatch.setenv("SMARTMONEY_PRO_MAX_WATCHLISTS_PER_KEY", str(max_watchlists))
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
        assert r.get_json()["workspace_limits"]["max_watchlists_per_key"] == 50
        assert r.get_json()["workspace_limits"]["max_tickers_per_watchlist"] == 50
        assert r.get_json()["key"]["rotation_due_at"]
        assert r.get_json()["key_lifecycle"]["rotation_due_at"]
        assert r.get_json()["key_lifecycle"]["rotation_required"] is False
        assert r.headers["Cache-Control"] == "private, no-store, max-age=0"
        assert r.headers["Pragma"] == "no-cache"
        assert r.headers["Expires"] == "0"
        vary = {v.strip() for v in r.headers["Vary"].split(",")}
        assert {"Authorization", "X-13FLOW-Key"} <= vary


def test_pro_usage_report_is_customer_safe_and_bounded(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(monkeypatch, d, rate_per_min=120)
        hdr = {
            "Authorization": "Bearer " + token,
            "User-Agent": "Secret Desk Agent/1.0",
            "X-Forwarded-For": "198.51.100.99",
        }

        assert c.get("/api/pro/v1/status", headers=hdr).status_code == 200
        assert c.get("/api/pro/v1/funds", headers=hdr).status_code == 200
        r = c.get("/api/pro/v1/usage?recent_limit=2&route_limit=2", headers=hdr)

        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["api"] == "13flow-pro"
        usage = payload["usage"]
        assert usage["scope"] == "api_key"
        assert usage["key"]["id"] == key.key_id
        assert usage["quota"]["minute"]["limit"] == 120
        assert usage["quota"]["minute"]["used"] >= 3
        assert usage["quota"]["day"]["used"] >= 3
        assert usage["quota"]["month_observed"]["used"] >= 3
        assert usage["audit"]["total"] >= 2
        assert len(usage["recent_requests"]) <= 2
        assert len(usage["routes"]) <= 2
        assert usage["privacy"] == {
            "token_echoed": False,
            "ip_exposed": False,
            "user_agent_exposed": False,
            "payloads_logged": False,
        }
        raw = str(payload)
        assert token not in raw
        assert "198.51.100.99" not in raw
        assert "Secret Desk Agent" not in raw


def test_pro_onboarding_self_diagnostic_redacts_token(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(
            monkeypatch, d, scopes=("funds:read", "quality:read", "workspace:write"),
        )
        r = c.get("/api/pro/v1/onboarding", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["api"] == "13flow-pro"
        assert payload["key"]["id"] == key.key_id
        assert payload["key"]["label"] == "Test Institution"
        assert payload["key"]["rotation_due_at"]
        assert payload["diagnostic"]["status"] == "ready"
        assert payload["diagnostic"]["token_echoed"] is False
        assert payload["diagnostic"]["workspace_enabled"] is True
        assert payload["diagnostic"]["quality_enabled"] is True
        assert payload["diagnostic"]["self_serve_checkout"] is False
        assert payload["key_lifecycle"]["expired_keys_fail_closed"] is True
        assert payload["key_lifecycle"]["rotation_due_at"]
        checks = {item["id"]: item for item in payload["endpoints"]["checks"]}
        assert checks["usage"]["available"] is True
        assert checks["workspace_report"]["available"] is True
        assert checks["workspace_export"]["required_scope"] == "workspace:write"
        assert "validated alpha" in payload["truth_boundary"]["not_claimed"]
        assert "Authorization: Bearer <token>" in payload["security"]["credential_headers"]
        assert payload["security"]["token_in_url_allowed"] is False
        assert token not in str(payload)


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
        assert "/api/pro/v1/onboarding" in doc["paths"]
        assert "/api/pro/v1/usage" in doc["paths"]
        assert "/api/pro/v1/fund/{cik}" in doc["paths"]
        assert "/api/pro/v1/watchlist" in doc["paths"]
        assert "/api/pro/v1/watchlist/discover" in doc["paths"]
        assert "/api/pro/v1/workspace/overview" in doc["paths"]
        assert "/api/pro/v1/workspace/export" in doc["paths"]
        assert "/api/pro/v1/workspace/report" in doc["paths"]
        assert "/api/pro/v1/workspace/activity" in doc["paths"]
        assert "/api/pro/v1/workspace/alerts" in doc["paths"]
        assert "/api/pro/v1/workspace/alerts/{alert_id}" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/delete" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/preview" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/snapshot" in doc["paths"]
        assert "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history" in doc["paths"]
        assert "/api/pro/v1/admin/health" in doc["paths"]
        assert "/api/pro/v1/admin/ops" in doc["paths"]
        assert "/api/pro/v1/admin/pilot-fulfillment" in doc["paths"]


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
        readonly_export = c.get(
            "/api/pro/v1/workspace/export",
            headers={"Authorization": "Bearer " + readonly_token},
        )
        assert readonly_export.status_code == 403
        readonly_report = c.get(
            "/api/pro/v1/workspace/report",
            headers={"Authorization": "Bearer " + readonly_token},
        )
        assert readonly_report.status_code == 403

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

        workspace_report = c.get(
            "/api/pro/v1/workspace/report",
            headers={"Authorization": "Bearer " + token},
        )
        assert workspace_report.status_code == 200
        payload = workspace_report.get_json()
        assert payload["meta"]["deterministic"] is True
        assert payload["meta"]["watchlist_id"] is None
        assert payload["executive_summary"]
        assert payload["summary"]["watchlists"] == 1
        assert payload["top_open_alerts"] == []
        assert payload["watchlists"][0]["watchlist"]["id"] == watchlist_id
        assert payload["watchlists"][0]["latest_snapshot"]["id"] == second_snapshot.get_json()["snapshot"]["id"]
        assert "signals" not in payload["watchlists"][0]["latest_snapshot"]
        assert payload["watchlists"][0]["delta"]["baseline_snapshot_id"] == snapshot.get_json()["snapshot"]["id"]
        assert payload["watchlists"][0]["summary_lines"]
        assert payload["watchlists"][0]["top_alerts"][0]["ticker"] == first_alert["ticker"]
        assert payload["watchlists"][0]["top_signals"]

        scoped_report = c.get(
            f"/api/pro/v1/workspace/report?watchlist_id={watchlist_id}",
            headers={"Authorization": "Bearer " + token},
        )
        assert scoped_report.status_code == 200
        payload = scoped_report.get_json()
        assert payload["meta"]["watchlist_id"] == watchlist_id
        assert len(payload["watchlists"]) == 1

        other_report = c.get(
            f"/api/pro/v1/workspace/report?watchlist_id={watchlist_id}",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert other_report.status_code == 404

        workspace_export = c.get(
            "/api/pro/v1/workspace/export",
            headers={"Authorization": "Bearer " + token},
        )
        assert workspace_export.status_code == 200
        payload = workspace_export.get_json()
        assert payload["meta"]["format"] == "json"
        assert payload["meta"]["limits"]["watchlists"] == 50
        assert payload["summary"]["watchlists"] == 1
        assert payload["watchlists"][0]["watchlist"]["id"] == watchlist_id
        assert payload["watchlists"][0]["latest_snapshot"]["id"] == second_snapshot.get_json()["snapshot"]["id"]
        assert "signals" not in payload["watchlists"][0]["latest_snapshot"]
        assert payload["watchlists"][0]["alerts"]
        assert payload["watchlists"][0]["alerts"][0]["ticker"] == first_alert["ticker"]

        workspace_export_with_signals = c.get(
            "/api/pro/v1/workspace/export?include_signals=1",
            headers={"Authorization": "Bearer " + token},
        )
        assert workspace_export_with_signals.status_code == 200
        payload = workspace_export_with_signals.get_json()
        assert payload["meta"]["include_signals"] is True
        assert payload["watchlists"][0]["latest_snapshot"]["signals"]["metadata"]["version"] == "saved_watchlist_signals_v1"

        workspace_export_csv = c.get(
            "/api/pro/v1/workspace/export?format=csv",
            headers={"Authorization": "Bearer " + token},
        )
        assert workspace_export_csv.status_code == 200
        assert workspace_export_csv.mimetype == "text/csv"
        assert "attachment" in workspace_export_csv.headers["Content-Disposition"]
        rows = list(csv.DictReader(io.StringIO(workspace_export_csv.get_data(as_text=True))))
        assert rows
        assert rows[0]["watchlist_id"] == watchlist_id
        assert rows[0]["alert_ticker"] == first_alert["ticker"]

        bad_export = c.get(
            "/api/pro/v1/workspace/export?format=xlsx",
            headers={"Authorization": "Bearer " + token},
        )
        assert bad_export.status_code == 400

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

        deleted = c.post(
            f"/api/pro/v1/workspace/watchlists/{watchlist_id}/delete",
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


def test_pro_workspace_enforces_storage_limits_and_id_shape(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        c, token, key, pro_db = _client(
            monkeypatch, d, scopes=("funds:read", "workspace:write"), max_watchlists=1,
        )
        hdr = {"Authorization": "Bearer " + token}
        body = {
            "name": "One slot",
            "tickers": ["AAPL"],
            "filters": {},
            "alert_policy": {"enabled": False, "frequency": "manual"},
            "notes": "",
        }

        first = c.post("/api/pro/v1/workspace/watchlists", headers=hdr, json=body)
        assert first.status_code == 201
        assert first.get_json()["watchlist"]["id"]

        limited = c.post(
            "/api/pro/v1/workspace/watchlists",
            headers=hdr,
            json={**body, "name": "Second slot"},
        )
        assert limited.status_code == 409
        payload = limited.get_json()
        assert payload["error"] == "workspace_quota_exceeded"
        assert payload["workspace_limits"]["max_watchlists_per_key"] == 1

        bad_id = c.get("/api/pro/v1/workspace/watchlists/not-a-watchlist-id", headers=hdr)
        assert bad_id.status_code == 400

        bad_filter = c.get(
            "/api/pro/v1/workspace/alerts?watchlist_id=../bad",
            headers=hdr,
        )
        assert bad_filter.status_code == 400


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


def test_pro_admin_health_requires_admin_scope_and_redacts_secrets(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "admin-data.db")
        pro_db = str(Path(d) / "admin-pro.db")
        store = _seed_quality_db(data_db)
        store.close()
        with ProAPIStore(pro_db) as pro:
            workspace_token, workspace_key = pro.create_key(
                "Workspace only", scopes=("funds:read", "workspace:write"),
                rate_per_min=120, rate_per_day=10000, rotation_days=-1,
            )
            admin_token, admin_key = pro.create_key(
                "Ops admin", scopes=("admin:read",),
                rate_per_min=120, rate_per_day=10000,
            )
            pro.audit(workspace_key.key_id, "GET", "/api/pro/v1/status", 200,
                      "127.0.0.1", "pytest")
            pro.audit(workspace_key.key_id, "GET", "/api/pro/v1/funds", 500,
                      "127.0.0.1", "pytest")
            pro.create_watchlist(
                workspace_key.key_id,
                "Scheduled ops monitor",
                ["AAPL"],
                alert_policy={"enabled": True, "frequency": "daily"},
            )
        monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
        monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
        c = create_app(data_db, secure_cookies=False, open_mode=True).test_client()

        forbidden = c.get(
            "/api/pro/v1/admin/health",
            headers={"Authorization": "Bearer " + workspace_token},
        )
        assert forbidden.status_code == 403
        forbidden_ops = c.get(
            "/api/pro/v1/admin/ops",
            headers={"Authorization": "Bearer " + workspace_token},
        )
        assert forbidden_ops.status_code == 403
        forbidden_fulfillment = c.get(
            "/api/pro/v1/admin/pilot-fulfillment",
            headers={"Authorization": "Bearer " + workspace_token},
        )
        assert forbidden_fulfillment.status_code == 403

        ok = c.get(
            "/api/pro/v1/admin/health",
            headers={"Authorization": "Bearer " + admin_token},
        )
        assert ok.status_code == 200
        payload = ok.get_json()
        assert payload["meta"]["admin_key_id"] == admin_key.key_id
        assert payload["meta"]["scope"] == "admin:read"
        health = payload["health"]
        assert health["keys"]["active"] == 2
        assert health["keys"]["rotation_due"] == 1
        assert any("rotation" in warning for warning in health["warnings"])
        recent = {item["id"]: item for item in health["keys"]["recent"]}
        assert recent[workspace_key.key_id]["rotation_due"] is True
        assert recent[workspace_key.key_id]["rotation_due_at"]
        assert recent[workspace_key.key_id]["expires_at"] is None
        assert health["audit"]["server_errors"] == 1
        assert health["operator_events"]["api_key_created"] == 2
        assert health["operator_events"]["privacy"]["tokens_stored"] is False
        assert health["external_checks"]["collected_by_web_process"] is False
        assert "13flow-pro-backup.timer" in health["external_checks"]["expected_units"]
        body = ok.get_data(as_text=True)
        assert admin_token not in body
        assert "key_hash" not in body
        assert "127.0.0.1" not in body
        assert "pytest" not in body

        ops_response = c.get(
            "/api/pro/v1/admin/ops",
            headers={"Authorization": "Bearer " + admin_token},
        )
        assert ops_response.status_code == 200
        ops_payload = ops_response.get_json()
        assert ops_payload["meta"]["admin_key_id"] == admin_key.key_id
        assert ops_payload["meta"]["scope"] == "admin:read"
        assert ops_payload["meta"]["read_only"] is True
        ops = ops_payload["ops"]
        assert ops["status"] in {"ok", "warn", "critical"}
        assert ops["verdict"]["status"] == ops["status"]
        assert ops["public_data"]["public_state"] == "LIVE"
        assert "trusted_funds" in ops["public_data"]["quality_summary"]
        assert "no trusted funds available for signals" in ops["verdict"]["critical"]
        assert ops["pro_control_plane"]["keys"]["active"] == 2
        assert ops["workspace_automation"]["scheduled_watchlists"] == 1
        assert ops["workspace_automation"]["due_count"] == 1
        assert ops["workspace_automation"]["due_sample"][0]["key_id"] == workspace_key.key_id
        assert ops["service_contracts"]["read_only_web_worker_shell_checks"] is False
        assert ops["backup"]["restore_verify_by_web_process"] is False
        assert ops["privacy"] == {
            "tokens_exposed": False,
            "key_hashes_exposed": False,
            "audit_ips_exposed": False,
            "audit_user_agents_exposed": False,
            "payloads_logged": False,
        }
        ops_body = ops_response.get_data(as_text=True)
        assert admin_token not in ops_body
        assert '"key_hash":' not in ops_body
        assert "127.0.0.1" not in ops_body
        assert "pytest" not in ops_body

        fulfillment_response = c.get(
            "/api/pro/v1/admin/pilot-fulfillment",
            headers={"Authorization": "Bearer " + admin_token},
        )
        assert fulfillment_response.status_code == 200
        fulfillment_payload = fulfillment_response.get_json()
        assert fulfillment_payload["meta"]["admin_key_id"] == admin_key.key_id
        assert fulfillment_payload["meta"]["scope"] == "admin:read"
        assert fulfillment_payload["meta"]["read_only"] is True
        fulfillment = fulfillment_payload["pilot_fulfillment"]
        assert fulfillment["read_only"] is True
        assert fulfillment["web_worker_creates_tokens"] is False
        assert fulfillment["tokens_exposed"] is False
        assert fulfillment["secrets_exposed"] is False
        assert fulfillment["operator_events"]["api_key_created"] == 2
        assert fulfillment["operator_events"]["privacy"]["tokens_stored"] is False
        assert fulfillment["least_privilege_policy"]["customer_forbidden_scopes"] == ["admin:read"]
        assert "admin:read" not in fulfillment["least_privilege_policy"]["default_customer_scopes"]
        assert fulfillment["default_limits"]["expires_days"] == 30
        assert fulfillment["default_limits"]["rotation_days"] == 21
        assert "--create-api-key" in fulfillment["operator_commands"]["create_bounded_pilot_key"]
        assert "--api-key-scopes funds:read,quality:read,workspace:write" in fulfillment["operator_commands"]["create_bounded_pilot_key"]
        assert "--list-operator-events" in fulfillment["operator_commands"]["list_operator_events"]
        assert "smoke-pro-key-lifecycle.sh" in fulfillment["operator_commands"]["run_key_lifecycle_smoke"]
        assert any("key lifecycle smoke" in item for item in fulfillment["checklist"]["before_issue"])
        assert "13flow_live_" not in str(fulfillment)
        assert "<issued_token>" in fulfillment["operator_commands"]["verify_issued_key_status"]
        assert "organization" in fulfillment["intake_boundary"]["required_fields"]
        assert "Record key id" in " ".join(fulfillment["checklist"]["issue"])
        fulfillment_body = fulfillment_response.get_data(as_text=True)
        assert admin_token not in fulfillment_body
        assert '"key_hash":' not in fulfillment_body
        assert "127.0.0.1" not in fulfillment_body
        assert "pytest" not in fulfillment_body


def test_pro_admin_ops_treats_stale_only_quality_gate_as_notice(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "ops-stale-data.db")
        pro_db = str(Path(d) / "ops-stale-pro.db")
        store = Store(data_db)
        try:
            _save(store, "0000000001", "Trusted Fund", "PM1", "T1", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("APPLE INC", AAPL, 1_000, 100, "")])
            _save(store, "0000000001", "Trusted Fund", "PM1", "T2", "13F-HR",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, 1_100, 110, "")])
            _save(store, "0000000002", "Stale Fund", "PM2", "S1", "13F-HR",
                  "2025-11-14", "2025-09-30",
                  [("MICROSOFT", MSFT, 1_000, 50, "")])
        finally:
            store.close()
        with ProAPIStore(pro_db) as pro:
            admin_token, admin_key = pro.create_key(
                "Ops admin", scopes=("admin:read",),
                rate_per_min=120, rate_per_day=10000,
            )
        monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
        monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
        c = create_app(data_db, secure_cookies=False, open_mode=True).test_client()

        r = c.get(
            "/api/pro/v1/admin/ops",
            headers={"Authorization": "Bearer " + admin_token},
        )
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["meta"]["admin_key_id"] == admin_key.key_id
        ops = payload["ops"]
        assert ops["status"] == "ok"
        assert ops["public_data"]["quality_summary"]["quality_gate_status"] == "gated"
        assert ops["public_data"]["quality_summary"]["trusted_funds"] == 1
        assert ops["public_data"]["quality_summary"]["signal_eligible_funds"] == 1
        assert ops["public_data"]["quality_summary"]["stale_funds"] == 1
        assert ops["public_data"]["quality_summary"]["degraded_funds"] == 0
        assert ops["public_data"]["quality_summary"]["quarantined_funds"] == 0
        assert ops["verdict"]["critical"] == []
        assert ops["verdict"]["warnings"] == []
        assert ops["verdict"]["notices"] == [
            "quality gate is gated because stale funds are excluded fail-closed"
        ]
        assert "Check /api/data-quality and keep quality disclosures visible." in (
            ops["verdict"]["operator_actions"]
        )


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


def test_pro_operator_events_track_key_lifecycle_without_secrets():
    with tempfile.TemporaryDirectory() as d:
        pro_db = str(Path(d) / "pro.db")
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key(
                "Pilot Buyer",
                scopes=("funds:read", "quality:read", "workspace:write"),
                expires_days=30,
                rotation_days=21,
            )
            assert pro.revoke_key(key.key_id) is True
            events = pro.list_operator_events(limit=10)
            assert [e["event_type"] for e in events] == ["api_key.revoked", "api_key.created"]
            assert events[0]["key_id"] == key.key_id
            assert events[0]["label"] == "Pilot Buyer"
            assert events[0]["actor"] == "cli"
            assert events[0]["detail"]["scopes"] == ["funds:read", "quality:read", "workspace:write"]
            assert events[0]["detail"]["token_stored"] is False
            assert events[0]["detail"]["token_hash_exposed"] is False
            raw = str(events)
            assert token not in raw
            assert "key_hash" not in raw
            health = pro.admin_health()
            assert health["operator_events"]["total"] == 2
            assert health["operator_events"]["api_key_created"] == 1
            assert health["operator_events"]["api_key_revoked"] == 1
            assert health["operator_events"]["privacy"] == {
                "tokens_stored": False,
                "token_hashes_exposed": False,
                "ip_exposed": False,
                "user_agent_exposed": False,
            }


def test_pro_api_expired_key_fails_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "expired-data.db")
        pro_db = str(Path(d) / "expired-pro.db")
        store = _seed_quality_db(data_db)
        store.close()
        with ProAPIStore(pro_db) as pro:
            token, key = pro.create_key("Expired", expires_days=-1)
            try:
                pro.authenticate(token, "funds:read")
                assert False, "expired key must not authenticate"
            except APIKeyExpired:
                pass
        monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
        monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_db)
        c = create_app(data_db, secure_cookies=False, open_mode=True).test_client()
        r = c.get("/api/pro/v1/status", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 401
        assert r.get_json()["error"] == "expired_api_key"
        rows = sqlite3.connect(pro_db).execute(
            "SELECT status FROM api_audit ORDER BY id"
        ).fetchall()
        assert rows[-1][0] == 401


def test_pro_db_migrates_rotation_due_column_for_existing_keys():
    with tempfile.TemporaryDirectory() as d:
        pro_db = str(Path(d) / "legacy-pro.db")
        conn = sqlite3.connect(pro_db)
        conn.execute(
            """CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                scopes TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'pro',
                rate_per_min INTEGER NOT NULL DEFAULT 120,
                rate_per_day INTEGER NOT NULL DEFAULT 10000,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                last_used_at TEXT
            )"""
        )
        conn.commit()
        conn.close()
        with ProAPIStore(pro_db) as pro:
            columns = {
                row["name"]
                for row in pro.conn.execute("PRAGMA table_info(api_keys)").fetchall()
            }
            assert "rotation_due_at" in columns


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
