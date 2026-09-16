import json
import sqlite3
from datetime import datetime, timezone

import pytest

from smartmoney.api import create_app
from smartmoney.db import Store
from smartmoney.filing_events import capture_baselines, read
from tests.test_db_offline import _save, AAPL, MSFT

CIK = "0001998597"
QUARTER = "2026-03-31"


def save(store, number=1, *, kind=None, value=100):
    return _save(store, CIK, "JANA test fixture", "Test manager",
                 f"0000902664-26-{number:06d}", "13F-HR/A" if kind else "13F-HR",
                 f"2026-05-{10 + number:02d}", QUARTER,
                 [("Test issuer", MSFT if kind == "NEW HOLDINGS" else AAPL, value, value / 10, "")],
                 kind, number - 1 if kind else None)


def test_revisions_are_immutable_idempotent_and_preserve_raw_rows(tmp_path):
    with Store(str(tmp_path / "data.db")) as s:
        save(s)
        first = read(s.conn)["events"][0]
        assert first["kind"] == "filing_observed"
        assert first["before"] is None
        save(s)
        assert read(s.conn)["head"] == 1
        save(s, 2, kind="NEW HOLDINGS", value=20)
        added = read(s.conn)["events"][-1]
        assert added["kind"] == "amendment_observed"
        assert added["before"]["portfolio_value_usd"] == 100
        assert added["after"]["reported_value_usd"] == 20
        assert added["after"]["portfolio_value_usd"] == 120
        assert added["after"]["positions"] == 2
        assert len(added["after"]["sources"]) == 2
        save(s, 3, kind="RESTATEMENT", value=80)
        replaced = read(s.conn)["events"][-1]
        assert replaced["after"]["portfolio_value_usd"] == 80
        assert len(replaced["after"]["sources"]) == 1
        # A force repair changes the interpretation, not the SEC publication date.
        save(s, 3, kind="RESTATEMENT", value=90)
        repaired = read(s.conn)["events"][-1]
        assert repaired["kind"] == "data_revised"
        assert repaired["before"]["accession"] == repaired["after"]["accession"]
        assert repaired["before"]["published_on"] == repaired["after"]["published_on"]
        assert "holdings_hash" in repaired["changed_fields"]
        assert read(s.conn)["events"][0] == first
        assert s.conn.execute("SELECT total_value FROM filings WHERE accession LIKE '%000002'").fetchone()[0] == 20
        assert not s.conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]


def test_legacy_baselines_are_dated_now_and_never_reconstruct_past_knowledge(tmp_path):
    path = str(tmp_path / "data.db")
    with Store(path) as s:
        save(s)
        s.conn.execute("DROP TABLE filing_events")
    started = datetime.now(timezone.utc)
    with Store(path) as s:
        capture_baselines(s.conn, {CIK})
        capture_baselines(s.conn, {CIK})
        page = read(s.conn)
        assert len(page["events"]) == 1
        event = page["events"][0]
        assert event["kind"] == "baseline" and event["before"] is None
        assert datetime.fromisoformat(event["recorded_at"]) >= started
        assert event["after"]["published_on"] == "2026-05-11"
        assert event["interpretation"] == "initial_observation"


def test_incomplete_composition_has_no_usable_total_and_recovery_is_recorded(tmp_path):
    with Store(str(tmp_path / "data.db")) as s:
        save(s, 2, kind="NEW HOLDINGS", value=20)
        broken = read(s.conn)["events"][-1]
        assert broken["after"]["composition_status"] == "missing_base"
        assert broken["after"]["portfolio_value_usd"] is None
        assert broken["after"]["positions"] is None
        save(s)
        recovered = read(s.conn)["events"][-1]
        assert recovered["kind"] == "data_revised"
        assert recovered["after"]["composition_status"] == "complete"
        assert recovered["after"]["portfolio_value_usd"] == 120


def test_event_and_filing_writes_rollback_together(tmp_path, monkeypatch):
    from smartmoney import filing_events

    with Store(str(tmp_path / "data.db")) as s:
        save(s)
        original = filing_events.capture

        def fail(conn, cik, report_date, **kwargs):
            original(conn, cik, report_date, **kwargs)
            if not kwargs.get("baseline"):
                raise RuntimeError("fixture rollback")

        monkeypatch.setattr(filing_events, "capture", fail)
        with pytest.raises(RuntimeError):
            save(s, 2, kind="NEW HOLDINGS")
        assert not s.has_filing("0000902664-26-000002")
        assert read(s.conn)["head"] == 1


def test_public_cursor_contract_is_read_only_scoped_and_rejects_garbage(tmp_path):
    path = str(tmp_path / "data.db")
    with Store(path) as s:
        save(s)
        save(s, 2, kind="NEW HOLDINGS")
    client = create_app(path, open_mode=True, secure_cookies=False).test_client()
    before = open(path, "rb").read()
    first = client.get("/api/events/filings?limit=1").get_json()
    assert first["status"] == "ok" and first["has_more"] is True
    second = client.get(f"/api/events/filings?after={first['next_cursor']}").get_json()
    assert len(second["events"]) == 1 and not second["has_more"]
    assert first["stream_id"] == second["stream_id"]
    assert client.get("/api/events/filings?cik=0000949509").get_json()["events"] == []
    assert client.get("/api/events/filings?after=garbage").status_code == 400
    assert client.get("/api/events/filings?cik=%27OR1=1").status_code == 400
    assert client.get("/api/events/filings?cik=123456789012").status_code == 400
    assert client.get("/api/events/filings?cik=0").status_code == 400
    assert client.get("/api/events/filings?limit=100000").status_code == 200
    assert open(path, "rb").read() == before
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE filing_events")
    before = open(path, "rb").read()
    unavailable = client.get("/api/events/filings").get_json()
    assert unavailable["status"] == "not_initialized" and unavailable["events"] == []
    assert open(path, "rb").read() == before
    assert "/api/events/filings" in client.get("/api/openapi.json").get_json()["paths"]


def test_untrusted_source_identifiers_are_never_turned_into_links(tmp_path):
    with Store(str(tmp_path / "data.db")) as s:
        _save(s, CIK, "Fixture", "Fixture", '\"><script>alert(1)</script>',
              "13F-HR", "2026-05-11", QUARTER, [("Test", AAPL, 1, 1, "")])
        assert read(s.conn)["events"] == []


def test_cursor_page_is_consistent_during_a_concurrent_append(tmp_path):
    path = str(tmp_path / "data.db")
    with Store(path) as writer:
        save(writer)
        with Store(path, read_only=True) as reader:
            appended = []

            def append_between_queries(sql):
                if "MAX(sequence)" in sql and not appended:
                    appended.append(True)
                    save(writer, 2, kind="NEW HOLDINGS")

            reader.conn.set_trace_callback(append_between_queries)
            page = read(reader.conn)
            assert appended
            assert page["head"] == page["next_cursor"] == 1
            assert [event["sequence"] for event in page["events"]] == [1]
            reader.conn.set_trace_callback(None)
            assert read(reader.conn, after=1)["head"] == 2
