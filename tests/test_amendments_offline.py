import sqlite3

import pytest

from smartmoney.db import Store
from smartmoney.diff import diff_portfolios
from smartmoney.parser import parse_amendment_metadata
from smartmoney.quality import quality_gate_report
from tests.test_db_offline import AAPL, KO, MSFT, _save


def save(store, accession, rows, kind=None, number=None):
    return _save(store, "0000000001", "Test Fund", "Manager", accession,
                 "13F-HR/A" if kind or number else "13F-HR",
                 f"2026-05-{int(accession[-1]) + 10:02d}", "2026-03-31", rows,
                 kind, number)


def test_explicit_additions_compose_without_overwriting_raw_holdings(tmp_path):
    path = str(tmp_path / "data.db")
    with Store(path) as s:
        save(s, "F1", [("Apple", AAPL, 100, 10, "")])
        save(s, "F2", [("Coca Cola", KO, 300, 30, "")], "NEW HOLDINGS", 1)
        save(s, "F3", [("Apple", AAPL, 100, 10, ""),
                       ("Microsoft", MSFT, 500, 50, "Put")], "NEW HOLDINGS", 2)
        pf = s.load_portfolio("1")
        assert pf.composition_status == "complete"
        assert pf.total_value == 1000
        assert pf.positions[(AAPL, "")].shares == 20
        assert pf.positions[(AAPL, "")].weight == .2
        assert pf.positions[(MSFT, "Put")].weight == .5
        assert pf.source_accessions == ["F1", "F2", "F3"]
        assert s.get_filing("F3")["n_positions"] == 3
        assert s.get_filing("F3")["total_value"] == 1000
        assert s.conn.execute("SELECT total_value FROM filings WHERE accession='F3'").fetchone()[0] == 600
        assert s.conn.execute("SELECT COUNT(*) FROM holdings WHERE accession='F3'").fetchone()[0] == 2
        assert s.consensus_holdings("2026-03-31", min_funds=1)[0]["total_value"] == 300
        assert s.holders(AAPL, "2026-03-31")[0]["shares"] == 20
        assert s.conviction_timeline("1", AAPL)[0]["weight"] == .2
        assert s.fund_value_timeline("1")[0]["total_value"] == 1000
        assert quality_gate_report(s)["trusted_ciks"] == ["0000000001"]
        # A later one-position restatement is authoritative regardless of size.
        save(s, "F4", [("Apple", AAPL, 80, 8, "")], "RESTATEMENT", 3)
        assert s.load_portfolio("1").total_value == 80
        assert s.load_portfolio("1").source_accessions == ["F4"]
        save(s, "F5", [("Coca Cola", KO, 20, 2, "")], "NEW HOLDINGS", 4)
        assert s.load_portfolio("1").total_value == 100
        assert s.load_portfolio("1").source_accessions == ["F4", "F5"]
    with Store(path, read_only=True) as s:
        assert s.load_portfolio("1").total_value == 100


def test_missing_base_or_amendment_is_not_a_complete_portfolio(tmp_path):
    with Store(str(tmp_path / "data.db")) as s:
        save(s, "F3", [("Coca Cola", KO, 30, 3, "")], "NEW HOLDINGS", 2)
        assert s.load_portfolio("1").composition_status == "missing_base"
        save(s, "F1", [("Apple", AAPL, 100, 10, "")])
        assert s.load_portfolio("1").composition_status == "missing_amendment"
        assert not quality_gate_report(s)["trusted_ciks"]
        save(s, "F2", [("Microsoft", MSFT, 20, 2, "")], "NEW HOLDINGS", 1)
        assert s.load_portfolio("1").composition_status == "complete"
        assert s.load_portfolio("1").total_value == 150
        # Re-ingestion is idempotent and recomposes subsequent revisions.
        save(s, "F2", [("Microsoft", MSFT, 40, 4, "")], "NEW HOLDINGS", 1)
        assert s.load_portfolio("1").total_value == 170


