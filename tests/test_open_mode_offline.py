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
        assert cfg["demo"] is False
        assert cfg["public_state"] == "LIVE"
        assert cfg["features"] == {"auth": False, "alerts": False, "billing": False,
                                   "pro_api": False}
        ver = c.get("/api/version").get_json()
        assert ver["app"] == "13flow"
        assert ver["open"] is True
        assert ver["public_state"] == "LIVE"
        assert ver["commit"] == ver["git_sha"]
        assert ver["generated_at"]
        assert ver["git_sha"]
        assert c.get("/healthz").get_json()["app"] == "13flow"

        # public, read-only endpoints are present
        for path in ("/api/funds", "/api/consensus/buys", "/api/compare",
                     "/api/coverage", "/api/data-quality",
                     "/api/live-status", "/api/product-status",
                     "/api/version", "/healthz", "/"):
            assert c.get(path).status_code == 200, path
        # Confluence no longer serves demo data implicitly. It needs a cache, live provider,
        # or explicit SMARTMONEY_CONFLUENCE_DEMO=1.
        cf = c.get("/api/signals/confluence")
        assert cf.status_code == 503
        assert cf.get_json()["error"] == "confluence_unavailable"

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


def test_demo_mode_is_open_and_non_commercial(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "demo.db")
        _seed(db)
        monkeypatch.setenv("SMARTMONEY_DEMO", "1")
        c = create_app(db, secure_cookies=False).test_client()
        cfg = c.get("/api/config").get_json()
        assert cfg["open"] is True
        assert cfg["demo"] is True
        assert cfg["public_state"] == "DEMO"
        assert cfg["features"]["auth"] is False
        assert cfg["features"]["billing"] is False
        assert c.get("/api/auth/me").status_code == 404
        assert c.get("/api/billing/config").status_code == 404
        live = c.get("/api/live-status").get_json()
        assert live["public_state"] == "DEMO"
        assert live["uses_synthetic_data"] is True
        assert live["auth_enabled"] is False
        assert live["checkout_enabled"] is False


def test_dashboard_initial_html_exposes_live_state_for_crawlers():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "crawler.db")
        s = Store(db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "0001-26-000001", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.commit()
        s.close()

        html = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/").get_data(as_text=True)

        assert '<span id="srcText">LIVE · EDGAR</span>' in html
        assert "SAMPLE DATA" not in html
        assert "Sign in" not in html
        assert "Upgrade to Pro" not in html
        assert "Continue to checkout" not in html
        assert "€12" not in html
        assert 'data-view="alerts"' not in html
        assert 'document.getElementById("closeUpgrade").onclick=' not in html
        assert 'document.getElementById("goCheckout").onclick=' not in html
        assert 'document.getElementById("authSubmit").onclick=' not in html
        assert "Live data status: LIVE EDGAR." in html
        assert "uses_synthetic_data=false" in html
        assert "/api/funds serves 1 funds" in html
        assert "latest 13F quarter 2026-03-31" in html

        live = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/api/live-status").get_json()
        assert live["data_mode"] == "live_edgar"
        assert live["public_state"] == "LIVE"
        assert live["uses_synthetic_data"] is False
        assert live["source"] == "SEC EDGAR"
        assert live["generated_at"]
        assert live["commit"] == live["git_sha"]
        assert live["data_as_of"] == "2026-05-15"
        assert live["period_13f"] == {"from": "2026-03-31", "to": "2026-03-31"}
        assert live["coverage"]["overall_value_share"] == 1.0
        assert live["accessions"]["latest_count"] == 1
        assert live["accessions"]["sample"][0]["accession"] == "0001-26-000001"
        assert live["auth_enabled"] is False
        assert live["checkout_enabled"] is False
        assert live["counts"]["funds"] == 1
        assert live["latest_13f_quarter"] == "2026-03-31"

        product = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/api/product-status").get_json()
        assert product["public_state"] == "LIVE"
        assert product["data"]["uses_synthetic_data"] is False
        assert product["commercial_readiness"]["public_api"] == "live_read_only"
        assert product["commercial_readiness"]["pro_api"] == \
            "separate_service_expected_on_/api/pro/v1_with_api_key"
        assert product["commercial_readiness"]["mcp"] == "available_read_only"
        assert product["commercial_readiness"]["x402"] == "not_enabled"
        assert product["validation"]["current_artifact"]["publishable_as_full_validation"] is False
        assert "validated alpha" in product["offer_boundary"]["do_not_claim_yet"]
        assert "verifiable SEC EDGAR-derived 13F data" in product["offer_boundary"]["sell_now"]


def test_static_research_pages_public_openapi_and_mcp(monkeypatch):
    import json
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "static.db")
        s = Store(db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "0001-26-000001", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.commit()
        s.close()

        cache = Path(d)
        (cache / "confluence-90.json").write_text(json.dumps({
            "kpis": {"n_signals": 1, "window_days": 90},
            "signals": [{"ticker": "AAPL", "issuer_name": "Apple Inc.",
                         "score": 71.2, "quadrant": "conviction",
                         "rationale": "test signal",
                         "institutional": {"fund_labels": ["Berkshire Hathaway"]},
                         "insider": {}}],
        }), encoding="utf-8")
        monkeypatch.setenv("SMARTMONEY_CACHE_DIR", str(cache))
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()

        for path, needle in (
            ("/funds", "Berkshire Hathaway"),
            ("/funds/0001067983", "SEC filing"),
            ("/stocks", "AAPL"),
            ("/stocks/AAPL", "SEC company search"),
            ("/signals", "test signal"),
            ("/signals/AAPL", "Latest 13F holders"),
            ("/faq", "Frequently asked questions"),
            ("/legal", "Legal, privacy and data terms"),
        ):
            r = c.get(path)
            assert r.status_code == 200, path
            assert needle in r.get_data(as_text=True), path

        doc = c.get("/api/openapi.json").get_json()
        assert "/api/mcp" in doc["paths"]
        assert "/api/product-status" in doc["paths"]
        assert "/api/methodology/confluence-v1" in doc["paths"]
        assert "/api/stocks/{ticker}" in doc["paths"]

        api_stock = c.get("/api/stocks/AAPL").get_json()
        assert api_stock["ticker"] == "AAPL"
        assert api_stock["holder_count"] == 1

        mcp = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
        }).get_json()
        assert any(t["name"] == "stocks.get" for t in mcp["result"]["tools"])
        assert any(t["name"] == "product.status" for t in mcp["result"]["tools"])

        stock = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "stocks.get", "arguments": {"ticker": "AAPL"}},
        }).get_json()
        assert stock["result"]["structuredContent"]["ticker"] == "AAPL"
        assert stock["result"]["structuredContent"]["holder_count"] == 1

        product = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "product.status", "arguments": {}},
        }).get_json()
        assert product["result"]["structuredContent"]["validation"]["current_artifact"][
            "publishable_as_full_validation"
        ] is False


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
        assert "font-src 'self'" in csp
        assert "fonts.googleapis.com" not in csp and "fonts.gstatic.com" not in csp
        assert "/assets/fonts/13flow-fonts.css" in html
        assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
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
        font_css = c.get("/assets/fonts/13flow-fonts.css")
        assert font_css.status_code == 200
        assert font_css.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
        assert "fonts.gstatic.com" not in font_css.get_data(as_text=True)


