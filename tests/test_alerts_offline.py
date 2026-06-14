"""
End-to-end offline test for alert delivery. No network.

We seed the store directly (skipping the network sync) and exercise dispatch:
  - first dispatch delivers the newest filing's diff once
  - second dispatch is a no-op (idempotent via the deliveries table)
  - a new filing triggers a new delivery
  - priming suppresses backfill for new subscribers
  - free tier cannot subscribe (paywall)
  - a failing channel is recorded 'failed' and retried next run
  - webhook channel posts the alert JSON (fake session)
"""

import json
import tempfile
from pathlib import Path

from smartmoney.edgar import Filing
from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from smartmoney.db import Store
from smartmoney.tracker import Tier, EntitlementError
from smartmoney.alerts import AlertEngine, build_alert
from smartmoney.channels import CallableChannel, WebhookChannel
from smartmoney.registry import Fund
from tests.test_offline import _table

AAPL, KO, NVDA = "037833100", "191216100", "67066G104"
FUND = Fund("Berkshire Hathaway", "Warren Buffett", "0001067983", "Berkshire Hathaway")


def _save(store, acc, form, fdate, rdate, rows):
    pf = build_portfolio(FUND.cik, FUND.label, rdate, form, parse_info_table(_table(rows)))
    f = Filing(cik=FUND.cik, accession=acc, form=form, filing_date=fdate,
               report_date=rdate, primary_doc="primary_doc.xml")
    store.save_portfolio(pf, f, manager=FUND.manager)


def test_dispatch_dedup_and_new_filing():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        _save(store, "acc-q1", "13F-HR", "2024-05-15", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        _save(store, "acc-q2", "13F-HR", "2024-08-14", "2024-06-30",
              [("APPLE INC", AAPL, 1100, 100, ""), ("NVIDIA", NVDA, 500, 50, "")])

        received = []
        engine = AlertEngine(store, tracker=None,
                             channels={"console": CallableChannel(received.append)})
        # Subscribe WITHOUT priming so the existing latest (q2) gets delivered.
        sub_id = engine.subscribe(Tier("paid", []), "u1", FUND, "console", prime=False)

        # First dispatch: one alert for the newest filing (q2), carrying the diff.
        r1 = engine.dispatch_for_fund(FUND.cik)
        assert len(r1) == 1 and r1[0]["status"] == "sent"
        assert len(received) == 1
        alert = received[0]
        assert alert.report_date == "2024-06-30"
        assert alert.counts["NEW"] == 1                      # NVIDIA opened
        assert any(m.issuer == "NVIDIA" and m.move == "NEW" for m in alert.moves)

        # Second dispatch: idempotent, nothing re-sent.
        r2 = engine.dispatch_for_fund(FUND.cik)
        assert r2 == [] and len(received) == 1

        # New filing arrives -> new delivery.
        _save(store, "acc-q3", "13F-HR", "2024-11-14", "2024-09-30",
              [("APPLE INC", AAPL, 1200, 100, ""), ("COCA COLA", KO, 300, 30, "")])
        r3 = engine.dispatch_for_fund(FUND.cik)
        assert len(r3) == 1 and r3[0]["accession"] == "acc-q3"
        assert received[-1].report_date == "2024-09-30"
        assert received[-1].counts["EXIT"] == 1              # NVIDIA gone


def test_priming_suppresses_backfill():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        _save(store, "acc-q1", "13F-HR", "2024-05-15", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        received = []
        engine = AlertEngine(store, channels={"console": CallableChannel(received.append)})

        engine.subscribe(Tier("paid", []), "u1", FUND, "console", prime=True)
        assert engine.dispatch_for_fund(FUND.cik) == []      # primed -> no backfill
        assert received == []

        _save(store, "acc-q2", "13F-HR", "2024-08-14", "2024-06-30",
              [("APPLE INC", AAPL, 1100, 100, ""), ("NVIDIA", NVDA, 500, 50, "")])
        r = engine.dispatch_for_fund(FUND.cik)               # genuinely new -> delivered
        assert len(r) == 1 and len(received) == 1


def test_free_tier_cannot_subscribe():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        engine = AlertEngine(store)
        try:
            engine.subscribe(Tier("free", []), "u1", FUND, "console")
            assert False, "free tier should not be allowed to subscribe to alerts"
        except EntitlementError:
            pass


def test_failure_is_recorded_and_retried():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        _save(store, "acc-q1", "13F-HR", "2024-05-15", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])

        attempts = {"n": 0}

        def flaky(alert):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("smtp down")

        engine = AlertEngine(store, channels={"console": CallableChannel(flaky)})
        engine.subscribe(Tier("paid", []), "u1", FUND, "console", prime=False)

        r1 = engine.dispatch_for_fund(FUND.cik)
        assert r1[0]["status"] == "failed"                   # first attempt fails
        r2 = engine.dispatch_for_fund(FUND.cik)              # failed -> eligible to retry
        assert r2[0]["status"] == "sent"
        r3 = engine.dispatch_for_fund(FUND.cik)              # now terminal
        assert r3 == []


def test_webhook_channel_posts_json():
    with tempfile.TemporaryDirectory() as d:
        store = Store(str(Path(d) / "s.db"))
        _save(store, "acc-q1", "13F-HR", "2024-05-15", "2024-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        _save(store, "acc-q2", "13F-HR", "2024-08-14", "2024-06-30",
              [("APPLE INC", AAPL, 1100, 100, ""), ("NVIDIA", NVDA, 500, 50, "")])

        posted = {}

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass

        class FakeSession:
            def post(self, url, json=None, timeout=None, **kw):
                posted["url"] = url
                posted["body"] = json
                return FakeResp()

        sess = FakeSession()
        engine = AlertEngine(store, channels={
            "webhook": lambda sub: WebhookChannel(sub["target"], session=sess, validate=False)
        })
        engine.subscribe(Tier("paid", []), "u1", FUND, "webhook",
                         target="https://hooks.example.com/x", prime=False)
        r = engine.dispatch_for_fund(FUND.cik)
        assert r[0]["status"] == "sent"
        assert posted["url"] == "https://hooks.example.com/x"
        # Body is JSON-serializable and carries the structured alert.
        assert posted["body"]["report_date"] == "2024-06-30"
        assert posted["body"]["counts"]["NEW"] == 1
        json.dumps(posted["body"])  # must not raise


if __name__ == "__main__":
    test_dispatch_dedup_and_new_filing()
    test_priming_suppresses_backfill()
    test_free_tier_cannot_subscribe()
    test_failure_is_recorded_and_retried()
    test_webhook_channel_posts_json()
    print("All alert-delivery offline tests passed.")