def test_unknown_previous_quarter_blocks_comparisons_but_not_unrelated_history(tmp_path):
    with Store(str(tmp_path / "data.db")) as s:
        _save(s, "1", "Test Fund", "Manager", "OLD", "13F-HR/A", "2026-02-15",
              "2025-12-31", [("Apple", AAPL, 100, 10, "")])
        save(s, "F1", [("Apple", AAPL, 100, 10, "")])
        curr = s.load_portfolio("1")
        prev = s.load_portfolio("1", "2025-12-31")
        assert diff_portfolios(prev, curr).status == "incomplete_amendment_chain"
        assert not diff_portfolios(prev, curr).changes
        assert not quality_gate_report(s)["trusted_ciks"]
        _save(s, "1", "Test Fund", "Manager", "NEXT", "13F-HR", "2026-08-15",
              "2026-06-30", [("Apple", AAPL, 100, 10, "")])
        assert quality_gate_report(s)["trusted_ciks"] == ["0000000001"]


@pytest.mark.parametrize("kind,number,expected", [
    ("RESTATEMENT", "1", ("RESTATEMENT", 1)),
    ("NEW HOLDINGS", "2", ("NEW HOLDINGS", 2)),
    ("UNKNOWN", "1", (None, None)),
    ("RESTATEMENT", "0", (None, None)),
    ("NEW HOLDINGS", "999999", (None, None)),
])
def test_cover_metadata(kind, number, expected):
    xml = f'''<edgarSubmission xmlns="urn:sec"><formData><coverPage>
        <isAmendment>true</isAmendment><amendmentNo>{number}</amendmentNo>
        <amendmentInfo><amendmentType>{kind}</amendmentType></amendmentInfo>
        </coverPage></formData></edgarSubmission>'''
    assert parse_amendment_metadata(xml) == expected


def test_cover_metadata_rejects_ambiguous_and_hostile_xml():
    assert parse_amendment_metadata('<edgarSubmission/>') == (None, None)
    with pytest.raises(Exception):
        parse_amendment_metadata('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')


def test_read_only_legacy_database_is_not_written(tmp_path):
    path = str(tmp_path / "legacy.db")
    with Store(path) as s:
        save(s, "F1", [("Apple", AAPL, 100, 10, "")])
    conn = sqlite3.connect(path)
    for name in ("portfolio_holdings", "portfolio_filings", "composed_holdings"):
        conn.execute(f"DROP VIEW {name}")
    conn.execute("DROP TABLE filing_components")
    conn.execute("DROP TABLE filing_revisions")
    conn.commit()
    conn.close()
    before = (tmp_path / "legacy.db").read_bytes()
    with Store(path, read_only=True) as s:
        assert s.load_portfolio("1").total_value == 100
    assert (tmp_path / "legacy.db").read_bytes() == before


def test_rebuilding_does_not_mark_legacy_metadata_as_fetched(tmp_path):
    with Store(str(tmp_path / "legacy.db")) as s:
        save(s, "F2", [("Apple", AAPL, 100, 10, "")], "NEW HOLDINGS", 1)
        s.conn.execute("DELETE FROM filing_revisions WHERE accession='F2'")
        s.conn.commit()
        save(s, "F1", [("Coca Cola", KO, 100, 10, "")])
        assert s.unclassified_amendments("1") == {"F2"}
        assert s.load_portfolio("1").composition_status == "unknown_amendment"


def test_alert_uses_exact_revision_and_waits_for_complete_chain(tmp_path):
    from smartmoney.alerts import AlertEngine, build_alert
    from smartmoney.channels import CallableChannel
    from smartmoney.registry import Fund
    from smartmoney.tracker import Tier

    with Store(str(tmp_path / "alerts.db")) as s:
        save(s, "F1", [("Apple", AAPL, 100, 10, "")])
        save(s, "F3", [("Coca Cola", KO, 30, 3, "")], "NEW HOLDINGS", 2)
        assert build_alert(s, "1", "F1").counts["NEW"] == 1
        assert build_alert(s, "2", "F1") is None
        assert build_alert(s, "1", "F3") is None
        received = []
        engine = AlertEngine(s, channels={"console": CallableChannel(received.append)})
        engine.subscribe(Tier("paid", []), "test", Fund("Test", None, "1", "Test"),
                         "console", prime=False)
        assert engine.dispatch_for_fund("1")[0]["status"] == "blocked"
        assert received == []
        save(s, "F2", [("Microsoft", MSFT, 20, 2, "")], "NEW HOLDINGS", 1)
        assert engine.dispatch_for_fund("1")[0]["status"] == "sent"
        assert received[0].counts["NEW"] == 3
        assert engine.dispatch_for_fund("1") == []


