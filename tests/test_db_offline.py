"""
End-to-end offline test for persistence + analytics. No network.

Covers: save/load round-trip, amendment supersede via latest_filings, consensus
holdings (SQL), conviction timeline, holders, and consensus BUYS (diff-based).
"""

import tempfile
from pathlib import Path

from smartmoney.edgar import Filing
from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from smartmoney.db import Store
from smartmoney.analytics import consensus_moves
from smartmoney.diff import Move
from smartmoney.tracker import Tracker
from tests.test_offline import _table

# CUSIPs reused across the suite
AAPL, KO, NVDA, MSFT = "037833100", "191216100", "67066G104", "594918104"


def _save(store, cik, label, manager, accession, form, fdate, rdate, rows):
    pf = build_portfolio(cik, label, rdate, form, parse_info_table(_table(rows)))
    filing = Filing(cik=cik, accession=accession, form=form,
                    filing_date=fdate, report_date=rdate, primary_doc="primary_doc.xml")
    store.save_portfolio(pf, filing, manager=manager)
    return pf


def test_round_trip_and_amendment_supersede():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        cik = "0000000001"

        _save(store, cik, "Fund One", "PM One", "0001-24-000001", "13F-HR",
              "2024-02-10", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, ""), ("COCA COLA", KO, 500, 50, "")])

        assert store.has_filing("0001-24-000001")
        assert store.stored_accessions(cik) == {"0001-24-000001"}

        pf = store.load_portfolio(cik, "2024-03-31")
        assert pf is not None and len(pf.positions) == 2
        assert pf.positions[(AAPL, "")].shares == 100

        # Amendment for the SAME quarter, later filing_date, different holdings.
        _save(store, cik, "Fund One", "PM One", "0001-24-000002", "13F-HR/A",
              "2024-03-15", "2024-03-31",
              [("APPLE INC", AAPL, 1200, 120, "")])  # restated: KO dropped, AAPL up

        pf2 = store.load_portfolio(cik, "2024-03-31")
        assert len(pf2.positions) == 1, "amendment should supersede the original"
        assert pf2.positions[(AAPL, "")].shares == 120
        assert store.quarters(cik) == ["2024-03-31"]  # still one quarter, two filings


def test_partial_amendment_does_not_replace_full_snapshot():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        cik = "0000000001"

        _save(store, cik, "Fund One", "PM One", "0001-24-000001", "13F-HR",
              "2024-11-14", "2024-09-30",
              [("APPLE INC", AAPL, 1000, 100, ""),
               ("COCA COLA", KO, 500, 50, ""),
               ("NVIDIA", NVDA, 300, 30, ""),
               ("MICROSOFT", MSFT, 200, 20, "")])

        _save(store, cik, "Fund One", "PM One", "0001-25-000001", "13F-HR/A",
              "2025-02-14", "2024-09-30",
              [("APPLE INC", AAPL, 1000, 100, "")])

        pf = store.load_portfolio(cik, "2024-09-30")
        assert len(pf.positions) == 4
        assert pf.form == "13F-HR"

        _save(store, cik, "Fund One", "PM One", "0001-25-000002", "13F-HR/A",
              "2025-03-01", "2024-09-30",
              [("APPLE INC", AAPL, 1200, 120, ""),
               ("COCA COLA", KO, 600, 60, ""),
               ("NVIDIA", NVDA, 450, 45, ""),
               ("MICROSOFT", MSFT, 300, 30, "")])

        pf2 = store.load_portfolio(cik, "2024-09-30")
        assert len(pf2.positions) == 4
        assert pf2.form == "13F-HR/A"
        assert pf2.positions[(AAPL, "")].shares == 120


