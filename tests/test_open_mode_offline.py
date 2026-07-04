"""
Open-build gating: with open_mode=True the app must register only public, read-only
endpoints — no auth, no billing, no subscriptions — and still serve the public screens.
Core V1 no longer ships a browser account/payment build, so open_mode=False must
not re-enable auth, billing or subscriptions. No network.
"""
import base64
import hashlib
import hmac
import os
import re
import struct
import time

import tempfile
from pathlib import Path

from smartmoney.db import Store
from smartmoney.api import create_app
from tests.test_db_offline import AAPL, MSFT, _save


def _totp(secret: str, when: int | None = None) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int((when if when is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


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
                     "/api/commercial-readiness",
                     "/api/security-posture",
                     "/api/pilot-intake",
                     "/api/pilot-intake.md",
                     "/api/buyer-pack",
                     "/api/buyer-pack.md",
                     "/api/version", "/healthz", "/", "/coverage"):
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


def test_core_v1_does_not_reenable_legacy_full_build():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "full.db")
        _seed(db)
        c = create_app(db, secure_cookies=False, open_mode=False).test_client()
        cfg = c.get("/api/config").get_json()
        assert cfg["open"] is True
        assert cfg["features"] == {"auth": False, "alerts": False, "billing": False,
                                   "pro_api": False}
        assert c.get("/api/auth/me").status_code == 404
        assert c.get("/api/billing/config").status_code == 404
        assert c.get("/api/subscriptions").status_code == 404


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
        assert "Open Signals" in html
        assert 'href="/signals"' in html
        topnav = html.split("</nav>", 1)[0]
        for label in ("Confluence", "Status", "Coverage", "Security", "Validation", "Methodology", "Pilot", "About"):
            assert f">{label}<" not in topnav
        for label in ("Cockpit", "Signals", "Funds", "Stocks", "API", "Pro"):
            assert f">{label}<" in topnav
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

        readiness = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/api/commercial-readiness").get_json()
        assert readiness["status"] in {
            "controlled_pilot_ready",
            "controlled_pilot_ready_with_disclosures",
        }
        assert readiness["sales_motion"] == "controlled_pilot_only"
        assert readiness["self_serve_checkout"] is False
        assert readiness["public_quote_ready"] is False
        assert readiness["snapshot"]["quality_gate"]["trusted_funds"] >= 1
        assert readiness["snapshot"]["quality_gate"][
            "human_review_required_for_routine_publication"
        ] is False
        assert any(
            check["id"] == "public_live_data" and check["status"] == "pass"
            for check in readiness["public_checks"]
        )
        assert any(
            check["id"] == "pro_workspace_smoke"
            and check["status"] == "external_required"
            for check in readiness["external_checks"]
        )
        assert "validated alpha" in readiness["do_not_claim_yet"]

        buyer_pack = create_app(db, secure_cookies=False, open_mode=True).test_client() \
            .get("/api/buyer-pack").get_json()
        assert buyer_pack["status"] in {
            "controlled_pilot_ready",
            "controlled_pilot_ready_with_disclosures",
        }
        assert buyer_pack["sales_motion"] == "controlled_pilot_only"
        assert buyer_pack["public_quote_ready"] is False
        assert buyer_pack["self_serve_checkout"] is False
        assert buyer_pack["snapshot"]["trusted_funds"] >= 1
        assert buyer_pack["terms_boundary"]["operator_review_required"] is True
        assert "validated alpha" in buyer_pack["do_not_claim_yet"]
        assert any(item["href"] == "/pro/onboarding" for item in buyer_pack["evidence_links"])
        assert any("Pro API keys are scoped" in item for item in buyer_pack["proof_points"])


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
            ("/coverage", "Trusted Fund Coverage"),
            ("/security", "Controlled Pilot Security"),
            ("/pilot", "Controlled Pilot Intake"),
            ("/readiness", "Readiness Checklist"),
            ("/buyer-pack", "13FLOW Buyer Review Pack"),
            ("/buyer-pack/print", "PDF-ready printable view"),
            ("/pro", "13FLOW Pro API"),
            ("/pro/onboarding", "Integration Diagnostic"),
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
        assert "/api/commercial-readiness" in doc["paths"]
        assert "/api/security-posture" in doc["paths"]
        assert "/api/pilot-intake" in doc["paths"]
        assert "/api/pilot-intake.md" in doc["paths"]
        assert "/api/pilot-request-assist" in doc["paths"]
        assert "/api/buyer-pack" in doc["paths"]
        assert "/api/buyer-pack.md" in doc["paths"]
        assert "/api/pro-offer" in doc["paths"]
        assert "/api/methodology/confluence-v1" in doc["paths"]
        assert "/api/methodology/app" in doc["paths"]
        assert "/api/methodology/mcp" in doc["paths"]
        assert "/api/stocks/{ticker}" in doc["paths"]
        assert "/api/watchlist/discover" in doc["paths"]

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
        assert [item["category"] for item in commercial["market_context"]] == [
            "official_source",
            "generic_sec_api_vendor",
            "generic_alternative_data_platform",
            "generic_free_curated_portfolio_site",
        ]
        assert all(item["provider"].startswith(("SEC", "unnamed")) for item in commercial["market_context"])
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
        assert "Source-position boundary" in pro_page
        assert "bounded_operator_review_before_any_quote" in pro_page
        assert "generic alternative data platform" in pro_page
        assert "generic sec api vendor" in pro_page
        assert "Evidence pack" in pro_page
        assert 'href="/buyer-pack"' in pro_page
        assert "Buyer pack" in pro_page
        assert 'href="/validation"' in pro_page
        assert 'href="/pro/onboarding"' in pro_page
        assert "Onboarding diagnostic" in pro_page
        assert 'href="/pro/workspace"' in pro_page
        assert "Workspace cockpit" in pro_page
        assert "Public filings research. Not investment advice." in pro_page
        assert 'href="/developers"' in pro_page
        assert 'href="/api/live-status"' in pro_page
        assert 'href="/status"' in pro_page

        pro_workspace_page = c.get("/pro/workspace").get_data(as_text=True)
        assert "Workspace Cockpit" in pro_workspace_page
        assert "Saved watchlists, ticker-flow alerts" in pro_workspace_page
        assert "data-pro-workspace-app" in pro_workspace_page
        assert "Edit Watchlist" in pro_workspace_page
        assert "Save changes" in pro_workspace_page
        assert "workspaceCancelEdit" in pro_workspace_page
        assert "Workspace Report" in pro_workspace_page
        assert "workspaceReportRefresh" in pro_workspace_page
        assert "renderWorkspaceReport" in pro_workspace_page
        assert "/workspace/report?watchlist_id=" in pro_workspace_page
        assert "Export JSON" in pro_workspace_page
        assert "Export CSV" in pro_workspace_page
        assert "downloadWorkspaceExport" in pro_workspace_page
        assert "/api/pro/v1/workspace/export?format=${safeFormat}" in pro_workspace_page
        assert "URL.createObjectURL" in pro_workspace_page
        assert "Scheduled alerts" in pro_workspace_page
        assert "alert_enabled" in pro_workspace_page
        assert "alert_frequency" in pro_workspace_page
        assert "Scheduled alerts require daily or weekly frequency" in pro_workspace_page
        assert "policy.enabled ? (\"alerts:\"" in pro_workspace_page
        assert 'type="password"' in pro_workspace_page
        assert "sessionStorage" in pro_workspace_page
        assert "13flow.pro.workspace.token" in pro_workspace_page
        assert "Authorization" in pro_workspace_page
        assert "Bearer " in pro_workspace_page
        assert "/api/pro/v1\" + path" in pro_workspace_page
        assert "api(\"/workspace/overview\")" in pro_workspace_page
        assert "workspaceAlertStatus" in pro_workspace_page
        assert "workspaceAlertTicker" in pro_workspace_page
        assert "workspaceAlertMinSeverity" in pro_workspace_page
        assert "workspaceAlertMinScore" in pro_workspace_page
        assert "workspaceAlertSort" in pro_workspace_page
        assert "visibleAlerts" in pro_workspace_page
        assert "showing=${esc(number(visible.length))}" in pro_workspace_page
        assert "Ack visible" in pro_workspace_page
        assert "Dismiss visible" in pro_workspace_page
        assert "workspaceAckAll" in pro_workspace_page
        assert "workspaceDismissAll" in pro_workspace_page
        assert "Alert Details" in pro_workspace_page
        assert "workspaceAlertDetail" in pro_workspace_page
        assert "data-alert-detail" in pro_workspace_page
        assert "renderAlertDetail" in pro_workspace_page
        assert "watchlist=${esc(alert.watchlist_id)}" in pro_workspace_page
        assert "status=${encodeURIComponent(state.alertStatus)}&limit=50" in pro_workspace_page
        assert "updateVisibleAlerts(\"acknowledged\")" in pro_workspace_page
        assert "updateVisibleAlerts(\"dismissed\")" in pro_workspace_page
        assert "api(\"/workspace/watchlists\"" in pro_workspace_page
        assert "method: \"PUT\"" in pro_workspace_page
        assert "method: \"PATCH\"" in pro_workspace_page
        assert "alert_policy: {enabled: alertEnabled" in pro_workspace_page
        assert "/signals/snapshot" in pro_workspace_page
        assert "/delete" in pro_workspace_page
        assert "window.confirm" in pro_workspace_page
        assert "localStorage" not in pro_workspace_page
        assert "?token=" not in pro_workspace_page
        assert "checkout" not in pro_workspace_page.lower()

        pro_onboarding_page = c.get("/pro/onboarding").get_data(as_text=True)
        assert "Integration Diagnostic" in pro_onboarding_page
        assert "data-pro-onboarding-app" in pro_onboarding_page
        assert "13flow.pro.onboarding.token" in pro_onboarding_page
        assert "/api/pro/v1/onboarding" in pro_onboarding_page
        assert "sessionStorage" in pro_onboarding_page
        assert "Authorization" in pro_onboarding_page
        assert "token_echoed" in pro_onboarding_page
        assert "token_in_url_allowed" in pro_onboarding_page
        assert "localStorage" not in pro_onboarding_page
        assert "?token=" not in pro_onboarding_page
        assert "checkout" not in pro_onboarding_page.lower()

        assert c.get("/pro/admin").status_code == 404

        monkeypatch.setenv(
            "SMARTMONEY_ADMIN_PANEL_PASSWORD_SHA256",
            "1c8bfe8f801d79745c4631d09fff36c82aa37fc4cce4fc946683d7b336b63032",
        )
        monkeypatch.setenv("SMARTMONEY_ADMIN_SESSION_SECRET", "test-admin-session-secret")
        protected = create_app(db, secure_cookies=False, open_mode=True).test_client()
        unauth_admin = protected.get("/pro/admin")
        assert unauth_admin.status_code == 302
        assert unauth_admin.headers["Location"].endswith("/pro/admin/login")
        login_page = protected.get("/pro/admin/login").get_data(as_text=True)
        assert "13FLOW Admin" in login_page
        assert "name=\"csrf\"" in login_page
        assert "totp_required=false" in login_page
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page).group(1)
        bad_login = protected.post(
            "/pro/admin/login",
            data={"csrf": csrf, "username": "admin", "password": "bad"},
        )
        assert bad_login.status_code == 401
        login_page = protected.get("/pro/admin/login").get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page).group(1)
        login = protected.post(
            "/pro/admin/login",
            data={"csrf": csrf, "username": "admin", "password": "letmein"},
            follow_redirects=True,
        )
        assert login.status_code == 200
        pro_admin_page = login.get_data(as_text=True)
        assert "Admin Console" in pro_admin_page
        assert "data-pro-admin-app" in pro_admin_page
        assert "admin:write" in pro_admin_page
        assert "Server-side admin session protects this page" in pro_admin_page
        assert "13flow.pro.admin.token" in pro_admin_page
        assert "adminLogout" in pro_admin_page
        assert "/api/pro/v1\" + path" in pro_admin_page
        assert "include=surface" in pro_admin_page
        assert "/admin/health" in pro_admin_page
        assert "/admin/pilot-fulfillment" in pro_admin_page
        assert "/admin/buyer-handoff" in pro_admin_page
        assert "/admin/release-readiness" in pro_admin_page
        assert "/admin/pilot-closeout" in pro_admin_page
        assert "/admin/pilot-renewal" in pro_admin_page
        assert "/admin/pilot-request-assist" in pro_admin_page
        assert "Create API key" in pro_admin_page
        assert "data-revoke-key" in pro_admin_page
        assert "/admin/keys" in pro_admin_page
        assert "Priority" in pro_admin_page
        assert "Errors" in pro_admin_page
        assert "renderCloseout" in pro_admin_page
        assert "renderRenewal" in pro_admin_page
        assert "renderRelease" in pro_admin_page
        assert "adminReviewRequest" in pro_admin_page
        assert "web_worker_creates_tokens" in pro_admin_page
        assert "auth_self_serve" in pro_admin_page
        assert "payment_self_serve" in pro_admin_page
        assert "operator_events" in pro_admin_page
        assert "Recent operator events" in pro_admin_page
        assert "Rotation due" in pro_admin_page
        assert "rotation_due_at" in pro_admin_page
        assert "sessionStorage" in pro_admin_page
        assert "Authorization" in pro_admin_page
        assert "localStorage" not in pro_admin_page
        assert "?token=" not in pro_admin_page
        assert protected.post("/pro/admin/logout").status_code == 302
        assert protected.get("/pro/admin").status_code == 302

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

        readiness_page = c.get("/readiness").get_data(as_text=True)
        assert "Readiness Checklist" in readiness_page
        assert "Commercial readiness" in readiness_page
        assert "controlled_pilot" in readiness_page
        assert "Public Checks" in readiness_page
        assert "External Operator Checks" in readiness_page
        assert "/api/commercial-readiness" in readiness_page
        assert "/pro/admin" not in readiness_page
        assert "/api/pro/v1/admin/health" in readiness_page
        assert "validated alpha" in readiness_page

        security = c.get("/api/security-posture").get_json()
        assert security["status"] == "controlled_pilot_security_ready"
        assert security["public_surface"]["mode"] == "read_only_open_build"
        assert security["public_surface"]["synthetic_data"] is False
        assert security["pro_surface"]["token_in_url_allowed"] is False
        assert security["privacy"]["tokens_echoed"] is False
        assert security["privacy"]["secrets_in_payloads"] is False
        assert security["data_quality"]["manual_13f_review_required_for_routine_publication"] is False
        assert "third-party penetration test" in security["non_claims"]
        assert "/api/security-posture" in [x["href"] for x in security["evidence_links"]]

        security_page = c.get("/security").get_data(as_text=True)
        assert "Controlled Pilot Security" in security_page
        assert "Machine-readable security posture" in security_page
        assert "Operator Checks" in security_page
        assert "Non-Claims" in security_page
        assert "tokens_echoed:false" in security_page
        assert "secrets_in_payloads:false" in security_page
        assert "third-party penetration test" in security_page

        pilot = c.get("/api/pilot-intake").get_json()
        assert pilot["status"] == "operator_review_required"
        assert pilot["self_serve_checkout"] is False
        assert pilot["public_form_submission"] is False
        assert pilot["public_submission_endpoint"] is None
        assert pilot["privacy"]["server_side_pii_storage"] is False
        assert pilot["privacy"]["token_collection"] is False
        assert pilot["privacy"]["secret_collection"] is False
        assert "organization" in [x["id"] for x in pilot["required_fields"]]
        assert "requested_scopes" in [x["id"] for x in pilot["required_fields"]]
        assert "13FLOW PILOT INTAKE" in pilot["operator_note_template"][0]

        pilot_assist = c.get("/api/pilot-request-assist").get_json()
        assert pilot_assist["public_submission_endpoint"] is None
        assert pilot_assist["server_side_pii_storage"] is False
        assert pilot_assist["request_persisted"] is False
        assert pilot_assist["tokens_collected"] is False
        assert pilot_assist["web_worker_creates_tokens"] is False
        assert "organization" in pilot_assist["input_schema"]["required"]
        assert "admin:read" in pilot_assist["input_schema"]["forbidden_customer_scopes"]
        assert pilot_assist["admin_transform"]["endpoint"] == "/api/pro/v1/admin/pilot-request-assist"
        assert pilot_assist["admin_transform"]["stores_request"] is False
        assert pilot_assist["privacy"]["payloads_logged"] is False
        assert "13flow_live_" not in str(pilot_assist)

        pilot_page = c.get("/pilot").get_data(as_text=True)
        assert "Controlled Pilot Intake" in pilot_page
        assert "Operator Note Template" in pilot_page
        assert "Required Fields" in pilot_page
        assert "public_form_submission=false" in pilot_page
        assert "server_side_pii_storage=false" in pilot_page
        assert "/api/pilot-intake" in pilot_page
        assert "/api/pilot-intake.md" in pilot_page

        pilot_request_page = c.get("/pilot/request").get_data(as_text=True)
        assert "Assisted Pilot Request" in pilot_request_page
        assert "data-pilot-request-app" in pilot_request_page
        assert "/api/pilot-request-assist" in pilot_request_page
        assert "public_submission_endpoint:none" in pilot_request_page
        assert "server_side_pii_storage:false" in pilot_request_page
        assert "navigator.clipboard.writeText" in pilot_request_page
        assert "localStorage" not in pilot_request_page
        assert "sessionStorage" not in pilot_request_page

        pilot_md_resp = c.get("/api/pilot-intake.md")
        assert pilot_md_resp.status_code == 200
        assert pilot_md_resp.mimetype == "text/markdown"
        pilot_md = pilot_md_resp.get_data(as_text=True)
        assert "# 13FLOW Pilot Intake" in pilot_md
        assert "Public form submission: false" in pilot_md
        assert "## Operator Note Template" in pilot_md
        assert "requested_scopes" in pilot_md
        assert "/security" in pilot_md

        buyer_pack_page = c.get("/buyer-pack").get_data(as_text=True)
        assert "13FLOW Buyer Review Pack" in buyer_pack_page
        assert "Proof Points" in buyer_pack_page
        assert "Buyer Checklist" in buyer_pack_page
        assert "Qualification Questions" in buyer_pack_page
        assert "Pilot Handoff" in buyer_pack_page
        assert "Terms Boundary" in buyer_pack_page
        assert "/api/buyer-pack" in buyer_pack_page
        assert "/api/buyer-pack.md" in buyer_pack_page
        assert "/buyer-pack/print" in buyer_pack_page
        assert "/coverage" in buyer_pack_page
        assert "/pilot" in buyer_pack_page
        assert "/security" in buyer_pack_page
        assert "/pro/onboarding" in buyer_pack_page
        assert "not a performance claim" in buyer_pack_page
        assert "validated alpha" in buyer_pack_page

        buyer_pack_print = c.get("/buyer-pack/print").get_data(as_text=True)
        assert "13FLOW Buyer Review Pack" in buyer_pack_print
        assert "PDF-ready printable view" in buyer_pack_print
        assert "Proof Points" in buyer_pack_print
        assert "Pilot Packages" in buyer_pack_print
        assert "Terms Boundary" in buyer_pack_print
        assert "/api/buyer-pack.md" in buyer_pack_print
        assert "not investment advice" in buyer_pack_print

        buyer_pack_md_resp = c.get("/api/buyer-pack.md")
        assert buyer_pack_md_resp.status_code == 200
        assert buyer_pack_md_resp.mimetype == "text/markdown"
        buyer_pack_md = buyer_pack_md_resp.get_data(as_text=True)
        assert "# 13FLOW Buyer Review Pack" in buyer_pack_md
        assert "## Proof Points" in buyer_pack_md
        assert "## Evidence Links" in buyer_pack_md
        assert "not investment advice" in buyer_pack_md
        assert "/coverage" in buyer_pack_md
        assert "/pilot" in buyer_pack_md
        assert "/security" in buyer_pack_md

        coverage_page = c.get("/coverage").get_data(as_text=True)
        assert "Trusted Fund Coverage" in coverage_page
        assert "Signal Eligibility Rule" in coverage_page
        assert "Excluded Funds" in coverage_page
        assert "Trusted Sample" in coverage_page
        assert "/api/data-quality" in coverage_page
        assert "/methodology" in coverage_page
        assert "not a performance claim" in coverage_page

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
        assert any(t["name"] == "watchlist.preview" for t in mcp["result"]["tools"])
        assert any(t["name"] == "watchlist.discover" for t in mcp["result"]["tools"])
        assert any(t["name"] == "product.status" for t in mcp["result"]["tools"])
        assert any(t["name"] == "pro.offer" for t in mcp["result"]["tools"])

        stock = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "stocks.get", "arguments": {"ticker": "AAPL"}},
        }).get_json()
        assert stock["result"]["structuredContent"]["ticker"] == "AAPL"
        assert stock["result"]["structuredContent"]["holder_count"] == 1
        assert stock["result"]["structuredContent"]["score"]["version"] == "ticker_flow_v1"

        watchlist = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "watchlist.preview", "arguments": {"tickers": ["AAPL"]}},
        }).get_json()
        assert watchlist["result"]["structuredContent"]["metadata"]["version"] == "watchlist_preview_v1"
        assert watchlist["result"]["structuredContent"]["items"][0]["ticker"] == "AAPL"

        discovery = c.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {
                "name": "watchlist.discover",
                "arguments": {"limit": 5, "action": "alert", "min_score": 80, "move": "NEW"},
            },
        }).get_json()
        assert discovery["result"]["structuredContent"]["metadata"]["version"] == \
            "watchlist_discovery_v1"
        assert discovery["result"]["structuredContent"]["metadata"][
            "human_review_required_for_routine_publication"
        ] is False
        assert discovery["result"]["structuredContent"]["metadata"]["filters"]["action"] == ["alert"]

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


