"""
Accounts: users, sessions, lockout, password reset — all in SQLite (same DB file as Store).

Security properties:
  - Passwords stored only as KDF hashes (see pwhash). Never logged, never returned.
  - Sessions are opaque 256-bit random tokens; only their SHA-256 is stored, so a DB read
    does not yield usable tokens. Sessions are revocable (logout, logout-all, password
    change) and have both absolute and idle expiry. (Chosen over JWT precisely for
    revocability and to avoid alg-confusion / key-management footguns.)
  - Login is enumeration-resistant: identical generic error and a dummy KDF verify when the
    account does not exist, so response/timing don't reveal which emails are registered.
  - Brute force: per-account failed-attempt counter with temporary lockout.
  - Each session carries a CSRF token (double-submit; enforced in auth.py).
  - Tier ('free'/'paid') lives on the user row and is the single server-side source of truth.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .netsec import validate_email_recipient   # reuse: shape + CRLF-safe
from .pwhash import PasswordHasher, dummy_verify

# Policy / limits
MIN_PASSWORD_LEN = 12
MAX_FAILED = 5
LOCK_SECONDS = 900           # 15 min
SESSION_ABS_TTL = timedelta(days=30)
SESSION_IDLE_TTL = timedelta(days=7)
RESET_TTL = timedelta(hours=1)
VERIFY_TTL = timedelta(hours=24)
_LAST_SEEN_THROTTLE = timedelta(minutes=5)

# A tiny, illustrative deny-list. In production, check against a breached-password
# corpus (e.g. HaveIBeenPwned k-anonymity range API) instead.
_COMMON = {
    "password", "123456", "123456789", "qwerty", "111111", "password1",
    "12345678", "abc123", "letmein", "iloveyou", "admin", "welcome",
    "monkey", "dragon", "passw0rd", "qwertyuiop", "changeme", "secret",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    pw_hash       TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'free',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    pw_changed_at TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf        TEXT NOT NULL,
    created_at  TEXT, expires_at TEXT, last_seen TEXT,
    ip          TEXT, user_agent TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, email TEXT, ok INTEGER, ip TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS reset_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT, used INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS email_verify_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT, used INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS billing_events (
    event_id TEXT PRIMARY KEY,
    type TEXT, at TEXT
);
"""


class AuthError(Exception):
    """Generic authentication failure (intentionally non-specific)."""


class EmailTaken(Exception):
    pass


class EmailNotVerified(Exception):
    """Raised by authenticate() when the password is correct but the email is unverified.
    Deliberately NOT an AuthError subclass so routes can return a distinct 403 + resend path
    without leaking account existence (it only fires after a correct password)."""


class PasswordPolicyError(Exception):
    pass


@dataclass
class User:
    id: str
    email: str
    tier: str
    is_active: bool
    verified: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return validate_email_recipient((email or "").strip().lower())


def validate_password(password: str, email: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password) > 1024:
        raise PasswordPolicyError("password too long")
    low = password.lower()
    if low in _COMMON:
        raise PasswordPolicyError("password is too common")
    if email and low == email.lower():
        raise PasswordPolicyError("password must not equal your email")
    if len(set(password)) < 4:
        raise PasswordPolicyError("password is not complex enough")


