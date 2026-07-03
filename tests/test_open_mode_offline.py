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
from tests.test_db_offline import AAPL, MSFT, _save


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

        assert "<h1>13FLOW</h1>" in html
        assert '<span id="srcText" class="pill">LIVE · EDGAR</span>' in html
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
        assert "Open Confluence" in html
        assert 'href="/confluence"' in html
        assert "No complete view of shorts" in html
        assert "No exhaustive insider-only" in html
        assert "Open research app" in html
        assert 'href="/app"' in html
        assert 'href="/developers"' in html
        assert 'href="/methodology"' in html
        assert 'href="/pro"' in html
        assert 'href="/status"' in html
        assert 'href="/faq"' in html
        assert 'href="faq.html"' not in html
        assert "Public filings research. Not investment advice." in html

        app_html = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/app").get_data(as_text=True)
        assert "The Filings" in app_html
        assert '<span id="srcText">LIVE · EDGAR</span>' in app_html

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
        assert "shorts" in product["data"]["coverage_boundary"]["form_13f"]
        assert "10b5-1" in product["data"]["coverage_boundary"]["form_4"]
        assert "not exhaustive" in product["data"]["coverage_boundary"]["insider_universe"]
        assert product["validation"]["status"] == \
            "mechanical_evidence_ready_for_review_metrics_unreviewed"
        artifact = product["validation"]["current_artifact"]
        assert artifact["scope"] == "25-ticker mature 13F + Form 4 joined validation artifact"
        assert artifact["schema_status"] == "valid_minimum_schema"
        assert artifact["metrics_status"] == "minimum_schema_valid_metrics_unreviewed"
        assert artifact["evidence_review_status"] == "mechanical_evidence_ready_for_review"
        assert artifact["row_count"] == 125
        assert artifact["ticker_count"] == 25
        assert artifact["row_error_count"] == 0
        assert artifact["forward_return_coverage"]["forward_return_120d"] == 1.0
        assert artifact["public_validation_claim"] is False
        metrics = product["validation"]["metrics_snapshot"]
        assert metrics["horizon_days"] == 60
        assert metrics["n"] == 113
        assert metrics["rank_ic"] == -0.003655
        assert "weak_or_neutral_descriptive_metrics" in metrics["interpretation"]
        assert product["validation"]["current_artifact"]["publishable_as_full_validation"] is False
        assert "validated alpha" in product["offer_boundary"]["do_not_claim_yet"]
        assert "25-ticker mature Form 4 joined mechanical evidence pack ready for human review" \
            in product["offer_boundary"]["sell_now"]
        assert "verifiable SEC EDGAR-derived 13F data" in product["offer_boundary"]["sell_now"]


