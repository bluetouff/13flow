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
    ) -> dict:
        now = _iso(_now())
        watchlist_id = secrets.token_hex(8)
        with self.conn:
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
        now = _iso(_now())
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
        return {
            "watchlists": int((watchlists or {})["c"] or 0),
            "signal_snapshots": int((snapshots or {})["c"] or 0),
            "latest_snapshot_at": (snapshots or {})["latest"],
            "alerts": self.workspace_alert_summary(key_id),
        }

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