def test_pro_admin_login_can_require_totp(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "admin-totp.db")
        _seed(db)
        secret = "JBSWY3DPEHPK3PXP"
        monkeypatch.setenv(
            "SMARTMONEY_ADMIN_PANEL_PASSWORD_SHA256",
            "1c8bfe8f801d79745c4631d09fff36c82aa37fc4cce4fc946683d7b336b63032",
        )
        monkeypatch.setenv("SMARTMONEY_ADMIN_SESSION_SECRET", "test-admin-session-secret")
        monkeypatch.setenv("SMARTMONEY_ADMIN_TOTP_SECRET", secret)
        monkeypatch.setenv("SMARTMONEY_ADMIN_TOTP_REQUIRED", "1")
        c = create_app(db, secure_cookies=False, open_mode=True).test_client()

        login_page = c.get("/pro/admin/login").get_data(as_text=True)
        assert "totp_required=true" in login_page
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page).group(1)
        missing_totp = c.post(
            "/pro/admin/login",
            data={"csrf": csrf, "username": "admin", "password": "letmein"},
        )
        assert missing_totp.status_code == 401

        login_page = c.get("/pro/admin/login").get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page).group(1)
        login = c.post(
            "/pro/admin/login",
            data={
                "csrf": csrf,
                "username": "admin",
                "password": "letmein",
                "totp": _totp(secret),
            },
            follow_redirects=True,
        )
        assert login.status_code == 200
        assert "Admin Console" in login.get_data(as_text=True)


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

        watchlist = c.get("/api/watchlist/preview?tickers=AAPL,MSFT").get_json()
        assert watchlist["metadata"]["version"] == "watchlist_preview_v1"
        assert watchlist["metadata"]["human_review_required_for_routine_publication"] is False
        assert "buyers count" in watchlist["metadata"]["rank_basis"]
        assert watchlist["summary"]["alerts"] >= 1
        by_ticker = {item["ticker"]: item for item in watchlist["items"]}
        assert by_ticker["AAPL"]["action"] in {"alert", "watch"}
        assert any(t["code"] in {"high_score", "accumulation", "new_position"}
                   for t in by_ticker["AAPL"]["triggers"])

        discovery = c.get("/api/watchlist/discover?limit=5").get_json()
        assert discovery["metadata"]["version"] == "watchlist_discovery_v1"
        assert discovery["metadata"]["human_review_required_for_routine_publication"] is False
        assert discovery["metadata"]["source"] == "trusted_ticker_flow"
        assert "new positions" in discovery["metadata"]["rank_basis"]
        assert discovery["metadata"]["returned_count"] <= 5
        assert discovery["metadata"]["quality_gate"]["trusted_funds"] == 3
        assert "quality_gate_detail" in discovery["metadata"]
        discovered = {item["ticker"] for item in discovery["items"]}
        assert {"AAPL", "MSFT"} <= discovered
        assert all("discovery" in item for item in discovery["items"])
        assert all("excluded_funds" not in item["quality_gate"] for item in discovery["items"])

        filtered = c.get(
            "/api/watchlist/discover?limit=5&action=alert&min_score=30&move=NEW"
            "&min_holders=1&min_buyers=1&exclude_mega_cap=1"
        ).get_json()
        assert filtered["metadata"]["filters"]["action"] == ["alert"]
        assert filtered["metadata"]["filters"]["move"] == ["NEW"]
        assert filtered["metadata"]["filters"]["min_score"] == 30.0
        assert filtered["metadata"]["filters"]["exclude_mega_cap"] is True
        assert filtered["metadata"]["filtered_count"] >= len(filtered["items"])
        assert filtered["items"]
        assert all(item["action"] == "alert" for item in filtered["items"])
        assert all(item["score"]["score"] >= 30 for item in filtered["items"])
        assert all("NEW" in item["movement_codes"] for item in filtered["items"])


