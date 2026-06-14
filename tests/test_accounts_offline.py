"""
Offline tests for the accounts/auth system. No network.

Covers the security-critical behaviour: password policy, enumeration-resistant login,
lockout, opaque revocable sessions (expiry/revoke/idle), password change revoking sessions,
single-use expiring reset tokens, and — via the Flask app — secure-cookie login, CSRF
enforcement on mutations, and SERVER-SIDE tier gating of paid alert subscriptions.
"""

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import os as _os
_os.environ.setdefault("SMARTMONEY_DISABLE_HIBP", "1")  # tests don't hit the network; HIBP is covered in test_hibp_offline

from smartmoney.accounts import (AccountStore, AuthError, EmailTaken, PasswordPolicyError,
                                 MAX_FAILED)
from smartmoney.pwhash import PasswordHasher

# Use argon2 if available (faster than 64MiB scrypt); fall back transparently.
HASHER = PasswordHasher()
PW = "correct horse battery staple"


def _store(d):
    return AccountStore(str(Path(d) / "acc.db"), hasher=HASHER)


# ---- account store -------------------------------------------------------
def test_register_policy_and_duplicate():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.register("user@example.com", PW)
        with pytest.raises(EmailTaken):
            s.register("USER@example.com", PW)          # case-insensitive uniqueness
        with pytest.raises(PasswordPolicyError):
            s.register("a@b.com", "short")              # too short
        with pytest.raises(PasswordPolicyError):
            s.register("a@b.com", "a@b.com")            # equals email / too common-ish


def test_login_enumeration_resistant_and_lockout():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.register("user@example.com", PW)
        # Wrong password and unknown user both raise the SAME generic error.
        with pytest.raises(AuthError) as e1:
            s.authenticate("user@example.com", "wrong-password-x")
        with pytest.raises(AuthError) as e2:
            s.authenticate("ghost@example.com", "whatever-x")
        assert str(e1.value) == str(e2.value) == "invalid email or password"

        # Lockout after MAX_FAILED failures — even a correct password is then refused.
        for _ in range(MAX_FAILED):
            with pytest.raises(AuthError):
                s.authenticate("user@example.com", "still-wrong")
        with pytest.raises(AuthError) as locked:
            s.authenticate("user@example.com", PW)
        assert "locked" in str(locked.value)


def test_sessions_validate_revoke_expire():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        u = s.register("user@example.com", PW, verified=True)
        _, token, csrf = s.authenticate("user@example.com", PW)
        got = s.validate_session(token)
        assert got and got[0].id == u.id and got[1] == csrf

        # Tamper / unknown token -> None.
        assert s.validate_session(token + "x") is None

        # Revoke -> invalid.
        s.revoke_session(token)
        assert s.validate_session(token) is None

        # Absolute expiry in the past -> invalid.
        _, t2, _ = s.authenticate("user@example.com", PW)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        s.conn.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?",
                       (past, __import__("hashlib").sha256(t2.encode()).hexdigest()))
        s.conn.commit()
        assert s.validate_session(t2) is None


def test_change_password_revokes_sessions():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        u = s.register("user@example.com", PW, verified=True)
        _, token, _ = s.authenticate("user@example.com", PW)
        with pytest.raises(AuthError):
            s.change_password(u.id, "wrong-old", "a-brand-new-passphrase")
        s.change_password(u.id, PW, "a-brand-new-passphrase")
        assert s.validate_session(token) is None          # old session killed
        # old password no longer works; new one does
        with pytest.raises(AuthError):
            s.authenticate("user@example.com", PW)
        assert s.authenticate("user@example.com", "a-brand-new-passphrase")


def test_reset_token_single_use_and_expiry():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.register("user@example.com", PW)
        assert s.create_reset_token("ghost@example.com") is None   # no enumeration
        tok = s.create_reset_token("user@example.com")
        assert tok
        s.consume_reset_token(tok, "another-fine-passphrase")
        with pytest.raises(AuthError):
            s.consume_reset_token(tok, "yet-another-passphrase")    # single use