def test_partial_new_quarter_does_not_bypass_selected_quarter_quality(tmp_path):
    with Store(str(tmp_path / "partial.db")) as s:
        for cik in range(1, 12):
            _save(s, str(cik), f"Fund {cik}", "Manager", f"Q1-{cik}",
                  "13F-HR/A" if cik == 1 else "13F-HR", "2026-05-15", "2026-03-31",
                  [("Apple", AAPL, 100, 10, "")])
        for quarter, date in (("2026-06-30", "2026-08-15"), ("2026-09-30", "2026-11-15")):
            _save(s, "1", "Fund 1", "Manager", quarter, "13F-HR", date, quarter,
                  [("Apple", AAPL, 100, 10, "")])
        gate = quality_gate_report(s)
        assert "0000000001" in gate["excluded_ciks"]


def test_public_and_pro_api_preserve_composition_and_block_incomplete_moves(tmp_path, monkeypatch):
    from smartmoney.api import create_app
    from smartmoney.pro import ProAPIStore
    from tests.test_workspace_automation_offline import _workspace_data_db

    path, pro_path = str(tmp_path / "data.db"), str(tmp_path / "pro.db")
    _workspace_data_db(path)
    with ProAPIStore(pro_path) as pro:
        token, _ = pro.create_key("Test", scopes=("funds:read",))
    monkeypatch.setenv("SMARTMONEY_PRO_API", "1")
    monkeypatch.setenv("SMARTMONEY_PRO_DB", pro_path)
    monkeypatch.setenv("SMARTMONEY_PUBLIC_PAYLOAD_CACHE_SECONDS", "0")
    client = create_app(path, secure_cookies=False, open_mode=True).test_client()
    headers = {"Authorization": "Bearer " + token}

    def amend(accession, kind, number):
        with Store(path) as s:
            _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett", accession,
                  "13F-HR/A", "2026-05-20", "2026-03-31",
                  [("Apple", AAPL, 100, 10, "")], kind, number)
            s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
            s.conn.commit()

    amend("BRK-Q2-A1", "NEW HOLDINGS", 1)
    public = client.get("/api/fund/0001067983").get_json()
    paid = client.get("/api/pro/v1/fund/0001067983", headers=headers).get_json()
    assert public["aum"] == paid["portfolio"]["aum"] == 1900
    assert public["moves_status"] == paid["moves"]["status"] == "available"
    assert paid["filing"]["source_accessions"] == ["BRK-Q2", "BRK-Q2-A1"]
    stock = client.get("/api/stocks/AAPL").get_json()
    assert stock["movement_summary"]["holder_count"] == 2
    assert stock["confidence"]["status"] == "ok"
    artifact = client.get("/api/trust-artifact").get_json()
    assert next(r for r in artifact["rows"] if r["symbol"] == "AAPL")["value_usd"] == 2100
    amend("BRK-Q2-A2", None, None)
    public = client.get("/api/fund/0001067983").get_json()
    paid = client.get("/api/pro/v1/fund/0001067983", headers=headers).get_json()
    assert public["moves_status"] == paid["moves"]["status"] == "incomplete_amendment_chain"
    assert paid["moves"]["changes"] == []
    assert client.get("/api/stocks/AAPL").get_json()["confidence"]["status"] == "review"
    artifact = client.get("/api/trust-artifact").get_json()
    assert next(r for r in artifact["rows"] if r["symbol"] == "AAPL")["value_usd"] == 700
    amend("BRK-Q2-A3", "RESTATEMENT", 3)
    artifact = client.get("/api/trust-artifact").get_json()
    assert next(r for r in artifact["rows"] if r["symbol"] == "AAPL")["value_usd"] == 800
