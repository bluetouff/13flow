"""
Billing — Stripe subscriptions, with a local mock mode for graphical testing.

SECURITY MODEL (the part that matters):
  - Entitlement (tier -> 'paid') flips ONLY from a signature-verified Stripe webhook, never
    from the browser, never from the checkout success redirect. The success page just says
    "thanks"; the actual upgrade happens server-side when Stripe POSTs a signed event.
  - Webhooks are verified with the webhook signing secret (Stripe's HMAC scheme) and handled
    idempotently (each event id processed once).
  - The checkout/portal endpoints require an authenticated session + CSRF (enforced by auth).

MODE SELECTION:
  - Real Stripe when STRIPE_SECRET_KEY is set (needs the `stripe` package + STRIPE_PRICE_ID,
    STRIPE_WEBHOOK_SECRET).
  - Otherwise MOCK mode: a local, styled fake-checkout page lets you click "Pay" and watch
    your account flip to Pro — no Stripe account, no network. Mock endpoints exist ONLY in
    mock mode and must never run in production.
"""

from __future__ import annotations

import json
import os

from flask import (Blueprint, Response, g, jsonify, make_response, redirect,
                   request)

PRICE_LABEL = os.environ.get("SMARTMONEY_PRICE_LABEL", "SmartMoney Pro — €12 / month")


def billing_mode() -> str:
    return "stripe" if os.environ.get("STRIPE_SECRET_KEY") else "mock"