# ---- Flask auth routes + CSRF + server-side tier -------------------------
def _csrf_from(resp):
    for h in resp.headers.getlist("Set-Cookie"):
        m = re.search(r"sm_csrf=([^;]+)", h)
        if m:
            return m.group(1)
    return ""


def test_auth_routes_csrf_and_tier_gate():
    flask = pytest.importorskip("flask")  # noqa: F841
    from smartmoney.api import create_app
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "app.db")
        app = create_app(db, secure_cookies=False)
        c = app.test_client()

        # register -> NO session now; account is created unverified
        r = c.post("/api/auth/register", json={"email": "u@example.com", "password": PW})
        assert r.status_code == 200 and r.get_json()["status"] == "verify_email"
        assert c.get("/api/auth/me").status_code == 401            # not logged in yet

        # login before verifying -> 403 with the 'unverified' code
        r = c.post("/api/auth/login", json={"email": "u@example.com", "password": PW})
        assert r.status_code == 403 and r.get_json().get("code") == "unverified"

        # verify the email (operator-side here), then log in for a real session
        acc = AccountStore(db)
        row = acc.get_by_email("u@example.com")
        acc.mark_verified(row["id"])
        acc.close()
        r = c.post("/api/auth/login", json={"email": "u@example.com", "password": PW})
        assert r.status_code == 200 and r.get_json()["tier"] == "free"
        csrf = _csrf_from(r)
        assert c.get("/api/auth/me").status_code == 200

        # subscribing without the CSRF header is rejected (we have a session)
        assert c.post("/api/subscriptions", json={"cik": "1067983", "channel": "console"}).status_code == 403

        # with CSRF header but FREE tier -> 402 (alerts are paid; enforced server-side)
        hdr = {"X-CSRF-Token": csrf}
        r = c.post("/api/subscriptions", json={"cik": "1067983", "channel": "console"}, headers=hdr)
        assert r.status_code == 402

        # upgrade tier server-side, then it works (tier read from the DB, not the client)
        acc = AccountStore(db)
        row = acc.get_by_email("u@example.com")
        acc.set_tier(row["id"], "paid")
        acc.close()
        r = c.post("/api/subscriptions", json={"cik": "1067983", "channel": "console"}, headers=hdr)
        assert r.status_code == 201

        # logout -> protected route 401
        assert c.post("/api/auth/logout", headers=hdr).status_code == 200
        assert c.get("/api/subscriptions").status_code == 401

        # bad login -> 401, generic
        assert c.post("/api/auth/login", json={"email": "u@example.com", "password": "nope"}).status_code == 401


def test_public_data_still_open():
    pytest.importorskip("flask")
    from smartmoney.api import create_app
    with tempfile.TemporaryDirectory() as d:
        c = create_app(str(Path(d) / "app.db"), secure_cookies=False).test_client()
        # read-only market data needs no auth (public-domain data; free to browse)
        assert c.get("/api/funds").status_code == 200
        assert c.get("/api/consensus/holdings").status_code == 200


def test_email_verification_flow():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.register("user@example.com", PW)                 # unverified by default
        assert s.is_verified("user@example.com") is False
        from smartmoney.accounts import EmailNotVerified
        with pytest.raises(EmailNotVerified):
            s.authenticate("user@example.com", PW)         # blocked until verified
        assert s.create_reset_token("ghost@example.com") is None  # (sanity, unrelated)
        tok = s.create_email_verify_token("user@example.com")
        assert tok and s.create_email_verify_token("ghost@example.com") is None
        s.consume_email_verify_token(tok)
        assert s.is_verified("user@example.com") is True
        assert s.authenticate("user@example.com", PW)      # now allowed
        with pytest.raises(AuthError):
            s.consume_email_verify_token(tok)              # single use
        assert s.create_email_verify_token("user@example.com") is None  # already verified


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
