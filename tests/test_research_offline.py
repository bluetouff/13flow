"""
Research contract tests. No network.
"""

import json
import tempfile
from pathlib import Path

from smartmoney.research import (
    append_signal_history,
    confluence_v1_spec,
    read_signal_history,
    stable_json_hash,
)


def test_confluence_v1_spec_is_frozen_and_machine_readable():
    spec = confluence_v1_spec("abc123")
    assert spec["version"] == "confluence_v1"
    assert spec["status"] == "frozen_hypothesis_not_validated"
    assert spec["score_interpretation"]["is_probability"] is False
    assert spec["weight_version"] == "heuristic_default_v1"
    assert spec["parameters"]["combination_weights"]["institutional_breadth"] == 36.0
    assert spec["parameter_hash"] == stable_json_hash(spec["parameters"])


def test_signal_history_append_and_filter():
    payload = {
        "metadata": {"generated_at": "2026-07-02T16:00:00+00:00"},
        "signals": [
            {"ticker": "AAPL", "score": 81.2, "quadrant": "conviction"},
            {"ticker": "MSFT", "score": 62.5, "quadrant": "institutional_only"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "history.jsonl")
        report = append_signal_history([(90, "cache/confluence-90.json", payload)],
                                       path, code_commit="abc123")

        assert report["signals_appended"] == 2
        rows = read_signal_history(path, limit=10)
        assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]
        assert rows[0]["score_version"] == "confluence_v1"
        assert rows[0]["revision_hash"]
        assert read_signal_history(path, ticker="MSFT")[0]["score"] == 62.5
        assert read_signal_history(path, window_days=30) == []


def test_research_endpoints_are_read_only(monkeypatch):
    from smartmoney.api import create_app
    from smartmoney.db import Store

    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "market.db")
        Store(db).close()
        hist = Path(d) / "confluence-history.jsonl"
        hist.write_text(json.dumps({
            "ticker": "AAPL",
            "score": 81.2,
            "window_days": 90,
            "score_version": "confluence_v1",
        }) + "\n", encoding="utf-8")
        monkeypatch.setenv("SMARTMONEY_CACHE_DIR", d)
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()

        spec = c.get("/api/methodology/confluence-v1")
        assert spec.status_code == 200
        assert spec.get_json()["version"] == "confluence_v1"

        hist_resp = c.get("/api/signals/confluence/history?ticker=AAPL&window=90")
        assert hist_resp.status_code == 200
        body = hist_resp.get_json()
        assert body["metadata"]["append_only"] is True
        assert body["count"] == 1
        assert body["history"][0]["ticker"] == "AAPL"