def test_ticker_flow_uses_trusted_universe_and_exposes_automatic_exclusions():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "trusted-ticker-flow.db")
        s = Store(db)
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 1_000, 100, "")])
        _save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett",
              "BRK-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 1_100, 110, "")])
        # Pershing is active-registry but fails the automated current AUM jump gate.
        _save(s, "0001336528", "Pershing Square", "Bill Ackman",
              "PS-Q1", "13F-HR", "2026-02-14", "2025-12-31",
              [("APPLE INC", AAPL, 1, 1, "")])
        _save(s, "0001336528", "Pershing Square", "Bill Ackman",
              "PS-Q2", "13F-HR", "2026-05-15", "2026-03-31",
              [("APPLE INC", AAPL, 200_000, 100, "")])
        s.conn.execute("UPDATE holdings SET ticker='AAPL' WHERE cusip=?", (AAPL,))
        s.conn.commit()
        s.close()

        c = create_app(db, secure_cookies=False, open_mode=True).test_client()
        payload = c.get("/api/stocks/AAPL").get_json()

        assert payload["movement_summary"]["holder_count"] == 1
        assert [h["label"] for h in payload["holders"]] == ["Berkshire Hathaway"]
        assert {m["label"] for m in payload["movements"]} == {"Berkshire Hathaway"}
        assert payload["quality_gate"]["summary"]["trusted_funds"] == 1
        assert payload["quality_gate"]["summary"]["quarantined_funds"] == 1
        excluded = {f["label"]: f for f in payload["quality_gate"]["excluded_funds"]}
        assert excluded["Pershing Square"]["status"] == "quarantined"
        assert any(r["code"] == "current_aum_jump" for r in excluded["Pershing Square"]["reasons"])

        coverage_page = c.get("/coverage").get_data(as_text=True)
        assert "Trusted Fund Coverage" in coverage_page
        assert "Pershing Square" in coverage_page
        assert "quarantined" in coverage_page
        assert "current_aum_jump" in coverage_page
        assert "Berkshire Hathaway" in coverage_page


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
