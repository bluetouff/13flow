"""
Open-build gating: with open_mode=True the app must register only public, read-only
endpoints — no auth, no billing, no subscriptions — and still serve the public screens.
The full build (open_mode=False) must keep auth present. No network.
"""
import os
os.environ.setdefault("SMARTMONEY_DISABLE_HIBP", "1")

import tempfile
from pathlib import Path

from smartmoney.db import Store
from smartmoney.api import create_app
from tests.test_db_offline import AAPL, _save


def _seed(path):
    # Minimal real DB so read endpoints have a schema to query.
    s = Store(path)
    s.upsert_fund("0001067983", "Berkshire Hathaway", "Warren Buffett")
    s.close()


def test_open_mode_hides_private_surface_and_keeps_public():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "open.db")
        _seed(db)
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()

        # config advertises the open build
        cfg = c.get("/api/config").get_json()
        assert cfg["open"] is True
        assert cfg["features"] == {"auth": False, "alerts": False, "billing": False,
                                   "pro_api": False}
        ver = c.get("/api/version").get_json()
        assert ver["app"] == "13flow"
        assert ver["open"] is True
        assert ver["git_sha"]
        assert c.get("/healthz").get_json()["app"] == "13flow"

        # public, read-only endpoints are present
        for path in ("/api/funds", "/api/consensus/buys", "/api/compare",
                     "/api/signals/confluence", "/api/coverage", "/api/data-quality",
                     "/api/live-status", "/api/version", "/healthz", "/"):
            assert c.get(path).status_code == 200, path

        # the entire private surface is unregistered -> 404 (not 401), incl. mutations
        assert c.get("/api/auth/me").status_code == 404
        assert c.post("/api/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 404
        assert c.get("/api/billing/config").status_code == 404
        assert c.get("/api/subscriptions").status_code == 404
        assert c.post("/api/subscriptions", json={"cik": "1067983"}).status_code == 404
        assert c.delete("/api/subscriptions/1").status_code == 404
        assert c.get("/api/alerts/preview/1067983").status_code == 404


def test_full_build_keeps_auth():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "full.db")
        _seed(db)
        c = create_app(db, secure_cookies=False, open_mode=False).test_client()
        cfg = c.get("/api/config").get_json()
        assert cfg["open"] is False and cfg["features"]["auth"] is True
        # auth route exists -> 401 (unauthenticated), not 404
        assert c.get("/api/auth/me").status_code == 401
        assert c.get("/api/subscriptions").status_code == 401


def test_env_var_enables_open_mode(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "envopen.db")
        _seed(db)
        monkeypatch.setenv("SMARTMONEY_OPEN", "1")
        c = create_app(db, secure_cookies=False).test_client()   # no explicit open_mode arg
        assert c.get("/api/config").get_json()["open"] is True
        assert c.get("/api/auth/me").status_code == 404


