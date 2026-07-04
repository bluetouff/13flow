"""
Professional API control plane: API keys, scopes, rate limits, and audit.

This module is intentionally separate from the market-data Store. In production,
the 13F database can remain read-only for the web tier while the Pro API uses a
small writable runtime database for keys, counters, and audit events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

KEY_PREFIX = "13flow_live"
KEY_HASH_HMAC_PREFIX = "hmac-sha256:"
KEY_HASH_SHA256_PREFIX = "sha256:"
KEY_PEPPER_ENV = "SMARTMONEY_PRO_KEY_PEPPER"
KEY_PEPPER_REQUIRED_ENV = "SMARTMONEY_PRO_REQUIRE_KEY_PEPPER"
DEFAULT_SCOPES = ("funds:read", "quality:read")
DEFAULT_MAX_WATCHLISTS_PER_KEY = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    contact_email TEXT,
    key_hash     TEXT NOT NULL,
    scopes       TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'pro',
    rate_per_min INTEGER NOT NULL DEFAULT 120,
    rate_per_day INTEGER NOT NULL DEFAULT 10000,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    rotation_due_at TEXT,
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
CREATE INDEX IF NOT EXISTS ix_api_audit_key_route_at ON api_audit(key_id, route, at);

CREATE TABLE IF NOT EXISTS pro_operator_events (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    key_id      TEXT,
    label       TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT 'cli',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pro_operator_events_created
    ON pro_operator_events(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_pro_operator_events_key_created
    ON pro_operator_events(key_id, created_at DESC);

CREATE TABLE IF NOT EXISTS saved_watchlists (
    id                TEXT PRIMARY KEY,
    key_id            TEXT NOT NULL,
    name              TEXT NOT NULL,
    tickers_json      TEXT NOT NULL,
    filters_json      TEXT NOT NULL DEFAULT '{}',
    alert_policy_json TEXT NOT NULL DEFAULT '{}',
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_watchlists_key_updated
    ON saved_watchlists(key_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS saved_watchlist_signal_snapshots (
    id             TEXT PRIMARY KEY,
    key_id         TEXT NOT NULL,
    watchlist_id   TEXT NOT NULL,
    signals_json   TEXT NOT NULL,
    summary_json   TEXT NOT NULL DEFAULT '{}',
    tickers_json   TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_watchlist_signal_snapshots_key_watchlist
    ON saved_watchlist_signal_snapshots(key_id, watchlist_id, created_at DESC);

CREATE TABLE IF NOT EXISTS saved_workspace_alerts (
    id              TEXT PRIMARY KEY,
    key_id          TEXT NOT NULL,
    watchlist_id    TEXT NOT NULL,
    snapshot_id     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,
    severity        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open',
    reason_json     TEXT NOT NULL DEFAULT '{}',
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    acknowledged_at TEXT,
    dismissed_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_saved_workspace_alerts_current
    ON saved_workspace_alerts(key_id, watchlist_id, ticker, action);
CREATE INDEX IF NOT EXISTS ix_saved_workspace_alerts_key_status_last_seen
    ON saved_workspace_alerts(key_id, status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS saved_workspace_activity (
    id           TEXT PRIMARY KEY,
    key_id       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_workspace_activity_key_created
    ON saved_workspace_activity(key_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_saved_workspace_activity_key_event_created
    ON saved_workspace_activity(key_id, event_type, created_at DESC);
"""


class APIKeyError(Exception):
    status_code = 401
    code = "invalid_api_key"


class APIKeyForbidden(APIKeyError):
    status_code = 403
    code = "insufficient_scope"


class APIKeyExpired(APIKeyError):
    status_code = 401
    code = "expired_api_key"


class APIRateLimited(APIKeyError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int = 60):
        super().__init__(self.code)
        self.retry_after = retry_after


