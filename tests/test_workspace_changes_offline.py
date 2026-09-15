from copy import deepcopy

import pytest

from smartmoney.pro import ProAPIStore
from smartmoney.workspace_changes import filing_sources, observation_metadata, signal_delta


def item(ticker="AAPL", action="alert", value=70, accession="0001067983-26-000001"):
    return {
        "ticker": ticker, "action": action, "score": {"score": value},
        "confidence": {"status": "review" if action == "blocked" else "ok"},
        "latest_13f_quarter": "2026-03-31", "movement_summary": {"buyers_count": 2},
        "top_movements": [{"cik": "0001067983", "filing": {"accession": accession}}],
    }


def payload(items, *, filtered=None, scope=None):
    return {
        "items": items if filtered is None else filtered,
        "metadata": {**observation_metadata(items, scope if scope is not None else [i["ticker"] for i in items]),
                     "filters": {"action": ["alert"]}},
    }


def test_delta_is_quiet_when_unchanged_and_explains_sources_without_score_change():
    old = payload([item()])
    snapshot = {"id": "previous", "created_at": "2026-06-01T12:00:00Z", "signals": old}
    assert signal_delta(deepcopy(old), snapshot)["events"] == []
    new = payload([item(accession="0001067983-26-000002")])
    delta = signal_delta(new, snapshot)
    assert delta["baseline_created_at"] == snapshot["created_at"]
    assert delta["changed_scores"] == []
    assert len(delta["events"]) == 1
    assert "SEC filings" in delta["events"][0]["reasons"][0]
    assert delta["events"][0]["sources_after"][0]["url"].endswith("000106798326000002/")


def test_delta_keeps_missing_scores_unknown_and_does_not_claim_filtered_removal_is_sale():
    old = payload([item(value=None)])
    snap = {"id": "old", "signals": old}
    new = payload([item(value=10)])
    assert signal_delta(new, snap)["changed_scores"] == [{"ticker": "AAPL", "from": None, "to": 10}]
    removed = payload([item(action="watch")], filtered=[])
    event = signal_delta(removed, snap)["events"][0]
    assert event["state"] == "resolved"
    assert "filters" in event["reasons"][0]
    missing = signal_delta({"items": []}, snap)["events"][0]
    assert missing["state"] == "unavailable"
    blocked = payload([item(action="blocked")], filtered=[])
    assert signal_delta(blocked, snap)["events"][0]["state"] == "invalidated"
    assert signal_delta(old, None)["events"][0]["state"] == "baseline"
    resolved = payload([item(action="monitor")])
    assert signal_delta(resolved, snap)["events"][0]["state"] == "resolved"
    assert signal_delta(old, {"signals": resolved})["events"][0]["state"] == "reactivated"


def test_sources_are_constructed_from_validated_ids_not_urls():
    row = item()
    row["top_movements"][0]["sec_filing_url"] = "javascript:alert(1)"
    assert filing_sources(row)[0]["url"].startswith("https://www.sec.gov/Archives/")
    row["top_movements"][0]["filing"]["accession"] = '"><script>alert(1)</script>'
    assert filing_sources(row) == []


