"""
Offline billing tests. No network.

Covers the security-critical behaviour:
  - tier flips to 'paid' only via apply_event (the verified-webhook code path), and is
    idempotent across duplicate events;
  - subscription cancellation downgrades to 'free';
  - the mock graphical flow (checkout -> mock-complete) upgrades the signed-in user, and the
    mock-complete endpoint requires auth + CSRF;
  - a real Stripe webhook with a bad signature is rejected, and a correctly-signed one parses
    (pure-local HMAC; no Stripe network needed).
"""

import json
import re
import tempfile
import time
from pathlib import Path

import pytest
import os as _os
_os.environ.setdefault("SMARTMONEY_DISABLE_HIBP", "1")  # tests don't hit the network; HIBP is covered in test_hibp_offline

from smartmoney.accounts import AccountStore
from smartmoney.billing import apply_event

PW = "correct horse battery staple"


def _csrf_from(resp):
    for h in resp.headers.getlist("Set-Cookie"):
        m = re.search(r"sm_csrf=([^;]+)", h)
        if m:
            return m.group(1)
    return ""


# ---- core handler --------------------------------------------------------
def test_apply_event_upgrades_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        s = AccountStore(str(Path(d) / "a.db"))
        u = s.register("u@example.com", PW)
        evt = {"id": "evt_1", "type": "checkout.session.completed",
               "data": {"object": {"client_reference_id": u.id, "customer": "cus_1",
                                   "subscription": "sub_1"}}}
        apply_event(s, evt)
        assert s.get_user(u.id).tier == "paid"
        assert s.get_row(u.id)["stripe_customer_id"] == "cus_1"
        # replay the same event id -> no change, no error (idempotent)
        s.set_tier(u.id, "free")
        apply_event(s, evt)
        assert s.get_user(u.id).tier == "free"      # ignored: event already processed


def test_subscription_cancel_downgrades():
    with tempfile.TemporaryDirectory() as d:
        s = AccountStore(str(Path(d) / "a.db"))
        u = s.register("u@example.com", PW)
        apply_event(s, {"id": "e1", "type": "checkout.session.completed",
                        "data": {"object": {"client_reference_id": u.id, "customer": "cus_9",
                                            "subscription": "sub_9"}}})
        assert s.get_user(u.id).tier == "paid"
        apply_event(s, {"id": "e2", "type": "customer.subscription.deleted",
                        "data": {"object": {"customer": "cus_9", "status": "canceled"}}})
        assert s.get_user(u.id).tier == "free"


# ---- mock graphical flow via the app -------------------------------------
def test_mock_checkout_flow(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)   # force mock mode
    pytest.importorskip("flask")
    from smartmoney.api import create_app
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "app.db")
        app = create_app(db, secure_cookies=False)
        c = app.test_client()
        # register (unverified) -> verify -> login to get a real session
        c.post("/api/auth/register", json={"email": "u@example.com", "password": PW})
        acc = AccountStore(db)
        acc.mark_verified(acc.get_by_email("u@example.com")["id"])
        acc.close()
        r = c.post("/api/auth/login", json={"email": "u@example.com", "password": PW})
        csrf = _csrf_from(r)
        assert c.get("/api/billing/config").get_json()["mode"] == "mock"

        # checkout returns the local mock page URL
        r = c.post("/api/billing/checkout", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and "/billing/mock-checkout" in r.get_json()["url"]
        assert c.get("/billing/mock-checkout").status_code == 200      # styled page renders

        # mock-complete WITHOUT csrf -> 403; WITH csrf -> upgrades to paid
        assert c.post("/api/billing/mock-complete").status_code == 403
        assert c.post("/api/billing/mock-complete", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert c.get("/api/auth/me").get_json()["tier"] == "paid"

        # now a paid user can actually subscribe (the whole point)
        r = c.post("/api/subscriptions", json={"cik": "1067983", "channel": "console"},
                   headers={"X-CSRF-Token": csrf})
        assert r.status_code == 201


# ---- real Stripe signature verification (local HMAC, no network) ---------
def test_stripe_webhook_signature(monkeypatch):
    stripe = pytest.importorskip("stripe")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_testsecret")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_123")
    pytest.importorskip("flask")
    from smartmoney.api import create_app
    with tempfile.TemporaryDirectory() as d:
        app = create_app(str(Path(d) / "app.db"), secure_cookies=False)
        c = app.test_client()
        assert c.get("/api/billing/config").get_json()["mode"] == "stripe"

        payload = json.dumps({"id": "evt_x", "type": "checkout.session.completed",
                              "data": {"object": {}}}).encode()
        # bad signature -> 400
        assert c.post("/api/billing/webhook", data=payload,
                      headers={"Stripe-Signature": "t=1,v1=deadbeef"}).status_code == 400
        # correctly-signed -> accepted
        ts = int(time.time())
        secret = "whsec_testsecret"
        signed = stripe.WebhookSignature  # noqa (ensure attr exists)
        import hashlib, hmac
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        r = c.post("/api/billing/webhook", data=payload,
                   headers={"Stripe-Signature": f"t={ts},v1={sig}"})
        assert r.status_code == 200 and r.get_json()["received"] is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