def test_dashboard_source_does_not_embed_legacy_retail_chrome():
    html = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8")
    forbidden = [
        "SAMPLE DATA",
        "Sign in",
        "Upgrade to Pro",
        "Continue to checkout",
        "€12",
        'data-view="alerts"',
        'document.getElementById("closeUpgrade").onclick=',
        'document.getElementById("goCheckout").onclick=',
        'document.getElementById("authSubmit").onclick=',
    ]

    for needle in forbidden:
        assert needle not in html


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
            ("/status", "Evidence status"),
            ("/pro", "13FLOW Pro API"),
            ("/developers", "MCP tools/list"),
            ("/methodology", "Application methodology"),
            ("/methodology/app", "13F filings are delayed regulatory disclosures"),
            ("/methodology/mcp", "Pro tools must fail closed"),
            ("/faq", "Frequently asked questions"),
            ("/about", "13FLOW is operated by l0g"),
            ("/legal", "Legal, privacy and data terms"),
            ("/legal/pro-api", "Pro API, MCP and x402 terms"),
        ):
            r = c.get(path)
            assert r.status_code == 200, path
            assert needle in r.get_data(as_text=True), path

        for path, target in (
            ("/dashboard.html", "/app"),
            ("/confluence", "/app#confluence"),
            ("/faq.html", "/faq"),
            ("/mentions-legales", "/legal"),
            ("/mentions-legales.html", "/legal"),
        ):
            r = c.get(path, follow_redirects=False)
            assert r.status_code in {301, 302}, path
            assert r.headers["Location"] == target, path

        doc = c.get("/api/openapi.json").get_json()
        assert "/api/mcp" in doc["paths"]
        assert "/api/product-status" in doc["paths"]
        assert "/api/pro-offer" in doc["paths"]
        assert "/api/methodology/confluence-v1" in doc["paths"]
        assert "/api/methodology/app" in doc["paths"]
        assert "/api/methodology/mcp" in doc["paths"]
        assert "/api/stocks/{ticker}" in doc["paths"]

        offer = c.get("/api/pro-offer").get_json()
        assert offer["offer"]["name"] == "13FLOW Pro API"
        assert offer["offer"]["self_serve_checkout"] is False
        assert offer["offer"]["contact"]["email"] == "admin@toonux.com"
        assert [p["name"] for p in offer["plans"]] == [
            "Technical pilot review",
            "API integration review",
            "MCP integration review",
        ]
        assert "organization name and billing contact" in offer["buyer_checklist"]
        sales_packet = offer["sales_packet"]
        assert "Which desk, product or automated workflow" in sales_packet["qualification_questions"][0]
        assert "Before I issue a scoped pilot key" in sales_packet["lead_reply_template"]
        assert sales_packet["operator_note_schema"]["package"] == \
            "Technical pilot review | API integration review | MCP integration review"
        assert "Verify the key id in api_audit" in sales_packet["pilot_handoff"][3]
        commercial = offer["commercial_model"]
        assert commercial["pricing_status"] == "paused_until_terms_and_capacity_are_ready"
        assert commercial["recommended_packages"][0]["price_eur_per_month"] == "not publicly quoted"
        assert commercial["do_not_discount_below"]["full_live_api_access_eur_per_month"] is None
        assert "raw SEC data" in commercial["principle"]
        assert commercial["pricing_policy"]["strategy"] == "bounded_operator_review_before_any_quote"
        assert "13F plus Form 4 confluence workflow" in commercial["pricing_policy"]["compete_on"]
        assert [item["provider"] for item in commercial["market_context"]] == [
            "SEC.gov",
            "SEC-API.io",
            "Quiver Quantitative",
            "Dataroma",
        ]
        assert "/api/pro/v1/openapi.json" in commercial["evidence_pack"]
        assert offer["default_limits"]["rate_per_min"] == 120
        assert "validated alpha" in offer["not_included_yet"]
        assert "create_key" in offer["operator_commands"]
        assert offer["truth_boundary"]["current_artifact"]["publishable_as_full_validation"] is False

        app_method = c.get("/api/methodology/app").get_json()
        assert app_method["current_state"]["public_state"] == "LIVE"
        assert "13F filings are delayed regulatory disclosures" in app_method["user_interpretation"][0]
        assert any("open public build has no browser account" in item
                   for item in app_method["verified_now"])
        assert any("25-ticker mature 13F + Form 4 joined validation artifact" in item
                   for item in app_method["verified_now"])
        assert any("Confluence v1 is not validated as alpha" in item
                   for item in app_method["not_verified_yet"])
        assert any("metrics remain unreviewed" in item
                   for item in app_method["not_verified_yet"])
        assert "verifiable SEC EDGAR-derived 13F data" in app_method["sellable_now"]
        assert "validated alpha" in app_method["not_claimed"]

        app_method_page = c.get("/methodology/app").get_data(as_text=True)
        assert "What is verified" in app_method_page
        assert "What is not verified yet" in app_method_page
        assert "Sellable now" in app_method_page
        assert "Do not claim" in app_method_page
        assert "Current validation artifact" in app_method_page
        assert "mechanical_evidence_ready_for_review" in app_method_page
        assert "minimum_schema_valid_metrics_unreviewed" in app_method_page
        assert "Public validation claim" in app_method_page
        assert "Publishable as full validation" in app_method_page

        mcp_method = c.get("/api/methodology/mcp").get_json()
        assert "Pro tools must fail closed" in mcp_method["contract"][1]
        assert mcp_method["security"]["credential_headers"][0].startswith("Authorization")

        pro_page = c.get("/pro").get_data(as_text=True)
        assert "Request access" in pro_page
        assert "Access request checklist" in pro_page
        assert "Operator lead kit" in pro_page
        assert "not publicly quoted" in pro_page
        assert "Technical pilot review" in pro_page
        assert "limited-capacity service" in pro_page
        assert "490 EUR / month" not in pro_page
        assert "Competitive position" in pro_page
        assert "bounded_operator_review_before_any_quote" in pro_page
        assert "Quiver Quantitative" in pro_page
        assert "Evidence pack" in pro_page
        assert 'href="/validation"' in pro_page
        assert "Public filings research. Not investment advice." in pro_page
        assert 'href="/developers"' in pro_page
        assert 'href="/api/live-status"' in pro_page
        assert 'href="/status"' in pro_page

        validation_page = c.get("/validation").get_data(as_text=True)
        assert "Current Confluence evidence pack" in validation_page
        assert "Mechanical Evidence" in validation_page
        assert "Descriptive Metrics" in validation_page
        assert "weak or neutral" in validation_page
        assert "Rank IC" in validation_page
        assert "-0.003655" in validation_page
        assert "Public validation claim" in validation_page
        assert "Publishable as full validation" in validation_page
        assert "What this does not prove" in validation_page
        assert "It does not prove validated alpha" in validation_page
        assert "/api/product-status" in validation_page

        status_page = c.get("/status").get_data(as_text=True)
        assert "Use this page to distinguish deployed production state" in status_page
        assert "uses_synthetic_data=false" in status_page
        assert "Berkshire Hathaway" not in status_page
        assert "/api/live-status" in status_page
        assert "/api/product-status" in status_page
        assert "mechanical_evidence_ready_for_review" in status_page
        assert "minimum_schema_valid_metrics_unreviewed" in status_page
        assert "rows=125" in status_page
        assert "Public validation claim" in status_page
        assert "Publishable as full validation" in status_page
        assert "validated alpha" in status_page
        assert "Public filings research. Not investment advice." in status_page

        developers = c.get("/developers").get_data(as_text=True)
        assert "/status" in developers
        assert "/api/openapi.json" in developers
        assert "/api/pro/v1/openapi.json" in developers
        assert "Pro tools are intentionally visible" in developers
        assert "Redistribution" in developers
        assert "SEC EDGAR-derived 13F and Form 4 research surfaces" in developers

        about_page = c.get("/about").get_data(as_text=True)
        assert "Filing intelligence, built in the l0g lab" in about_page
        assert "https://l0g.fr/" in about_page
        assert "13FLOW is operated by l0g" in about_page
        assert "machine-readable financial intelligence" in about_page
        assert "It does not sell a magic trading signal" in about_page
        assert 'href="/legal"' in about_page

        legal_page = c.get("/legal").get_data(as_text=True)
        assert "GDPR / RGPD" in legal_page
        assert "CNIL" in legal_page
        assert "admin@toonux.com" in legal_page
        assert "advertising or behavioral analytics cookies" in legal_page
        assert "operated and published by" in legal_page
        assert "https://l0g.fr/" in legal_page
        assert "Technical server logs" in legal_page
        assert "Pro API terms" in legal_page
        assert "Built by" in legal_page

        pro_terms = c.get("/legal/pro-api").get_data(as_text=True)
        assert "Self-serve checkout is disabled" in pro_terms
        assert "No public package pricing" in pro_terms
        assert "Access can be declined" in pro_terms
        assert "No resale, redistribution" in pro_terms
        assert "does not sell raw SEC access as proprietary data" in pro_terms

        api_stock = c.get("/api/stocks/AAPL").get_json()
        assert api_stock["ticker"] == "AAPL"
        assert api_stock["holder_count"] == 1
        assert api_stock["confidence"]["status"] == "ok"
        assert api_stock["score"]["version"] == "ticker_flow_v1"
        assert "movements" in api_stock

        mcp = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
        }).get_json()
        assert any(t["name"] == "stocks.get" for t in mcp["result"]["tools"])
        assert any(t["name"] == "product.status" for t in mcp["result"]["tools"])
        assert any(t["name"] == "pro.offer" for t in mcp["result"]["tools"])

        stock = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "stocks.get", "arguments": {"ticker": "AAPL"}},
        }).get_json()
        assert stock["result"]["structuredContent"]["ticker"] == "AAPL"
        assert stock["result"]["structuredContent"]["holder_count"] == 1
        assert stock["result"]["structuredContent"]["score"]["version"] == "ticker_flow_v1"

        product = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "product.status", "arguments": {}},
        }).get_json()
        assert product["result"]["structuredContent"]["validation"]["current_artifact"][
            "publishable_as_full_validation"
        ] is False

        pro_offer = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "pro.offer", "arguments": {}},
        }).get_json()
        assert pro_offer["result"]["structuredContent"]["offer"]["self_serve_checkout"] is False