def test_alert_lifecycle_is_idempotent_scoped_and_requires_a_fresh_signal(tmp_path):
    with ProAPIStore(str(tmp_path / "pro.db")) as pro:
        _, key = pro.create_key("Local test", scopes=("funds:read", "workspace:write"))
        _, other = pro.create_key("Other local test", scopes=("funds:read", "workspace:write"))
        watch = pro.create_watchlist(key.key_id, "Test", ["AAPL"])
        other_watch = pro.create_watchlist(other.key_id, "Other", ["AAPL"])

        def capture(data, owner=key.key_id, watchlist=watch["id"]):
            snap = pro.create_signal_snapshot(owner, watchlist, data)
            pro.upsert_workspace_alerts(owner, watchlist, snap["id"], data)
            return snap

        first = capture(payload([item()]))
        capture(payload([item()]), other.key_id, other_watch["id"])
        alert = pro.list_workspace_alerts(key.key_id)[0]
        pro.update_workspace_alert_status(key.key_id, alert["id"], "acknowledged")
        capture(payload([item(value=72)]))
        updated = pro.list_workspace_alerts(key.key_id, status=None)[0]
        assert updated["id"] == alert["id"]
        assert updated["status"] == "acknowledged"
        assert updated["reason"]["lifecycle"]["state"] == "strengthened"
        capture(payload([item(value=65)]))
        assert pro.list_workspace_alerts(key.key_id, status=None)[0]["reason"]["lifecycle"]["state"] == "weakened"
        # A filtered blocked observation invalidates the previous alert, not a sale.
        capture(payload([item(action="blocked")], filtered=[]))
        invalid = pro.list_workspace_alerts(key.key_id, status="invalidated")[0]
        assert invalid["reason"]["lifecycle"]["state"] == "invalidated"
        with pytest.raises(ValueError, match="inactive signal"):
            pro.update_workspace_alert_status(key.key_id, alert["id"], "open")
        assert pro.update_workspace_alert_status(other.key_id, alert["id"], "open") is None
        assert pro.list_workspace_alerts(other.key_id)[0]["status"] == "open"
        assert pro.upsert_workspace_alerts(key.key_id, watch["id"], first["id"], payload([item()]))["skipped"] == "superseded_snapshot"
        assert pro.upsert_workspace_alerts(other.key_id, watch["id"], invalid["snapshot_id"], payload([item()]))["skipped"] == "superseded_snapshot"
        # Missing coverage must not resolve or reactivate a signal.
        capture({"items": []})
        assert pro.list_workspace_alerts(key.key_id, status="invalidated")[0]["id"] == alert["id"]
        capture(payload([item()]))
        reopened = pro.list_workspace_alerts(key.key_id)[0]
        assert reopened["reason"]["lifecycle"]["state"] == "reactivated"
        assert reopened["acknowledged_at"] is None
        capture(payload([item(action="monitor")]))
        resolved = pro.list_workspace_alerts(key.key_id, status="resolved")[0]
        resolved_at = resolved["reason"]["lifecycle"]["changed_at"]
        capture(payload([item(action="monitor")]))
        assert pro.list_workspace_alerts(key.key_id, status="resolved")[0]["reason"]["lifecycle"]["changed_at"] == resolved_at
        assert pro.workspace_alert_summary(key.key_id)["by_status"]["resolved"] == 1


def test_changing_alert_level_leaves_only_the_current_level_active(tmp_path):
    with ProAPIStore(str(tmp_path / "pro.db")) as pro:
        _, key = pro.create_key("Test")
        watch = pro.create_watchlist(key.key_id, "Test", ["AAPL"])
        for action in ("alert", "watch", "alert"):
            data = payload([item(action=action)])
            snap = pro.create_signal_snapshot(key.key_id, watch["id"], data)
            pro.upsert_workspace_alerts(key.key_id, watch["id"], snap["id"], data)
            current = pro.list_workspace_alerts(key.key_id)
            assert len(current) == 1 and current[0]["action"] == action


def test_api_lists_inactive_alerts_but_cannot_manually_reopen_them(tmp_path, monkeypatch):
    from tests.test_pro_api_offline import _client

    client, token, key, pro_path = _client(
        monkeypatch, tmp_path, scopes=("funds:read", "workspace:write"),
    )
    with ProAPIStore(pro_path) as pro:
        watch = pro.create_watchlist(key.key_id, "Test", ["AAPL"])
        for action in ("alert", "blocked"):
            signals = payload([item(action=action)])
            snapshot = pro.create_signal_snapshot(key.key_id, watch["id"], signals)
            pro.upsert_workspace_alerts(key.key_id, watch["id"], snapshot["id"], signals)
        alert_id = pro.list_workspace_alerts(key.key_id, status="invalidated")[0]["id"]
    headers = {"Authorization": "Bearer " + token}
    response = client.get("/api/pro/v1/workspace/alerts?status=invalidated", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["alerts"][0]["status"] == "invalidated"
    response = client.patch(f"/api/pro/v1/workspace/alerts/{alert_id}", headers=headers,
                            json={"status": "open"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "inactive_alert"
