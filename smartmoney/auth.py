"""
Flask integration for accounts: secure session cookies, CSRF, rate limiting, auth routes.

Cookie design:
  - sm_session: opaque session token. HttpOnly + Secure + SameSite=Strict -> not readable
    by JS, not sent cross-site (primary CSRF defense), not sent over plain HTTP.
  - sm_csrf: the session's CSRF token, readable by JS. Mutating /api requests must echo it
    in X-CSRF-Token (double-submit). Compared in constant time.

A fresh AccountStore (its own SQLite connection) is opened per request via `factory()` and
closed after — SQLite connections are not safe to share across threads.

The auth rate limiter is in-process (fine for one host). Behind multiple workers/hosts,
back it with Redis — see SECURITY.md.
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import time
from collections import deque

from flask import Blueprint, g, jsonify, make_response, redirect, request

from .accounts import (AuthError, EmailNotVerified, EmailTaken, PasswordPolicyError,
                       SESSION_ABS_TTL)
from .notify import make_default_mailer

log = logging.getLogger("smartmoney.auth")

SESSION_COOKIE = "sm_session"
CSRF_COOKIE = "sm_csrf"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register",
                "/api/auth/reset/request", "/api/auth/reset/confirm",
                "/api/auth/verify", "/api/auth/resend-verification",
                "/api/billing/webhook"}


def _dev_echo() -> bool:
    return os.environ.get("SMARTMONEY_DEV_EMAIL_ECHO", "").lower() in ("1", "true", "yes")


class _RateLimiter:
    def __init__(self, limit: int, window_sec: int):
        self.limit, self.window = limit, window_sec
        self._hits: dict[str, deque] = {}

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True


def init_auth(app, factory, secure_cookies: bool = True, mailer=None):
    """`factory()` returns a fresh AccountStore (per-request connection)."""
    if os.environ.get("SMARTMONEY_INSECURE_COOKIES", "").lower() in ("1", "true", "yes"):
        secure_cookies = False
    mailer = mailer or make_default_mailer()

    login_limiter = _RateLimiter(10, 300)       # 10 / 5 min / IP
    register_limiter = _RateLimiter(5, 3600)    # 5 / hour / IP

    def _send_verification(acc, email):
        """Create + email a verification link. Returns the link IF dev-echo is on, else None.
        Always quiet on failure to send (we never reveal whether the email exists)."""
        token = acc.create_email_verify_token(email)
        if not token:
            return None
        link = request.host_url.rstrip("/") + "/api/auth/verify?token=" + token
        body = ("Welcome to SmartMoney.\n\nConfirm your email to activate alerts:\n"
                f"{link}\n\nThis link expires in 24 hours. If you didn't sign up, ignore this.")
        try:
            mailer(email, "Confirm your SmartMoney email", body)
        except Exception as e:           # don't leak existence / don't 500 the signup
            log.warning("verification email send failed: %s", e)
        return link if _dev_echo() else None

    def _ip():
        xff = request.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"

    def _set_cookies(resp, token, csrf):
        common = dict(secure=secure_cookies, samesite="Strict", path="/",
                      max_age=int(SESSION_ABS_TTL.total_seconds()))
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
        resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)
        return resp

    def _clear_cookies(resp):
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(CSRF_COOKIE, path="/")
        return resp

    @app.before_request
    def _load_user():
        g.user = None
        g.csrf = None
        tok = request.cookies.get(SESSION_COOKIE)
        if not tok:
            return
        acc = factory()
        try:
            res = acc.validate_session(tok)
            if res:
                g.user, g.csrf = res
        finally:
            acc.close()

    @app.before_request
    def _csrf_guard():
        if request.method in _SAFE_METHODS:
            return
        if not request.path.startswith("/api/") or request.path in _CSRF_EXEMPT:
            return
        if g.get("user"):
            sent = request.headers.get(CSRF_HEADER, "")
            if not g.get("csrf") or not hmac.compare_digest(sent, g.csrf):
                return jsonify({"error": "csrf check failed"}), 403

    bp = Blueprint("auth", __name__)

    @bp.post("/api/auth/register")
    def register():
        if not register_limiter.hit(_ip()):
            return jsonify({"error": "too many attempts, slow down"}), 429
        d = request.get_json(silent=True) or {}
        email = d.get("email", "")
        acc = factory()
        try:
            acc.register(email, d.get("password", ""))     # creates an UNVERIFIED account
            dev_link = _send_verification(acc, email)
        except EmailTaken:
            return jsonify({"error": "email already registered"}), 409
        except PasswordPolicyError as e:
            return jsonify({"error": str(e)}), 400
        except (AuthError, ValueError):
            return jsonify({"error": "could not register"}), 400
        finally:
            acc.close()
        # No session is issued: the user must verify their email, then log in.
        out = {"status": "verify_email",
               "message": "Account created. Check your email to verify, then sign in."}
        if dev_link:
            out["dev_verify_url"] = dev_link
        return jsonify(out)

    @bp.post("/api/auth/login")
    def login():
        if not login_limiter.hit(_ip()):
            return jsonify({"error": "too many attempts, slow down"}), 429
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            user, token, csrf = acc.authenticate(
                d.get("email", ""), d.get("password", ""), _ip(),
                request.headers.get("User-Agent", ""))
        except EmailNotVerified:
            return jsonify({"error": "Please verify your email first.", "code": "unverified"}), 403
        except AuthError as e:
            return jsonify({"error": str(e)}), 401
        finally:
            acc.close()
        return _set_cookies(make_response(jsonify({"email": user.email, "tier": user.tier})), token, csrf)

    @bp.post("/api/auth/logout")
    def logout():
        tok = request.cookies.get(SESSION_COOKIE)
        if tok:
            acc = factory()
            try:
                acc.revoke_session(tok)
            finally:
                acc.close()
        return _clear_cookies(make_response(jsonify({"ok": True})))

    @bp.post("/api/auth/logout-all")
    def logout_all():
        if not g.get("user"):
            return jsonify({"error": "not authenticated"}), 401
        acc = factory()
        try:
            acc.revoke_all(g.user.id)
        finally:
            acc.close()
        return _clear_cookies(make_response(jsonify({"ok": True})))

    @bp.get("/api/auth/me")
    def me():
        if not g.get("user"):
            return jsonify({"error": "not authenticated"}), 401
        return jsonify({"email": g.user.email, "tier": g.user.tier})

    @bp.post("/api/auth/change-password")
    def change_password():
        if not g.get("user"):
            return jsonify({"error": "not authenticated"}), 401
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            acc.change_password(g.user.id, d.get("old", ""), d.get("new", ""))
        except PasswordPolicyError as e:
            return jsonify({"error": str(e)}), 400
        except AuthError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            acc.close()
        return _clear_cookies(make_response(jsonify({"ok": True})))

    @bp.post("/api/auth/reset/request")
    def reset_request():
        if not login_limiter.hit(_ip()):
            return jsonify({"error": "too many attempts"}), 429
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            acc.create_reset_token(d.get("email", ""))   # token delivered out of band
        finally:
            acc.close()
        return jsonify({"ok": True})       # always 200 (no enumeration)

    @bp.post("/api/auth/reset/confirm")
    def reset_confirm():
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            acc.consume_reset_token(d.get("token", ""), d.get("new", ""))
        except PasswordPolicyError as e:
            return jsonify({"error": str(e)}), 400
        except AuthError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            acc.close()
        return jsonify({"ok": True})

    @bp.get("/api/auth/verify")
    def verify_link():
        # Endpoint hit by the emailed link. Consume the token, then bounce to the dashboard.
        token = request.args.get("token", "")
        acc = factory()
        try:
            acc.consume_email_verify_token(token)
            return redirect("/?verified=1")
        except AuthError:
            return redirect("/?verify_error=1")
        finally:
            acc.close()

    @bp.post("/api/auth/verify")
    def verify_json():
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            acc.consume_email_verify_token(d.get("token", ""))
            return jsonify({"ok": True})
        except AuthError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            acc.close()

    @bp.post("/api/auth/resend-verification")
    def resend_verification():
        if not login_limiter.hit(_ip()):
            return jsonify({"error": "too many attempts"}), 429
        d = request.get_json(silent=True) or {}
        acc = factory()
        try:
            dev_link = _send_verification(acc, d.get("email", ""))
        finally:
            acc.close()
        out = {"ok": True}                 # always 200 (no enumeration)
        if dev_link:
            out["dev_verify_url"] = dev_link
        return jsonify(out)

    app.register_blueprint(bp)
    return app


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.get("user"):
            return jsonify({"error": "authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper


def current_user():
    return g.get("user")
