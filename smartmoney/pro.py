"""
Professional API control plane: API keys, scopes, rate limits, and audit.

This module is intentionally separate from the market-data Store. In production,
the 13F database can remain read-only for the web tier while the Pro API uses a
small writable runtime database for keys, counters, and audit events.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

KEY_PREFIX = "13flow_live"
DEFAULT_SCOPES = ("funds:read", "quality:read")

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    key_hash     TEXT NOT NULL,
    scopes       TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'pro',
    rate_per_min INTEGER NOT NULL DEFAULT 120,
    rate_per_day INTEGER NOT NULL DEFAULT 10000,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    revoked_at   TEXT,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_api_keys_active ON api_keys(revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS api_key_usage (
    key_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    count  INTEGER NOT NULL,
    PRIMARY KEY (key_id, bucket)
);

CREATE TABLE IF NOT EXISTS api_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT,
    method TEXT,
    route TEXT,
    status INTEGER,
    ip TEXT,
    user_agent TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_audit_key_at ON api_audit(key_id, at);
"""


class APIKeyError(Exception):
    status_code = 401
    code = "invalid_api_key"


class APIKeyForbidden(APIKeyError):
    status_code = 403
    code = "insufficient_scope"


class APIRateLimited(APIKeyError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int = 60):
        super().__init__(self.code)
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_token(token: str) -> tuple[str, str]:
    if not token or not token.startswith(KEY_PREFIX + "_"):
        raise APIKeyError("invalid API key")
    rest = token[len(KEY_PREFIX) + 1:]
    parts = rest.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise APIKeyError("invalid API key")
    return parts[0], token


def _scopes_to_string(scopes) -> str:
    vals = [str(s).strip() for s in scopes if str(s).strip()]
    return " ".join(dict.fromkeys(vals or DEFAULT_SCOPES))


@dataclass(frozen=True)
class APIKey:
    key_id: str
    label: str
    scopes: tuple[str, ...]
    tier: str
    rate_per_min: int
    rate_per_day: int


class ProAPIStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ProAPIStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def create_key(
        self,
        label: str,
        scopes=DEFAULT_SCOPES,
        tier: str = "pro",
        rate_per_min: int = 120,
        rate_per_day: int = 10000,
        expires_days: Optional[int] = None,
    ) -> tuple[str, APIKey]:
        label = (label or "").strip()
        if not label:
            raise ValueError("label is required")
        key_id = secrets.token_hex(8)
        token = f"{KEY_PREFIX}_{key_id}_{secrets.token_urlsafe(32)}"
        now = _now()
        expires_at = _iso(now + timedelta(days=expires_days)) if expires_days else None
        scopes_s = _scopes_to_string(scopes)
        with self.conn:
            self.conn.execute(
                """INSERT INTO api_keys(key_id,label,key_hash,scopes,tier,rate_per_min,
                                        rate_per_day,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    key_id, label, _hash_key(token), scopes_s, tier,
                    int(rate_per_min), int(rate_per_day), _iso(now), expires_at,
                ),
            )
        return token, APIKey(
            key_id=key_id, label=label, scopes=tuple(scopes_s.split()), tier=tier,
            rate_per_min=int(rate_per_min), rate_per_day=int(rate_per_day),
        )

    def revoke_key(self, key_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
                (_iso(_now()), key_id),
            )
        return cur.rowcount > 0

    def list_keys(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT key_id,label,scopes,tier,rate_per_min,rate_per_day,created_at,
                      expires_at,revoked_at,last_used_at
               FROM api_keys ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def authenticate(self, token: str, required_scope: str) -> APIKey:
        key_id, full_token = _parse_token(token)
        row = self.conn.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise APIKeyError("invalid API key")
        if not hmac.compare_digest(row["key_hash"], _hash_key(full_token)):
            raise APIKeyError("invalid API key")
        if row["revoked_at"]:
            raise APIKeyError("revoked API key")
        if row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"]) <= _now():
                    raise APIKeyError("expired API key")
            except ValueError:
                raise APIKeyError("expired API key")
        scopes = tuple((row["scopes"] or "").split())
        if required_scope and required_scope not in scopes:
            raise APIKeyForbidden("insufficient scope")
        key = APIKey(
            key_id=row["key_id"], label=row["label"], scopes=scopes, tier=row["tier"],
            rate_per_min=int(row["rate_per_min"]), rate_per_day=int(row["rate_per_day"]),
        )
        self._hit_rate_limit(key)
        with self.conn:
            self.conn.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?",
                              (_iso(_now()), key.key_id))
        return key

    def _hit_rate_limit(self, key: APIKey) -> None:
        now = _now()
        minute = "m:" + now.strftime("%Y%m%d%H%M")
        day = "d:" + now.strftime("%Y%m%d")
        with self.conn:
            min_count = self._increment_bucket(key.key_id, minute)
            day_count = self._increment_bucket(key.key_id, day)
            # Keep recent windows only. Audit is the durable history.
            self.conn.execute(
                "DELETE FROM api_key_usage WHERE key_id=? AND bucket LIKE 'm:%' AND bucket < ?",
                (key.key_id, "m:" + (now - timedelta(hours=2)).strftime("%Y%m%d%H%M")),
            )
        if min_count > key.rate_per_min or day_count > key.rate_per_day:
            raise APIRateLimited(60)

    def _increment_bucket(self, key_id: str, bucket: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM api_key_usage WHERE key_id=? AND bucket=?",
            (key_id, bucket),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO api_key_usage(key_id,bucket,count) VALUES (?,?,1)",
                (key_id, bucket),
            )
            return 1
        count = int(row["count"]) + 1
        self.conn.execute(
            "UPDATE api_key_usage SET count=? WHERE key_id=? AND bucket=?",
            (count, key_id, bucket),
        )
        return count

    def audit(self, key_id: Optional[str], method: str, route: str, status: int,
              ip: str = "", user_agent: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO api_audit(key_id,method,route,status,ip,user_agent,at)
                   VALUES (?,?,?,?,?,?,?)""",
                (key_id, method, route, int(status), (ip or "")[:80],
                 (user_agent or "")[:300], _iso(_now())),
            )

    def prune_audit(self, retention_days: int) -> dict:
        days = int(retention_days)
        if days < 1:
            raise ValueError("retention_days must be >= 1")
        cutoff = _iso(_now() - timedelta(days=days))
        before = self.conn.execute("SELECT COUNT(*) c FROM api_audit").fetchone()["c"]
        with self.conn:
            cur = self.conn.execute("DELETE FROM api_audit WHERE at < ?", (cutoff,))
            deleted = int(cur.rowcount or 0)
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        after = self.conn.execute("SELECT COUNT(*) c FROM api_audit").fetchone()["c"]
        return {
            "retention_days": days,
            "cutoff": cutoff,
            "before": int(before or 0),
            "after": int(after or 0),
            "deleted": deleted,
        }