def test_dashboard_initial_html_exposes_live_state_for_crawlers():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "crawler.db")
        s = Store(db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "0001-26-000001", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        s.close()

        html = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/").get_data(as_text=True)

        assert '<span id="srcText">LIVE · EDGAR</span>' in html
        assert "SAMPLE DATA" not in html
        assert "Live data status: LIVE EDGAR." in html
        assert "uses_synthetic_data=false" in html
        assert "/api/funds serves 1 funds" in html
        assert "latest 13F quarter 2026-03-31" in html

        live = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/api/live-status").get_json()
        assert live["data_mode"] == "live_edgar"
        assert live["uses_synthetic_data"] is False
        assert live["source"] == "SEC EDGAR"
        assert live["counts"]["funds"] == 1
        assert live["latest_13f_quarter"] == "2026-03-31"


if __name__ == "__main__":
    import sys, pytest
    sys.exit(pytest.main([__file__, "-q"]))


def test_csp_nonce_and_no_inline_handlers_and_json_errors():
    import re
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "csp.db")
        _seed(db)
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()

        r = c.get("/")
        csp = r.headers.get("Content-Security-Policy", "")
        html = r.get_data(as_text=True)
        # script-src uses a nonce and NOT 'unsafe-inline'
        m = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9_-]+)'", csp)
        assert m, csp
        nonce = m.group(1)
        assert "'unsafe-inline'" not in csp.split("style-src")[0]   # not in script-src
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        # the served page's single <script> carries that exact nonce
        assert f'<script nonce="{nonce}">' in html
        # nonce is per-request (different each call)
        assert re.search(r"nonce-([A-Za-z0-9_-]+)", c.get("/").headers["Content-Security-Policy"]).group(1) != nonce
        # no inline event-handler ATTRIBUTES survive (JS `.onclick=` assignments are fine)
        import re as _re
        assert _re.search(r'\son\w+="', html) is None, "inline on*= attribute present"
        assert '="javascript:' not in html

        # JSON, not HTML, on errors — and a bad int param is a clean 400
        bad = c.get("/api/consensus/buys?min_funds=abc")
        assert bad.status_code == 400 and bad.is_json
        nf = c.get("/api/does-not-exist")
        assert nf.status_code == 404 and nf.is_json and nf.get_json()["error"] == "not_found"
        # JSON responses carry a locked-down baseline CSP
        assert c.get("/api/funds").headers.get("Content-Security-Policy") == \
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def test_confluence_cache_served_when_present():
    import os, json
    from smartmoney.api_signals import confluence_payload
    from smartmoney.sample_confluence import sample_signals
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "c.db"); _seed(db)
        cache_dir = Path(d)
        fake = {"kpis": {"n_signals": 1, "n_conviction": 1, "n_csuite_clusters": 0,
                         "top_ticker": "CACHED", "top_score": 88.8,
                         "insider_buy_usd": 1234.0, "window_days": 90},
                "signals": [{"ticker": "CACHED", "issuer_name": "Cache Co",
                             "score": 88.8, "quadrant": "conviction"}]}
        (cache_dir / "confluence-90.json").write_text(json.dumps(fake))
        os.environ["SMARTMONEY_CACHE_DIR"] = str(cache_dir)
        try:
            c = create_app(db, secure_cookies=False, open_mode=True).test_client()
            # cached window -> served straight from the file
            j = c.get("/api/signals/confluence?window=90").get_json()
            assert j["kpis"]["top_ticker"] == "CACHED"
            assert j["signals"][0]["ticker"] == "CACHED"
            assert j["metadata"]["validation_status"] == "hypothesis_not_live_validated"
            assert "heuristic" in j["metadata"]["weight_policy"]
            assert j["metadata"]["validation_protocol"]["forward_horizons_days"] == [20, 60, 120]
            # window without a cache file -> falls back to the (sample) provider
            j2 = c.get("/api/signals/confluence?window=45").get_json()
            assert j2["signals"] and j2["signals"][0]["ticker"] != "CACHED"
            assert j2["metadata"]["score_interpretation"].startswith("Ordinal exploratory")
            assert j2["metadata"]["calibration_status"] == "not_calibrated_on_live_history"
            assert "Backtest harness available" in j2["metadata"]["backtest_status"]
        finally:
            del os.environ["SMARTMONEY_CACHE_DIR"]

    # the shared payload helper produces the endpoint shape
    p = confluence_payload(sample_signals(90), 90)
    assert set(p) == {"metadata", "kpis", "signals"} and p["kpis"]["window_days"] == 90
    assert p["metadata"]["known_limitations"]
    assert p["metadata"]["quantitative_evidence_boundary"].startswith("Current production score")


def test_live_confluence_provider_enriches_institutional_signal(monkeypatch):
    from smartmoney.api import _StoreConfluence

    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "live-confluence.db")
        s = Store(db)
        for cik, label in (("0000000001", "Fund One"), ("0000000002", "Fund Two")):
            _save(s, cik, label, "PM", f"{cik}-old", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("COCA COLA", "191216100", 1000, 100, "")])
            _save(s, cik, label, "PM", f"{cik}-new", "13F-HR",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, 1000, 100, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.commit()
        s.close()

        monkeypatch.setenv("SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS", "2")
        provider = _StoreConfluence(db, "13flow-tests test@example.com")
        inst = provider._institutional()["AAPL"]

    assert inst.funds_accumulating == 2
    assert inst.total_value_usd > 0
    assert inst.avg_weight_pct == 100.0
    assert inst.conviction_funds == 2
    assert provider.confluence_metadata()["effective_universe"].startswith(
        "Form 4 scans are limited"
    )
