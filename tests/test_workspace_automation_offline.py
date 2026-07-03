import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartmoney.db import Store
from smartmoney.pro import ProAPIStore
from smartmoney.workspace_automation import run_workspace_automation
from tests.test_db_offline import AAPL, MSFT, _save


def _workspace_data_db(path: str) -> None:
    s = Store(path)
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


def _count(pro_db: str, table: str) -> int:
    return sqlite3.connect(pro_db).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_workspace_automation_snapshots_enabled_due_watchlists_only(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "data.db")
        pro_db = str(Path(d) / "pro.db")
        _workspace_data_db(data_db)
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        with ProAPIStore(pro_db) as pro:
            _, key = pro.create_key("Automation key", scopes=("funds:read", "workspace:write"))
            due = pro.create_watchlist(
                key.key_id,
                "Daily enabled",
                ["AAPL", "MSFT"],
                filters={"action": ["alert"], "min_score": 30},
                alert_policy={"enabled": True, "frequency": "daily"},
            )
            pro.create_watchlist(
                key.key_id,
                "Manual disabled",
                ["AAPL"],
                filters={},
                alert_policy={"enabled": False, "frequency": "manual"},
            )

        monkeypatch.setenv("SMARTMONEY_DB_READONLY", "1")
        dry = run_workspace_automation(data_db, pro_db, dry_run=True, now=now)
        assert dry["ok"] is True
        assert dry["due"] == 1
        assert dry["processed"][0]["watchlist_id"] == due["id"]
        assert _count(pro_db, "saved_watchlist_signal_snapshots") == 0

        result = run_workspace_automation(data_db, pro_db, now=now)
        assert result["ok"] is True
        assert result["due"] == 1
        assert result["processed"][0]["signals"] >= 1
        assert result["processed"][0]["alerts"]["candidates"] >= 1
        assert _count(pro_db, "saved_watchlist_signal_snapshots") == 1
        assert _count(pro_db, "saved_workspace_alerts") >= 1
        with ProAPIStore(pro_db) as pro:
            events = pro.list_workspace_activity(key.key_id, event_type="signals.snapshot.automated")
        assert len(events) == 1
        assert events[0]["detail"]["frequency"] == "daily"

        second = run_workspace_automation(data_db, pro_db, now=now + timedelta(hours=2))
        assert second["due"] == 0
        assert _count(pro_db, "saved_watchlist_signal_snapshots") == 1


def test_workspace_automation_respects_weekly_cadence(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        data_db = str(Path(d) / "data.db")
        pro_db = str(Path(d) / "pro.db")
        _workspace_data_db(data_db)
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        with ProAPIStore(pro_db) as pro:
            _, key = pro.create_key("Weekly automation key", scopes=("funds:read", "workspace:write"))
            item = pro.create_watchlist(
                key.key_id,
                "Weekly enabled",
                ["AAPL", "MSFT"],
                filters={"move": ["NEW"]},
                alert_policy={"enabled": True, "frequency": "weekly"},
            )

        monkeypatch.setenv("SMARTMONEY_DB_READONLY", "1")
        first = run_workspace_automation(data_db, pro_db, now=now)
        assert first["due"] == 1
        too_soon = run_workspace_automation(data_db, pro_db, now=now + timedelta(days=2))
        assert too_soon["due"] == 0

        old = (now - timedelta(days=8)).isoformat(timespec="seconds")
        conn = sqlite3.connect(pro_db)
        conn.execute(
            "UPDATE saved_watchlist_signal_snapshots SET created_at=? WHERE watchlist_id=?",
            (old, item["id"]),
        )
        conn.commit()
        conn.close()
        due_again = run_workspace_automation(data_db, pro_db, now=now)
        assert due_again["due"] == 1
        assert _count(pro_db, "saved_watchlist_signal_snapshots") == 2
