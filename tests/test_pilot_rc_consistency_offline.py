import os

import tempfile
from pathlib import Path

from smartmoney.api import create_app
from tests.test_quality_offline import _seed_quality_db


def _client(tmpdir):
    data_db = str(Path(tmpdir) / "pilot-rc.db")
    store = _seed_quality_db(data_db)
    store.close()
    return create_app(data_db, secure_cookies=False, open_mode=True).test_client()


def _boundary(payload):
    boundary = payload.get("core_v1_boundary") or {}
    assert boundary["status"] == "controlled_pilot_core_v1"
    assert boundary["sales_motion"] == "controlled_pilot_only"
    assert boundary["operator_review_required"] is True
    assert boundary["public_open_build"]["read_only"] is True
    assert boundary["public_open_build"]["browser_accounts"] is False
    assert boundary["public_open_build"]["self_serve_checkout"] is False
    assert boundary["public_open_build"]["public_submission_endpoint"] is False
    assert boundary["public_open_build"]["token_collection"] is False
    assert boundary["pro_boundary"]["operator_issued_keys"] is True
    assert boundary["pro_boundary"]["web_worker_creates_tokens"] is False
    assert boundary["pro_boundary"]["tokens_included_in_payloads"] is False
    assert boundary["pro_boundary"]["forbidden_customer_scopes"] == ["admin:read"]
    assert boundary["routine_publication"]["mode"] == "automated_fail_closed"
    assert boundary["routine_publication"]["manual_13f_review_required"] is False
    assert "validated alpha, expected-return or probability claims" in boundary["keep_out"]
    assert "Pro key lifecycle smoke" in boundary["required_gates"]
    return boundary


def test_pilot_release_candidate_surfaces_share_core_boundary():
    with tempfile.TemporaryDirectory() as d:
        c = _client(d)
        readiness = c.get("/api/commercial-readiness").get_json()
        security = c.get("/api/security-posture").get_json()
        offer = c.get("/api/pro-offer").get_json()
        intake = c.get("/api/pilot-intake").get_json()
        buyer = c.get("/api/buyer-pack").get_json()

    boundaries = [_boundary(payload) for payload in (readiness, security, offer, intake, buyer)]
    assert {b["source"] for b in boundaries} == {"docs/CORE_V1_BOUNDARY.md"}

    assert readiness["sales_motion"] == buyer["sales_motion"] == intake["sales_motion"] == \
        "controlled_pilot_only"
    assert readiness["self_serve_checkout"] is buyer["self_serve_checkout"] is \
        intake["self_serve_checkout"] is False
    assert readiness["public_quote_ready"] is buyer["public_quote_ready"] is False

    assert security["status"] in {"controlled_pilot_security_ready", "security_review_required"}
    assert security["status"] == (
        "security_review_required" if readiness["hard_blocks"]
        else "controlled_pilot_security_ready"
    )
    assert security["privacy"]["tokens_echoed"] is False
    assert security["privacy"]["secrets_in_payloads"] is False
    assert security["privacy"]["self_serve_checkout"] is False

    assert offer["offer"]["access_model"] == "operator_issued_api_key"
    assert offer["offer"]["self_serve_checkout"] is False
    assert offer["commercial_model"]["pricing_status"] == \
        "paused_until_terms_and_capacity_are_ready"
    assert offer["commercial_model"]["qualification_filter"]["bad_fit"] == [
        "wants cheap raw SEC access only",
        "requires a public self-serve checkout today",
        "expects investment advice, price targets or validated alpha",
        "needs redistribution without a custom contract",
    ]

    assert intake["public_submission_endpoint"] is None
    assert intake["public_form_submission"] is False
    assert intake["privacy"]["server_side_pii_storage"] is False
    assert intake["privacy"]["token_collection"] is False
    assert intake["privacy"]["secret_collection"] is False

    assert buyer["terms_boundary"]["operator_review_required"] is True
    assert buyer["terms_boundary"]["investment_advice"] is False
    assert buyer["terms_boundary"]["managed_service_sla"] is False
    assert buyer["pilot_intake"]["server_side_pii_storage"] is False
    assert "validated alpha" in buyer["do_not_claim_yet"]


def test_pilot_release_candidate_pages_do_not_show_self_serve_language():
    with tempfile.TemporaryDirectory() as d:
        c = _client(d)
        pages = [
            c.get("/pro").get_data(as_text=True),
            c.get("/pilot/request").get_data(as_text=True),
            c.get("/pro/onboarding").get_data(as_text=True),
            c.get("/pro/workspace").get_data(as_text=True),
            c.get("/buyer-pack").get_data(as_text=True),
            c.get("/security").get_data(as_text=True),
            c.get("/readiness").get_data(as_text=True),
        ]

    joined = "\n".join(pages).lower()
    assert "continue to checkout" not in joined
    assert "stripe checkout" not in joined
    assert "490 eur" not in joined
    assert "validated alpha claim" not in joined
    assert "server_side_pii_storage:false" in joined
    assert "not investment advice" in joined
