"""
Offline production preflight checks.

These checks intentionally do not call EDGAR or any market-data provider. They validate the
local runtime contract: read-only market DB access, Pro control-plane writability, audit
presence, data-quality summary, and deploy SHA traceability.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Store
from .pro import ProAPIStore
from .quality import data_quality_report

SYSTEMD_VERSION_CONF = "/etc/systemd/system/13flow.service.d/version.conf"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    data: dict[str, Any] | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _check(name: str, status: str, detail: str, **data) -> Check:
    return Check(name=name, status=status, detail=detail, data=(data or None))


def deployed_sha_from_systemd(path: str = SYSTEMD_VERSION_CONF) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                prefix = "Environment=SMARTMONEY_GIT_SHA="
                if line.startswith(prefix):
                    return line[len(prefix):].strip().strip('"') or None
    except OSError:
        return None
    return None


def _market_db_checks(db_path: str) -> list[Check]:
    checks: list[Check] = []
    if not os.path.exists(db_path):
        return [_check("market_db.exists", "fail", f"missing: {db_path}")]

    try:
        s = Store(db_path, read_only=True)
    except Exception as e:  # noqa: BLE001
        return [_check("market_db.open_read_only", "fail", str(e))]

    try:
        checks.append(_check("market_db.open_read_only", "pass", db_path))
        try:
            s.conn.execute("CREATE TABLE __preflight_write_probe(id INTEGER)")
            checks.append(_check("market_db.rejects_writes", "fail",
                                 "read-only connection unexpectedly accepted a write"))
        except sqlite3.Error:
            checks.append(_check("market_db.rejects_writes", "pass",
                                 "SQLite read-only connection rejects writes"))

        funds = s.conn.execute("SELECT COUNT(*) c FROM funds").fetchone()["c"]
        filings = s.conn.execute("SELECT COUNT(*) c FROM filings").fetchone()["c"]
        latest = s.conn.execute("SELECT COUNT(*) c FROM latest_filings").fetchone()["c"]
        status = "pass" if funds > 0 and filings > 0 and latest > 0 else "fail"
        checks.append(_check("market_db.content", status,
                             f"{funds} funds, {filings} filings, {latest} latest rows",
                             funds=funds, filings=filings, latest_filings=latest))

        quality = data_quality_report(s, limit=25)
        summary = quality["summary"]
        q_status = "fail" if summary["unit_scale_candidates"] else "pass"
        detail = (
            f"status={summary['status']}, "
            f"aum_jump_warnings={summary['aum_jump_warnings']}, "
            f"unit_scale_candidates={summary['unit_scale_candidates']}"
        )
        checks.append(_check("market_db.data_quality", q_status, detail, summary=summary))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("market_db.integrity", "fail", str(e)))
    finally:
        s.close()
    return checks


def _pro_db_checks(pro_db_path: str, require_pro: bool, audit_recent_hours: int) -> list[Check]:
    checks: list[Check] = []
    if not pro_db_path:
        if require_pro:
            return [_check("pro_db.configured", "fail", "missing --pro-db/SMARTMONEY_PRO_DB")]
        return [_check("pro_db.configured", "warn", "not configured")]
    if not os.path.exists(pro_db_path):
        status = "fail" if require_pro else "warn"
        return [_check("pro_db.exists", status, f"missing: {pro_db_path}")]

    try:
        pro = ProAPIStore(pro_db_path)
    except Exception as e:  # noqa: BLE001
        return [_check("pro_db.open_writable", "fail", str(e))]

    try:
        checks.append(_check("pro_db.open_writable", "pass", pro_db_path))
        rows = pro.list_keys()
        active = [r for r in rows if not r["revoked_at"]]
        status = "pass" if active else ("fail" if require_pro else "warn")
        checks.append(_check("pro_db.active_keys", status, f"{len(active)} active / {len(rows)} total",
                             active=len(active), total=len(rows)))

        audit = pro.conn.execute(
            "SELECT COUNT(*) c, MAX(at) last_at FROM api_audit"
        ).fetchone()
        audit_count = int(audit["c"] or 0)
        last_at = _parse_iso(audit["last_at"])
        cutoff = _utcnow() - timedelta(hours=max(1, audit_recent_hours))
        if audit_count == 0:
            status = "fail" if require_pro else "warn"
            detail = "no audit rows"
        elif last_at is None:
            status = "warn"
            detail = f"{audit_count} audit rows, last timestamp unparsable"
        elif last_at < cutoff:
            status = "warn"
            detail = f"{audit_count} audit rows, last_at={last_at.isoformat()}"
        else:
            status = "pass"
            detail = f"{audit_count} audit rows, last_at={last_at.isoformat()}"
        checks.append(_check("pro_db.audit_recent", status, detail,
                             count=audit_count,
                             last_at=(last_at.isoformat() if last_at else None),
                             audit_recent_hours=audit_recent_hours))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("pro_db.integrity", "fail", str(e)))
    finally:
        pro.close()
    return checks


def _pro_api_contract_checks(
    db_path: str,
    pro_db_path: str,
    api_token: str | None,
    require_pro: bool,
) -> list[Check]:
    checks: list[Check] = []
    if not api_token:
        return [_check("pro_api.contract", "fail" if require_pro else "warn",
                       "not checked; provide a token via the configured env var")]

    old_pro_api = os.environ.get("SMARTMONEY_PRO_API")
    old_pro_db = os.environ.get("SMARTMONEY_PRO_DB")
    try:
        os.environ["SMARTMONEY_PRO_API"] = "1"
        os.environ["SMARTMONEY_PRO_DB"] = pro_db_path
        from .api import create_app
        client = create_app(db_path, open_mode=True).test_client()

        denied = client.get("/api/pro/v1/status")
        challenge = denied.headers.get("WWW-Authenticate")
        if denied.status_code == 401 and challenge == 'Bearer realm="13flow-pro"':
            checks.append(_check("pro_api.unauth_challenge", "pass",
                                 "401 Bearer challenge present"))
        else:
            checks.append(_check("pro_api.unauth_challenge", "fail",
                                 f"got status={denied.status_code}, "
                                 f"www-authenticate={challenge}"))

        ok = client.get("/api/pro/v1/status", headers={"Authorization": "Bearer " + api_token})
        if ok.status_code != 200:
            checks.append(_check("pro_api.auth_status", "fail", f"got HTTP {ok.status_code}"))
            return checks
        payload = ok.get_json() or {}
        key = payload.get("key") or {}
        rate_per_min = int(key.get("rate_per_min") or 0)
        rate_per_day = int(key.get("rate_per_day") or 0)
        if rate_per_min > 0 and rate_per_day > 0:
            checks.append(_check("pro_api.rate_limits_configured", "pass",
                                 f"{rate_per_min}/min, {rate_per_day}/day",
                                 key_id=key.get("id"),
                                 rate_per_min=rate_per_min,
                                 rate_per_day=rate_per_day))
        else:
            checks.append(_check("pro_api.rate_limits_configured", "fail",
                                 f"{rate_per_min}/min, {rate_per_day}/day"))

        cache_control = ok.headers.get("Cache-Control", "")
        vary = {v.strip() for v in ok.headers.get("Vary", "").split(",") if v.strip()}
        if (
            cache_control == "private, no-store, max-age=0"
            and ok.headers.get("Pragma") == "no-cache"
            and ok.headers.get("Expires") == "0"
            and {"Authorization", "X-13FLOW-Key"} <= vary
        ):
            checks.append(_check("pro_api.cache_headers", "pass",
                                 "private/no-store with credential Vary"))
        else:
            checks.append(_check("pro_api.cache_headers", "fail",
                                 "missing strict Pro cache/Vary headers",
                                 cache_control=cache_control,
                                 pragma=ok.headers.get("Pragma"),
                                 expires=ok.headers.get("Expires"),
                                 vary=sorted(vary)))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("pro_api.contract", "fail", str(e)))
    finally:
        if old_pro_api is None:
            os.environ.pop("SMARTMONEY_PRO_API", None)
        else:
            os.environ["SMARTMONEY_PRO_API"] = old_pro_api
        if old_pro_db is None:
            os.environ.pop("SMARTMONEY_PRO_DB", None)
        else:
            os.environ["SMARTMONEY_PRO_DB"] = old_pro_db
    return checks


def run_preflight(
    db_path: str,
    *,
    pro_db_path: str | None = None,
    require_pro: bool = False,
    expected_sha: str | None = None,
    current_sha: str | None = None,
    audit_recent_hours: int = 24,
    api_token: str | None = None,
) -> dict[str, Any]:
    checks: list[Check] = []

    if expected_sha:
        got = (current_sha or "").strip()
        if got == expected_sha:
            checks.append(_check("deploy.sha", "pass", got))
        else:
            checks.append(_check("deploy.sha", "fail",
                                 f"expected {expected_sha}, got {got or 'unknown'}",
                                 expected=expected_sha, got=(got or None)))
    elif current_sha:
        checks.append(_check("deploy.sha", "pass", current_sha))
    else:
        checks.append(_check("deploy.sha", "warn", "not checked"))

    checks.extend(_market_db_checks(db_path))
    if pro_db_path and os.path.exists(pro_db_path):
        checks.extend(_pro_api_contract_checks(db_path, pro_db_path, api_token, require_pro))
    checks.extend(_pro_db_checks(pro_db_path or "", require_pro, audit_recent_hours))

    counts = {
        "pass": sum(1 for c in checks if c.status == "pass"),
        "warn": sum(1 for c in checks if c.status == "warn"),
        "fail": sum(1 for c in checks if c.status == "fail"),
    }
    status = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    return {
        "status": status,
        "counts": counts,
        "checks": [c.__dict__ for c in checks],
    }