def test_ticker_flow_payload_explains_quarter_moves_and_confidence():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "ticker-flow.db")
        s = Store(db)
        # Q1: Berkshire and Greenlight hold AAPL.
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 1000, 100, "")])
        _save(s, "0001489933", "Greenlight", "David Einhorn",
              "GL-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 800, 80, "")])
        # Q2: Berkshire adds, Pershing opens, Greenlight exits AAPL.
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1500, 130, "")])
        _save(s, "0001336528", "Pershing Square", "Bill Ackman",
              "PS-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 700, 35, "")])
        _save(s, "0001489933", "Greenlight", "David Einhorn",
              "GL-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("MICROSOFT", MSFT, 900, 20, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.execute("UPDATE holdings SET ticker='MSFT' WHERE cusip=?", (MSFT,))
        s.conn.commit()
        s.close()

        c = create_app(db, secure_cookies=False, open_mode=True).test_client()
        payload = c.get("/api/stocks/AAPL").get_json()

        assert payload["latest_13f_quarter"] == "2026-03-31"
        assert payload["movement_summary"]["holder_count"] == 2
        assert payload["movement_summary"]["buyers_count"] == 2
        assert payload["movement_summary"]["sellers_count"] == 1
        assert payload["movement_summary"]["new_positions"] == 1
        assert payload["movement_summary"]["exits"] == 1
        assert payload["confidence"]["status"] == "ok"
        assert payload["score"]["score"] > 0
        moves = {(m["label"], m["move"]) for m in payload["movements"]}
        assert ("Berkshire Hathaway", "ADD") in moves
        assert ("Pershing Square", "NEW") in moves
        assert ("Greenlight", "EXIT") in moves

        html = c.get("/stocks/AAPL").get_data(as_text=True)
        assert "Ticker Flow Score" in html
        assert "Quarter Moves" in html
        assert "Data Confidence" in html


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
        assert "shorts" in j["metadata"]["filing_scope_boundary"]["form_13f"]
        assert "10b5-1" in j["metadata"]["filing_scope_boundary"]["form_4"]
        assert any("Form 4 universe is partial" in item
                   for item in j["metadata"]["known_limitations"])
        assert any("Table II" in item and "10b5-1" in item
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