# ------------------------------------------------------------------ gateways
class StripeGateway:
    """Real Stripe. Imported lazily so the package is only needed in stripe mode."""

    def __init__(self):
        import stripe  # noqa
        self.stripe = stripe
        self.stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        self.price_id = os.environ.get("STRIPE_PRICE_ID", "")
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    def create_checkout(self, user_row, success_url, cancel_url):
        kwargs = dict(
            mode="subscription",
            line_items=[{"price": self.price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=user_row["id"],
            metadata={"user_id": user_row["id"]},
        )
        if user_row["stripe_customer_id"]:
            kwargs["customer"] = user_row["stripe_customer_id"]
        else:
            kwargs["customer_email"] = user_row["email"]
        s = self.stripe.checkout.Session.create(**kwargs)
        return s.url

    def create_portal(self, user_row, return_url):
        cust = user_row["stripe_customer_id"]
        if not cust:
            return None
        s = self.stripe.billing_portal.Session.create(customer=cust, return_url=return_url)
        return s.url

    def parse_webhook(self, payload, sig_header: str):
        # verify_header builds "%d.%s" % (ts, payload); if payload is bytes Python inserts its
        # repr and the check fails — so hand it a decoded str. Raises on bad signature.
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        self.stripe.WebhookSignature.verify_header(payload, sig_header, self.webhook_secret)
        return True


# ------------------------------------------------------------------ handler
def apply_event(accounts, event: dict) -> None:
    """Apply a (already verified) Stripe-shaped event to a user's tier. Idempotent."""
    event_id = event.get("id", "")
    if accounts.was_event_processed(event_id):
        return
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
        if user_id and accounts.get_row(user_id):
            accounts.link_stripe(user_id, customer_id=obj.get("customer"),
                                 subscription_id=obj.get("subscription"))
            accounts.set_tier(user_id, "paid")
    elif etype in ("customer.subscription.deleted",
                   "customer.subscription.updated",
                   "invoice.payment_failed"):
        status = obj.get("status", "")
        customer = obj.get("customer")
        row = accounts.get_by_customer(customer) if customer else None
        if row:
            # Downgrade when the subscription is no longer active/trialing.
            if etype == "customer.subscription.deleted" or status in (
                    "canceled", "unpaid", "incomplete_expired", "past_due"):
                accounts.set_tier(row["id"], "free")
            elif status in ("active", "trialing"):
                accounts.set_tier(row["id"], "paid")

    accounts.mark_event_processed(event_id, etype)


# ------------------------------------------------------------------ routes
def init_billing(app, factory, secure_cookies: bool = True):
    mode = billing_mode()
    app.config["BILLING_MODE"] = mode
    gateway = StripeGateway() if mode == "stripe" else None
    bp = Blueprint("billing", __name__)

    def _base():
        return request.host_url.rstrip("/")

    @bp.get("/api/billing/config")
    def config():
        return jsonify({"mode": mode, "price_label": PRICE_LABEL,
                        "tier": g.user.tier if g.get("user") else None})

    @bp.post("/api/billing/checkout")
    def checkout():
        if not g.get("user"):
            return jsonify({"error": "authentication required"}), 401
        if not getattr(g.user, "verified", True):
            return jsonify({"error": "verify your email first"}), 403
        acc = factory()
        try:
            row = acc.get_row(g.user.id)
            if row and row["tier"] == "paid":
                return jsonify({"error": "already subscribed"}), 400
            success = _base() + "/?upgraded=1"
            cancel = _base() + "/?canceled=1"
            if mode == "stripe":
                url = gateway.create_checkout(row, success, cancel)
            else:
                # Mock: send the browser to a local fake-checkout page.
                url = _base() + "/billing/mock-checkout"
            return jsonify({"url": url})
        except Exception as e:  # noqa: keep the message generic to the client
            app.logger.warning("checkout error: %s", e)
            return jsonify({"error": "could not start checkout"}), 502
        finally:
            acc.close()

    @bp.post("/api/billing/portal")
    def portal():
        if not g.get("user"):
            return jsonify({"error": "authentication required"}), 401
        if mode != "stripe":
            return jsonify({"error": "billing portal is only available with Stripe configured"}), 400
        acc = factory()
        try:
            row = acc.get_row(g.user.id)
            url = gateway.create_portal(row, _base() + "/")
            if not url:
                return jsonify({"error": "no billing account yet"}), 400
            return jsonify({"url": url})
        finally:
            acc.close()

    @bp.post("/api/billing/webhook")
    def webhook():
        acc = factory()
        try:
            if mode == "stripe":
                raw = request.get_data()
                try:
                    gateway.parse_webhook(raw, request.headers.get("Stripe-Signature", ""))
                except Exception:
                    return jsonify({"error": "invalid signature"}), 400
                # Signature proved integrity; use a plain dict for our handler.
                event = json.loads(raw or b"{}")
            else:
                # Mock mode accepts a plain JSON event (no real Stripe to sign it). This
                # endpoint is harmless in mock mode and absent-by-policy in production.
                event = request.get_json(silent=True) or {}
            apply_event(acc, event)
            return jsonify({"received": True})
        finally:
            acc.close()

    # ---- mock-only endpoints (local graphical testing) ----
    if mode == "mock":
        @bp.get("/billing/mock-checkout")
        def mock_checkout():
            return Response(_MOCK_CHECKOUT_HTML.replace("{{PRICE}}", PRICE_LABEL),
                            mimetype="text/html")

        @bp.post("/api/billing/mock-complete")
        def mock_complete():
            # Browser-driven -> requires a real session + CSRF (enforced by auth guard).
            if not g.get("user"):
                return jsonify({"error": "authentication required"}), 401
            # Simulate the signed webhook that Stripe would send after payment.
            event = {
                "id": "evt_mock_" + g.user.id,
                "type": "checkout.session.completed",
                "data": {"object": {"client_reference_id": g.user.id,
                                    "customer": "cus_mock_" + g.user.id,
                                    "subscription": "sub_mock_" + g.user.id}},
            }
            acc = factory()
            try:
                apply_event(acc, event)
            finally:
                acc.close()
            return jsonify({"ok": True})

    app.register_blueprint(bp)
    return app


_MOCK_CHECKOUT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Checkout (test mode)</title>
<link href="/assets/fonts/13flow-fonts.css" rel="stylesheet">
<style>
  :root{--accent:#0f9d76;--ink:#15171c;--muted:#71757e;--line:#e9e7e0}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:'Hanken Grotesk',sans-serif;
    color:var(--ink);background:#f5f4f0;
    background-image:radial-gradient(60% 50% at 80% -5%,rgba(15,157,118,.10),transparent 70%)}
  .card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:30px;width:380px;max-width:92vw;
    box-shadow:0 1px 2px rgba(18,20,24,.04),0 18px 40px -20px rgba(18,20,24,.25)}
  .badge{font-family:'Geist Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;
    color:#b06a00;background:#fff3d6;border:1px solid #ffe3a3;border-radius:999px;padding:5px 10px;display:inline-block}
  h1{font-size:21px;margin:16px 0 4px;letter-spacing:-.02em}
  p.sub{color:var(--muted);font-size:14px;margin:0 0 22px}
  .line{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-top:1px solid var(--line);
    font-family:'Geist Mono',monospace}
  label{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
  input{width:100%;font-family:'Geist Mono',monospace;font-size:14px;padding:11px 13px;margin-top:6px;margin-bottom:14px;
    border:1px solid var(--line);border-radius:10px;background:#faf9f6;outline:none}
  input:focus{border-color:var(--accent);background:#fff}
  button{width:100%;border:none;border-radius:12px;background:var(--accent);color:#fff;font-weight:600;font-size:15px;
    padding:13px;cursor:pointer;font-family:inherit}
  button:hover{background:#0c7d5e} button:disabled{opacity:.5;cursor:default}
  .err{color:#d65440;font-size:13px;min-height:18px;margin-top:10px}
  .foot{margin-top:16px;text-align:center;font-size:12px;color:var(--muted)}
  a{color:var(--muted)}
</style></head><body>
<div class="card">
  <span class="badge">Test mode — no real charge</span>
  <h1>{{PRICE}}</h1>
  <p class="sub">This is a local mock of Stripe Checkout so you can test the flow end-to-end.</p>
  <div class="line"><span>Subtotal</span><span>€12.00</span></div>
  <div class="line"><span>Billed monthly</span><span>€12.00</span></div>
  <div style="height:14px"></div>
  <label>Card number</label><input id="card" value="4242 4242 4242 4242" autocomplete="off">
  <div style="display:flex;gap:10px"><div style="flex:1"><label>Expiry</label><input value="12 / 34"></div>
    <div style="width:96px"><label>CVC</label><input value="123"></div></div>
  <button id="pay">Pay €12.00</button>
  <div class="err" id="err"></div>
  <div class="foot"><a href="/?canceled=1">← Cancel and go back</a></div>
</div>
<script>
  function getCookie(n){const m=document.cookie.match(new RegExp('(?:^|; )'+n+'=([^;]*)'));return m?decodeURIComponent(m[1]):''}
  document.getElementById('pay').onclick=async()=>{
    const b=document.getElementById('pay'),e=document.getElementById('err');b.disabled=true;b.textContent='Processing…';e.textContent='';
    try{
      const r=await fetch('/api/billing/mock-complete',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':getCookie('sm_csrf')}});
      if(r.ok){location.href='/?upgraded=1';}
      else{const d=await r.json().catch(()=>({}));e.textContent=d.error||'Payment failed.';b.disabled=false;b.textContent='Pay €12.00';}
    }catch(err){e.textContent='Network error.';b.disabled=false;b.textContent='Pay €12.00';}
  };
</script></body></html>"""