def test_confluence_cache_served_when_present(monkeypatch):
    import json
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
        monkeypatch.setenv("SMARTMONEY_CACHE_DIR", str(cache_dir))
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()
        # cached window -> served straight from the file
        j = c.get("/api/signals/confluence?window=90").get_json()
        assert j["kpis"]["top_ticker"] == "CACHED"
        assert j["signals"][0]["ticker"] == "CACHED"
        assert j["metadata"]["validation_status"] == "hypothesis_not_live_validated"
        assert "heuristic" in j["metadata"]["weight_policy"]
        assert j["metadata"]["validation_protocol"]["forward_horizons_days"] == [20, 60, 120]
        assert "SEC-rate-limit control" in j["metadata"]["effective_universe"]["insider"]
        assert any("Form 4 universe is partial" in item
                   for item in j["metadata"]["known_limitations"])
        assert j["metadata"]["served_from_cache"] is True
        assert j["metadata"].get("provider") != "unconfigured"
        # window without a cache file -> explicit error, not implicit sample data
        r2 = c.get("/api/signals/confluence?window=45")
        assert r2.status_code == 503
        assert r2.get_json()["error"] == "confluence_unavailable"

    # the shared payload helper produces the endpoint shape
    p = confluence_payload(sample_signals(90), 90)
    assert set(p) == {"metadata", "kpis", "signals"} and p["kpis"]["window_days"] == 90
    assert p["metadata"]["known_limitations"]
    assert p["metadata"]["quantitative_evidence_boundary"].startswith("Current production score")


def test_confluence_demo_is_explicit(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "demo-confluence.db")
        _seed(db)
        monkeypatch.setenv("SMARTMONEY_CONFLUENCE_DEMO", "1")
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()
        j = c.get("/api/signals/confluence?window=90").get_json()
        assert j["signals"]
        assert j["metadata"]["provider"] == "sample_confluence"
        assert j["metadata"]["demo_mode"] is True
        assert j["metadata"]["sample_data"] is True


def test_confluence_cache_institutional_fields_are_repaired_from_db(monkeypatch):
    import json

    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "cache-enrich.db")
        s = Store(db)
        for cik, label, shares, value in (
            ("0000000001", "Fund One", 1000, 100),
            ("0000000002", "Fund Two", 2000, 200),
        ):
            _save(s, cik, label, "PM", f"{cik}-old", "13F-HR",
                  "2026-02-14", "2025-12-31",
                  [("COCA COLA", "191216100", 1000, 100, "")])
            _save(s, cik, label, "PM", f"{cik}-new", "13F-HR",
                  "2026-05-15", "2026-03-31",
                  [("APPLE INC", AAPL, shares, value, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.commit()
        s.close()

        cache_dir = Path(d)
        stale = {
            "kpis": {"n_signals": 1, "window_days": 90},
            "signals": [{
                "ticker": "AAPL",
                "score": 50,
                "institutional": {
                    "fund_labels": ["Fund One", "Fund Two"],
                    "funds_accumulating": 2,
                    "avg_weight_pct": 0.0,
                    "conviction_funds": 0,
                    "total_value_usd": 0.0,
                },
            }],
        }
        (cache_dir / "confluence-90.json").write_text(json.dumps(stale))
        monkeypatch.setenv("SMARTMONEY_CACHE_DIR", str(cache_dir))

        c = create_app(db, secure_cookies=False, open_mode=True).test_client()
        j = c.get("/api/signals/confluence?window=90").get_json()
        inst = j["signals"][0]["institutional"]

    assert inst["total_value_usd"] == 3000.0
    assert inst["avg_weight_pct"] == 100.0
    assert inst["conviction_funds"] == 2
    assert inst["quarters_ago"] == 0
    assert j["metadata"]["cache_institutional_enrichment"]["signals_enriched"] == 1


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
    assert inst.quarters_ago == 0
    assert provider.confluence_metadata()["effective_universe"].startswith(
        "Form 4 scans are limited"
    )