class AccountStore:
    def __init__(self, path: str = "smartmoney.db", hasher: Optional[PasswordHasher] = None,
                 breach_checker=None):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self.hasher = hasher or PasswordHasher()
        # callable(password)->bool (True => reject). None disables the breach check.
        self._breach_checker = breach_checker

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for col in ("stripe_customer_id", "stripe_subscription_id"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        if "email_verified" not in cols:
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            # Grandfather any accounts that predate this feature so they aren't locked out.
            self.conn.execute("UPDATE users SET email_verified=1")

    def _check_breached(self, password: str) -> None:
        if self._breach_checker and self._breach_checker(password):
            raise PasswordPolicyError(
                "this password has appeared in a known data breach; please choose another")

    def close(self) -> None:
        self.conn.close()

    # -- helpers ----------------------------------------------------------
    def _row_to_user(self, r: sqlite3.Row) -> User:
        verified = bool(r["email_verified"]) if "email_verified" in r.keys() else True
        return User(id=r["id"], email=r["email"], tier=r["tier"],
                    is_active=bool(r["is_active"]), verified=verified)

    def get_by_email(self, email: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def get_user(self, user_id: str) -> Optional[User]:
        r = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._row_to_user(r) if r else None

    # -- registration -----------------------------------------------------
    def register(self, email: str, password: str, tier: str = "free",
                 verified: bool = False) -> User:
        email = normalize_email(email)
        validate_password(password, email)
        self._check_breached(password)               # HIBP (network) after cheap local checks
        if self.get_by_email(email):
            raise EmailTaken("email already registered")
        uid = secrets.token_hex(16)
        with self.conn:
            try:
                self.conn.execute(
                    """INSERT INTO users(id,email,pw_hash,tier,is_active,created_at,pw_changed_at,email_verified)
                       VALUES (?,?,?,?,1,?,?,?)""",
                    (uid, email, self.hasher.hash(password), tier, _iso(_now()), _iso(_now()),
                     1 if verified else 0),
                )
            except sqlite3.IntegrityError:
                raise EmailTaken("email already registered")
        return User(uid, email, tier, True, verified)

    # -- authentication ---------------------------------------------------
    def authenticate(self, email: str, password: str, ip: str = "", ua: str = "") -> tuple[User, str, str]:
        """Return (user, session_token, csrf_token) or raise AuthError (generic)."""
        try:
            email = normalize_email(email)
        except Exception:
            dummy_verify(self.hasher, password or "")
            raise AuthError("invalid email or password")

        row = self.get_by_email(email)
        if row is None:
            dummy_verify(self.hasher, password or "")      # equalize timing
            self._record(None, email, False, ip)
            raise AuthError("invalid email or password")

        now = _now()
        if row["locked_until"]:
            try:
                if datetime.fromisoformat(row["locked_until"]) > now:
                    self._record(row["id"], email, False, ip)
                    raise AuthError("account temporarily locked; try again later")
            except ValueError:
                pass

        if not row["is_active"] or not self.hasher.verify(row["pw_hash"], password):
            self._register_failure(row, now)
            self._record(row["id"], email, False, ip)
            raise AuthError("invalid email or password")

        # success: password is correct -> reset lockout counters
        with self.conn:
            self.conn.execute(
                "UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (row["id"],))
        # Block unverified accounts here, AFTER the password check, so this never reveals
        # account existence to someone who doesn't already know the password.
        if not row["email_verified"]:
            raise EmailNotVerified("email not verified")
        with self.conn:
            if self.hasher.needs_rehash(row["pw_hash"]):
                self.conn.execute("UPDATE users SET pw_hash=? WHERE id=?",
                                  (self.hasher.hash(password), row["id"]))
        self._record(row["id"], email, True, ip)
        token, csrf = self._create_session(row["id"], ip, ua)
        return self._row_to_user(row), token, csrf

    def _register_failure(self, row: sqlite3.Row, now: datetime) -> None:
        attempts = (row["failed_attempts"] or 0) + 1
        locked = _iso(now + timedelta(seconds=LOCK_SECONDS)) if attempts >= MAX_FAILED else None
        with self.conn:
            self.conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                              (attempts, locked, row["id"]))

    def _record(self, user_id: Optional[str], email: str, ok: bool, ip: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO login_events(user_id,email,ok,ip,at) VALUES (?,?,?,?,?)",
                (user_id, email, 1 if ok else 0, ip, _iso(_now())))

    # -- sessions ---------------------------------------------------------
    def _create_session(self, user_id: str, ip: str, ua: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)        # 256-bit
        csrf = secrets.token_urlsafe(32)
        now = _now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO sessions(token_hash,user_id,csrf,created_at,expires_at,last_seen,ip,user_agent)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (_hash_token(token), user_id, csrf, _iso(now), _iso(now + SESSION_ABS_TTL),
                 _iso(now), ip, (ua or "")[:300]))
        return token, csrf

    def validate_session(self, token: str) -> Optional[tuple[User, str]]:
        if not token:
            return None
        r = self.conn.execute(
            "SELECT * FROM sessions WHERE token_hash=?", (_hash_token(token),)).fetchone()
        if r is None or r["revoked"]:
            return None
        now = _now()
        try:
            if datetime.fromisoformat(r["expires_at"]) <= now:
                return None
            if datetime.fromisoformat(r["last_seen"]) + SESSION_IDLE_TTL <= now:
                return None
        except (ValueError, TypeError):
            return None
        user = self.get_user(r["user_id"])
        if user is None or not user.is_active:
            return None
        # throttle last_seen writes to avoid a write on every request
        try:
            if datetime.fromisoformat(r["last_seen"]) + _LAST_SEEN_THROTTLE <= now:
                with self.conn:
                    self.conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                                      (_iso(now), r["token_hash"]))
        except (ValueError, TypeError):
            pass
        return user, r["csrf"]

    def revoke_session(self, token: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?",
                              (_hash_token(token),))

    def revoke_all(self, user_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))

    # -- password change / reset -----------------------------------------
    def change_password(self, user_id: str, old: str, new: str) -> None:
        r = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if r is None or not self.hasher.verify(r["pw_hash"], old):
            raise AuthError("current password is incorrect")
        validate_password(new, r["email"])
        self._check_breached(new)
        with self.conn:
            self.conn.execute("UPDATE users SET pw_hash=?, pw_changed_at=? WHERE id=?",
                              (self.hasher.hash(new), _iso(_now()), user_id))
        self.revoke_all(user_id)                 # force re-login everywhere

    def create_reset_token(self, email: str) -> Optional[str]:
        """Return a one-time token if the account exists, else None. Callers MUST behave
        identically either way (the route returns 200 regardless) to avoid enumeration."""
        try:
            email = normalize_email(email)
        except Exception:
            return None
        row = self.get_by_email(email)
        if row is None:
            return None
        token = secrets.token_urlsafe(32)
        with self.conn:
            self.conn.execute(
                "INSERT INTO reset_tokens(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                (_hash_token(token), row["id"], _iso(_now() + RESET_TTL), _iso(_now())))
        return token

    def consume_reset_token(self, token: str, new_password: str) -> None:
        r = self.conn.execute(
            "SELECT * FROM reset_tokens WHERE token_hash=?", (_hash_token(token),)).fetchone()
        if r is None or r["used"]:
            raise AuthError("invalid or used reset token")
        try:
            if datetime.fromisoformat(r["expires_at"]) <= _now():
                raise AuthError("reset token expired")
        except ValueError:
            raise AuthError("invalid reset token")
        urow = self.conn.execute("SELECT email FROM users WHERE id=?", (r["user_id"],)).fetchone()
        validate_password(new_password, urow["email"] if urow else "")
        self._check_breached(new_password)
        with self.conn:
            self.conn.execute("UPDATE users SET pw_hash=?, pw_changed_at=? WHERE id=?",
                              (self.hasher.hash(new_password), _iso(_now()), r["user_id"]))
            self.conn.execute("UPDATE reset_tokens SET used=1 WHERE token_hash=?", (r["token_hash"],))
        self.revoke_all(r["user_id"])

    # -- email verification ----------------------------------------------
    def mark_verified(self, user_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))

    def is_verified(self, email: str) -> bool:
        row = self.get_by_email(normalize_email(email))
        return bool(row and row["email_verified"])

    def create_email_verify_token(self, email: str) -> Optional[str]:
        """One-time token if the account exists AND is not already verified, else None.
        The route always responds identically (no enumeration)."""
        try:
            email = normalize_email(email)
        except Exception:
            return None
        row = self.get_by_email(email)
        if row is None or row["email_verified"]:
            return None
        token = secrets.token_urlsafe(32)
        with self.conn:
            # invalidate any prior unused tokens for this user, then issue a fresh one
            self.conn.execute("UPDATE email_verify_tokens SET used=1 WHERE user_id=? AND used=0",
                              (row["id"],))
            self.conn.execute(
                "INSERT INTO email_verify_tokens(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                (_hash_token(token), row["id"], _iso(_now() + VERIFY_TTL), _iso(_now())))
        return token

    def consume_email_verify_token(self, token: str) -> bool:
        r = self.conn.execute(
            "SELECT * FROM email_verify_tokens WHERE token_hash=?", (_hash_token(token or ""),)).fetchone()
        if r is None or r["used"]:
            raise AuthError("invalid or used verification token")
        try:
            if datetime.fromisoformat(r["expires_at"]) <= _now():
                raise AuthError("verification token expired")
        except ValueError:
            raise AuthError("invalid verification token")
        with self.conn:
            self.conn.execute("UPDATE users SET email_verified=1 WHERE id=?", (r["user_id"],))
            self.conn.execute("UPDATE email_verify_tokens SET used=1 WHERE token_hash=?",
                              (r["token_hash"],))
        return True
    def set_tier(self, user_id: str, tier: str) -> None:
        if tier not in ("free", "paid"):
            raise ValueError("invalid tier")
        with self.conn:
            self.conn.execute("UPDATE users SET tier=? WHERE id=?", (tier, user_id))

    # -- billing / Stripe -------------------------------------------------
    def get_row(self, user_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def get_by_customer(self, customer_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()

    def link_stripe(self, user_id: str, customer_id: Optional[str] = None,
                    subscription_id: Optional[str] = None) -> None:
        with self.conn:
            if customer_id is not None:
                self.conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?",
                                  (customer_id, user_id))
            if subscription_id is not None:
                self.conn.execute("UPDATE users SET stripe_subscription_id=? WHERE id=?",
                                  (subscription_id, user_id))

    def was_event_processed(self, event_id: str) -> bool:
        if not event_id:
            return False
        return self.conn.execute(
            "SELECT 1 FROM billing_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_event_processed(self, event_id: str, etype: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO billing_events(event_id,type,at) VALUES (?,?,?)",
                (event_id, etype, _iso(_now())))