class WorkspaceQuotaExceeded(Exception):
    """Raised when a Pro workspace write would exceed bounded storage policy."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _key_pepper() -> str:
    return os.environ.get(KEY_PEPPER_ENV, "").strip()


def _key_pepper_required() -> bool:
    return _truthy_env(KEY_PEPPER_REQUIRED_ENV)


def _hash_key(token: str) -> str:
    pepper = _key_pepper()
    if pepper:
        digest = hmac.new(
            pepper.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return KEY_HASH_HMAC_PREFIX + digest
    if _key_pepper_required():
        raise RuntimeError(f"{KEY_PEPPER_ENV} is required to create Pro API keys")
    return KEY_HASH_SHA256_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _key_hash_matches(stored_hash: str, token: str) -> bool:
    stored = str(stored_hash or "")
    if stored.startswith(KEY_HASH_HMAC_PREFIX):
        pepper = _key_pepper()
        if not pepper:
            return False
        return hmac.compare_digest(stored, _hash_key(token))
    if stored.startswith(KEY_HASH_SHA256_PREFIX):
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored, KEY_HASH_SHA256_PREFIX + digest)
    # Backward compatibility for pre-pepper rows already present in a Pro DB.
    return hmac.compare_digest(stored, hashlib.sha256(token.encode("utf-8")).hexdigest())


def _parse_token(token: str) -> tuple[str, str]:
    if not token or not token.startswith(KEY_PREFIX + "_"):
        raise APIKeyError("invalid API key")
    rest = token[len(KEY_PREFIX) + 1:]
    parts = rest.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise APIKeyError("invalid API key")
    return parts[0], token


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _scopes_to_string(scopes) -> str:
    vals = [str(s).strip() for s in scopes if str(s).strip()]
    return " ".join(dict.fromkeys(vals or DEFAULT_SCOPES))


def _json_compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class APIKey:
    key_id: str
    label: str
    scopes: tuple[str, ...]
    tier: str
    rate_per_min: int
    rate_per_day: int
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    rotation_due_at: Optional[str] = None


class ProAPIStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(api_keys)").fetchall()
        }
        if "rotation_due_at" not in columns:
            self.conn.execute("ALTER TABLE api_keys ADD COLUMN rotation_due_at TEXT")
        if "contact_email" not in columns:
            self.conn.execute("ALTER TABLE api_keys ADD COLUMN contact_email TEXT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ProAPIStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _record_operator_event(
        self,
        event_type: str,
        *,
        key_id: Optional[str] = None,
        label: str = "",
        actor: str = "cli",
        detail: Optional[dict] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        event = {
            "id": secrets.token_hex(8),
            "event_type": str(event_type or "").strip()[:120],
            "key_id": key_id,
            "label": str(label or "")[:200],
            "actor": str(actor or "cli")[:80],
            "detail": detail or {},
            "created_at": created_at or _iso(_now()),
        }
        if not event["event_type"]:
            raise ValueError("event_type is required")
        self.conn.execute(
            """INSERT INTO pro_operator_events(id,event_type,key_id,label,actor,detail_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                event["id"],
                event["event_type"],
                event["key_id"],
                event["label"],
                event["actor"],
                _json_compact(event["detail"]),
                event["created_at"],
            ),
        )
        return event

    def record_operator_event(
        self,
        event_type: str,
        *,
        key_id: Optional[str] = None,
        label: str = "",
        actor: str = "cli",
        detail: Optional[dict] = None,
    ) -> dict:
        """Record a non-secret operator action for audit/fulfillment evidence."""
        with self.conn:
            return self._record_operator_event(
                event_type,
                key_id=key_id,
                label=label,
                actor=actor,
                detail=detail,
            )

    def create_key(
        self,
        label: str,
        scopes=DEFAULT_SCOPES,
        tier: str = "pro",
        rate_per_min: int = 120,
        rate_per_day: int = 10000,
        expires_days: Optional[int] = None,
        rotation_days: Optional[int] = 90,
        actor: str = "cli",
        contact_email: str = "",
    ) -> tuple[str, APIKey]:
        label = (label or "").strip()
        if not label:
            raise ValueError("label is required")
        contact_email = (contact_email or "").strip()
        key_id = secrets.token_hex(8)
        token = f"{KEY_PREFIX}_{key_id}_{secrets.token_urlsafe(32)}"
        now = _now()
        expires_at = _iso(now + timedelta(days=expires_days)) if expires_days else None
        rotation_due_at = (
            _iso(now + timedelta(days=rotation_days))
            if rotation_days is not None else None
        )
        scopes_s = _scopes_to_string(scopes)
        key_hash = _hash_key(token)
        with self.conn:
            self.conn.execute(
                """INSERT INTO api_keys(key_id,label,contact_email,key_hash,scopes,tier,rate_per_min,
                                        rate_per_day,created_at,expires_at,rotation_due_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key_id, label, contact_email, key_hash, scopes_s, tier,
                    int(rate_per_min), int(rate_per_day), _iso(now), expires_at,
                    rotation_due_at,
                ),
            )
            self._record_operator_event(
                "api_key.created",
                key_id=key_id,
                label=label,
                actor=actor,
                created_at=_iso(now),
                detail={
                    "scopes": scopes_s.split(),
                    "tier": tier,
                    "rate_per_min": int(rate_per_min),
                    "rate_per_day": int(rate_per_day),
                    "expires_at": expires_at,
                    "rotation_due_at": rotation_due_at,
                    "contact_email": contact_email,
                    "token_stored": False,
                    "token_hash_exposed": False,
                    "token_binding_scheme": (
                        "hmac-sha256" if key_hash.startswith(KEY_HASH_HMAC_PREFIX) else "sha256"
                    ),
                    "instance_bound": key_hash.startswith(KEY_HASH_HMAC_PREFIX),
                },
            )
        return token, APIKey(
            key_id=key_id, label=label, scopes=tuple(scopes_s.split()), tier=tier,
            rate_per_min=int(rate_per_min), rate_per_day=int(rate_per_day),
            created_at=_iso(now), expires_at=expires_at, rotation_due_at=rotation_due_at,
        )

    def revoke_key(self, key_id: str, *, actor: str = "cli") -> bool:
        row = self.conn.execute(
            """SELECT key_id,label,scopes,tier,expires_at,rotation_due_at,revoked_at
               FROM api_keys WHERE key_id=?""",
            (key_id,),
        ).fetchone()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
                (_iso(_now()), key_id),
            )
            if cur.rowcount > 0 and row:
                self._record_operator_event(
                    "api_key.revoked",
                    key_id=key_id,
                    label=row["label"],
                    actor=actor,
                    detail={
                        "scopes": str(row["scopes"] or "").split(),
                        "tier": row["tier"],
                        "expires_at": row["expires_at"],
                        "rotation_due_at": row["rotation_due_at"],
                        "token_stored": False,
                        "token_hash_exposed": False,
                    },
                )
        return cur.rowcount > 0

    def list_keys(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT key_id,label,contact_email,scopes,tier,rate_per_min,rate_per_day,created_at,
                      expires_at,rotation_due_at,revoked_at,last_used_at
               FROM api_keys ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def list_operator_events(self, limit: int = 25, key_id: Optional[str] = None) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 25)))
        args: list[object] = []
        where = ""
        if key_id:
            where = "WHERE key_id=?"
            args.append(key_id)
        rows = self.conn.execute(
            f"""SELECT id,event_type,key_id,label,actor,detail_json,created_at
                FROM pro_operator_events
                {where}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?""",
            (*args, safe_limit),
        ).fetchall()
        events = []
        for r in rows:
            detail = {}
            try:
                detail = json.loads(r["detail_json"] or "{}")
            except json.JSONDecodeError:
                detail = {}
            events.append({
                "id": r["id"],
                "event_type": r["event_type"],
                "key_id": r["key_id"],
                "label": r["label"],
                "actor": r["actor"],
                "detail": detail,
                "created_at": r["created_at"],
            })
        return events

    def admin_health(self) -> dict:
        """Return bounded Pro control-plane health without exposing secrets."""
        now = _now()
        now_iso = _iso(now)
        current_minute = "m:" + now.strftime("%Y%m%d%H%M")
        current_day = "d:" + now.strftime("%Y%m%d")
        keys = self.list_keys()
        active_keys = [
            k for k in keys
            if not k.get("revoked_at") and (not k.get("expires_at") or k["expires_at"] > now_iso)
        ]
        expired_keys = [
            k for k in keys
            if not k.get("revoked_at") and k.get("expires_at") and k["expires_at"] <= now_iso
        ]
        rotation_due_keys = [
            k for k in active_keys
            if k.get("rotation_due_at") and k["rotation_due_at"] <= now_iso
        ]
        rotation_due_soon_keys = []
        soon_cutoff = _iso(now + timedelta(days=14))
        for k in active_keys:
            due = k.get("rotation_due_at")
            if due and now_iso < due <= soon_cutoff:
                rotation_due_soon_keys.append(k)
        usage_rows = self.conn.execute(
            """SELECT u.key_id,k.label,k.tier,k.rate_per_min,k.rate_per_day,
                      SUM(CASE WHEN u.bucket=? THEN u.count ELSE 0 END) AS minute_count,
                      SUM(CASE WHEN u.bucket=? THEN u.count ELSE 0 END) AS day_count
               FROM api_keys k
               LEFT JOIN api_key_usage u ON u.key_id=k.key_id
               GROUP BY k.key_id
               ORDER BY day_count DESC, minute_count DESC, k.created_at DESC
               LIMIT 25""",
            (current_minute, current_day),
        ).fetchall()
        audit_summary = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      MAX(at) AS latest_at,
                      SUM(CASE WHEN status BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN status=401 THEN 1 ELSE 0 END) AS unauthorized,
                      SUM(CASE WHEN status=403 THEN 1 ELSE 0 END) AS forbidden,
                      SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) AS rate_limited,
                      SUM(CASE WHEN status>=500 THEN 1 ELSE 0 END) AS server_errors
               FROM api_audit"""
        ).fetchone()
        recent_errors = self.conn.execute(
            """SELECT key_id,method,route,status,at
               FROM api_audit
               WHERE status >= 400
               ORDER BY at DESC, id DESC
               LIMIT 10"""
        ).fetchall()
        route_rows = self.conn.execute(
            """SELECT route, COUNT(*) AS count, MAX(at) AS latest_at
               FROM api_audit
               GROUP BY route
               ORDER BY latest_at DESC
               LIMIT 15"""
        ).fetchall()
        workspace = self.conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM saved_watchlists) AS watchlists,
                   (SELECT COUNT(*) FROM saved_watchlist_signal_snapshots) AS signal_snapshots,
                   (SELECT MAX(created_at) FROM saved_watchlist_signal_snapshots) AS latest_snapshot_at,
                   (SELECT COUNT(*) FROM saved_workspace_alerts) AS alerts,
                   (SELECT COUNT(*) FROM saved_workspace_alerts WHERE status='open') AS open_alerts,
                   (SELECT COUNT(*) FROM saved_workspace_activity) AS activity_events,
                   (SELECT MAX(created_at) FROM saved_workspace_activity) AS latest_activity_at"""
        ).fetchone()
        operator_summary_row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      MAX(created_at) AS latest_at,
                      SUM(CASE WHEN event_type='api_key.created' THEN 1 ELSE 0 END) AS created,
                      SUM(CASE WHEN event_type='api_key.revoked' THEN 1 ELSE 0 END) AS revoked
               FROM pro_operator_events"""
        ).fetchone()
        operator_summary = dict(operator_summary_row or {})
        audit = dict(audit_summary or {})
        status = "ok"
        warnings: list[str] = []
        pepper_configured = bool(_key_pepper())
        if not active_keys:
            status = "warn"
            warnings.append("no active Pro API key")
        if _key_pepper_required() and not pepper_configured:
            status = "warn"
            warnings.append(f"{KEY_PEPPER_ENV} is required but not configured")
        if int(audit.get("server_errors") or 0) > 0:
            status = "warn"
            warnings.append("Pro API audit contains server errors")
        if int(audit.get("rate_limited") or 0) > 0:
            warnings.append("Pro API audit contains rate-limited requests")
        if rotation_due_keys:
            status = "warn"
            warnings.append("one or more active Pro API keys are due for rotation")
        if rotation_due_soon_keys:
            warnings.append("one or more active Pro API keys rotate within 14 days")
        return {
            "status": status,
            "warnings": warnings,
            "generated_at": now_iso,
            "key_security": {
                "hash_scheme_for_new_keys": "hmac-sha256" if pepper_configured else "sha256",
                "instance_pepper_configured": pepper_configured,
                "instance_pepper_required": _key_pepper_required(),
                "accepts_legacy_sha256_rows": True,
                "prod_clone_key_reuse_blocked": pepper_configured,
            },
            "keys": {
                "total": len(keys),
                "active": len(active_keys),
                "revoked": len([k for k in keys if k.get("revoked_at")]),
                "expired": len(expired_keys),
                "rotation_due": len(rotation_due_keys),
                "rotation_due_soon": len(rotation_due_soon_keys),
                "recent": [
                    {
                        "id": k["key_id"],
                        "label": k["label"],
                        "contact_email": k.get("contact_email") or "",
                        "tier": k["tier"],
                        "scopes": str(k.get("scopes") or "").split(),
                        "created_at": k["created_at"],
                        "expires_at": k.get("expires_at"),
                        "rotation_due_at": k.get("rotation_due_at"),
                        "last_used_at": k.get("last_used_at"),
                        "revoked": bool(k.get("revoked_at")),
                        "expired": bool(k.get("expires_at") and k["expires_at"] <= now_iso),
                        "rotation_due": bool(k.get("rotation_due_at") and k["rotation_due_at"] <= now_iso),
                    }
                    for k in keys[:10]
                ],
            },
            "usage": {
                "current_minute_bucket": current_minute,
                "current_day_bucket": current_day,
                "keys": [
                    {
                        "key_id": r["key_id"],
                        "label": r["label"],
                        "tier": r["tier"],
                        "minute_count": int(r["minute_count"] or 0),
                        "minute_limit": int(r["rate_per_min"] or 0),
                        "day_count": int(r["day_count"] or 0),
                        "day_limit": int(r["rate_per_day"] or 0),
                    }
                    for r in usage_rows if r["key_id"]
                ],
            },
            "audit": {
                "total": int(audit.get("total") or 0),
                "latest_at": audit.get("latest_at"),
                "ok": int(audit.get("ok") or 0),
                "unauthorized": int(audit.get("unauthorized") or 0),
                "forbidden": int(audit.get("forbidden") or 0),
                "rate_limited": int(audit.get("rate_limited") or 0),
                "server_errors": int(audit.get("server_errors") or 0),
                "recent_errors": [dict(r) for r in recent_errors],
                "recent_routes": [dict(r) for r in route_rows],
            },
            "workspace": {
                "watchlists": int(workspace["watchlists"] or 0),
                "signal_snapshots": int(workspace["signal_snapshots"] or 0),
                "latest_snapshot_at": workspace["latest_snapshot_at"],
                "alerts": int(workspace["alerts"] or 0),
                "open_alerts": int(workspace["open_alerts"] or 0),
                "activity_events": int(workspace["activity_events"] or 0),
                "latest_activity_at": workspace["latest_activity_at"],
            },
            "operator_events": {
                "total": int(operator_summary.get("total") or 0),
                "latest_at": operator_summary.get("latest_at"),
                "api_key_created": int(operator_summary.get("created") or 0),
                "api_key_revoked": int(operator_summary.get("revoked") or 0),
                "recent": self.list_operator_events(limit=10),
                "privacy": {
                    "tokens_stored": False,
                    "token_hashes_exposed": False,
                    "ip_exposed": False,
                    "user_agent_exposed": False,
                },
            },
            "external_checks": {
                "collected_by_web_process": False,
                "reason": "systemd timers, encrypted backups and smoke outputs are verified outside the web worker",
                "expected_units": [
                    "13flow-pro-backup.timer",
                    "13flow-pro-workspace-snapshot.timer",
                ],
                "expected_smokes": [
                    "deploy/smoke-public.sh",
                    "deploy/smoke-pro-workspace.sh",
                    "deploy/verify-pro-db-backup.sh",
                ],
            },
        }

    def usage_report(self, key_id: str, *, recent_limit: int = 25,
                     route_limit: int = 15) -> dict:
        """Return customer-safe usage and quota telemetry for one API key."""
        safe_recent_limit = max(1, min(100, int(recent_limit or 25)))
        safe_route_limit = max(1, min(50, int(route_limit or 15)))
        now = _now()
        now_iso = _iso(now)
        current_minute = "m:" + now.strftime("%Y%m%d%H%M")
        current_day = "d:" + now.strftime("%Y%m%d")
        month_prefix = "d:" + now.strftime("%Y%m")
        row = self.conn.execute(
            """SELECT key_id,label,scopes,tier,rate_per_min,rate_per_day,created_at,
                      expires_at,rotation_due_at,revoked_at,last_used_at
               FROM api_keys WHERE key_id=?""",
            (key_id,),
        ).fetchone()
        if row is None:
            raise APIKeyError("invalid API key")
        minute_count = self._usage_count(key_id, current_minute)
        day_count = self._usage_count(key_id, current_day)
        month_row = self.conn.execute(
            """SELECT COALESCE(SUM(count), 0) AS c
               FROM api_key_usage
               WHERE key_id=? AND bucket LIKE ?""",
            (key_id, month_prefix + "%"),
        ).fetchone()
        audit_summary = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      MAX(at) AS latest_at,
                      SUM(CASE WHEN status BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN status=401 THEN 1 ELSE 0 END) AS unauthorized,
                      SUM(CASE WHEN status=403 THEN 1 ELSE 0 END) AS forbidden,
                      SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) AS rate_limited,
                      SUM(CASE WHEN status>=500 THEN 1 ELSE 0 END) AS server_errors
               FROM api_audit
               WHERE key_id=?""",
            (key_id,),
        ).fetchone()
        route_rows = self.conn.execute(
            """SELECT route, method, COUNT(*) AS count, MAX(at) AS latest_at,
                      SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors
               FROM api_audit
               WHERE key_id=?
               GROUP BY route, method
               ORDER BY count DESC, latest_at DESC
               LIMIT ?""",
            (key_id, safe_route_limit),
        ).fetchall()
        recent_rows = self.conn.execute(
            """SELECT method,route,status,at
               FROM api_audit
               WHERE key_id=?
               ORDER BY at DESC, id DESC
               LIMIT ?""",
            (key_id, safe_recent_limit),
        ).fetchall()
        audit = dict(audit_summary or {})
        minute_limit = int(row["rate_per_min"] or 0)
        day_limit = int(row["rate_per_day"] or 0)
        def quota(count: int, limit: int) -> dict:
            remaining = max(0, limit - count) if limit else 0
            pct = round((count / limit) * 100.0, 2) if limit else None
            return {"used": count, "limit": limit, "remaining": remaining,
                    "used_pct": pct}
        return {
            "generated_at": now_iso,
            "scope": "api_key",
            "key": {
                "id": row["key_id"],
                "label": row["label"],
                "tier": row["tier"],
                "scopes": str(row["scopes"] or "").split(),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "rotation_due_at": row["rotation_due_at"],
                "last_used_at": row["last_used_at"],
                "revoked": bool(row["revoked_at"]),
            },
            "quota": {
                "current_minute_bucket": current_minute,
                "current_day_bucket": current_day,
                "minute": quota(minute_count, minute_limit),
                "day": quota(day_count, day_limit),
                "month_observed": {
                    "bucket_prefix": month_prefix,
                    "used": int((month_row or {})["c"] or 0),
                    "limit": None,
                    "note": "monthly usage is observed for customer reporting; enforcement is per-minute and per-day",
                },
            },
            "audit": {
                "total": int(audit.get("total") or 0),
                "latest_at": audit.get("latest_at"),
                "ok": int(audit.get("ok") or 0),
                "unauthorized": int(audit.get("unauthorized") or 0),
                "forbidden": int(audit.get("forbidden") or 0),
                "rate_limited": int(audit.get("rate_limited") or 0),
                "server_errors": int(audit.get("server_errors") or 0),
            },
            "routes": [dict(r) for r in route_rows],
            "recent_requests": [dict(r) for r in recent_rows],
            "privacy": {
                "token_echoed": False,
                "ip_exposed": False,
                "user_agent_exposed": False,
                "payloads_logged": False,
            },
        }

    def pilot_closeout_report(
        self,
        *,
        key_id: Optional[str] = None,
        days: int = 7,
        key_limit: int = 10,
    ) -> dict:
        """Return a bounded, customer-safe pilot closeout report."""
        safe_days = max(1, min(30, int(days or 7)))
        safe_limit = max(1, min(25, int(key_limit or 10)))
        now = _now()
        now_iso = _iso(now)
        since_iso = _iso(now - timedelta(days=safe_days))
        args: list[object] = []
        where = ""
        if key_id:
            where = "WHERE key_id=?"
            args.append(key_id)
        else:
            where = "WHERE (created_at>=? OR last_used_at>=?) AND scopes NOT LIKE '%admin:read%'"
            args.extend([since_iso, since_iso])
        key_rows = self.conn.execute(
            f"""SELECT key_id,label,scopes,tier,rate_per_min,rate_per_day,created_at,
                       expires_at,rotation_due_at,revoked_at,last_used_at
                FROM api_keys
                {where}
                ORDER BY COALESCE(last_used_at, created_at) DESC, created_at DESC
                LIMIT ?""",
            (*args, safe_limit),
        ).fetchall()
        reports = [self._pilot_closeout_for_key(dict(row), since_iso, now_iso) for row in key_rows]
        totals = {
            "keys": len(reports),
            "requests": sum(r["usage"]["requests"] for r in reports),
            "ok_requests": sum(r["usage"]["ok"] for r in reports),
            "server_errors": sum(r["usage"]["server_errors"] for r in reports),
            "rate_limited": sum(r["usage"]["rate_limited"] for r in reports),
            "watchlists": sum(r["workspace"]["watchlists"] for r in reports),
            "snapshots": sum(r["workspace"]["snapshots_in_window"] for r in reports),
            "alerts": sum(r["workspace"]["alerts_total"] for r in reports),
            "open_alerts": sum(r["workspace"]["open_alerts"] for r in reports),
        }
        verdict = "continue"
        reasons: list[str] = []
        if not reports:
            verdict = "hold"
            reasons.append("no Pro API key matched the closeout window")
        elif totals["server_errors"] > 0:
            verdict = "hold"
            reasons.append("server errors were observed during the pilot window")
        elif totals["ok_requests"] == 0:
            verdict = "hold"
            reasons.append("no successful customer API request was observed")
        elif any(r["key_lifecycle"]["rotation_required"] or r["key_lifecycle"]["expired"] or r["key_lifecycle"]["revoked"] for r in reports):
            verdict = "continue"
            reasons.append("key lifecycle action is required before expansion")
        elif totals["requests"] >= 10 and (totals["watchlists"] > 0 or totals["snapshots"] > 0):
            verdict = "expand_candidate"
            reasons.append("pilot shows repeated usage and workspace adoption")
        else:
            reasons.append("pilot is usable but needs more observed workflow evidence before expansion")
        return {
            "generated_at": now_iso,
            "window": {"days": safe_days, "since": since_iso, "until": now_iso},
            "selection": {"key_id": key_id, "key_limit": safe_limit},
            "summary": totals,
            "verdict": {
                "status": verdict,
                "reasons": reasons,
                "not_investment_advice": True,
                "not_claimed": ["validated alpha", "investment recommendation", "performance attribution"],
            },
            "keys": reports,
            "privacy": {
                "tokens_echoed": False,
                "token_hashes_exposed": False,
                "audit_ips_exposed": False,
                "audit_user_agents_exposed": False,
                "payloads_logged": False,
            },
        }

    def _pilot_closeout_for_key(self, key: dict, since_iso: str, now_iso: str) -> dict:
        key_id = key["key_id"]
        audit = dict(self.conn.execute(
            """SELECT COUNT(*) AS total,
                      MAX(at) AS latest_at,
                      SUM(CASE WHEN status BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN status=401 THEN 1 ELSE 0 END) AS unauthorized,
                      SUM(CASE WHEN status=403 THEN 1 ELSE 0 END) AS forbidden,
                      SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) AS rate_limited,
                      SUM(CASE WHEN status>=500 THEN 1 ELSE 0 END) AS server_errors
               FROM api_audit
               WHERE key_id=? AND at>=?""",
            (key_id, since_iso),
        ).fetchone() or {})
        routes = self.conn.execute(
            """SELECT method,route,COUNT(*) AS count,MAX(at) AS latest_at,
                      SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END) AS errors
               FROM api_audit
               WHERE key_id=? AND at>=?
               GROUP BY method,route
               ORDER BY count DESC, latest_at DESC
               LIMIT 10""",
            (key_id, since_iso),
        ).fetchall()
        watchlists = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS created_in_window
               FROM saved_watchlists WHERE key_id=?""",
            (since_iso, key_id),
        ).fetchone()
        snapshots = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS in_window,
                      MAX(created_at) AS latest_at
               FROM saved_watchlist_signal_snapshots WHERE key_id=?""",
            (since_iso, key_id),
        ).fetchone()
        alerts = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
                      SUM(CASE WHEN status='acknowledged' THEN 1 ELSE 0 END) AS acknowledged,
                      SUM(CASE WHEN status='dismissed' THEN 1 ELSE 0 END) AS dismissed,
                      SUM(CASE WHEN first_seen_at>=? THEN 1 ELSE 0 END) AS new_in_window,
                      MAX(last_seen_at) AS latest_at
               FROM saved_workspace_alerts WHERE key_id=?""",
            (since_iso, key_id),
        ).fetchone()
        activity = self.conn.execute(
            """SELECT COUNT(*) AS total, MAX(created_at) AS latest_at
               FROM saved_workspace_activity WHERE key_id=? AND created_at>=?""",
            (key_id, since_iso),
        ).fetchone()
        top_alerts = self.conn.execute(
            """SELECT ticker,action,severity,status,last_seen_at
               FROM saved_workspace_alerts
               WHERE key_id=?
               ORDER BY severity DESC, last_seen_at DESC
               LIMIT 5""",
            (key_id,),
        ).fetchall()
        usage_total = int(audit.get("total") or 0)
        ok_total = int(audit.get("ok") or 0)
        errors_total = (
            int(audit.get("unauthorized") or 0)
            + int(audit.get("forbidden") or 0)
            + int(audit.get("rate_limited") or 0)
            + int(audit.get("server_errors") or 0)
        )
        lifecycle = {
            "revoked": bool(key.get("revoked_at")),
            "expired": bool(key.get("expires_at") and key["expires_at"] <= now_iso),
            "rotation_required": bool(key.get("rotation_due_at") and key["rotation_due_at"] <= now_iso),
            "expires_at": key.get("expires_at"),
            "rotation_due_at": key.get("rotation_due_at"),
            "last_used_at": key.get("last_used_at"),
        }
        return {
            "key": {
                "id": key_id,
                "label": key["label"],
                "tier": key["tier"],
                "scopes": str(key.get("scopes") or "").split(),
                "created_at": key["created_at"],
            },
            "key_lifecycle": lifecycle,
            "usage": {
                "requests": usage_total,
                "ok": ok_total,
                "errors": errors_total,
                "unauthorized": int(audit.get("unauthorized") or 0),
                "forbidden": int(audit.get("forbidden") or 0),
                "rate_limited": int(audit.get("rate_limited") or 0),
                "server_errors": int(audit.get("server_errors") or 0),
                "latest_at": audit.get("latest_at"),
                "routes": [dict(r) for r in routes],
            },
            "workspace": {
                "watchlists": int((watchlists or {})["total"] or 0),
                "watchlists_created_in_window": int((watchlists or {})["created_in_window"] or 0),
                "snapshots_total": int((snapshots or {})["total"] or 0),
                "snapshots_in_window": int((snapshots or {})["in_window"] or 0),
                "latest_snapshot_at": (snapshots or {})["latest_at"],
                "alerts_total": int((alerts or {})["total"] or 0),
                "open_alerts": int((alerts or {})["open"] or 0),
                "acknowledged_alerts": int((alerts or {})["acknowledged"] or 0),
                "dismissed_alerts": int((alerts or {})["dismissed"] or 0),
                "new_alerts_in_window": int((alerts or {})["new_in_window"] or 0),
                "latest_alert_at": (alerts or {})["latest_at"],
                "activity_events_in_window": int((activity or {})["total"] or 0),
                "latest_activity_at": (activity or {})["latest_at"],
                "top_alerts": [dict(r) for r in top_alerts],
            },
        }

    def authenticate(self, token: str, required_scope: str) -> APIKey:
        key_id, full_token = _parse_token(token)
        row = self.conn.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise APIKeyError("invalid API key")
        if not _key_hash_matches(row["key_hash"], full_token):
            raise APIKeyError("invalid API key")
        if row["revoked_at"]:
            raise APIKeyError("revoked API key")
        expires_at = _parse_iso(row["expires_at"])
        if row["expires_at"] and (expires_at is None or expires_at <= _now()):
            raise APIKeyExpired("expired API key")
        scopes = tuple((row["scopes"] or "").split())
        scope_ok = (
            not required_scope
            or required_scope in scopes
            or (required_scope == "admin:read" and "admin:write" in scopes)
        )
        if not scope_ok:
            raise APIKeyForbidden("insufficient scope")
        key = APIKey(
            key_id=row["key_id"], label=row["label"], scopes=scopes, tier=row["tier"],
            rate_per_min=int(row["rate_per_min"]), rate_per_day=int(row["rate_per_day"]),
            created_at=row["created_at"], expires_at=row["expires_at"],
            rotation_due_at=row["rotation_due_at"],
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

    def _usage_count(self, key_id: str, bucket: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM api_key_usage WHERE key_id=? AND bucket=?",
            (key_id, bucket),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def audit(self, key_id: Optional[str], method: str, route: str, status: int,
              ip: str = "", user_agent: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO api_audit(key_id,method,route,status,ip,user_agent,at)
                   VALUES (?,?,?,?,?,?,?)""",
                (key_id, method, route, int(status), (ip or "")[:80],
                 (user_agent or "")[:300], _iso(_now())),
            )

    def _watchlist_row(self, row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "tickers": json.loads(row["tickers_json"] or "[]"),
            "filters": json.loads(row["filters_json"] or "{}"),
            "alert_policy": json.loads(row["alert_policy_json"] or "{}"),
            "notes": row["notes"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_watchlists(self, key_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM saved_watchlists
               WHERE key_id=?
               ORDER BY updated_at DESC, name ASC""",
            (key_id,),
        ).fetchall()
        return [self._watchlist_row(r) for r in rows]

    def list_due_automated_watchlists(
        self,
        max_items: int = 25,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        """Return enabled workspace watchlists that are due for a server snapshot.

        This intentionally uses the saved alert policy and previous snapshot
        timestamps only. It does not depend on plaintext API tokens, which are
        never stored in the Pro control-plane DB.
        """
        now = now or _now()
        now_iso = _iso(now)
        safe_limit = max(1, min(100, int(max_items or 25)))
        scan_limit = max(safe_limit, min(2000, safe_limit * 20))
        rows = self.conn.execute(
            """SELECT wl.*,
                      k.label AS key_label,
                      MAX(ss.created_at) AS latest_snapshot_at
               FROM saved_watchlists wl
               JOIN api_keys k ON k.key_id = wl.key_id
               LEFT JOIN saved_watchlist_signal_snapshots ss
                      ON ss.key_id = wl.key_id AND ss.watchlist_id = wl.id
               WHERE k.revoked_at IS NULL
                 AND (k.expires_at IS NULL OR k.expires_at > ?)
               GROUP BY wl.id
               ORDER BY latest_snapshot_at IS NOT NULL ASC,
                        latest_snapshot_at ASC,
                        wl.updated_at DESC
               LIMIT ?""",
            (now_iso, scan_limit),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            item = self._watchlist_row(row)
            policy = item.get("alert_policy") or {}
            if not policy.get("enabled"):
                continue
            frequency = str(policy.get("frequency") or "manual").lower()
            if frequency not in {"daily", "weekly"}:
                continue
            latest_raw = row["latest_snapshot_at"]
            due = latest_raw is None
            if latest_raw:
                try:
                    latest = datetime.fromisoformat(str(latest_raw))
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=timezone.utc)
                except ValueError:
                    latest = now - timedelta(days=365)
                cadence = timedelta(days=7 if frequency == "weekly" else 1)
                due = latest <= now - cadence
            if not due:
                continue
            item["key_id"] = row["key_id"]
            item["key_label"] = row["key_label"]
            item["latest_snapshot_at"] = latest_raw
            item["automation_frequency"] = frequency
            out.append(item)
            if len(out) >= safe_limit:
                break
        return out

    def workspace_automation_summary(
        self,
        max_due: int = 25,
        now: Optional[datetime] = None,
    ) -> dict:
        """Summarize scheduled workspace snapshot demand without tokens."""
        now = now or _now()
        now_iso = _iso(now)
        safe_limit = max(1, min(100, int(max_due or 25)))
        rows = self.conn.execute(
            """SELECT wl.*,
                      k.label AS key_label,
                      MAX(ss.created_at) AS latest_snapshot_at
               FROM saved_watchlists wl
               JOIN api_keys k ON k.key_id = wl.key_id
               LEFT JOIN saved_watchlist_signal_snapshots ss
                      ON ss.key_id = wl.key_id AND ss.watchlist_id = wl.id
               WHERE k.revoked_at IS NULL
                 AND (k.expires_at IS NULL OR k.expires_at > ?)
               GROUP BY wl.id
               ORDER BY latest_snapshot_at DESC, wl.updated_at DESC""",
            (now_iso,),
        ).fetchall()
        scheduled = 0
        daily = 0
        weekly = 0
        invalid_policy = 0
        due_count = 0
        due: list[dict] = []
        latest_snapshot_at = None
        for row in rows:
            item = self._watchlist_row(row)
            policy = item.get("alert_policy") or {}
            if not policy.get("enabled"):
                continue
            scheduled += 1
            frequency = str(policy.get("frequency") or "manual").lower()
            if frequency == "daily":
                daily += 1
            elif frequency == "weekly":
                weekly += 1
            else:
                invalid_policy += 1
                continue
            latest_raw = row["latest_snapshot_at"]
            if latest_raw and (latest_snapshot_at is None or latest_raw > latest_snapshot_at):
                latest_snapshot_at = latest_raw
            is_due = latest_raw is None
            if latest_raw:
                try:
                    latest = datetime.fromisoformat(str(latest_raw))
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=timezone.utc)
                except ValueError:
                    latest = now - timedelta(days=365)
                cadence = timedelta(days=7 if frequency == "weekly" else 1)
                is_due = latest <= now - cadence
            if is_due:
                due_count += 1
                if len(due) < safe_limit:
                    due.append({
                        "watchlist_id": row["id"],
                        "name": row["name"],
                        "key_id": row["key_id"],
                        "key_label": row["key_label"],
                        "frequency": frequency,
                        "latest_snapshot_at": latest_raw,
                        "tickers_count": len(item.get("tickers") or []),
                        "updated_at": row["updated_at"],
                    })
        return {
            "scheduled_watchlists": scheduled,
            "daily": daily,
            "weekly": weekly,
            "invalid_policy": invalid_policy,
            "due_count": due_count,
            "due_sample": due,
            "latest_snapshot_at": latest_snapshot_at,
            "max_due_returned": safe_limit,
        }

    def watchlist_count(self, key_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM saved_watchlists WHERE key_id=?",
            (key_id,),
        ).fetchone()
        return int((row or {})["c"] or 0)

    def get_watchlist(self, key_id: str, watchlist_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM saved_watchlists WHERE key_id=? AND id=?",
            (key_id, watchlist_id),
        ).fetchone()
        return self._watchlist_row(row) if row else None

    def create_watchlist(
        self,
        key_id: str,
        name: str,
        tickers: list[str],
        filters: Optional[dict] = None,
        alert_policy: Optional[dict] = None,
        notes: str = "",
        max_watchlists: int = DEFAULT_MAX_WATCHLISTS_PER_KEY,
    ) -> dict:
        now = _now().isoformat(timespec="microseconds")
        watchlist_id = secrets.token_hex(8)
        safe_max = max(1, min(500, int(max_watchlists or DEFAULT_MAX_WATCHLISTS_PER_KEY)))
        with self.conn:
            if self.watchlist_count(key_id) >= safe_max:
                raise WorkspaceQuotaExceeded(f"saved watchlist limit reached ({safe_max})")
            self.conn.execute(
                """INSERT INTO saved_watchlists(
                       id,key_id,name,tickers_json,filters_json,alert_policy_json,
                       notes,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    watchlist_id, key_id, name, json.dumps(tickers, separators=(",", ":")),
                    json.dumps(filters or {}, sort_keys=True, separators=(",", ":")),
                    json.dumps(alert_policy or {}, sort_keys=True, separators=(",", ":")),
                    notes, now, now,
                ),
            )
        item = self.get_watchlist(key_id, watchlist_id)
        if item is None:
            raise RuntimeError("saved watchlist was not persisted")
        return item

    def update_watchlist(
        self,
        key_id: str,
        watchlist_id: str,
        name: str,
        tickers: list[str],
        filters: Optional[dict] = None,
        alert_policy: Optional[dict] = None,
        notes: str = "",
    ) -> Optional[dict]:
        now = _now().isoformat(timespec="microseconds")
        with self.conn:
            cur = self.conn.execute(
                """UPDATE saved_watchlists
                   SET name=?, tickers_json=?, filters_json=?, alert_policy_json=?,
                       notes=?, updated_at=?
                   WHERE key_id=? AND id=?""",
                (
                    name, json.dumps(tickers, separators=(",", ":")),
                    json.dumps(filters or {}, sort_keys=True, separators=(",", ":")),
                    json.dumps(alert_policy or {}, sort_keys=True, separators=(",", ":")),
                    notes, now, key_id, watchlist_id,
                ),
            )
        if cur.rowcount == 0:
            return None
        return self.get_watchlist(key_id, watchlist_id)

    def delete_watchlist(self, key_id: str, watchlist_id: str) -> bool:
        with self.conn:
            self.conn.execute(
                "DELETE FROM saved_watchlist_signal_snapshots WHERE key_id=? AND watchlist_id=?",
                (key_id, watchlist_id),
            )
            self.conn.execute(
                "DELETE FROM saved_workspace_alerts WHERE key_id=? AND watchlist_id=?",
                (key_id, watchlist_id),
            )
            cur = self.conn.execute(
                "DELETE FROM saved_watchlists WHERE key_id=? AND id=?",
                (key_id, watchlist_id),
            )
        return cur.rowcount > 0

    def _signal_snapshot_row(self, row, include_signals: bool = False) -> dict:
        signals = json.loads(row["signals_json"] or "{}")
        out = {
            "id": row["id"],
            "watchlist_id": row["watchlist_id"],
            "created_at": row["created_at"],
            "summary": json.loads(row["summary_json"] or "{}"),
            "tickers": json.loads(row["tickers_json"] or "[]"),
            "metadata": signals.get("metadata") or {},
        }
        if include_signals:
            out["signals"] = signals
        return out

    def list_signal_snapshots(
        self,
        key_id: str,
        watchlist_id: str,
        limit: int = 20,
        include_signals: bool = False,
    ) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 20)))
        rows = self.conn.execute(
            """SELECT * FROM saved_watchlist_signal_snapshots
               WHERE key_id=? AND watchlist_id=?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (key_id, watchlist_id, safe_limit),
        ).fetchall()
        return [self._signal_snapshot_row(r, include_signals=include_signals) for r in rows]

    def create_signal_snapshot(
        self,
        key_id: str,
        watchlist_id: str,
        signals: dict,
        max_snapshots: int = 100,
    ) -> dict:
        snapshot_id = secrets.token_hex(8)
        now = _now().isoformat(timespec="microseconds")
        items = list((signals or {}).get("items") or [])
        tickers = [
            str(item.get("ticker") or "").upper()
            for item in items
            if str(item.get("ticker") or "").strip()
        ]
        summary = dict((signals or {}).get("summary") or {})
        keep = max(1, min(500, int(max_snapshots or 100)))
        with self.conn:
            self.conn.execute(
                """INSERT INTO saved_watchlist_signal_snapshots(
                       id,key_id,watchlist_id,signals_json,summary_json,tickers_json,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    snapshot_id, key_id, watchlist_id, _json_compact(signals or {}),
                    _json_compact(summary), _json_compact(tickers), now,
                ),
            )
            self.conn.execute(
                """DELETE FROM saved_watchlist_signal_snapshots
                   WHERE key_id=? AND watchlist_id=?
                     AND id NOT IN (
                         SELECT id FROM saved_watchlist_signal_snapshots
                         WHERE key_id=? AND watchlist_id=?
                         ORDER BY created_at DESC, id DESC
                         LIMIT ?
                     )""",
                (key_id, watchlist_id, key_id, watchlist_id, keep),
            )
        row = self.conn.execute(
            """SELECT * FROM saved_watchlist_signal_snapshots
               WHERE key_id=? AND watchlist_id=? AND id=?""",
            (key_id, watchlist_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("saved watchlist signal snapshot was not persisted")
        return self._signal_snapshot_row(row)

    def _workspace_alert_row(self, row) -> dict:
        return {
            "id": row["id"],
            "watchlist_id": row["watchlist_id"],
            "snapshot_id": row["snapshot_id"],
            "ticker": row["ticker"],
            "action": row["action"],
            "severity": int(row["severity"] or 0),
            "status": row["status"],
            "reason": json.loads(row["reason_json"] or "{}"),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "acknowledged_at": row["acknowledged_at"],
            "dismissed_at": row["dismissed_at"],
        }

    def upsert_workspace_alerts(
        self,
        key_id: str,
        watchlist_id: str,
        snapshot_id: str,
        signals: dict,
    ) -> dict:
        now = _now().isoformat(timespec="microseconds")
        candidates = []
        severity_by_action = {"alert": 3, "watch": 2}
        for item in (signals or {}).get("items") or []:
            action = str(item.get("action") or "").lower()
            if action not in severity_by_action:
                continue
            ticker = str(item.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            reason = {
                "score": (item.get("score") or {}).get("score"),
                "confidence": (item.get("confidence") or {}).get("status"),
                "movement_codes": list(item.get("movement_codes") or []),
                "movement_summary": item.get("movement_summary") or {},
                "triggers": item.get("triggers") or [],
                "latest_13f_quarter": item.get("latest_13f_quarter"),
            }
            candidates.append({
                "id": secrets.token_hex(8),
                "ticker": ticker,
                "action": action,
                "severity": severity_by_action[action],
                "reason": reason,
            })
        with self.conn:
            for alert in candidates:
                self.conn.execute(
                    """INSERT INTO saved_workspace_alerts(
                           id,key_id,watchlist_id,snapshot_id,ticker,action,severity,
                           status,reason_json,first_seen_at,last_seen_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key_id,watchlist_id,ticker,action) DO UPDATE SET
                           snapshot_id=excluded.snapshot_id,
                           severity=excluded.severity,
                           reason_json=excluded.reason_json,
                           last_seen_at=excluded.last_seen_at""",
                    (
                        alert["id"], key_id, watchlist_id, snapshot_id, alert["ticker"],
                        alert["action"], alert["severity"], "open",
                        _json_compact(alert["reason"]), now, now,
                    ),
                )
        counts = self.workspace_alert_summary(key_id)
        return {
            "candidates": len(candidates),
            "open": counts["by_status"].get("open", 0),
            "acknowledged": counts["by_status"].get("acknowledged", 0),
            "dismissed": counts["by_status"].get("dismissed", 0),
        }

    def list_workspace_alerts(
        self,
        key_id: str,
        status: Optional[str] = "open",
        limit: int = 50,
        watchlist_id: Optional[str] = None,
    ) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 50)))
        clauses = ["key_id=?"]
        params: list[object] = [key_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if watchlist_id:
            clauses.append("watchlist_id=?")
            params.append(watchlist_id)
        params.append(safe_limit)
        rows = self.conn.execute(
            f"""SELECT * FROM saved_workspace_alerts
                WHERE {' AND '.join(clauses)}
                ORDER BY severity DESC, last_seen_at DESC, ticker ASC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [self._workspace_alert_row(r) for r in rows]

    def update_workspace_alert_status(
        self,
        key_id: str,
        alert_id: str,
        status: str,
    ) -> Optional[dict]:
        now = _now().isoformat(timespec="microseconds")
        if status == "open":
            acknowledged_at = None
            dismissed_at = None
        else:
            row = self.conn.execute(
                """SELECT acknowledged_at,dismissed_at FROM saved_workspace_alerts
                   WHERE key_id=? AND id=?""",
                (key_id, alert_id),
            ).fetchone()
            if row is None:
                return None
            acknowledged_at = now if status == "acknowledged" else row["acknowledged_at"]
            dismissed_at = now if status == "dismissed" else row["dismissed_at"]
        with self.conn:
            cur = self.conn.execute(
                """UPDATE saved_workspace_alerts
                   SET status=?, acknowledged_at=?, dismissed_at=?
                   WHERE key_id=? AND id=?""",
                (status, acknowledged_at, dismissed_at, key_id, alert_id),
            )
        if cur.rowcount == 0:
            return None
        row = self.conn.execute(
            "SELECT * FROM saved_workspace_alerts WHERE key_id=? AND id=?",
            (key_id, alert_id),
        ).fetchone()
        return self._workspace_alert_row(row) if row else None

    def workspace_alert_summary(self, key_id: str) -> dict:
        rows = self.conn.execute(
            """SELECT status, COUNT(*) AS c FROM saved_workspace_alerts
               WHERE key_id=?
               GROUP BY status""",
            (key_id,),
        ).fetchall()
        by_status = {str(r["status"]): int(r["c"] or 0) for r in rows}
        return {
            "total": sum(by_status.values()),
            "by_status": {
                "open": by_status.get("open", 0),
                "acknowledged": by_status.get("acknowledged", 0),
                "dismissed": by_status.get("dismissed", 0),
            },
        }

    def workspace_summary(self, key_id: str) -> dict:
        watchlists = self.conn.execute(
            "SELECT COUNT(*) AS c FROM saved_watchlists WHERE key_id=?",
            (key_id,),
        ).fetchone()
        snapshots = self.conn.execute(
            """SELECT COUNT(*) AS c, MAX(created_at) AS latest
               FROM saved_watchlist_signal_snapshots
               WHERE key_id=?""",
            (key_id,),
        ).fetchone()
        activity = self.conn.execute(
            """SELECT COUNT(*) AS c, MAX(created_at) AS latest
               FROM saved_workspace_activity
               WHERE key_id=?""",
            (key_id,),
        ).fetchone()
        return {
            "watchlists": int((watchlists or {})["c"] or 0),
            "signal_snapshots": int((snapshots or {})["c"] or 0),
            "latest_snapshot_at": (snapshots or {})["latest"],
            "alerts": self.workspace_alert_summary(key_id),
            "activity_events": int((activity or {})["c"] or 0),
            "latest_activity_at": (activity or {})["latest"],
        }

    def _workspace_activity_row(self, row) -> dict:
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "title": row["title"],
            "detail": json.loads(row["detail_json"] or "{}"),
            "created_at": row["created_at"],
        }

    def record_workspace_activity(
        self,
        key_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        title: str,
        detail: Optional[dict] = None,
        max_events: int = 500,
    ) -> dict:
        event_id = secrets.token_hex(8)
        now = _now().isoformat(timespec="microseconds")
        keep = max(50, min(2000, int(max_events or 500)))
        with self.conn:
            self.conn.execute(
                """INSERT INTO saved_workspace_activity(
                       id,key_id,event_type,entity_type,entity_id,title,detail_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    event_id, key_id, str(event_type)[:80], str(entity_type)[:80],
                    str(entity_id)[:120], str(title)[:240],
                    _json_compact(detail or {}), now,
                ),
            )
            self.conn.execute(
                """DELETE FROM saved_workspace_activity
                   WHERE key_id=?
                     AND id NOT IN (
                         SELECT id FROM saved_workspace_activity
                         WHERE key_id=?
                         ORDER BY created_at DESC, id DESC
                         LIMIT ?
                     )""",
                (key_id, key_id, keep),
            )
        row = self.conn.execute(
            "SELECT * FROM saved_workspace_activity WHERE key_id=? AND id=?",
            (key_id, event_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("workspace activity event was not persisted")
        return self._workspace_activity_row(row)

    def list_workspace_activity(
        self,
        key_id: str,
        limit: int = 50,
        event_type: Optional[str] = None,
        entity_type: Optional[str] = None,
    ) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 50)))
        clauses = ["key_id=?"]
        params: list[object] = [key_id]
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        params.append(safe_limit)
        rows = self.conn.execute(
            f"""SELECT * FROM saved_workspace_activity
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [self._workspace_activity_row(r) for r in rows]

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