def test_consensus_and_analytics():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        f1, f2 = "0000000001", "0000000002"

        # --- Q1 (2024-03-31) ---
        _save(store, f1, "Fund One", "PM1", "A1", "13F-HR", "2024-05-01", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        _save(store, f2, "Fund Two", "PM2", "B1", "13F-HR", "2024-05-02", "2024-03-31",
              [("MICROSOFT", MSFT, 1000, 100, "")])

        # --- Q2 (2024-06-30): both open NVDA; F1 keeps AAPL, F2 keeps MSFT ---
        _save(store, f1, "Fund One", "PM1", "A2", "13F-HR", "2024-08-01", "2024-06-30",
              [("APPLE INC", AAPL, 1100, 100, ""), ("NVIDIA", NVDA, 500, 50, "")])
        _save(store, f2, "Fund Two", "PM2", "B2", "13F-HR", "2024-08-02", "2024-06-30",
              [("MICROSOFT", MSFT, 1000, 100, ""), ("NVIDIA", NVDA, 300, 30, "")])

        # Consensus HOLDINGS at Q2: NVDA held by both funds.
        ch = store.consensus_holdings("2024-06-30", min_funds=2)
        assert len(ch) == 1 and ch[0]["cusip"] == NVDA and ch[0]["n_funds"] == 2

        # Consensus BUYS at Q2 (diff vs each fund's Q1): NVDA is NEW for both.
        cb = consensus_moves(store, [f1, f2], "2024-06-30",
                             kinds=(Move.NEW, Move.ADD), min_funds=2)
        assert len(cb) == 1
        assert cb[0].cusip == NVDA and cb[0].n_funds == 2
        assert set(cb[0].moves) == {"NEW"}

        # Conviction timeline for F1 / AAPL across both quarters.
        tl = store.conviction_timeline(f1, AAPL)
        assert [r["report_date"] for r in tl] == ["2024-03-31", "2024-06-30"]
        assert tl[0]["value_usd"] == 1000 and tl[1]["value_usd"] == 1100

        # Holders of NVDA at Q2.
        h = store.holders(NVDA, "2024-06-30")
        assert {r["fund"] for r in h} == {"Fund One", "Fund Two"}

        # Fund value timeline (13F AUM) for F1.
        fv = store.fund_value_timeline(f1)
        assert [r["report_date"] for r in fv] == ["2024-03-31", "2024-06-30"]
        assert fv[1]["total_value"] == 1600  # 1100 + 500

        # previous_quarter helper
        assert store.previous_quarter(f1, "2024-06-30") == "2024-03-31"
        assert store.previous_quarter(f1, "2024-03-31") is None


def test_force_sync_replaces_existing_filing():
    class FakeClient:
        def __init__(self):
            self.value = 1000
            self.filing = Filing(cik="0000000001", accession="A1", form="13F-HR",
                                 filing_date="2024-05-01", report_date="2024-03-31",
                                 primary_doc="primary.xml")

        def resolve_cik(self, _):
            return "0000000001"

        def list_13f_filings(self, cik, include_amendments=True):
            return [self.filing]

        def fetch_info_table_xml(self, filing):
            return _table([("APPLE INC", AAPL, self.value, 100, "")])

    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "force.db"))
        client = FakeClient()
        tracker = Tracker(client)
        fund = type("FundLike", (), {
            "cik": "0000000001", "label": "Fund One", "manager": "PM1",
            "search_name": "Fund One",
        })()

        assert tracker.sync_fund(store, fund) == 1
        assert store.load_portfolio("0000000001").total_value == 1000

        client.value = 2000
        assert tracker.sync_fund(store, fund) == 0
        assert store.load_portfolio("0000000001").total_value == 1000

        assert tracker.sync_fund(store, fund, force=True, report_date="2024-03-31") == 1
        assert store.load_portfolio("0000000001").total_value == 2000

        client.value = 3000
        assert tracker.sync_fund(store, fund, force=True, report_date="2099-12-31") == 0
        assert store.load_portfolio("0000000001").total_value == 2000


if __name__ == "__main__":
    test_round_trip_and_amendment_supersede()
    test_consensus_and_analytics()
    print("All persistence offline tests passed.")
