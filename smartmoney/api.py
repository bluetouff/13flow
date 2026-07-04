"""
Read-only JSON API over the Store, plus static proof pages and the research app.

Endpoints (all under /api):
  GET /funds                          -> fund cards (AUM series, latest quarter)
  GET /fund/<cik>?basis=&value=1      -> holdings + quarter moves + conviction (+ valuation)
  GET /consensus/holdings?date=&min_funds=
  GET /consensus/buys?date=&min_funds=
  GET /compare?ciks=a,b&date=
  GET /data-quality?threshold=&limit=
  GET /pro/v1/status                  -> Pro API key status (API key)
  GET /pro/v1/funds                   -> Pro fund series + quality summary (API key)
  GET /pro/v1/fund/<cik>?basis=       -> Pro fund detail + moves + quality (API key)
  GET /pro/v1/data-quality            -> Pro data-quality report (API key)
  GET /pro/v1/workspace/export        -> Pro workspace export JSON/CSV (API key)
  GET /pro/v1/workspace/report        -> Pro workspace readable report (API key)
  GET /pro/v1/openapi.json            -> Pro API OpenAPI document
GET /      -> static public proof home
GET /app   -> dashboard.html research app

Core endpoints work fully offline (reported, quarter-end figures). Valuation (value=1)
needs a price provider and hits the network at request time, so it's opt-in.
"""

from __future__ import annotations

import csv
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import struct
import time
from datetime import datetime, timezone
from html import escape as html_escape
from types import SimpleNamespace
from typing import Optional

import functools
import secrets
from flask import (
    Flask, Response, abort, jsonify, make_response, redirect, request, send_from_directory,
    session, url_for,
)
from werkzeug.exceptions import HTTPException

from .analytics import consensus_moves
from .registry import Fund, active_ciks
from .db import Store
from .diff import Move, diff_portfolios
from .portfolio import Portfolio
from .pro import APIKeyError, APIRateLimited, ProAPIStore, WorkspaceQuotaExceeded
from .quality import data_quality_report, quality_gate_report
from .valuation import value_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
DASHBOARD = os.path.join(APP_ROOT, "dashboard.html")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _public_active_ciks(store: Store) -> set[str]:
    """Current product universe, falling back to DB rows in tiny test DBs.

    Historical/stale CIKs may remain in SQLite for auditability. Public surfaces
    should count only the registry-backed universe when that registry intersects
    the loaded DB. Offline tests often use synthetic CIKs, so no intersection
    means "use what the test DB contains."
    """
    rows = store.conn.execute("SELECT cik FROM funds").fetchall()
    db_ciks = {r["cik"] for r in rows}
    active = active_ciks() & db_ciks
    return active if active else db_ciks


def _fund_rows(store: Store, ciks: set[str] | None = None) -> list[dict]:
    if ciks:
        values = tuple(sorted(ciks))
        rows = store.conn.execute(
            f"SELECT cik,label,manager FROM funds WHERE cik IN ({_placeholders(values)}) "
            "ORDER BY label",
            values,
        ).fetchall()
    else:
        rows = store.conn.execute("SELECT cik,label,manager FROM funds ORDER BY label").fetchall()
    return [dict(r) for r in rows]


def _latest_filings_count(store: Store, ciks: set[str] | None = None) -> int:
    if not ciks:
        return store.conn.execute("SELECT COUNT(*) c FROM latest_filings").fetchone()["c"] or 0
    values = tuple(sorted(ciks))
    return store.conn.execute(
        f"SELECT COUNT(*) c FROM latest_filings WHERE cik IN ({_placeholders(values)})",
        values,
    ).fetchone()["c"] or 0


def _latest_filings_date(store: Store, fn: str, ciks: set[str] | None = None):
    if not ciks:
        return store.conn.execute(f"SELECT {fn}(report_date) d FROM latest_filings").fetchone()["d"]
    values = tuple(sorted(ciks))
    return store.conn.execute(
        f"SELECT {fn}(report_date) d FROM latest_filings WHERE cik IN ({_placeholders(values)})",
        values,
    ).fetchone()["d"]


def _trusted_active_ciks(store: Store) -> tuple[set[str], dict]:
    active = _public_active_ciks(store)
    gate = quality_gate_report(store, active_ciks=active)
    trusted = set(gate.get("trusted_ciks") or [])
    return trusted, gate


def _git_sha() -> str:
    env_sha = os.environ.get("SMARTMONEY_GIT_SHA", "").strip()
    if env_sha:
        return env_sha

    git_dir = os.path.join(APP_ROOT, ".git")
    head_path = os.path.join(git_dir, "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = os.path.join(git_dir, *ref.split("/"))
            with open(ref_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        return head
    except OSError:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso_due(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


class _StoreConfluence:
    """Live Confluence provider: institutional side from the 13F store, insider side from
    Form 4s via EDGAR. Issuer ticker->CIK comes from SEC's company_tickers.json. Any failure
    raises a structured 503 instead of falling back to sample data."""

    def __init__(self, db_path: str, user_agent: str):
        self._db_path = db_path
        self._ua = user_agent
        self._issuer_ciks = None    # lazy {TICKER: zero-padded CIK}

    def _issuer_index(self):
        if self._issuer_ciks is None:
            import requests
            self._issuer_ciks = {}
            try:
                r = requests.get("https://www.sec.gov/files/company_tickers.json",
                                 headers={"User-Agent": self._ua}, timeout=30)
                r.raise_for_status()
                for row in r.json().values():
                    t = str(row.get("ticker", "")).upper()
                    if t:
                        self._issuer_ciks[t] = str(row.get("cik_str", "")).zfill(10)
            except Exception as e:   # pragma: no cover - network
                app_logger = __import__("logging").getLogger("smartmoney.api")
                app_logger.warning("SEC issuer index fetch failed: %s", e)
        return self._issuer_ciks

    def _institutional(self):
        from .crosssignal import InstitutionalSignal
        s = Store(self._db_path, read_only=True)
        try:
            row = s.conn.execute("SELECT MAX(report_date) d FROM filings").fetchone()
            rd = row["d"] if row else None
            if not rd:
                return {}
            trusted, _gate = _trusted_active_ciks(s)
            ciks = list(trusted)
            # Only scan Form 4s for tickers with meaningful institutional accumulation.
            # min_funds=1 means "any single fund bought" -> ~1000 tickers -> hours of Form 4
            # fetches. The conviction quadrant needs multiple funds anyway, so gate here.
            import os as _os
            _scan_min = int(_os.environ.get("SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS", "3"))
            adds = consensus_moves(s, ciks, rd, kinds=(Move.NEW, Move.ADD), min_funds=_scan_min)
            # trims still computed at min_funds=1 (cheap, DB-only, no network)
            trims = consensus_moves(s, ciks, rd, kinds=(Move.EXIT, Move.TRIM), min_funds=1)
            trim_by_ticker = {m.ticker.upper(): m.n_funds for m in trims if m.ticker}
            out = {}
            for m in adds:
                if not m.ticker:
                    continue
                t = m.ticker.upper()
                enrich = self._institutional_enrichment(s, t, tuple(m.funds), rd, tuple(m.moves))
                out[t] = InstitutionalSignal(
                    ticker=t,
                    funds_accumulating=m.n_funds,
                    funds_trimming=trim_by_ticker.get(t, 0),
                    total_value_usd=enrich["total_value_usd"],
                    fund_labels=tuple(m.funds),
                    conviction_funds=enrich["conviction_funds"],
                    avg_weight_pct=enrich["avg_weight_pct"],
                    quarters_ago=0,
                )
            return out
        finally:
            s.close()

    def _institutional_enrichment(self, store: Store, ticker: str, fund_labels: tuple[str, ...],
                                  report_date: str, moves: tuple[str, ...]) -> dict:
        if not fund_labels:
            return {"total_value_usd": 0.0, "avg_weight_pct": 0.0, "conviction_funds": 0}
        placeholders = ",".join("?" for _ in fund_labels)
        rows = store.conn.execute(
            f"""SELECT fn.label, h.value_usd, h.weight
                FROM latest_filings lf
                JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                JOIN funds fn ON fn.cik=lf.cik
                WHERE lf.report_date=? AND UPPER(h.ticker)=? AND fn.label IN ({placeholders})""",
            (report_date, ticker.upper(), *fund_labels),
        ).fetchall()
        total_value = sum((r["value_usd"] or 0.0) for r in rows)
        weights = [(r["weight"] or 0.0) for r in rows]
        avg_weight_pct = (sum(weights) / len(weights) * 100.0) if weights else 0.0
        move_by_fund = {label: (moves[i] if i < len(moves) else "")
                        for i, label in enumerate(fund_labels)}
        conviction = 0
        for r in rows:
            move = move_by_fund.get(r["label"], "")
            if move == Move.NEW.value or (r["weight"] or 0.0) >= 0.05:
                conviction += 1
        return {
            "total_value_usd": float(total_value),
            "avg_weight_pct": float(avg_weight_pct),
            "conviction_funds": int(conviction),
        }

    def confluence_metadata(self) -> dict:
        import os as _os
        scan_min = int(_os.environ.get("SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS", "3"))
        return {
            "provider": "live_store_confluence",
            "effective_universe": (
                "Form 4 scans are limited to tickers with at least "
                f"{scan_min} tracked fund(s) opening or adding in the latest 13F quarter. "
                "Trim/exits are computed across the broader tracked universe, but insider-only, "
                "distribution, and divergent categories are not exhaustive in this production path."
            ),
            "institutional_enrichment": {
                "total_value_usd": "sum of latest-quarter reported 13F value across accumulating funds",
                "avg_weight_pct": "average latest-quarter portfolio weight across accumulating funds",
                "conviction_funds": "NEW positions plus positions at or above 5% portfolio weight",
                "quarters_ago": "0 for the latest 13F quarter used by the live provider",
            },
        }

    def confluence(self, window_days: int):
        from .crosssignal import aggregate_insider_activity, build_confluence
        from .forms4 import Form4Client
        from .api_signals import ConfluenceUnavailable
        try:
            inst = self._institutional()
        except Exception as e:
            __import__("logging").getLogger("smartmoney.api").warning("inst build failed: %s", e)
            raise ConfluenceUnavailable(f"Institutional Confluence build failed: {e}") from e
        if not inst:
            raise ConfluenceUnavailable("No institutional accumulation candidates for Confluence.")
        idx = self._issuer_index()
        if not idx:
            raise ConfluenceUnavailable("SEC issuer index is unavailable; cannot map tickers to issuer CIKs.")
        f4 = Form4Client(user_agent=self._ua)
        insiders = {}
        for ticker in inst:
            cik = idx.get(ticker)
            if not cik:
                continue
            try:
                forms = f4.insider_filings(cik, window_days=window_days)
                insiders[ticker] = aggregate_insider_activity(ticker, forms, window_days=window_days)
            except Exception as e:   # pragma: no cover - network; one bad issuer won't sink it
                __import__("logging").getLogger("smartmoney.api").warning(
                    "Form 4 fetch failed for %s: %s", ticker, e)
        return build_confluence(inst, insiders)


def _enrich_confluence_cache_payload(db_path: str, payload: dict) -> dict:
    """Repair institutional fields in precomputed Confluence caches from the SQLite store.

    This is intentionally DB-only: it never touches EDGAR or price providers. It lets an
    older Form 4 cache inherit current institutional enrichment semantics without a risky
    SEC refetch.
    """
    out = dict(payload or {})
    signals = list(out.get("signals") or [])
    if not signals:
        return out

    store = Store(db_path, read_only=True)
    enriched = 0
    try:
        row = store.conn.execute("SELECT MAX(report_date) d FROM filings").fetchone()
        report_date = row["d"] if row else None
        if not report_date:
            return out

        for sig in signals:
            if not isinstance(sig, dict):
                continue
            ticker = str(sig.get("ticker") or "").upper()
            inst = dict(sig.get("institutional") or {})
            labels = [str(x) for x in (inst.get("fund_labels") or []) if str(x).strip()]
            if not ticker or not labels:
                continue

            placeholders = ",".join("?" for _ in labels)
            rows = store.conn.execute(
                f"""SELECT fn.label, fn.cik, h.value_usd, h.weight
                    FROM latest_filings lf
                    JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                    JOIN funds fn ON fn.cik=lf.cik
                    WHERE lf.report_date=? AND UPPER(h.ticker)=?
                      AND fn.label IN ({placeholders})""",
                (report_date, ticker, *labels),
            ).fetchall()
            if not rows:
                continue

            total_value = sum((r["value_usd"] or 0.0) for r in rows)
            weights = [(r["weight"] or 0.0) for r in rows]
            conviction = 0
            for r in rows:
                is_large_weight = (r["weight"] or 0.0) >= 0.05
                prev_date_row = store.conn.execute(
                    """SELECT MAX(report_date) d FROM latest_filings
                       WHERE cik=? AND report_date<?""",
                    (r["cik"], report_date),
                ).fetchone()
                prev_date = prev_date_row["d"] if prev_date_row else None
                was_held = False
                if prev_date:
                    was_held = bool(store.conn.execute(
                        """SELECT 1
                           FROM latest_filings lf
                           JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                           WHERE lf.cik=? AND lf.report_date=? AND UPPER(h.ticker)=?
                           LIMIT 1""",
                        (r["cik"], prev_date, ticker),
                    ).fetchone())
                if is_large_weight or not was_held:
                    conviction += 1

            inst.update({
                "total_value_usd": float(total_value),
                "avg_weight_pct": float(sum(weights) / len(weights) * 100.0) if weights else 0.0,
                "conviction_funds": int(conviction),
                "quarters_ago": 0,
            })
            sig["institutional"] = inst
            enriched += 1

        out["signals"] = signals
        meta = dict(out.get("metadata") or {})
        meta["cache_institutional_enrichment"] = {
            "source": "sqlite_latest_holdings",
            "report_date": report_date,
            "signals_enriched": enriched,
        }
        out["metadata"] = meta
        return out
    finally:
        store.close()


def create_app(db_path: str = "smartmoney.db", provider=None,
               dashboard_path: Optional[str] = None, secure_cookies: bool = True,
               open_mode: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024   # this API takes no large bodies
    app.config["SESSION_COOKIE_NAME"] = "13flow_admin_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_SECURE"] = bool(secure_cookies)
    dash = dashboard_path or DASHBOARD
    _truthy = lambda v: str(v or "").strip().lower() in ("1", "true", "yes", "on")
    def _env_int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(os.environ.get(name, "") or default)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, value))
    demo_mode = _truthy(os.environ.get("SMARTMONEY_DEMO"))
    # Core V1 has no browser account or payment build. The argument is kept for
    # compatibility, but the Flask app always registers the controlled-pilot
    # public/Pro surface only.
    open_mode = True
    read_only = demo_mode or _truthy(os.environ.get("SMARTMONEY_DB_READONLY"))
    pro_enabled = _truthy(os.environ.get("SMARTMONEY_PRO_API"))
    pro_db_path = os.environ.get("SMARTMONEY_PRO_DB") or os.path.join(APP_ROOT, "13flow-pro.db")
    pro_workspace_max_watchlists = _env_int("SMARTMONEY_PRO_MAX_WATCHLISTS_PER_KEY", 50, 1, 500)
    admin_session_secret = os.environ.get("SMARTMONEY_ADMIN_SESSION_SECRET", "").strip()
    if admin_session_secret:
        app.secret_key = admin_session_secret
    admin_login_failures: dict[str, list[int]] = {}
    public_payload_cache: dict[str, tuple[float, dict]] = {}

    def _cached_public_payload(name: str, factory):
        ttl = _env_int("SMARTMONEY_PUBLIC_PAYLOAD_CACHE_SECONDS", 30, 0, 300)
        now = time.time()
        if ttl > 0:
            cached = public_payload_cache.get(name)
            if cached and now - cached[0] <= ttl:
                return cached[1]
        payload = factory()
        if ttl > 0:
            public_payload_cache[name] = (now, payload)
        return payload

    # Confluence (13F × Form 4) signals endpoint. Resolution order at request time:
    #   1) a precomputed cache file in SMARTMONEY_CACHE_DIR (run.py --confluence) — instant, no network
    #   2) live provider if SMARTMONEY_CONFLUENCE_LIVE=1 + SEC_UA (fetches Form 4 from EDGAR per request)
    #   3) explicit demo provider only when SMARTMONEY_CONFLUENCE_DEMO=1
    # Without cache/live/demo the endpoint returns a 503 JSON error. No silent samples.
    from .api_signals import (
        make_signals_blueprint,
        merge_methodology_metadata,
        SampleConfluenceProvider,
        UnconfiguredConfluenceProvider,
    )
    if _truthy(os.environ.get("SMARTMONEY_CONFLUENCE_LIVE")) and os.environ.get("SEC_UA"):
        confluence_provider = _StoreConfluence(db_path, os.environ["SEC_UA"])
    elif demo_mode or _truthy(os.environ.get("SMARTMONEY_CONFLUENCE_DEMO")):
        confluence_provider = SampleConfluenceProvider()
    else:
        confluence_provider = UnconfiguredConfluenceProvider()
    _cache_dir = os.environ.get("SMARTMONEY_CACHE_DIR") or "."
    app.register_blueprint(make_signals_blueprint(
        confluence_provider,
        cache_dir=_cache_dir,
        cache_enricher=lambda payload: _enrich_confluence_cache_payload(db_path, payload),
    ))

    def store() -> Store:
        return Store(db_path, read_only=read_only)

    def clean_cik(raw: str) -> str:
        """CIKs are numeric; reject anything else before it touches a query/URL."""
        c = (raw or "").strip().lstrip("0") or "0"
        if not c.isdigit() or len(c) > 12:
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid CIK")
        return raw.strip().zfill(10)

    def clean_int(raw, default: int, lo: int, hi: int) -> int:
        """Coerce a query param to a bounded int; reject garbage with 400 instead of 500."""
        if raw is None:
            return default
        try:
            v = int(raw)
        except (TypeError, ValueError):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid integer parameter")
        return max(lo, min(hi, v))

    def clean_float(raw, default: float, lo: float, hi: float) -> float:
        if raw is None:
            return default
        try:
            v = float(raw)
        except (TypeError, ValueError):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid float parameter")
        return max(lo, min(hi, v))

    def redact_public_secret_like_text(raw) -> tuple[str, bool]:
        text = str(raw or "")[:500]
        redacted = False
        for pattern in (
            r"13flow_live_[A-Za-z0-9_\-]+",
            r"sk" + r"-[A-Za-z0-9_\-]+",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        ):
            cleaned = re.sub(pattern, "[redacted-secret-like-value]", text)
            if cleaned != text:
                redacted = True
            text = cleaned
        return text.strip(), redacted

    def clean_bool(raw, default: bool = False) -> bool:
        if raw is None:
            return default
        v = str(raw).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        from werkzeug.exceptions import BadRequest
        raise BadRequest("invalid boolean parameter")

    def clean_date(raw: str | None) -> str | None:
        if raw is None or raw == "":
            return None
        if not DATE_RE.match(raw):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid date parameter")
        return raw

    def pro_methodology(threshold: float = 100.0) -> dict:
        return {
            "source": "SEC EDGAR 13F-HR information tables",
            "unit_normalization": (
                "13F value fields are normalized to USD using filing_date: filings before "
                "2023-01-03 report values in thousands; later filings report whole dollars."
            ),
            "latest_filing_rule": (
                "For each CIK/report_date, use the latest complete-enough accession; tiny "
                "partial amendments do not replace fuller portfolio snapshots."
            ),
            "move_classification": (
                "Quarter-over-quarter moves are classified by share count, not value, with "
                "a 0.5% hold epsilon to absorb rounding noise."
            ),
            "quality_policy": (
                "Warnings are read-only review signals. They are not automatic corrections."
            ),
            "aum_jump_threshold": threshold,
        }

    def filing_row_for(s: Store, cik: str, report_date: str | None = None) -> Optional[dict]:
        args = [cik.zfill(10)]
        where = "WHERE lf.cik=?"
        if report_date:
            where += " AND lf.report_date=?"
            args.append(report_date)
        row = s.conn.execute(
            f"""SELECT f.* FROM latest_filings lf
                JOIN filings f ON f.accession=lf.accession
                {where}
                ORDER BY lf.report_date DESC LIMIT 1""",
            tuple(args),
        ).fetchone()
        return dict(row) if row else None

    def filing_payload(row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        return {
            "accession": row["accession"],
            "form": row["form"],
            "filing_date": row["filing_date"],
            "report_date": row["report_date"],
            "total_value": row["total_value"],
            "n_positions": row["n_positions"],
        }

    def position_payload(p) -> dict:
        return {
            "cusip": p.cusip,
            "ticker": p.ticker,
            "issuer": p.issuer,
            "title_of_class": p.title_of_class,
            "put_call": p.put_call,
            "shares": p.shares,
            "value_usd": p.value_usd,
            "weight": p.weight,
            "ticker_source": p.ticker_source,
            "ticker_confidence": p.ticker_confidence,
        }

    def change_payload(c) -> dict:
        return {
            "move": c.move.value,
            "cusip": c.cusip,
            "ticker": c.ticker,
            "issuer": c.issuer,
            "put_call": c.put_call,
            "prev_shares": c.prev_shares,
            "curr_shares": c.curr_shares,
            "prev_value_usd": c.prev_value,
            "curr_value_usd": c.curr_value,
            "curr_weight": c.curr_weight,
            "share_change_pct": c.share_change_pct,
        }

    def sec_accession_url(cik: str, accession: str) -> str:
        cik_i = str(int(cik)) if str(cik).strip().isdigit() else str(cik).lstrip("0")
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{cik_i}/{str(accession).replace('-', '')}/"
        )

    def public_openapi_doc() -> dict:
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "13FLOW Public API",
                "version": "v1",
                "description": (
                    "Read-only public API over SEC EDGAR-derived 13F data, live-status, "
                    "data-quality review signals, Confluence methodology, and append-only "
                    "signal history."
                ),
            },
            "servers": [{"url": "https://13flow.eu"}],
            "paths": {
                "/api/version": {"get": {"summary": "Runtime version and public state",
                                          "responses": {"200": {"description": "Version metadata"}}}},
                "/api/live-status": {"get": {"summary": "Verifiable live/demo/degraded data state",
                                             "responses": {"200": {"description": "Live status"}}}},
                "/api/product-status": {"get": {"summary": "Go-to-market readiness and proof boundary",
                                                "responses": {"200": {"description": "Product status"}}}},
                "/api/commercial-readiness": {"get": {"summary": "Commercial readiness checklist and sales boundary",
                                                       "responses": {"200": {"description": "Commercial readiness"}}}},
                "/api/security-posture": {"get": {"summary": "Controlled-pilot security posture and evidence links",
                                                   "responses": {"200": {"description": "Security posture"}}}},
                "/api/pilot-intake": {"get": {"summary": "Controlled-pilot intake checklist and operator note template",
                                               "responses": {"200": {"description": "Pilot intake pack"}}}},
                "/api/pilot-intake.md": {"get": {"summary": "Controlled-pilot intake checklist in Markdown",
                                                  "responses": {"200": {"description": "Markdown pilot intake pack"}}}},
                "/api/pilot-request-assist": {"get": {"summary": "Public no-submit pilot request assistant contract",
                                                       "responses": {"200": {"description": "Pilot request assistant"}}}},
                "/api/buyer-pack": {"get": {"summary": "Shareable buyer review pack",
                                             "responses": {"200": {"description": "Buyer review pack"}}}},
                "/api/buyer-pack.md": {"get": {"summary": "Shareable buyer review pack in Markdown",
                                                "responses": {"200": {"description": "Markdown buyer review pack"}}}},
                "/api/pro-offer": {"get": {"summary": "Pro offer packaging and onboarding runbook",
                                           "responses": {"200": {"description": "Pro offer"}}}},
                "/api/funds": {"get": {"summary": "List tracked funds with AUM series",
                                        "responses": {"200": {"description": "Fund list"}}}},
                "/api/fund/{cik}": {
                    "get": {
                        "summary": "Fund portfolio, moves and filing metadata",
                        "parameters": [{"name": "cik", "in": "path", "required": True,
                                        "schema": {"type": "string", "pattern": "^[0-9]{1,12}$"}}],
                        "responses": {"200": {"description": "Fund detail"},
                                      "404": {"description": "Fund not found"}},
                    }
                },
                "/api/stocks/{ticker}": {
                    "get": {
                        "summary": "Ticker flow intelligence with holders, moves, score and data confidence",
                        "parameters": [{"name": "ticker", "in": "path", "required": True,
                                        "schema": {"type": "string", "pattern": "^[A-Z0-9.\\-]{1,12}$"}}],
                        "responses": {"200": {"description": "Ticker flow detail"},
                                      "400": {"description": "Invalid ticker"}},
                    }
                },
                "/api/watchlist/preview": {
                    "get": {
                        "summary": "Stateless watchlist trigger preview from trusted ticker flow",
                        "parameters": [{"name": "tickers", "in": "query", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Watchlist trigger feed"},
                                      "400": {"description": "Invalid ticker list"}},
                    }
                },
                "/api/watchlist/discover": {
                    "get": {
                        "summary": "Automatic trusted 13F watchlist discovery feed",
                        "parameters": [
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 50,
                                        "default": 25}},
                            {"name": "action", "in": "query", "required": False,
                             "schema": {"type": "string", "description": "Comma-separated alert, watch, monitor, blocked"}},
                            {"name": "min_score", "in": "query", "required": False,
                             "schema": {"type": "number", "minimum": 0, "maximum": 100}},
                            {"name": "move", "in": "query", "required": False,
                             "schema": {"type": "string", "description": "Comma-separated NEW, ADD, TRIM, EXIT"}},
                            {"name": "min_holders", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                            {"name": "min_buyers", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                            {"name": "max_13f_value_usd", "in": "query", "required": False,
                             "schema": {"type": "number", "minimum": 0}},
                            {"name": "exclude_mega_cap", "in": "query", "required": False,
                             "schema": {"type": "boolean", "description": "Uses aggregated latest 13F value as a proxy, not market cap"}},
                        ],
                        "responses": {"200": {"description": "Ranked discovery watchlist"}},
                    }
                },
                "/api/consensus/holdings": {"get": {"summary": "Consensus holdings by quarter",
                                                     "responses": {"200": {"description": "Holdings"}}}},
                "/api/consensus/buys": {"get": {"summary": "Consensus buys/openings by quarter",
                                                 "responses": {"200": {"description": "Moves"}}}},
                "/api/compare": {"get": {"summary": "Compare fund holdings overlap",
                                          "responses": {"200": {"description": "Overlap matrix"}}}},
                "/api/coverage": {"get": {"summary": "Ticker-resolution coverage",
                                           "responses": {"200": {"description": "Coverage report"}}}},
                "/api/data-quality": {"get": {"summary": "Read-only data-quality warnings",
                                               "responses": {"200": {"description": "Quality report"}}}},
                "/api/signals/confluence": {"get": {"summary": "Confluence signal screen",
                                                     "responses": {"200": {"description": "Signals"},
                                                                   "503": {"description": "Unavailable"}}}},
                "/api/signals/confluence/history": {
                    "get": {"summary": "Append-only Confluence signal revisions",
                            "responses": {"200": {"description": "Signal history"}}}
                },
                "/api/methodology/confluence-v1": {
                    "get": {"summary": "Frozen Confluence v1 research contract",
                            "responses": {"200": {"description": "Methodology contract"}}}
                },
                "/api/methodology/app": {
                    "get": {"summary": "Application data methodology and proof boundary",
                            "responses": {"200": {"description": "App methodology"}}}
                },
                "/api/methodology/mcp": {
                    "get": {"summary": "MCP tool methodology, auth and fail-closed contract",
                            "responses": {"200": {"description": "MCP methodology"}}}
                },
                "/api/mcp": {"post": {"summary": "Read-only MCP JSON-RPC endpoint",
                                       "responses": {"200": {"description": "MCP response"}}}},
            },
        }

    def pro_openapi_doc() -> dict:
        security = [{"bearerAuth": []}, {"apiKeyHeader": []}]
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "13FLOW Pro API",
                "version": "v1",
                "description": "Versioned institutional API over SEC EDGAR-derived 13F datasets.",
            },
            "servers": [{"url": "https://13flow.eu"}],
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"},
                    "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-13FLOW-Key"},
                }
            },
            "paths": {
                "/api/pro/v1/status": {
                    "get": {"security": security, "summary": "Validate an API key",
                            "responses": {"200": {"description": "API key metadata"}}}
                },
                "/api/pro/v1/usage": {
                    "get": {
                        "security": security,
                        "summary": "Customer-safe usage, quota and recent request telemetry",
                        "parameters": [
                            {"name": "recent_limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                                        "default": 25}},
                            {"name": "route_limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 50,
                                        "default": 15}},
                        ],
                        "responses": {"200": {"description": "Usage and quota report"}},
                    }
                },
                "/api/pro/v1/onboarding": {
                    "get": {"security": security, "summary": "Authenticated Pro integration self-diagnostic",
                            "responses": {"200": {"description": "Pro onboarding checklist"}}}
                },
                "/api/pro/v1/funds": {
                    "get": {"security": security, "summary": "List funds with AUM series and quality flags",
                            "responses": {"200": {"description": "Fund list"}}}
                },
                "/api/pro/v1/fund/{cik}": {
                    "get": {
                        "security": security,
                        "summary": "Get a fund portfolio, filing metadata, moves, and quality flags",
                        "parameters": [
                            {"name": "cik", "in": "path", "required": True,
                             "schema": {"type": "string", "pattern": "^[0-9]{1,12}$"}},
                            {"name": "basis", "in": "query", "required": False,
                             "schema": {"type": "string", "format": "date"}},
                            {"name": "include_holds", "in": "query", "required": False,
                             "schema": {"type": "boolean", "default": True}},
                            {"name": "limit_positions", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 1000}},
                            {"name": "limit_moves", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 2000}},
                        ],
                        "responses": {"200": {"description": "Fund detail"}, "404": {"description": "Fund not found"}},
                    }
                },
                "/api/pro/v1/data-quality": {
                    "get": {
                        "security": security,
                        "summary": "Get the data-quality report",
                        "parameters": [
                            {"name": "threshold", "in": "query", "schema": {"type": "number", "default": 100}},
                            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100}},
                        ],
                        "responses": {"200": {"description": "Quality report"}},
                    }
                },
                "/api/pro/v1/watchlist": {
                    "get": {
                        "security": security,
                        "summary": "Stateless watchlist trigger feed from trusted ticker flow",
                        "parameters": [
                            {"name": "tickers", "in": "query", "required": True,
                             "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Watchlist trigger feed"}},
                    }
                },
                "/api/pro/v1/watchlist/discover": {
                    "get": {
                        "security": security,
                        "summary": "Automatic trusted 13F watchlist discovery feed",
                        "parameters": [
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                                        "default": 50}},
                            {"name": "action", "in": "query", "required": False,
                             "schema": {"type": "string", "description": "Comma-separated alert, watch, monitor, blocked"}},
                            {"name": "min_score", "in": "query", "required": False,
                             "schema": {"type": "number", "minimum": 0, "maximum": 100}},
                            {"name": "move", "in": "query", "required": False,
                             "schema": {"type": "string", "description": "Comma-separated NEW, ADD, TRIM, EXIT"}},
                            {"name": "min_holders", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                            {"name": "min_buyers", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                            {"name": "max_13f_value_usd", "in": "query", "required": False,
                             "schema": {"type": "number", "minimum": 0}},
                            {"name": "exclude_mega_cap", "in": "query", "required": False,
                             "schema": {"type": "boolean", "description": "Uses aggregated latest 13F value as a proxy, not market cap"}},
                        ],
                        "responses": {"200": {"description": "Ranked discovery watchlist"}},
                    }
                },
                "/api/pro/v1/workspace/overview": {
                    "get": {
                        "security": security,
                        "summary": "Workspace dashboard summary for the authenticated API key",
                        "responses": {"200": {"description": "Workspace overview"}},
                    },
                },
                "/api/pro/v1/workspace/export": {
                    "get": {
                        "security": security,
                        "summary": "Export bounded workspace watchlists, alerts and latest snapshots",
                        "parameters": [
                            {"name": "format", "in": "query", "required": False,
                             "schema": {"type": "string", "enum": ["json", "csv"], "default": "json"}},
                            {"name": "include_signals", "in": "query", "required": False,
                             "schema": {"type": "boolean", "default": False}},
                        ],
                        "responses": {
                            "200": {"description": "Workspace export"},
                            "400": {"description": "Invalid export format"},
                        },
                    },
                },
                "/api/pro/v1/workspace/report": {
                    "get": {
                        "security": security,
                        "summary": "Human-readable deterministic workspace report",
                        "parameters": [
                            {"name": "watchlist_id", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                        ],
                        "responses": {
                            "200": {"description": "Workspace report"},
                            "404": {"description": "Watchlist not found"},
                        },
                    },
                },
                "/api/pro/v1/workspace/activity": {
                    "get": {
                        "security": security,
                        "summary": "List bounded workspace activity events",
                        "parameters": [
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                                        "default": 50}},
                            {"name": "event_type", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                            {"name": "entity_type", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Workspace activity feed"}},
                    },
                },
                "/api/pro/v1/workspace/alerts": {
                    "get": {
                        "security": security,
                        "summary": "List saved workspace alerts",
                        "parameters": [
                            {"name": "status", "in": "query", "required": False,
                             "schema": {"type": "string", "enum": ["open", "acknowledged", "dismissed", "all"],
                                        "default": "open"}},
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                                        "default": 50}},
                            {"name": "watchlist_id", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "Workspace alert inbox"}},
                    },
                },
                "/api/pro/v1/workspace/alerts/{alert_id}": {
                    "patch": {
                        "security": security,
                        "summary": "Update a workspace alert status",
                        "parameters": [{"name": "alert_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Workspace alert updated"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists": {
                    "get": {
                        "security": security,
                        "summary": "List saved workspace watchlists",
                        "responses": {"200": {"description": "Saved watchlists"}},
                    },
                    "post": {
                        "security": security,
                        "summary": "Create a saved workspace watchlist",
                        "responses": {"201": {"description": "Saved watchlist created"},
                                      "400": {"description": "Invalid watchlist payload"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}": {
                    "get": {
                        "security": security,
                        "summary": "Get a saved workspace watchlist",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Saved watchlist"},
                                      "404": {"description": "Not found"}},
                    },
                    "put": {
                        "security": security,
                        "summary": "Replace a saved workspace watchlist",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Saved watchlist updated"},
                                      "404": {"description": "Not found"}},
                    },
                    "delete": {
                        "security": security,
                        "summary": "Delete a saved workspace watchlist",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Saved watchlist deleted"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}/delete": {
                    "post": {
                        "security": security,
                        "summary": "Delete a saved workspace watchlist using POST for strict edge proxies",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Saved watchlist deleted"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}/preview": {
                    "get": {
                        "security": security,
                        "summary": "Preview ticker-flow triggers for a saved workspace watchlist",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Saved watchlist trigger preview"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals": {
                    "get": {
                        "security": security,
                        "summary": "Apply saved filters to a saved workspace watchlist",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Filtered saved watchlist signals"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/snapshot": {
                    "post": {
                        "security": security,
                        "summary": "Persist a point-in-time saved watchlist signal snapshot",
                        "parameters": [{"name": "watchlist_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"201": {"description": "Saved watchlist signal snapshot"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/workspace/watchlists/{watchlist_id}/signals/history": {
                    "get": {
                        "security": security,
                        "summary": "List bounded saved watchlist signal snapshots",
                        "parameters": [
                            {"name": "watchlist_id", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "limit", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                                        "default": 20}},
                            {"name": "include_signals", "in": "query", "required": False,
                             "schema": {"type": "boolean", "default": False}},
                        ],
                        "responses": {"200": {"description": "Saved watchlist signal history"},
                                      "404": {"description": "Not found"}},
                    },
                },
                "/api/pro/v1/admin/health": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only Pro control-plane health and usage summary",
                        "responses": {"200": {"description": "Pro admin health"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/keys": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only Pro API key list without token material",
                        "responses": {"200": {"description": "Pro key list"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                    "post": {
                        "security": security,
                        "summary": "Create a scoped non-admin Pro API key; token is returned once",
                        "responses": {"201": {"description": "Created Pro key"},
                                      "400": {"description": "Invalid key request"},
                                      "403": {"description": "Requires admin:write"}},
                    },
                },
                "/api/pro/v1/admin/keys/{key_id}/revoke": {
                    "post": {
                        "security": security,
                        "summary": "Revoke a Pro API key while preserving audit history",
                        "parameters": [{"name": "key_id", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Revoked Pro key"},
                                      "400": {"description": "Invalid revoke request"},
                                      "403": {"description": "Requires admin:write"},
                                      "404": {"description": "Not found or already revoked"}},
                    },
                },
                "/api/pro/v1/admin/ops": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only read-only commercial ops readiness monitor",
                        "responses": {"200": {"description": "Pro ops readiness report"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/pilot-fulfillment": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only pilot key issuance checklist and CLI runbook",
                        "responses": {"200": {"description": "Pilot fulfillment runbook"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/buyer-handoff": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only customer handoff pack without token material",
                        "responses": {"200": {"description": "Buyer handoff pack"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/release-readiness": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only go/no-go pack for controlled pilot release",
                        "parameters": [
                            {"name": "key_id", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                            {"name": "days", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 30,
                                        "default": 7}},
                        ],
                        "responses": {"200": {"description": "Release readiness decision pack"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/pilot-closeout": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only bounded pilot closeout report",
                        "parameters": [
                            {"name": "key_id", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                            {"name": "days", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 30,
                                        "default": 7}},
                        ],
                        "responses": {"200": {"description": "Pilot closeout report"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/pilot-renewal": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only pilot renewal or expansion recommendation",
                        "parameters": [
                            {"name": "key_id", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                            {"name": "days", "in": "query", "required": False,
                             "schema": {"type": "integer", "minimum": 1, "maximum": 30,
                                        "default": 7}},
                        ],
                        "responses": {"200": {"description": "Pilot renewal recommendation"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/admin/pilot-request-assist": {
                    "get": {
                        "security": security,
                        "summary": "Admin-only stateless pilot request assistant template",
                        "responses": {"200": {"description": "Pilot request assistant template"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                    "post": {
                        "security": security,
                        "summary": "Transform a pilot request note into an operator checklist without storing it",
                        "responses": {"200": {"description": "Pilot request operator checklist"},
                                      "403": {"description": "Insufficient scope"}},
                    },
                },
                "/api/pro/v1/openapi.json": {
                    "get": {"summary": "OpenAPI document for the Pro API",
                            "responses": {"200": {"description": "OpenAPI JSON"}}}
                },
            },
        }

    def client_ip() -> str:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Apache is the trusted reverse proxy; use the last hop it appends,
            # not the first client-supplied value.
            return xff.split(",")[-1].strip() or "?"
        return request.remote_addr or "?"

    def bearer_token() -> str:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return request.headers.get("X-13FLOW-Key", "").strip()

    def _admin_panel_password_hash() -> str:
        return os.environ.get("SMARTMONEY_ADMIN_PANEL_PASSWORD_SHA256", "").strip().lower()

    def _admin_pbkdf2_hash() -> str:
        return os.environ.get("SMARTMONEY_ADMIN_PASSWORD_PBKDF2", "").strip()

    def _admin_auth_user() -> str:
        return os.environ.get("SMARTMONEY_ADMIN_PANEL_USER", "admin").strip() or "admin"

    def _admin_session_seconds() -> int:
        return _env_int("SMARTMONEY_ADMIN_SESSION_SECONDS", 1800, 300, 43200)

    def _admin_auth_configured() -> bool:
        return bool(admin_session_secret and (_admin_pbkdf2_hash() or _admin_panel_password_hash()))

    def _admin_verify_pbkdf2(password: str, encoded: str) -> bool:
        try:
            algo, iterations_s, salt_hex, digest_hex = str(encoded).split("$", 3)
            if algo != "pbkdf2-sha256":
                return False
            iterations = int(iterations_s)
            if iterations < 200_000:
                return False
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except (TypeError, ValueError):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)

    def _admin_verify_password(password: str) -> bool:
        pbkdf2_hash = _admin_pbkdf2_hash()
        if pbkdf2_hash:
            return _admin_verify_pbkdf2(password or "", pbkdf2_hash)
        expected_hash = _admin_panel_password_hash()
        if not expected_hash:
            return False
        return hmac.compare_digest(
            hashlib.sha256((password or "").encode("utf-8")).hexdigest(),
            expected_hash,
        )

    def _admin_totp_secret() -> str:
        return os.environ.get("SMARTMONEY_ADMIN_TOTP_SECRET", "").strip().replace(" ", "")

    def _admin_totp_required() -> bool:
        return _truthy(os.environ.get("SMARTMONEY_ADMIN_TOTP_REQUIRED"))

    def _admin_totp_code(secret: str, for_time: Optional[int] = None) -> str:
        padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
        counter = int((for_time if for_time is not None else time.time()) // 30)
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % 1_000_000).zfill(6)

    def _admin_verify_totp(code: str) -> bool:
        if not _admin_totp_required():
            return True
        secret = _admin_totp_secret()
        if not secret or not re.fullmatch(r"\d{6}", str(code or "").strip()):
            return False
        value = str(code or "").strip()
        now = int(time.time())
        try:
            return any(hmac.compare_digest(value, _admin_totp_code(secret, now + (step * 30)))
                       for step in (-1, 0, 1))
        except (binascii.Error, ValueError):
            return False

    def _admin_session_active() -> bool:
        if not _admin_auth_configured():
            return False
        user = session.get("admin_user")
        last_seen = int(session.get("admin_last_seen") or 0)
        if user != _admin_auth_user() or not last_seen:
            return False
        if int(time.time()) - last_seen > _admin_session_seconds():
            session.clear()
            return False
        session["admin_last_seen"] = int(time.time())
        session.modified = True
        return True

    def _admin_csrf_token() -> str:
        token = session.get("admin_csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["admin_csrf"] = token
        return token

    def _admin_login_required_response():
        resp = redirect(url_for("static_pro_admin_login"))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _admin_login_blocked(ip: str) -> bool:
        now = int(time.time())
        failures = [ts for ts in admin_login_failures.get(ip, []) if now - ts <= 600]
        admin_login_failures[ip] = failures
        return len(failures) >= 5

    def _admin_record_login_failure(ip: str) -> None:
        now = int(time.time())
        failures = [ts for ts in admin_login_failures.get(ip, []) if now - ts <= 600]
        failures.append(now)
        admin_login_failures[ip] = failures[-10:]

    def _admin_clear_login_failures(ip: str) -> None:
        admin_login_failures.pop(ip, None)

    def admin_panel_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _admin_auth_configured():
                abort(404)
            if not _admin_session_active():
                return _admin_login_required_response()
            return fn(*args, **kwargs)
        return wrapper

    def pro_required(scope: str):
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                ps = ProAPIStore(pro_db_path)
                key_id = None
                status = 500
                try:
                    key = ps.authenticate(bearer_token(), scope)
                    key_id = key.key_id
                    request.pro_api_key = key
                    resp = make_response(fn(*args, **kwargs))
                    status = resp.status_code
                    return resp
                except APIRateLimited as e:
                    status = e.status_code
                    resp = jsonify({"error": e.code})
                    resp.status_code = e.status_code
                    resp.headers["Retry-After"] = str(e.retry_after)
                    return resp
                except APIKeyError as e:
                    status = e.status_code
                    resp = jsonify({"error": e.code})
                    resp.status_code = e.status_code
                    if e.status_code == 401:
                        resp.headers["WWW-Authenticate"] = 'Bearer realm="13flow-pro"'
                    return resp
                finally:
                    try:
                        ps.audit(key_id, request.method, request.path, status,
                                 client_ip(), request.headers.get("User-Agent", ""))
                    finally:
                        ps.close()
            return wrapper
        return deco

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers.setdefault("Cache-Control", "no-store")
        if request.path.startswith("/api/pro/v1/"):
            resp.headers["Cache-Control"] = "private, no-store, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            resp.vary.add("Authorization")
            resp.vary.add("X-13FLOW-Key")
        # Strict default CSP for non-HTML (JSON/errors). HTML routes set their own nonce policy.
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        return resp

    @app.errorhandler(HTTPException)
    def _json_http_error(e):
        # Uniform JSON errors; never leak internals or HTML error pages.
        return jsonify({"error": (e.name or "error").lower().replace(" ", "_")}), e.code

    @app.errorhandler(Exception)
    def _json_unhandled(e):
        app.logger.exception("unhandled error")
        return jsonify({"error": "internal_error"}), 500

    # ---- fund cards -----------------------------------------------------
    @app.get("/api/funds")
    def funds():
        s = store()
        try:
            rows = _fund_rows(s, _public_active_ciks(s))
            out = []
            for r in rows:
                cik = r["cik"]
                series = s.fund_value_timeline(cik)
                latest = series[-1] if series else None
                out.append({
                    "cik": cik, "label": r["label"], "manager": r["manager"],
                    "latest_quarter": latest["report_date"] if latest else None,
                    "n_quarters": len(series),
                    "aum": latest["total_value"] if latest else None,
                    "n_positions": latest["n_positions"] if latest else None,
                    "aum_series": [{"q": x["report_date"], "aum": x["total_value"]} for x in series],
                })
            return jsonify(out)
        finally:
            s.close()

    # ---- fund detail ----------------------------------------------------
    @app.get("/api/fund/<cik>")
    def fund(cik):
        cik = clean_cik(cik)
        basis = request.args.get("basis") or None
        do_value = request.args.get("value") in ("1", "true", "yes")
        s = store()
        try:
            pf = s.load_portfolio(cik, basis)
            if pf is None:
                return jsonify({"error": "not found"}), 404
            frow = s.fund_row(cik) or {}
            prev_q = s.previous_quarter(cik, pf.report_date)
            prev = s.load_portfolio(cik, prev_q) if prev_q else Portfolio(
                cik=cik, fund_label=pf.fund_label, report_date="", form="")
            rep = diff_portfolios(prev, pf)

            positions = sorted(pf.positions.values(), key=lambda p: p.value_usd, reverse=True)
            pos_json = [{
                "cusip": p.cusip, "ticker": p.ticker, "issuer": p.issuer,
                "put_call": p.put_call, "shares": p.shares,
                "value": p.value_usd, "weight": p.weight,
            } for p in positions]

            moves = {}
            for mv in (Move.NEW, Move.EXIT, Move.ADD, Move.TRIM):
                moves[mv.value] = [{
                    "ticker": c.ticker, "issuer": c.issuer, "put_call": c.put_call,
                    "value": (c.curr_value if mv != Move.EXIT else c.prev_value),
                    "pct": c.share_change_pct,
                } for c in rep.by_move(mv)[:50]]

            conviction = {}
            for p in positions[:8]:
                if p.put_call:
                    continue
                tl = s.conviction_timeline(cik, p.cusip)
                if len(tl) > 1:
                    conviction[p.cusip] = {
                        "ticker": p.ticker, "issuer": p.issuer,
                        "series": [{"q": t["report_date"], "weight": t["weight"],
                                    "value": t["value_usd"]} for t in tl],
                    }

            payload = {
                "cik": cik, "label": pf.fund_label, "manager": frow.get("manager"),
                "report_date": pf.report_date, "prev_report_date": prev_q,
                "form": pf.form, "aum": pf.total_value, "n_positions": len(pf.positions),
                "quarters": s.quarters(cik),
                "positions": pos_json, "moves": moves, "conviction": conviction,
            }

            if do_value and provider is not None:
                vp = value_portfolio(pf, provider)
                vmap = {(p.cusip, p.put_call): p for p in vp.positions}
                for pj in pos_json:
                    vpos = vmap.get((pj["cusip"], pj["put_call"]))
                    if vpos:
                        pj["current_value"] = vpos.current_value
                        pj["current_weight"] = vpos.current_weight
                        pj["pnl_pct"] = vpos.pnl_pct
                        pj["reconcile"] = vpos.reconcile_ratio
                        pj["status"] = vpos.status
                payload["valuation"] = {
                    "basis_date": vp.basis_date, "current_total": vp.current_total,
                    "pnl_abs": vp.pnl_abs, "pnl_pct": vp.pnl_pct,
                }
            return jsonify(payload)
        finally:
            s.close()

    # ---- consensus ------------------------------------------------------
    @app.get("/api/consensus/holdings")
    def consensus_holdings():
        date = request.args.get("date")
        min_funds = clean_int(request.args.get("min_funds"), 3, 1, 50)
        s = store()
        try:
            if not date:
                # default to the most recent quarter present anywhere
                r = s.conn.execute("SELECT MAX(report_date) m FROM filings").fetchone()
                date = r["m"]
            trusted, _gate = _trusted_active_ciks(s)
            return jsonify({
                "date": date,
                "rows": s.consensus_holdings(date, min_funds, trusted),
            })
        finally:
            s.close()

    @app.get("/api/consensus/buys")
    def consensus_buys():
        date = request.args.get("date")
        min_funds = clean_int(request.args.get("min_funds"), 3, 1, 50)
        s = store()
        try:
            if not date:
                r = s.conn.execute("SELECT MAX(report_date) m FROM filings").fetchone()
                date = r["m"]
            trusted, _gate = _trusted_active_ciks(s)
            ciks = list(trusted)
            rows = consensus_moves(s, ciks, date, min_funds=min_funds)
            return jsonify({"date": date, "rows": [{
                "cusip": m.cusip, "ticker": m.ticker, "issuer": m.issuer,
                "n_funds": m.n_funds, "funds": m.funds, "moves": m.moves,
            } for m in rows]})
        finally:
            s.close()

    # ---- compare --------------------------------------------------------
    @app.get("/api/compare")
    def compare():
        raw = [c for c in (request.args.get("ciks", "").split(",")) if c][:12]  # cap fan-out
        try:
            ciks = [clean_cik(c) for c in raw]
        except Exception:
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid CIK in list")
        date = request.args.get("date") or None
        s = store()
        try:
            funds_meta, totals = [], {}
            pfs = {}
            for cik in ciks:
                pf = s.load_portfolio(cik, date)
                if pf is None:
                    continue
                pfs[cik] = pf
                funds_meta.append({"cik": cik, "label": pf.fund_label,
                                   "report_date": pf.report_date})
                for p in pf.positions.values():
                    if p.put_call:
                        continue
                    totals[p.cusip] = totals.get(p.cusip, 0.0) + p.value_usd
            top = sorted(totals, key=totals.get, reverse=True)[:40]
            rows = []
            for cusip in top:
                ref = None
                cells = {}
                for cik, pf in pfs.items():
                    pos = pf.positions.get((cusip, ""))
                    if pos:
                        ref = ref or pos
                        cells[cik] = {"weight": pos.weight, "value": pos.value_usd}
                rows.append({"cusip": cusip, "ticker": ref.ticker if ref else None,
                             "issuer": ref.issuer if ref else cusip,
                             "n_funds": len(cells), "cells": cells})
            rows.sort(key=lambda r: (r["n_funds"], len(r["cells"])), reverse=True)
            return jsonify({"funds": funds_meta, "rows": rows})
        finally:
            s.close()

    # These features are user-scoped/mutating and only exist in the full build.
    @app.get("/api/coverage")
    def coverage_ep():
        date = request.args.get("date") or None
        s = store()
        try:
            active = _public_active_ciks(s)
            return jsonify({"coverage": s.coverage(date, active),
                            "tail": s.unresolved_holdings(date, active)[:25]})
        finally:
            s.close()

    @app.get("/api/data-quality")
    def data_quality_ep():
        threshold = clean_float(request.args.get("threshold"), 100.0, 2.0, 10000.0)
        limit = clean_int(request.args.get("limit"), 100, 1, 500)
        s = store()
        try:
            report = data_quality_report(
                s, aum_jump_threshold=threshold, limit=limit,
                active_ciks=_public_active_ciks(s),
            )
            report["quality_gate"] = quality_gate_report(
                s, active_ciks=_public_active_ciks(s),
                aum_jump_threshold=threshold,
            )
            return jsonify(report)
        finally:
            s.close()

    @app.get("/api/openapi.json")
    def public_openapi_ep():
        return jsonify(public_openapi_doc())

    def _mcp_tools() -> list[dict]:
        return [
            {
                "name": "funds.list",
                "description": "List tracked funds with latest quarter and AUM.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "product.status",
                "description": "Return go-to-market readiness, offer boundary and validation proof state.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "pro.offer",
                "description": "Return Pro API packaging, limits and onboarding runbook.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "funds.get",
                "description": "Get one fund portfolio by CIK.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"cik": {"type": "string"}},
                    "required": ["cik"],
                },
            },
            {
                "name": "stocks.get",
                "description": "Get ticker flow intelligence: latest holders, quarter moves, score and data confidence.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"ticker": {"type": "string"}},
                    "required": ["ticker"],
                },
            },
            {
                "name": "watchlist.preview",
                "description": "Preview automatic watchlist triggers from trusted ticker flow for up to 25 tickers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
                    "required": ["tickers"],
                },
            },
            {
                "name": "watchlist.discover",
                "description": "Discover ranked watchlist candidates from the latest trusted 13F universe.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "action": {"type": "string", "description": "Comma-separated alert, watch, monitor, blocked"},
                        "min_score": {"type": "number", "minimum": 0, "maximum": 100},
                        "move": {"type": "string", "description": "Comma-separated NEW, ADD, TRIM, EXIT"},
                        "min_holders": {"type": "integer", "minimum": 1, "maximum": 100},
                        "min_buyers": {"type": "integer", "minimum": 1, "maximum": 100},
                        "max_13f_value_usd": {"type": "number", "minimum": 0},
                        "exclude_mega_cap": {"type": "boolean"},
                    },
                },
            },
            {
                "name": "signals.history",
                "description": "Read append-only Confluence signal revisions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "window": {"type": "integer", "minimum": 7, "maximum": 365},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            },
            {
                "name": "methodology.confluence_v1",
                "description": "Return the frozen Confluence v1 methodology contract.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "data_quality.get",
                "description": "Return read-only data-quality summary and warnings.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "threshold": {"type": "number", "default": 100},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                },
            },
        ]

    def _portfolio_ticker_position(pf: Portfolio | None, ticker: str):
        if pf is None:
            return None
        ticker = ticker.upper()
        for p in pf.positions.values():
            if p.put_call:
                continue
            if (p.ticker or "").upper() == ticker:
                return p
        return None

    def _stock_move(prev_pos, curr_pos) -> str:
        if curr_pos is not None and prev_pos is None:
            return Move.NEW.value
        if curr_pos is None and prev_pos is not None:
            return Move.EXIT.value
        if curr_pos is None or prev_pos is None:
            return "NONE"
        prev_shares = float(prev_pos.shares or 0)
        curr_shares = float(curr_pos.shares or 0)
        if prev_shares <= 0:
            return Move.NEW.value if curr_shares > 0 else Move.HOLD.value
        change = (curr_shares - prev_shares) / prev_shares
        if change > 0.005:
            return Move.ADD.value
        if change < -0.005:
            return Move.TRIM.value
        return Move.HOLD.value

    def _stock_confidence_status(holders: list[dict], movements: list[dict],
                                 quality_flags: list[dict]) -> dict:
        reasons: list[str] = []
        status = "ok"
        if not holders:
            status = "thin"
            reasons.append("No active-registry holder in the latest selected 13F quarter.")
        if quality_flags:
            status = "review"
            reasons.append("At least one involved fund has an active data-quality warning.")
        partials = [
            h for h in holders
            if str(h.get("form") or "").endswith("/A") and int(h.get("n_positions") or 0) <= 3
        ]
        if partials:
            status = "review"
            reasons.append("One or more latest holder rows come from a tiny 13F amendment.")
        if not reasons:
            reasons.append("Latest holders come from active-registry 13F rows with no ticker-level quality warning.")
        return {
            "status": status,
            "reasons": reasons,
            "quality_flag_count": len(quality_flags),
            "movement_count": len([m for m in movements if m["move"] != "NONE"]),
        }

    def _stock_score(summary: dict, confidence: dict) -> dict:
        holders = int(summary.get("holder_count") or 0)
        buyers = int(summary.get("buyers_count") or 0)
        new_positions = int(summary.get("new_positions") or 0)
        conviction = int(summary.get("conviction_funds") or 0)
        avg_weight = float(summary.get("avg_weight_pct") or 0.0)
        raw = (
            min(holders, 8) * 6
            + min(buyers, 5) * 10
            + min(new_positions, 4) * 8
            + min(conviction, 5) * 7
            + min(avg_weight, 8.0) * 2.0
        )
        penalty = 15 if confidence["status"] == "review" else (6 if confidence["status"] == "thin" else 0)
        score = max(0.0, min(100.0, raw - penalty))
        return {
            "score": round(score, 1),
            "version": "ticker_flow_v1",
            "interpretation": "ordinal research screen, not a probability or price target",
            "components": {
                "holders": holders,
                "buyers": buyers,
                "new_positions": new_positions,
                "conviction_funds": conviction,
                "avg_weight_pct": round(avg_weight, 4),
                "data_quality_penalty": penalty,
            },
        }

    def _stock_payload(ticker: str) -> dict:
        t = (ticker or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", t):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid ticker")
        s = store()
        try:
            active = _public_active_ciks(s)
            trusted, gate = _trusted_active_ciks(s)
            latest = _latest_filings_date(s, "MAX", active)
            trusted_values = tuple(sorted(trusted))
            active_sql = ""
            active_args: tuple[str, ...] = ()
            if trusted_values:
                active_sql = f" AND lf.cik IN ({_placeholders(trusted_values)})"
                active_args = trusted_values
            else:
                active_sql = " AND 1=0"
            rows = [dict(r) for r in s.conn.execute(
                f"""SELECT fn.label, lf.cik, lf.report_date, f.accession, f.filing_date,
                          f.form, f.n_positions,
                          h.cusip, h.ticker, h.issuer, h.title_of_class, h.value_usd,
                          h.shares, h.weight
                   FROM latest_filings lf
                   JOIN filings f ON f.accession=lf.accession
                   JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                   JOIN funds fn ON fn.cik=lf.cik
                   WHERE lf.report_date=? AND UPPER(h.ticker)=? {active_sql}
                   ORDER BY h.value_usd DESC""",
                (latest, t, *active_args),
            )]

            movements: list[dict] = []
            labels = {r["cik"]: r["label"] for r in _fund_rows(s, active)}
            for cik in sorted(trusted):
                curr = s.load_portfolio(cik, latest)
                prev_q = s.previous_quarter(cik, latest) if latest else None
                prev = s.load_portfolio(cik, prev_q) if prev_q else None
                curr_pos = _portfolio_ticker_position(curr, t)
                prev_pos = _portfolio_ticker_position(prev, t)
                if curr_pos is None and prev_pos is None:
                    continue
                move = _stock_move(prev_pos, curr_pos)
                filing = filing_row_for(s, cik, latest) if curr_pos is not None else None
                movements.append({
                    "cik": cik,
                    "label": labels.get(cik, cik),
                    "move": move,
                    "previous_quarter": prev_q,
                    "current_quarter": latest,
                    "prev_value_usd": prev_pos.value_usd if prev_pos is not None else 0.0,
                    "curr_value_usd": curr_pos.value_usd if curr_pos is not None else 0.0,
                    "prev_shares": prev_pos.shares if prev_pos is not None else 0.0,
                    "curr_shares": curr_pos.shares if curr_pos is not None else 0.0,
                    "curr_weight": curr_pos.weight if curr_pos is not None else 0.0,
                    "filing": filing_payload(filing),
                    "sec_filing_url": (
                        sec_accession_url(cik, filing["accession"])
                        if filing and filing.get("accession") else None
                    ),
                })
            movements.sort(
                key=lambda m: (
                    m["move"] not in (Move.NEW.value, Move.ADD.value),
                    -float(m["curr_value_usd"] or m["prev_value_usd"] or 0),
                    m["label"],
                )
            )

            quality = data_quality_report(s, limit=500, active_ciks=active)
            involved_ciks = {r["cik"] for r in rows} | {m["cik"] for m in movements}
            quality_flags: list[dict] = []
            for bucket in ("warnings", "freshness_warnings"):
                for w in quality.get(bucket, []):
                    if (w.get("fund") or {}).get("cik") in involved_ciks:
                        quality_flags.append(w)
            for w in quality.get("duplicate_label_warnings", []):
                if any(f.get("cik") in involved_ciks for f in w.get("funds", [])):
                    quality_flags.append(w)

            buyers = [m for m in movements if m["move"] in (Move.NEW.value, Move.ADD.value)]
            sellers = [m for m in movements if m["move"] in (Move.TRIM.value, Move.EXIT.value)]
            conviction_funds = [
                m for m in movements
                if m["move"] == Move.NEW.value or float(m.get("curr_weight") or 0.0) >= 0.05
            ]
            holder_weights = [float(r["weight"] or 0.0) for r in rows]
            summary = {
                "holder_count": len(rows),
                "buyers_count": len(buyers),
                "sellers_count": len(sellers),
                "new_positions": len([m for m in movements if m["move"] == Move.NEW.value]),
                "exits": len([m for m in movements if m["move"] == Move.EXIT.value]),
                "conviction_funds": len(conviction_funds),
                "avg_weight_pct": (sum(holder_weights) / len(holder_weights) * 100.0) if holder_weights else 0.0,
                "total_value_usd": sum(r["value_usd"] or 0 for r in rows),
            }
            confidence = _stock_confidence_status(rows, movements, quality_flags)
            score = _stock_score(summary, confidence)
            excluded_funds = [
                d for d in gate.get("funds", [])
                if not d.get("signal_eligible")
            ]
        finally:
            s.close()
        return {
            "ticker": t,
            "latest_13f_quarter": latest,
            "holders": rows,
            "holder_count": summary["holder_count"],
            "total_value_usd": summary["total_value_usd"],
            "movement_summary": summary,
            "movements": movements,
            "confidence": confidence,
            "score": score,
            "quality_gate": {
                "summary": gate["summary"],
                "policy": gate["policy"],
                "excluded_funds": excluded_funds[:25],
            },
            "quality_flags": quality_flags[:25],
            "sec_company_search": f"https://www.sec.gov/edgar/search/#/q={t}",
        }

    def _clean_watchlist_tickers(raw, limit: int = 25) -> list[str]:
        from werkzeug.exceptions import BadRequest
        if isinstance(raw, str):
            parts = re.split(r"[\s,;]+", raw.strip())
        elif isinstance(raw, (list, tuple)):
            parts = [str(x).strip() for x in raw]
        else:
            parts = []
        out: list[str] = []
        seen: set[str] = set()
        for part in parts:
            ticker = part.upper()
            if not ticker:
                continue
            if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", ticker):
                raise BadRequest("invalid ticker")
            if ticker in seen:
                continue
            seen.add(ticker)
            out.append(ticker)
            if len(out) > limit:
                raise BadRequest(f"watchlist is limited to {limit} tickers")
        if not out:
            raise BadRequest("tickers required")
        return out

    def _watchlist_triggers(stock: dict) -> list[dict]:
        summary = stock.get("movement_summary") or {}
        confidence = stock.get("confidence") or {}
        score = float((stock.get("score") or {}).get("score") or 0.0)
        buyers = int(summary.get("buyers_count") or 0)
        sellers = int(summary.get("sellers_count") or 0)
        new_positions = int(summary.get("new_positions") or 0)
        exits = int(summary.get("exits") or 0)
        conviction = int(summary.get("conviction_funds") or 0)
        confidence_status = confidence.get("status") or "unknown"
        triggers: list[dict] = []

        def add(code: str, severity: str, detail: str) -> None:
            triggers.append({"code": code, "severity": severity, "detail": detail})

        if confidence_status == "review":
            add("blocked_by_quality_gate", "blocked", "Ticker has active data-quality review flags.")
            return triggers
        if score >= 80:
            add("high_score", "high", f"Ticker Flow Score is {score:.1f}.")
        elif score >= 65:
            add("elevated_score", "medium", f"Ticker Flow Score is {score:.1f}.")
        if new_positions:
            add("new_position", "high" if new_positions >= 2 else "medium",
                f"{new_positions} trusted fund(s) opened a position.")
        if buyers >= 2:
            add("accumulation", "high", f"{buyers} trusted fund(s) opened or added.")
        elif buyers == 1:
            add("single_buyer", "medium", "One trusted fund opened or added.")
        if exits:
            add("exit", "medium", f"{exits} trusted fund(s) exited.")
        if sellers > buyers and sellers:
            add("distribution", "medium", f"{sellers} trusted fund(s) trimmed or exited versus {buyers} buyer(s).")
        if conviction >= 2:
            add("multi_fund_conviction", "high", f"{conviction} trusted conviction fund(s).")
        if confidence_status == "thin" and not triggers:
            add("thin_signal", "low", "No latest-quarter trusted holder; keep as monitoring-only.")
        return triggers

    def _watchlist_action(triggers: list[dict]) -> str:
        severities = {t.get("severity") for t in triggers}
        if "blocked" in severities:
            return "blocked"
        if "high" in severities:
            return "alert"
        if "medium" in severities:
            return "watch"
        return "monitor"

    def _watchlist_rank_basis() -> list[str]:
        return [
            "action severity",
            "ticker flow score",
            "buyers count",
            "new positions",
            "conviction funds",
            "holder count",
            "trusted 13F reported value",
            "ticker",
        ]

    def _watchlist_rank_key(item: dict) -> tuple:
        summary = item.get("movement_summary") or {}
        rank = {"alert": 0, "watch": 1, "monitor": 2, "blocked": 3}
        return (
            rank.get(item.get("action"), 9),
            -float((item.get("score") or {}).get("score") or 0.0),
            -int(summary.get("buyers_count") or 0),
            -int(summary.get("new_positions") or 0),
            -int(summary.get("conviction_funds") or 0),
            -int(summary.get("holder_count") or 0),
            -float(summary.get("total_value_usd") or 0.0),
            item.get("ticker") or "",
        )

    def _watchlist_payload(raw_tickers, limit: int = 25) -> dict:
        tickers = _clean_watchlist_tickers(raw_tickers, limit=limit)
        items = []
        for ticker in tickers:
            stock = _stock_payload(ticker)
            triggers = _watchlist_triggers(stock)
            action = _watchlist_action(triggers)
            summary = stock.get("movement_summary") or {}
            movement_codes = sorted({m["move"] for m in stock.get("movements", [])})
            items.append({
                "ticker": ticker,
                "action": action,
                "triggers": triggers,
                "movement_codes": movement_codes,
                "score": stock.get("score"),
                "confidence": stock.get("confidence"),
                "latest_13f_quarter": stock.get("latest_13f_quarter"),
                "movement_summary": summary,
                "quality_gate": stock.get("quality_gate"),
                "top_movements": stock.get("movements", [])[:8],
                "links": {
                    "api": f"/api/stocks/{ticker}",
                    "page": f"/stocks/{ticker}",
                    "sec_company_search": stock.get("sec_company_search"),
                },
            })
        items.sort(key=_watchlist_rank_key)
        return {
            "metadata": {
                "version": "watchlist_preview_v1",
                "input_count": len(tickers),
                "human_review_required_for_routine_publication": False,
                "source": "trusted_ticker_flow",
                "rank_basis": _watchlist_rank_basis(),
            },
            "summary": {
                "alerts": len([i for i in items if i["action"] == "alert"]),
                "watch": len([i for i in items if i["action"] == "watch"]),
                "monitor": len([i for i in items if i["action"] == "monitor"]),
                "blocked": len([i for i in items if i["action"] == "blocked"]),
            },
            "items": items,
        }

    def _discover_watchlist_tickers(candidate_limit: int) -> tuple[list[dict], str | None, dict]:
        s = store()
        try:
            trusted, gate = _trusted_active_ciks(s)
            if not trusted:
                return [], None, gate
            latest = _latest_filings_date(s, "MAX", trusted)
            if not latest:
                return [], None, gate
            values = tuple(sorted(trusted))
            rows = s.conn.execute(
                f"""
                SELECT UPPER(TRIM(h.ticker)) AS ticker,
                       MAX(h.issuer) AS issuer,
                       COUNT(DISTINCT lf.cik) AS holder_count,
                       SUM(h.value_usd) AS total_value_usd
                FROM latest_filings lf
                JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
                WHERE lf.report_date = ?
                  AND lf.cik IN ({_placeholders(values)})
                  AND h.ticker IS NOT NULL
                  AND TRIM(h.ticker) != ''
                GROUP BY UPPER(TRIM(h.ticker))
                ORDER BY holder_count DESC, total_value_usd DESC, ticker ASC
                LIMIT ?
                """,
                (latest, *values, candidate_limit),
            ).fetchall()
            candidates = []
            seen: set[str] = set()
            for row in rows:
                ticker = (row["ticker"] or "").upper()
                if ticker in seen or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", ticker):
                    continue
                seen.add(ticker)
                candidates.append({
                    "ticker": ticker,
                    "issuer": row["issuer"],
                    "holder_count": row["holder_count"],
                    "total_value_usd": row["total_value_usd"],
                })
            return candidates, latest, gate
        finally:
            s.close()

    def _split_filter_values(raw, allowed: set[str], label: str, upper: bool = False) -> set[str]:
        from werkzeug.exceptions import BadRequest
        if raw is None or raw == "":
            return set()
        if isinstance(raw, str):
            parts = re.split(r"[\s,;]+", raw.strip())
        elif isinstance(raw, (list, tuple, set)):
            parts = [str(x).strip() for x in raw]
        else:
            parts = [str(raw).strip()]
        values = {(p.upper() if upper else p.lower()) for p in parts if p}
        invalid = sorted(values - allowed)
        if invalid:
            raise BadRequest(f"invalid {label}: {', '.join(invalid)}")
        return values

    def _watchlist_discovery_filters(args) -> dict:
        actions = _split_filter_values(
            args.get("action"), {"alert", "watch", "monitor", "blocked"}, "action"
        )
        moves = _split_filter_values(
            args.get("move"), {m.value for m in Move}, "move", upper=True
        )
        min_score = (
            clean_float(args.get("min_score"), 0.0, 0.0, 100.0)
            if args.get("min_score") is not None else None
        )
        min_holders = (
            clean_int(args.get("min_holders"), 1, 1, 100)
            if args.get("min_holders") is not None else None
        )
        min_buyers = (
            clean_int(args.get("min_buyers"), 1, 1, 100)
            if args.get("min_buyers") is not None else None
        )
        exclude_mega_cap = clean_bool(args.get("exclude_mega_cap"), False)
        max_13f_value = (
            clean_float(args.get("max_13f_value_usd"), 0.0, 0.0, 10_000_000_000_000.0)
            if args.get("max_13f_value_usd") is not None else None
        )
        if exclude_mega_cap and max_13f_value is None:
            max_13f_value = 50_000_000_000.0
        return {
            "actions": actions,
            "moves": moves,
            "min_score": min_score,
            "min_holders": min_holders,
            "min_buyers": min_buyers,
            "max_13f_value_usd": max_13f_value,
            "exclude_mega_cap": exclude_mega_cap,
        }

    def _discovery_filters_payload(filters: dict) -> dict:
        return {
            "action": sorted(filters.get("actions") or []),
            "move": sorted(filters.get("moves") or []),
            "min_score": filters.get("min_score"),
            "min_holders": filters.get("min_holders"),
            "min_buyers": filters.get("min_buyers"),
            "max_13f_value_usd": filters.get("max_13f_value_usd"),
            "exclude_mega_cap": bool(filters.get("exclude_mega_cap")),
            "exclude_mega_cap_basis": (
                "aggregated latest-quarter trusted 13F reported value, not issuer market capitalization"
                if filters.get("exclude_mega_cap") else None
            ),
        }

    def _discovery_filter_active(filters: dict) -> bool:
        return bool(
            filters.get("actions") or filters.get("moves")
            or filters.get("min_score") is not None
            or filters.get("min_holders") is not None
            or filters.get("min_buyers") is not None
            or filters.get("max_13f_value_usd") is not None
            or filters.get("exclude_mega_cap")
        )

    def _discovery_item_matches_filters(item: dict, filters: dict) -> bool:
        summary = item.get("movement_summary") or {}
        discovery = item.get("discovery") or {}
        score = float((item.get("score") or {}).get("score") or 0.0)
        if filters.get("actions") and item.get("action") not in filters["actions"]:
            return False
        if filters.get("moves") and not (set(item.get("movement_codes") or []) & filters["moves"]):
            return False
        if filters.get("min_score") is not None and score < float(filters["min_score"]):
            return False
        if filters.get("min_holders") is not None and int(summary.get("holder_count") or 0) < int(filters["min_holders"]):
            return False
        if filters.get("min_buyers") is not None and int(summary.get("buyers_count") or 0) < int(filters["min_buyers"]):
            return False
        if filters.get("max_13f_value_usd") is not None:
            value = float(discovery.get("total_value_usd") or summary.get("total_value_usd") or 0.0)
            if value > float(filters["max_13f_value_usd"]):
                return False
        return True

    def _watchlist_discovery_payload(limit: int = 25, filters: dict | None = None) -> dict:
        filters = filters or _watchlist_discovery_filters({})
        safe_limit = max(1, min(100, int(limit or 25)))
        candidate_limit = 200 if _discovery_filter_active(filters) else max(safe_limit, min(200, safe_limit * 3))
        candidates, latest, gate = _discover_watchlist_tickers(candidate_limit)
        if not candidates:
            return {
                "metadata": {
                    "version": "watchlist_discovery_v1",
                    "source": "trusted_ticker_flow",
                    "selection": "latest trusted 13F holdings ranked by fund count, value and ticker flow score",
                    "human_review_required_for_routine_publication": False,
                    "latest_13f_quarter": latest,
                    "candidate_count": 0,
                    "candidate_scan_limit": candidate_limit,
                    "returned_count": 0,
                    "filtered_count": 0,
                    "filters": _discovery_filters_payload(filters),
                    "rank_basis": _watchlist_rank_basis(),
                    "quality_gate": gate.get("summary", {}),
                    "quality_gate_detail": {
                        "policy": gate.get("policy", {}),
                        "excluded_funds": [
                            d for d in gate.get("funds", [])
                            if not d.get("signal_eligible")
                        ][:25],
                    },
                },
                "summary": {"alerts": 0, "watch": 0, "monitor": 0, "blocked": 0},
                "items": [],
            }
        candidate_by_ticker = {c["ticker"]: c for c in candidates}
        tickers = tuple(candidate_by_ticker)
        trusted = tuple(sorted(set(gate.get("trusted_ciks") or [])))
        current_rows: list[dict] = []
        previous_rows: list[dict] = []
        if trusted and tickers and latest:
            s = store()
            try:
                current_rows = [dict(r) for r in s.conn.execute(
                    f"""
                    SELECT fn.label, lf.cik, lf.report_date, f.accession, f.filing_date,
                           f.form, f.total_value, f.n_positions,
                           h.cusip, UPPER(TRIM(h.ticker)) AS ticker, h.issuer,
                           h.title_of_class, h.value_usd, h.shares, h.weight
                    FROM latest_filings lf
                    JOIN filings f ON f.accession = lf.accession
                    JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
                    JOIN funds fn ON fn.cik = lf.cik
                    WHERE lf.report_date = ?
                      AND lf.cik IN ({_placeholders(trusted)})
                      AND UPPER(TRIM(h.ticker)) IN ({_placeholders(tickers)})
                    """,
                    (latest, *trusted, *tickers),
                ).fetchall()]
                previous_rows = [dict(r) for r in s.conn.execute(
                    f"""
                    WITH previous_latest AS (
                        SELECT cik, MAX(report_date) AS report_date
                        FROM latest_filings
                        WHERE report_date < ?
                          AND cik IN ({_placeholders(trusted)})
                        GROUP BY cik
                    )
                    SELECT fn.label, lf.cik, lf.report_date, f.accession, f.filing_date,
                           f.form, f.total_value, f.n_positions,
                           h.cusip, UPPER(TRIM(h.ticker)) AS ticker, h.issuer,
                           h.title_of_class, h.value_usd, h.shares, h.weight
                    FROM previous_latest pl
                    JOIN latest_filings lf ON lf.cik = pl.cik AND lf.report_date = pl.report_date
                    JOIN filings f ON f.accession = lf.accession
                    JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
                    JOIN funds fn ON fn.cik = lf.cik
                    WHERE UPPER(TRIM(h.ticker)) IN ({_placeholders(tickers)})
                    """,
                    (latest, *trusted, *tickers),
                ).fetchall()]
            finally:
                s.close()

        current_by_ticker: dict[str, list[dict]] = {}
        current_by_key: dict[tuple[str, str], dict] = {}
        previous_by_key: dict[tuple[str, str], dict] = {}
        for row in current_rows:
            ticker = row["ticker"]
            current_by_ticker.setdefault(ticker, []).append(row)
            current_by_key[(row["cik"], ticker)] = row
        for row in previous_rows:
            previous_by_key[(row["cik"], row["ticker"])] = row

        excluded_funds = [d for d in gate.get("funds", []) if not d.get("signal_eligible")]
        gate_summary = gate.get("summary", {})
        item_gate = {
            "status": gate_summary.get("status"),
            "trusted_funds": gate_summary.get("trusted_funds"),
            "signal_eligible_funds": gate_summary.get("signal_eligible_funds"),
            "excluded_funds_count": len(excluded_funds),
        }
        items = []
        for ticker, candidate in candidate_by_ticker.items():
            holders = current_by_ticker.get(ticker, [])
            movements = []
            keys = sorted(
                {key for key in current_by_key if key[1] == ticker}
                | {key for key in previous_by_key if key[1] == ticker}
            )
            for key in keys:
                curr = current_by_key.get(key)
                prev = previous_by_key.get(key)
                move = _stock_move(
                    SimpleNamespace(**prev) if prev else None,
                    SimpleNamespace(**curr) if curr else None,
                )
                if move == "NONE":
                    continue
                filing = filing_payload(curr) if curr else None
                movements.append({
                    "cik": key[0],
                    "label": (curr or prev or {}).get("label") or key[0],
                    "move": move,
                    "previous_quarter": (prev or {}).get("report_date"),
                    "current_quarter": latest,
                    "prev_value_usd": (prev or {}).get("value_usd") or 0.0,
                    "curr_value_usd": (curr or {}).get("value_usd") or 0.0,
                    "prev_shares": (prev or {}).get("shares") or 0.0,
                    "curr_shares": (curr or {}).get("shares") or 0.0,
                    "curr_weight": (curr or {}).get("weight") or 0.0,
                    "filing": filing,
                    "sec_filing_url": (
                        sec_accession_url(key[0], curr["accession"])
                        if curr and curr.get("accession") else None
                    ),
                })
            movements.sort(
                key=lambda m: (
                    m["move"] not in (Move.NEW.value, Move.ADD.value),
                    -float(m["curr_value_usd"] or m["prev_value_usd"] or 0),
                    m["label"],
                )
            )
            buyers = [m for m in movements if m["move"] in (Move.NEW.value, Move.ADD.value)]
            sellers = [m for m in movements if m["move"] in (Move.TRIM.value, Move.EXIT.value)]
            conviction_funds = [
                m for m in movements
                if m["move"] == Move.NEW.value or float(m.get("curr_weight") or 0.0) >= 0.05
            ]
            holder_weights = [float(r["weight"] or 0.0) for r in holders]
            summary = {
                "holder_count": len(holders),
                "buyers_count": len(buyers),
                "sellers_count": len(sellers),
                "new_positions": len([m for m in movements if m["move"] == Move.NEW.value]),
                "exits": len([m for m in movements if m["move"] == Move.EXIT.value]),
                "conviction_funds": len(conviction_funds),
                "avg_weight_pct": (sum(holder_weights) / len(holder_weights) * 100.0) if holder_weights else 0.0,
                "total_value_usd": sum(r["value_usd"] or 0 for r in holders),
            }
            confidence = _stock_confidence_status(holders, movements, [])
            score = _stock_score(summary, confidence)
            stock = {"movement_summary": summary, "confidence": confidence, "score": score}
            triggers = _watchlist_triggers(stock)
            action = _watchlist_action(triggers)
            movement_codes = sorted({m["move"] for m in movements})
            items.append({
                "ticker": ticker,
                "action": action,
                "triggers": triggers,
                "movement_codes": movement_codes,
                "score": score,
                "confidence": confidence,
                "latest_13f_quarter": latest,
                "movement_summary": summary,
                "quality_gate": item_gate,
                "top_movements": movements[:8],
                "links": {
                    "api": f"/api/stocks/{ticker}",
                    "page": f"/stocks/{ticker}",
                    "sec_company_search": f"https://www.sec.gov/edgar/search/#/q={ticker}",
                },
                "discovery": candidate,
            })

        if _discovery_filter_active(filters):
            items = [item for item in items if _discovery_item_matches_filters(item, filters)]
        items.sort(key=_watchlist_rank_key)
        filtered_count = len(items)
        items = items[:safe_limit]
        return {
            "metadata": {
                "version": "watchlist_discovery_v1",
                "source": "trusted_ticker_flow",
                "selection": "latest trusted 13F holdings ranked by fund count, value and ticker flow score",
                "human_review_required_for_routine_publication": False,
                "latest_13f_quarter": latest,
                "candidate_count": len(candidates),
                "candidate_scan_limit": candidate_limit,
                "returned_count": len(items),
                "filtered_count": filtered_count,
                "filters": _discovery_filters_payload(filters),
                "rank_basis": _watchlist_rank_basis(),
                "quality_gate": gate_summary,
                "quality_gate_detail": {
                    "policy": gate.get("policy", {}),
                    "excluded_funds": excluded_funds[:25],
                },
            },
            "summary": {
                "alerts": len([i for i in items if i["action"] == "alert"]),
                "watch": len([i for i in items if i["action"] == "watch"]),
                "monitor": len([i for i in items if i["action"] == "monitor"]),
                "blocked": len([i for i in items if i["action"] == "blocked"]),
            },
            "items": items,
        }

    @app.get("/api/stocks/<ticker>")
    def stock_ep(ticker):
        return jsonify(_stock_payload(ticker))

    @app.get("/api/watchlist/preview")
    def watchlist_preview_ep():
        return jsonify(_watchlist_payload(request.args.get("tickers") or "", limit=25))

    @app.get("/api/watchlist/discover")
    def watchlist_discover_ep():
        return jsonify(_watchlist_discovery_payload(
            clean_int(request.args.get("limit"), 25, 1, 50),
            _watchlist_discovery_filters(request.args),
        ))

    def _clean_watchlist_name(raw) -> str:
        from werkzeug.exceptions import BadRequest
        name = str(raw or "").strip()
        if not name:
            raise BadRequest("watchlist name required")
        if len(name) > 80:
            raise BadRequest("watchlist name is limited to 80 characters")
        return name

    def _clean_watchlist_notes(raw) -> str:
        from werkzeug.exceptions import BadRequest
        notes = str(raw or "").strip()
        if len(notes) > 1000:
            raise BadRequest("watchlist notes are limited to 1000 characters")
        return notes

    def _clean_alert_policy(raw) -> dict:
        from werkzeug.exceptions import BadRequest
        if raw in (None, ""):
            return {}
        if not isinstance(raw, dict):
            raise BadRequest("alert_policy must be an object")
        frequency = str(raw.get("frequency") or "manual").strip().lower()
        if frequency not in {"manual", "daily", "weekly"}:
            raise BadRequest("invalid alert_policy.frequency")
        return {
            "frequency": frequency,
            "enabled": bool(raw.get("enabled", False)),
        }

    def _clean_saved_watchlist_payload(raw: dict) -> dict:
        from werkzeug.exceptions import BadRequest
        if not isinstance(raw, dict):
            raise BadRequest("JSON object required")
        tickers = _clean_watchlist_tickers(raw.get("tickers") or [], limit=50)
        filters = _discovery_filters_payload(_watchlist_discovery_filters(raw.get("filters") or {}))
        return {
            "name": _clean_watchlist_name(raw.get("name")),
            "tickers": tickers,
            "filters": filters,
            "alert_policy": _clean_alert_policy(raw.get("alert_policy") or {}),
            "notes": _clean_watchlist_notes(raw.get("notes")),
        }

    def _saved_watchlist_signals_payload(item: dict) -> dict:
        filters = _watchlist_discovery_filters(item.get("filters") or {})
        base = _watchlist_payload(item.get("tickers") or [], limit=50)
        items = base["items"]
        if _discovery_filter_active(filters):
            items = [i for i in items if _discovery_item_matches_filters(i, filters)]
        items.sort(key=_watchlist_rank_key)
        filtered_count = len(items)
        return {
            "metadata": {
                "version": "saved_watchlist_signals_v1",
                "source": "saved_workspace_watchlist",
                "saved_watchlist_id": item["id"],
                "saved_watchlist_name": item["name"],
                "input_count": len(item.get("tickers") or []),
                "returned_count": len(items),
                "filtered_count": filtered_count,
                "filters": _discovery_filters_payload(filters),
                "rank_basis": _watchlist_rank_basis(),
                "human_review_required_for_routine_publication": False,
            },
            "summary": {
                "alerts": len([i for i in items if i["action"] == "alert"]),
                "watch": len([i for i in items if i["action"] == "watch"]),
                "monitor": len([i for i in items if i["action"] == "monitor"]),
                "blocked": len([i for i in items if i["action"] == "blocked"]),
            },
            "items": items,
        }

    def _signal_item_delta_basis(payload: dict) -> dict[str, dict]:
        out = {}
        for item in (payload or {}).get("items") or []:
            ticker = str(item.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            out[ticker] = {
                "action": item.get("action"),
                "score": float((item.get("score") or {}).get("score") or 0.0),
            }
        return out

    def _saved_watchlist_signal_delta(current: dict, previous_snapshot: dict | None) -> dict:
        previous_signals = (previous_snapshot or {}).get("signals") or {}
        current_by_ticker = _signal_item_delta_basis(current)
        previous_by_ticker = _signal_item_delta_basis(previous_signals)
        current_tickers = set(current_by_ticker)
        previous_tickers = set(previous_by_ticker)
        shared = sorted(current_tickers & previous_tickers)
        changed_actions = []
        changed_scores = []
        for ticker in shared:
            prev = previous_by_ticker[ticker]
            curr = current_by_ticker[ticker]
            if prev.get("action") != curr.get("action"):
                changed_actions.append({
                    "ticker": ticker,
                    "from": prev.get("action"),
                    "to": curr.get("action"),
                })
            if abs(float(curr.get("score") or 0.0) - float(prev.get("score") or 0.0)) >= 0.1:
                changed_scores.append({
                    "ticker": ticker,
                    "from": round(float(prev.get("score") or 0.0), 2),
                    "to": round(float(curr.get("score") or 0.0), 2),
                })
        return {
            "baseline_snapshot_id": (previous_snapshot or {}).get("id"),
            "previous_count": len(previous_tickers),
            "current_count": len(current_tickers),
            "added_tickers": sorted(current_tickers - previous_tickers),
            "removed_tickers": sorted(previous_tickers - current_tickers),
            "changed_actions": changed_actions,
            "changed_scores": changed_scores,
        }

    def _workspace_export_payload(ps: ProAPIStore, key_id: str, *, include_signals: bool) -> dict:
        watchlists = ps.list_watchlists(key_id)[:50]
        alerts = ps.list_workspace_alerts(key_id, status=None, limit=100)
        alerts_by_watchlist: dict[str, list[dict]] = {}
        for alert in alerts:
            alerts_by_watchlist.setdefault(alert["watchlist_id"], []).append(alert)
        exported = []
        for item in watchlists:
            latest_rows = ps.list_signal_snapshots(
                key_id, item["id"], limit=1, include_signals=include_signals,
            )
            latest = latest_rows[0] if latest_rows else None
            exported.append({
                "watchlist": item,
                "latest_snapshot": latest,
                "alerts": alerts_by_watchlist.get(item["id"], []),
            })
        return {
            "meta": {
                "api": "13flow-pro",
                "version": "v1",
                "git_sha": _git_sha(),
                "generated_at": _now_iso(),
                "workspace_scope": "api_key",
                "format": "json",
                "include_signals": include_signals,
                "limits": {
                    "watchlists": 50,
                    "alerts": 100,
                    "latest_snapshots_per_watchlist": 1,
                },
            },
            "summary": ps.workspace_summary(key_id),
            "watchlists": exported,
        }

    def _workspace_export_csv(payload: dict) -> str:
        out = io.StringIO()
        fields = [
            "watchlist_id", "watchlist_name", "tickers", "filters", "alert_frequency",
            "alert_enabled", "snapshot_id", "snapshot_created_at", "snapshot_tickers",
            "alert_id", "alert_ticker", "alert_action", "alert_status", "alert_severity",
            "alert_score", "alert_confidence", "alert_last_seen_at",
        ]
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for entry in payload.get("watchlists") or []:
            watchlist = entry.get("watchlist") or {}
            snapshot = entry.get("latest_snapshot") or {}
            policy = watchlist.get("alert_policy") or {}
            base = {
                "watchlist_id": watchlist.get("id") or "",
                "watchlist_name": watchlist.get("name") or "",
                "tickers": ",".join(watchlist.get("tickers") or []),
                "filters": json.dumps(
                    watchlist.get("filters") or {}, sort_keys=True, separators=(",", ":"),
                ),
                "alert_frequency": policy.get("frequency") or "manual",
                "alert_enabled": bool(policy.get("enabled")),
                "snapshot_id": snapshot.get("id") or "",
                "snapshot_created_at": snapshot.get("created_at") or "",
                "snapshot_tickers": ",".join(snapshot.get("tickers") or []),
            }
            alerts = entry.get("alerts") or []
            if not alerts:
                writer.writerow(base)
                continue
            for alert in alerts:
                reason = alert.get("reason") or {}
                writer.writerow({
                    **base,
                    "alert_id": alert.get("id") or "",
                    "alert_ticker": alert.get("ticker") or "",
                    "alert_action": alert.get("action") or "",
                    "alert_status": alert.get("status") or "",
                    "alert_severity": alert.get("severity") or 0,
                    "alert_score": reason.get("score") if reason.get("score") is not None else "",
                    "alert_confidence": reason.get("confidence") or "",
                    "alert_last_seen_at": alert.get("last_seen_at") or "",
                })
        return out.getvalue()

    def _alert_score_value(alert: dict) -> float:
        raw = (alert.get("reason") or {}).get("score")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return -1.0

    def _alert_status_counts(alerts: list[dict]) -> dict:
        return {
            "open": len([a for a in alerts if a.get("status") == "open"]),
            "acknowledged": len([a for a in alerts if a.get("status") == "acknowledged"]),
            "dismissed": len([a for a in alerts if a.get("status") == "dismissed"]),
            "total": len(alerts),
        }

    def _workspace_signal_digest(snapshot: dict | None, limit: int = 5) -> list[dict]:
        items = list(((snapshot or {}).get("signals") or {}).get("items") or [])
        digest = []
        for item in items[:max(1, min(10, int(limit or 5)))]:
            digest.append({
                "ticker": item.get("ticker"),
                "action": item.get("action"),
                "score": (item.get("score") or {}).get("score"),
                "movement_codes": list(item.get("movement_codes") or [])[:5],
                "trigger_codes": [
                    t.get("code") or t.get("severity")
                    for t in list(item.get("triggers") or [])[:3]
                ],
            })
        return digest

    def _snapshot_without_signals(snapshot: dict | None) -> dict | None:
        if not snapshot:
            return None
        out = dict(snapshot)
        out.pop("signals", None)
        return out

    def _workspace_report_payload(
        ps: ProAPIStore,
        key_id: str,
        *,
        watchlist_id: str | None = None,
    ) -> dict | None:
        if watchlist_id:
            item = ps.get_watchlist(key_id, watchlist_id)
            if item is None:
                return None
            watchlists = [item]
        else:
            watchlists = ps.list_watchlists(key_id)[:50]
        alerts = ps.list_workspace_alerts(
            key_id, status=None, limit=100, watchlist_id=watchlist_id,
        )
        alerts_by_watchlist: dict[str, list[dict]] = {}
        for alert in alerts:
            alerts_by_watchlist.setdefault(alert["watchlist_id"], []).append(alert)
        reports = []
        for item in watchlists:
            history = ps.list_signal_snapshots(
                key_id, item["id"], limit=2, include_signals=True,
            )
            latest = history[0] if history else None
            previous = history[1] if len(history) > 1 else None
            delta = _saved_watchlist_signal_delta((latest or {}).get("signals") or {}, previous) if latest else {
                "baseline_snapshot_id": None,
                "previous_count": 0,
                "current_count": 0,
                "added_tickers": [],
                "removed_tickers": [],
                "changed_actions": [],
                "changed_scores": [],
            }
            watch_alerts = sorted(
                alerts_by_watchlist.get(item["id"], []),
                key=lambda a: (-int(a.get("severity") or 0), -_alert_score_value(a),
                               str(a.get("last_seen_at") or ""), str(a.get("ticker") or "")),
            )
            status_counts = _alert_status_counts(watch_alerts)
            top_alert = watch_alerts[0] if watch_alerts else None
            sentences = []
            if latest:
                sentences.append(
                    f"{item['name']}: {delta['current_count']} ticker(s) in latest snapshot; "
                    f"{len(delta['added_tickers'])} added, {len(delta['removed_tickers'])} removed, "
                    f"{len(delta['changed_actions'])} action change(s)."
                )
            else:
                sentences.append(f"{item['name']}: no saved signal snapshot yet.")
            if top_alert:
                score = _alert_score_value(top_alert)
                score_label = f"{score:g}" if score >= 0 else "n/a"
                sentences.append(
                    f"Top alert: {top_alert['ticker']} is {top_alert['action']} "
                    f"with priority {top_alert['severity']} and score {score_label}."
                )
            else:
                sentences.append("No saved alert for this watchlist.")
            reports.append({
                "watchlist": item,
                "latest_snapshot": _snapshot_without_signals(latest),
                "previous_snapshot": _snapshot_without_signals(previous),
                "delta": delta,
                "alert_counts": status_counts,
                "top_alerts": watch_alerts[:5],
                "top_signals": _workspace_signal_digest(latest, limit=5),
                "summary_lines": sentences,
            })
        summary = ps.workspace_summary(key_id)
        top_open_alerts = sorted(
            [a for a in alerts if a.get("status") == "open"],
            key=lambda a: (-int(a.get("severity") or 0), -_alert_score_value(a),
                           str(a.get("last_seen_at") or ""), str(a.get("ticker") or "")),
        )[:5]
        lines = [
            f"{summary['watchlists']} watchlist(s), "
            f"{summary['alerts']['by_status'].get('open', 0)} open alert(s), "
            f"{summary['signal_snapshots']} saved snapshot(s)."
        ]
        if top_open_alerts:
            lines.append(
                "Highest-priority open alert: "
                f"{top_open_alerts[0]['ticker']} on {top_open_alerts[0]['action']}."
            )
        else:
            lines.append("No open alert in the current workspace scope.")
        return {
            "meta": {
                "api": "13flow-pro",
                "version": "v1",
                "git_sha": _git_sha(),
                "generated_at": _now_iso(),
                "workspace_scope": "api_key",
                "watchlist_id": watchlist_id,
                "deterministic": True,
                "limits": {
                    "watchlists": 50,
                    "alerts": 100,
                    "snapshots_per_watchlist": 2,
                    "top_alerts_per_watchlist": 5,
                    "top_signals_per_watchlist": 5,
                },
            },
            "executive_summary": lines,
            "summary": summary,
            "top_open_alerts": top_open_alerts,
            "watchlists": reports,
        }

    def _clean_workspace_alert_status(raw, *, allow_all: bool = False) -> str | None:
        from werkzeug.exceptions import BadRequest
        value = str(raw or "open").strip().lower()
        allowed = {"open", "acknowledged", "dismissed"}
        if allow_all and value == "all":
            return None
        if value not in allowed:
            raise BadRequest("invalid alert status")
        return value

    def _clean_workspace_activity_filter(raw) -> str | None:
        from werkzeug.exceptions import BadRequest
        value = str(raw or "").strip().lower()
        if not value:
            return None
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}", value):
            raise BadRequest("invalid activity filter")
        return value

    def _clean_workspace_id(raw) -> str:
        from werkzeug.exceptions import BadRequest
        value = str(raw or "").strip()
        if not re.fullmatch(r"[a-f0-9]{16}", value):
            raise BadRequest("invalid workspace id")
        return value

    def _pro_workspace_limits_payload() -> dict:
        return {
            "max_watchlists_per_key": pro_workspace_max_watchlists,
            "max_tickers_per_watchlist": 50,
            "max_snapshots_per_watchlist": 100,
            "max_history_returned": 100,
            "max_alerts_returned": 100,
            "max_activity_returned": 100,
            "max_request_bytes": app.config.get("MAX_CONTENT_LENGTH"),
        }

    def _pro_store_call(fn):
        ps = ProAPIStore(pro_db_path)
        try:
            return fn(ps)
        finally:
            ps.close()

    def _pro_admin_ops_payload(live: dict, health: dict, automation: dict) -> dict:
        quality = live.get("quality_summary") or {}
        counts = live.get("counts") or {}
        keys = health.get("keys") or {}
        audit = health.get("audit") or {}
        critical: list[str] = []
        warnings: list[str] = []
        notices: list[str] = []
        actions: list[str] = []
        quality_gate_status = quality.get("quality_gate_status")
        trusted_funds = int(quality.get("trusted_funds") or 0)
        signal_eligible_funds = int(quality.get("signal_eligible_funds") or 0)
        degraded_funds = int(quality.get("degraded_funds") or 0)
        quarantined_funds = int(quality.get("quarantined_funds") or 0)

        if live.get("public_state") != "LIVE":
            critical.append("public_state is not LIVE")
        if live.get("uses_synthetic_data"):
            critical.append("synthetic data is enabled")
        if int(counts.get("funds") or 0) <= 0:
            critical.append("no public fund coverage")
        if trusted_funds <= 0:
            critical.append("no trusted funds available for signals")
        if int(keys.get("active") or 0) <= 0:
            critical.append("no active Pro API key")

        stale_only_fail_closed = (
            quality_gate_status == "gated"
            and trusted_funds > 0
            and signal_eligible_funds > 0
            and degraded_funds == 0
            and quarantined_funds == 0
        )
        if quality_gate_status not in {None, "ok"}:
            if stale_only_fail_closed:
                notices.append("quality gate is gated because stale funds are excluded fail-closed")
            else:
                warnings.append(f"quality gate status is {quality_gate_status}")
        if degraded_funds > 0:
            warnings.append("one or more funds are degraded by the quality gate")
        if quarantined_funds > 0:
            warnings.append("one or more funds are quarantined by the quality gate")
        if int(audit.get("server_errors") or 0) > 0:
            warnings.append("Pro API audit contains server errors")
        if int(audit.get("rate_limited") or 0) > 0:
            warnings.append("Pro API audit contains rate-limited requests")
        if int(keys.get("rotation_due") or 0) > 0:
            warnings.append("one or more active Pro keys are due for rotation")
        if int(automation.get("invalid_policy") or 0) > 0:
            warnings.append("one or more scheduled watchlists have invalid alert policy")

        if critical:
            status = "critical"
        elif warnings or (health.get("status") == "warn"):
            status = "warn"
        else:
            status = "ok"

        if status == "critical":
            actions.append("Hold commercial onboarding until critical ops checks are cleared.")
        if warnings:
            actions.append("Review warning reasons before promising production onboarding windows.")
        if int(keys.get("rotation_due") or 0) > 0:
            actions.append("Rotate due Pro API keys and confirm replacement tokens with customers.")
        if int(audit.get("server_errors") or 0) > 0:
            actions.append("Inspect recent Pro 5xx routes before expanding customer traffic.")
        if quality_gate_status not in {None, "ok"}:
            actions.append("Check /api/data-quality and keep quality disclosures visible.")
        actions.extend([
            "Run deploy/smoke-public.sh after each public deploy.",
            "Run deploy/smoke-pro-workspace.sh with a workspace-capable Pro key after each Pro deploy.",
            "Verify systemd timers and encrypted Pro DB backup restore outside the web worker.",
        ])

        return {
            "status": status,
            "generated_at": _now_iso(),
            "verdict": {
                "status": status,
                "critical": critical,
                "warnings": warnings,
                "notices": notices,
                "operator_actions": actions,
            },
            "public_data": {
                "public_state": live.get("public_state"),
                "uses_synthetic_data": live.get("uses_synthetic_data"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "data_as_of": live.get("data_as_of"),
                "counts": counts,
                "quality_summary": quality,
            },
            "pro_control_plane": health,
            "workspace_automation": automation,
            "service_contracts": {
                "git_sha": _git_sha(),
                "expected_public_state": "LIVE",
                "read_only_web_worker_shell_checks": False,
                "admin_scope": "admin:read",
                "public_smoke": "deploy/smoke-public.sh",
                "pro_workspace_smoke": "deploy/smoke-pro-workspace.sh",
            },
            "backup": {
                "encrypted_backup_expected": True,
                "restore_verify_by_web_process": False,
                "operator_verify_command": "deploy/verify-pro-db-backup.sh",
                "reason": "backup files, private keys and systemd timer state stay outside the Flask worker",
            },
            "privacy": {
                "tokens_exposed": False,
                "key_hashes_exposed": False,
                "audit_ips_exposed": False,
                "audit_user_agents_exposed": False,
                "payloads_logged": False,
            },
        }

    def _pro_admin_pilot_fulfillment_payload(live: dict, health: dict, ops: dict) -> dict:
        intake = pilot_intake_payload()
        security = security_posture_payload()
        offer = pro_offer_payload()
        defaults = offer.get("default_limits") or {}
        workspace_limits = _pro_workspace_limits_payload()
        verdict = ops.get("verdict") or {}
        critical = list(verdict.get("critical") or [])
        security_ready = security.get("status") == "controlled_pilot_security_ready"
        ready = not critical and security_ready and intake.get("status") == "operator_review_required"
        customer_scopes = ["funds:read", "quality:read", "workspace:write"]
        return {
            "status": "ready_to_issue_bounded_pilot" if ready else "hold_key_issuance",
            "generated_at": _now_iso(),
            "read_only": True,
            "web_worker_creates_tokens": False,
            "tokens_exposed": False,
            "secrets_exposed": False,
            "scope": "admin:read",
            "decision_inputs": {
                "ops_status": ops.get("status"),
                "critical": critical,
                "warnings": verdict.get("warnings") or [],
                "security_status": security.get("status"),
                "public_state": live.get("public_state"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "active_keys": (health.get("keys") or {}).get("active"),
            },
            "intake_boundary": {
                "public_form_submission": intake.get("public_form_submission"),
                "server_side_pii_storage": (intake.get("privacy") or {}).get("server_side_pii_storage"),
                "token_collection": (intake.get("privacy") or {}).get("token_collection"),
                "required_fields": [f.get("id") for f in (intake.get("required_fields") or [])],
                "operator_note_template": intake.get("operator_note_template") or [],
            },
            "operator_events": (health.get("operator_events") or {}),
            "least_privilege_policy": {
                "customer_allowed_scopes": customer_scopes,
                "customer_forbidden_scopes": ["admin:read"],
                "default_customer_scopes": customer_scopes,
                "admin_key_policy": "admin:read is operator-only and must never be issued to customers",
            },
            "default_limits": {
                "rate_per_min": int(defaults.get("rate_per_min") or 120),
                "rate_per_day": int(defaults.get("rate_per_day") or 10000),
                "max_watchlists_per_key": workspace_limits["max_watchlists_per_key"],
                "max_tickers_per_watchlist": workspace_limits["max_tickers_per_watchlist"],
                "max_request_bytes": workspace_limits["max_request_bytes"],
                "expires_days": 30,
                "rotation_days": 21,
            },
            "operator_commands": {
                "create_bounded_pilot_key": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db "
                    "--create-api-key \"<org> pilot\" "
                    "--api-key-scopes funds:read,quality:read,workspace:write "
                    "--api-key-rate-per-min 120 --api-key-rate-per-day 10000 "
                    "--api-key-expires-days 30 --api-key-rotation-days 21"
                ),
                "list_keys_after_issue": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db --list-api-keys"
                ),
                "verify_issued_key_status": (
                    "curl -fsS https://13flow.eu/api/pro/v1/status "
                    "-H \"Authorization: Bearer <issued_token>\""
                ),
                "verify_usage_audit": (
                    "curl -fsS \"https://13flow.eu/api/pro/v1/usage?recent_limit=5&route_limit=5\" "
                    "-H \"Authorization: Bearer <issued_token>\""
                ),
                "list_operator_events": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db --list-operator-events --operator-events-limit 20"
                ),
                "run_public_smoke": "sudo EXPECTED_SHA=$SHA /opt/13flow/deploy/smoke-public.sh",
                "run_pro_workspace_smoke": (
                    "sudo EXPECTED_SHA=$SHA PRO_TOKEN=\"<workspace_capable_token>\" "
                    "/opt/13flow/deploy/smoke-pro-workspace.sh"
                ),
                "run_key_lifecycle_smoke": (
                    "sudo EXPECTED_SHA=$SHA /opt/13flow/deploy/smoke-pro-key-lifecycle.sh"
                ),
                "revoke_if_needed": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db --revoke-api-key <key_id>"
                ),
            },
            "checklist": {
                "before_issue": [
                    "Archive the completed pilot intake operator note outside the public site.",
                    "Run public smoke and Pro workspace smoke on the deployed SHA.",
                    "Run Pro key lifecycle smoke before issuing the first real buyer key.",
                    "Confirm /api/security-posture is controlled_pilot_security_ready.",
                    "Confirm requested scopes are least-privilege and do not include admin:read.",
                    "Set expiry and rotation_due_at before token delivery.",
                ],
                "issue": [
                    "Run the create_bounded_pilot_key command as flowpro.",
                    "Copy the token once into the selected secure delivery channel.",
                    "Record key id, label, scopes, expiry and rotation_due_at in the operator note.",
                    "Run list_operator_events and confirm api_key.created was recorded without token material.",
                ],
                "after_issue": [
                    "Ask the buyer to call /api/pro/v1/status and confirm the key id.",
                    "Confirm first successful call appears in usage/audit without exposing token, IP or user-agent in customer payloads.",
                    "Schedule rotation follow-up before rotation_due_at.",
                ],
                "hold_or_decline": [
                    "Ops status is critical.",
                    "Security posture is not controlled_pilot_security_ready.",
                    "Buyer requests admin:read or broad redistribution without a custom agreement.",
                    "Buyer will not acknowledge research-screen and no-investment-advice boundaries.",
                ],
            },
            "evidence_links": [
                {"label": "Pilot intake", "href": "/pilot"},
                {"label": "Pilot intake JSON", "href": "/api/pilot-intake"},
                {"label": "Security posture", "href": "/api/security-posture"},
                {"label": "Admin ops", "href": "/api/pro/v1/admin/ops"},
                {"label": "Pro status", "href": "/api/pro/v1/status"},
                {"label": "Pro usage", "href": "/api/pro/v1/usage"},
            ],
        }

    def _pro_admin_buyer_handoff_payload(live: dict, health: dict, ops: dict) -> dict:
        fulfillment = _pro_admin_pilot_fulfillment_payload(live, health, ops)
        limits = fulfillment.get("default_limits") or {}
        scopes = list((fulfillment.get("least_privilege_policy") or {}).get("default_customer_scopes") or [])
        return {
            "status": "ready" if fulfillment.get("status") == "ready_to_issue_bounded_pilot" else "hold",
            "generated_at": _now_iso(),
            "read_only": True,
            "scope": "admin:read",
            "tokens_included": False,
            "secrets_included": False,
            "token_delivery": {
                "web_worker_delivers_token": False,
                "operator_delivery_required": True,
                "allowed_channels": ["customer-approved secret manager", "encrypted one-time channel"],
                "forbidden_channels": ["URL query strings", "browser localStorage", "email thread archives", "admin web payloads"],
            },
            "customer_pack": {
                "title": "13FLOW Pro controlled pilot handoff",
                "audience": ["research desk", "family office", "asset manager", "data team", "agent workflow"],
                "positioning": "SEC EDGAR-derived research screen with quality-gated 13F signals and saved watchlists.",
                "not_investment_advice": True,
                "not_claimed": ["validated alpha", "complete shorts", "real-time holdings", "brokerage execution"],
                "evidence_links": [
                    "/api/version",
                    "/api/live-status",
                    "/api/data-quality",
                    "/api/pro/v1/openapi.json",
                    "/pro/onboarding",
                    "/legal/pro-api",
                ],
            },
            "issued_key_summary_template": {
                "key_id": "<issued_key_id>",
                "label": "<org> pilot",
                "tier": "pro",
                "scopes": scopes,
                "expires_at": "<expires_at>",
                "rotation_due_at": "<rotation_due_at>",
                "rate_per_min": int(limits.get("rate_per_min") or 120),
                "rate_per_day": int(limits.get("rate_per_day") or 10000),
                "max_watchlists_per_key": int(limits.get("max_watchlists_per_key") or 50),
                "max_tickers_per_watchlist": int(limits.get("max_tickers_per_watchlist") or 50),
                "max_request_bytes": int(limits.get("max_request_bytes") or 262144),
            },
            "customer_commands": {
                "status": "curl -fsS https://13flow.eu/api/pro/v1/status -H 'Authorization: Bearer $PRO_TOKEN'",
                "onboarding": "curl -fsS https://13flow.eu/api/pro/v1/onboarding -H 'Authorization: Bearer $PRO_TOKEN'",
                "usage": "curl -fsS 'https://13flow.eu/api/pro/v1/usage?recent_limit=5&route_limit=5' -H 'Authorization: Bearer $PRO_TOKEN'",
                "workspace_overview": "curl -fsS https://13flow.eu/api/pro/v1/workspace/overview -H 'Authorization: Bearer $PRO_TOKEN'",
            },
            "operator_checklist": [
                "Run public, Pro workspace and Pro key lifecycle smokes on the deployed SHA.",
                "Issue the key with least-privilege customer scopes only.",
                "Copy the token once into the customer-approved secure channel.",
                "Send only this handoff pack plus key id, expiry and rotation metadata through normal channels.",
                "Confirm the customer ran /status and /onboarding without sharing the token back.",
                "Record the operator event ids and schedule rotation before rotation_due_at.",
            ],
            "privacy": {
                "tokens_echoed": False,
                "token_hashes_exposed": False,
                "audit_ips_exposed": False,
                "audit_user_agents_exposed": False,
                "payloads_logged": False,
            },
            "production_state": {
                "public_state": live.get("public_state"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "ops_status": ops.get("status"),
                "pilot_fulfillment_status": fulfillment.get("status"),
                "operator_events": health.get("operator_events") or {},
            },
        }

    def _pro_admin_pilot_renewal_payload(closeout: dict, fulfillment: dict) -> dict:
        limits = fulfillment.get("default_limits") or {}
        scopes = list((fulfillment.get("least_privilege_policy") or {}).get("default_customer_scopes") or [])
        verdict = closeout.get("verdict") or {}
        summary = closeout.get("summary") or {}
        verdict_status = verdict.get("status")
        if verdict_status == "hold":
            decision = "pause"
            status = "hold"
            rationale = "Pilot should not be expanded until usage, lifecycle or error issues are resolved."
            rate_per_min = int(limits.get("rate_per_min") or 120)
            rate_per_day = int(limits.get("rate_per_day") or 10000)
            renewal_days = 0
            rotation_days = 0
        elif verdict_status == "expand_candidate":
            decision = "expand"
            status = "ready"
            rationale = "Pilot shows enough repeated product usage to justify a bounded expansion discussion."
            rate_per_min = min(240, max(120, int(limits.get("rate_per_min") or 120) * 2))
            rate_per_day = min(20000, max(10000, int(limits.get("rate_per_day") or 10000) * 2))
            renewal_days = 30
            rotation_days = 21
        else:
            decision = "renew"
            status = "ready"
            rationale = "Pilot is healthy enough to renew at the existing controlled-pilot boundary."
            rate_per_min = int(limits.get("rate_per_min") or 120)
            rate_per_day = int(limits.get("rate_per_day") or 10000)
            renewal_days = 30
            rotation_days = 21
        recommended_terms = {
            "decision": decision,
            "scopes": scopes,
            "rate_per_min": rate_per_min,
            "rate_per_day": rate_per_day,
            "expires_days": renewal_days,
            "rotation_days": rotation_days,
            "max_watchlists_per_key": int(limits.get("max_watchlists_per_key") or 50),
            "max_tickers_per_watchlist": int(limits.get("max_tickers_per_watchlist") or 50),
            "max_request_bytes": int(limits.get("max_request_bytes") or 262144),
        }
        subject = {
            "expand": "13FLOW Pro pilot expansion recommendation",
            "renew": "13FLOW Pro pilot renewal recommendation",
            "pause": "13FLOW Pro pilot pause recommendation",
        }[decision]
        body_lines = [
            f"Recommendation: {decision}.",
            rationale,
            (
                "Observed pilot window: "
                f"{int(summary.get('requests') or 0)} requests, "
                f"{int(summary.get('watchlists') or 0)} watchlists, "
                f"{int(summary.get('snapshots') or 0)} snapshots, "
                f"{int(summary.get('alerts') or 0)} alerts."
            ),
            (
                "Recommended Pro API terms: "
                f"scopes {', '.join(scopes)}, "
                f"{rate_per_min}/min, {rate_per_day}/day, "
                f"expiry {renewal_days} days, rotation {rotation_days} days."
            ),
            "13FLOW remains a research screen based on public filings and is not investment advice.",
        ]
        return {
            "status": status,
            "generated_at": _now_iso(),
            "read_only": True,
            "scope": "admin:read",
            "tokens_included": False,
            "secrets_included": False,
            "decision": decision,
            "rationale": rationale,
            "source_verdict": verdict,
            "source_summary": summary,
            "recommended_terms": recommended_terms,
            "customer_message": {
                "subject": subject,
                "body_lines": body_lines,
                "requires_operator_review": True,
                "token_included": False,
            },
            "operator_commands": {
                "create_recommended_key": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db "
                    "--create-api-key \"<org> renewal\" "
                    f"--api-key-scopes {','.join(scopes)} "
                    f"--api-key-rate-per-min {rate_per_min} --api-key-rate-per-day {rate_per_day} "
                    f"--api-key-expires-days {renewal_days} --api-key-rotation-days {rotation_days}"
                ) if decision != "pause" else "Do not issue a renewal key until the hold reasons are resolved.",
                "list_operator_events": (
                    "sudo -u flowpro /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db --list-operator-events --operator-events-limit 20"
                ),
                "run_key_lifecycle_smoke": "sudo EXPECTED_SHA=$SHA /opt/13flow/deploy/smoke-pro-key-lifecycle.sh",
            },
            "commercial_boundary": {
                "not_investment_advice": True,
                "not_claimed": ["validated alpha", "investment recommendation", "performance guarantee"],
                "operator_review_required": True,
            },
            "privacy": {
                "tokens_echoed": False,
                "token_hashes_exposed": False,
                "audit_ips_exposed": False,
                "audit_user_agents_exposed": False,
                "payloads_logged": False,
            },
        }

    def _pro_admin_release_readiness_payload(
        ops: dict,
        fulfillment: dict,
        handoff: dict,
        closeout: dict,
        renewal: dict,
    ) -> dict:
        ops_verdict = ops.get("verdict") or {}
        closeout_verdict = closeout.get("verdict") or {}
        renewal_terms = renewal.get("recommended_terms") or {}
        blockers: list[str] = []
        notices: list[str] = []

        blockers.extend(list(ops_verdict.get("critical") or []))
        if fulfillment.get("status") != "ready_to_issue_bounded_pilot":
            blockers.append("pilot key issuance is not ready")
        if handoff.get("status") != "ready":
            blockers.append("buyer handoff is not ready")
        if not (handoff.get("token_delivery") or {}).get("operator_delivery_required"):
            blockers.append("token delivery boundary is not operator-controlled")

        if ops_verdict.get("warnings"):
            notices.extend(list(ops_verdict.get("warnings") or []))
        if ops_verdict.get("notices"):
            notices.extend(list(ops_verdict.get("notices") or []))
        if closeout_verdict.get("status") == "hold":
            notices.append("pilot closeout context is hold; use this as commercial context, not an automated stop")
        if renewal.get("decision") == "pause":
            notices.append("renewal recommendation is pause for the selected pilot context")

        status = "hold" if blockers else "ready_for_controlled_pilot"
        before_issue = (fulfillment.get("checklist") or {}).get("before_issue") or []
        handoff_checklist = handoff.get("operator_checklist") or []
        commands = fulfillment.get("operator_commands") or {}
        return {
            "status": status,
            "generated_at": _now_iso(),
            "read_only": True,
            "scope": "admin:read",
            "decision": {
                "go": not blockers,
                "blockers": blockers,
                "notices": notices,
                "can_issue_pilot_key": not blockers,
                "can_send_buyer_handoff": not blockers,
                "renewal_decision": renewal.get("decision"),
            },
            "source_statuses": {
                "ops": ops.get("status"),
                "pilot_fulfillment": fulfillment.get("status"),
                "buyer_handoff": handoff.get("status"),
                "pilot_closeout": closeout_verdict.get("status"),
                "pilot_renewal": renewal.get("status"),
            },
            "release_boundary": {
                "controlled_pilot_only": True,
                "browser_auth_self_serve": False,
                "self_serve_payment": False,
                "web_worker_creates_tokens": False,
                "operator_issued_keys": True,
                "operator_delivery_required": True,
                "customer_forbidden_scopes": ["admin:read"],
                "not_investment_advice": True,
            },
            "required_smokes": {
                "public": commands.get("run_public_smoke"),
                "pro_workspace": commands.get("run_pro_workspace_smoke"),
                "pro_key_lifecycle": commands.get("run_key_lifecycle_smoke"),
            },
            "minimum_operator_checklist": list(dict.fromkeys(
                before_issue[:6] + handoff_checklist[:4] + [
                    "Record the final go/no-go decision outside the web worker.",
                    "Do not issue admin:read to customers.",
                ]
            )),
            "commercial_context": {
                "closeout_summary": closeout.get("summary") or {},
                "renewal_terms": renewal_terms,
                "customer_message_token_included": (
                    (renewal.get("customer_message") or {}).get("token_included")
                ),
                "not_claimed": ["validated alpha", "investment recommendation", "performance guarantee"],
            },
            "privacy": {
                "tokens_included": False,
                "secrets_included": False,
                "tokens_echoed": False,
                "token_hashes_exposed": False,
                "audit_ips_exposed": False,
                "audit_user_agents_exposed": False,
                "payloads_logged": False,
            },
            "operator_next_actions": (
                ["Hold pilot release until blockers are cleared."]
                if blockers else [
                    "Run the required smokes on the deployed SHA.",
                    "Issue a least-privilege, expiring pilot key only through the operator CLI.",
                    "Send the buyer handoff without token material in normal channels.",
                    "Schedule rotation before rotation_due_at.",
                ]
            ),
        }

    def _mcp_call_tool(name: str, args: dict) -> dict:
        if name == "product.status":
            return product_status_payload()
        if name == "pro.offer":
            return pro_offer_payload()
        if name == "funds.list":
            s = store()
            try:
                rows = _fund_rows(s, _public_active_ciks(s))
                return {"funds": rows}
            finally:
                s.close()
        if name == "funds.get":
            cik = clean_cik(args.get("cik"))
            s = store()
            try:
                pf = s.load_portfolio(cik)
                if pf is None:
                    return {"error": "not_found"}
                frow = filing_row_for(s, cik, pf.report_date)
                return {
                    "fund": {"cik": cik, "label": pf.fund_label},
                    "filing": filing_payload(frow),
                    "positions": [position_payload(p) for p in pf.positions.values()],
                }
            finally:
                s.close()
        if name == "stocks.get":
            return _stock_payload(str(args.get("ticker") or ""))
        if name == "watchlist.preview":
            return _watchlist_payload(args.get("tickers") or [], limit=25)
        if name == "watchlist.discover":
            return _watchlist_discovery_payload(
                clean_int(args.get("limit"), 25, 1, 50),
                _watchlist_discovery_filters(args),
            )
        if name == "signals.history":
            from .research import HISTORY_FILENAME, read_signal_history
            rows = read_signal_history(
                os.path.join(_cache_dir, HISTORY_FILENAME),
                limit=int(args.get("limit") or 100),
                ticker=args.get("ticker"),
                window_days=args.get("window"),
            )
            return {"history": rows, "count": len(rows)}
        if name == "methodology.confluence_v1":
            from .research import confluence_v1_spec
            return confluence_v1_spec(_git_sha())
        if name == "data_quality.get":
            threshold = float(args.get("threshold") or 100.0)
            limit = int(args.get("limit") or 100)
            s = store()
            try:
                active = _public_active_ciks(s)
                report = data_quality_report(
                    s, aum_jump_threshold=threshold, limit=limit,
                    active_ciks=active,
                )
                report["quality_gate"] = quality_gate_report(
                    s, active_ciks=active, aum_jump_threshold=threshold,
                )
                return report
            finally:
                s.close()
        return {"error": "unknown_tool"}

    @app.post("/api/mcp")
    def mcp_ep():
        req = request.get_json(silent=True) or {}
        mid = req.get("id")
        method = req.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "13flow-readonly", "version": _git_sha()},
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {"tools": _mcp_tools()}
            elif method == "tools/call":
                params = req.get("params") or {}
                payload = _mcp_call_tool(params.get("name", ""), params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": jsonify(payload).get_data(as_text=True)}],
                    "structuredContent": payload,
                    "isError": bool(isinstance(payload, dict) and payload.get("error")),
                }
            else:
                return jsonify({"jsonrpc": "2.0", "id": mid,
                                "error": {"code": -32601, "message": "method not found"}})
            return jsonify({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:  # noqa: BLE001
            return jsonify({"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32603, "message": str(e)}}), 500

    if pro_enabled:
        @app.get("/api/pro/v1/status")
        @pro_required("funds:read")
        def pro_status_ep():
            key = request.pro_api_key
            return jsonify({
                "api": "13flow-pro",
                "version": "v1",
                "key": {
                    "id": key.key_id,
                    "label": key.label,
                    "tier": key.tier,
                    "scopes": list(key.scopes),
                    "rate_per_min": key.rate_per_min,
                    "rate_per_day": key.rate_per_day,
                    "created_at": key.created_at,
                    "expires_at": key.expires_at,
                    "rotation_due_at": key.rotation_due_at,
                },
                "key_lifecycle": {
                    "expires_at": key.expires_at,
                    "rotation_due_at": key.rotation_due_at,
                    "rotation_required": _iso_due(key.rotation_due_at),
                    "rotation_policy": "default reminder is 90 days from issue unless the operator overrides it",
                },
                "workspace_limits": _pro_workspace_limits_payload(),
            })

        @app.get("/api/pro/v1/usage")
        @pro_required("funds:read")
        def pro_usage_ep():
            key = request.pro_api_key
            recent_limit = clean_int(request.args.get("recent_limit"), 25, 1, 100)
            route_limit = clean_int(request.args.get("route_limit"), 15, 1, 50)
            with ProAPIStore(pro_db_path) as ps:
                report = ps.usage_report(
                    key.key_id,
                    recent_limit=recent_limit,
                    route_limit=route_limit,
                )
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "request": {
                        "recent_limit": recent_limit,
                        "route_limit": route_limit,
                    },
                },
                "usage": report,
            })

        @app.get("/api/pro/v1/onboarding")
        @pro_required("funds:read")
        def pro_onboarding_ep():
            key = request.pro_api_key
            scopes = set(key.scopes)
            workspace_enabled = "workspace:write" in scopes
            quality_enabled = "quality:read" in scopes
            base_url = request.url_root.rstrip("/") + "/api/pro/v1"
            endpoint_checks = [
                {
                    "id": "status",
                    "method": "GET",
                    "path": "/status",
                    "available": True,
                    "required_scope": "funds:read",
                },
                {
                    "id": "funds",
                    "method": "GET",
                    "path": "/funds",
                    "available": True,
                    "required_scope": "funds:read",
                },
                {
                    "id": "usage",
                    "method": "GET",
                    "path": "/usage",
                    "available": True,
                    "required_scope": "funds:read",
                },
                {
                    "id": "data_quality",
                    "method": "GET",
                    "path": "/data-quality",
                    "available": quality_enabled,
                    "required_scope": "quality:read",
                },
                {
                    "id": "workspace_overview",
                    "method": "GET",
                    "path": "/workspace/overview",
                    "available": workspace_enabled,
                    "required_scope": "workspace:write",
                },
                {
                    "id": "workspace_report",
                    "method": "GET",
                    "path": "/workspace/report",
                    "available": workspace_enabled,
                    "required_scope": "workspace:write",
                },
                {
                    "id": "workspace_export",
                    "method": "GET",
                    "path": "/workspace/export",
                    "available": workspace_enabled,
                    "required_scope": "workspace:write",
                },
            ]
            next_actions = [
                "Store the token server-side or in a secret manager; never place it in a URL.",
                "Run the status check and confirm the returned key id matches your onboarding note.",
                "Start with bounded read calls before enabling scheduled workspace snapshots.",
                "Keep the validation boundary visible: 13FLOW does not claim validated alpha.",
            ]
            if workspace_enabled:
                next_actions.append("Create a first workspace watchlist and snapshot it manually before scheduling alerts.")
            else:
                next_actions.append("Ask the operator to add workspace:write before using saved watchlists, reports or exports.")
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "generated_at": _now_iso(),
                    "workspace_scope": "api_key",
                },
                "key": {
                    "id": key.key_id,
                    "label": key.label,
                    "tier": key.tier,
                    "scopes": list(key.scopes),
                    "rate_per_min": key.rate_per_min,
                    "rate_per_day": key.rate_per_day,
                    "created_at": key.created_at,
                    "expires_at": key.expires_at,
                    "rotation_due_at": key.rotation_due_at,
                },
                "diagnostic": {
                    "status": "ready",
                    "token_echoed": False,
                    "workspace_enabled": workspace_enabled,
                    "quality_enabled": quality_enabled,
                    "self_serve_checkout": False,
                    "human_review_required_for_routine_publication": False,
                },
                "key_lifecycle": {
                    "expires_at": key.expires_at,
                    "rotation_due_at": key.rotation_due_at,
                    "rotation_required": _iso_due(key.rotation_due_at),
                    "expired_keys_fail_closed": True,
                },
                "limits": _pro_workspace_limits_payload(),
                "endpoints": {
                    "base_url": base_url,
                    "openapi": f"{base_url}/openapi.json",
                    "checks": endpoint_checks,
                },
                "quick_checks": [
                    f"curl -fsS {base_url}/status -H 'Authorization: Bearer $PRO_TOKEN'",
                    f"curl -fsS {base_url}/usage -H 'Authorization: Bearer $PRO_TOKEN'",
                    f"curl -fsS {base_url}/funds -H 'Authorization: Bearer $PRO_TOKEN'",
                    f"curl -fsS {base_url}/workspace/overview -H 'Authorization: Bearer $PRO_TOKEN'",
                ],
                "next_actions": next_actions,
                "security": {
                    "credential_headers": ["Authorization: Bearer <token>", "X-13FLOW-Key: <token>"],
                    "token_in_url_allowed": False,
                    "browser_storage": "sessionStorage only for the optional cockpit UI; server integrations should use secret storage",
                    "audit": "accepted, denied and rate-limited Pro API requests create audit rows",
                },
                "truth_boundary": {
                    "source": "SEC EDGAR-derived filings",
                    "not_investment_advice": True,
                    "not_claimed": ["validated alpha", "complete shorts", "real-time holdings"],
                },
            })

        @app.get("/api/pro/v1/funds")
        @pro_required("funds:read")
        def pro_funds_ep():
            s = store()
            try:
                active = _public_active_ciks(s)
                rows = _fund_rows(s, active)
                quality = data_quality_report(s, limit=500, active_ciks=active)
                gate = quality_gate_report(s, active_ciks=active)
                warnings_by_cik = {}
                for w in quality["warnings"]:
                    warnings_by_cik.setdefault(w["fund"]["cik"], []).append(w)
                for w in quality.get("freshness_warnings", []):
                    warnings_by_cik.setdefault(w["fund"]["cik"], []).append(w)
                for w in quality.get("duplicate_label_warnings", []):
                    for f in w.get("funds", []):
                        warnings_by_cik.setdefault(f["cik"], []).append(w)
                out = []
                for r in rows:
                    cik = r["cik"]
                    series = s.fund_value_timeline(cik)
                    latest = series[-1] if series else None
                    out.append({
                        "cik": cik,
                        "label": r["label"],
                        "manager": r["manager"],
                        "latest_quarter": latest["report_date"] if latest else None,
                        "n_quarters": len(series),
                        "aum": latest["total_value"] if latest else None,
                        "n_positions": latest["n_positions"] if latest else None,
                        "aum_series": [{"q": x["report_date"], "aum": x["total_value"],
                                        "n_positions": x["n_positions"]} for x in series],
                        "quality_warnings": warnings_by_cik.get(cik, []),
                    })
                return jsonify({
                    "meta": {
                        "api": "13flow-pro",
                        "version": "v1",
                        "git_sha": _git_sha(),
                        "methodology": pro_methodology(quality["parameters"]["aum_jump_threshold"]),
                    },
                    "quality_summary": quality["summary"],
                    "quality_gate": gate["summary"],
                    "funds": out,
                })
            finally:
                s.close()

        @app.get("/api/pro/v1/fund/<cik>")
        @pro_required("funds:read")
        def pro_fund_ep(cik):
            cik = clean_cik(cik)
            basis = clean_date(request.args.get("basis"))
            include_holds = clean_bool(request.args.get("include_holds"), True)
            limit_positions = clean_int(request.args.get("limit_positions"), 1000, 1, 1000)
            limit_moves = clean_int(request.args.get("limit_moves"), 2000, 1, 2000)
            s = store()
            try:
                pf = s.load_portfolio(cik, basis)
                if pf is None:
                    return jsonify({"error": "not_found"}), 404
                current_filing = filing_row_for(s, cik, pf.report_date)
                prev_q = s.previous_quarter(cik, pf.report_date)
                prev = s.load_portfolio(cik, prev_q) if prev_q else Portfolio(
                    cik=cik, fund_label=pf.fund_label, report_date="", form="")
                prev_filing = filing_row_for(s, cik, prev_q) if prev_q else None
                diff = diff_portfolios(prev, pf)
                active = _public_active_ciks(s)
                quality = data_quality_report(s, limit=500, active_ciks=active)
                fund_warnings = [
                    w for w in quality["warnings"] if w["fund"]["cik"] == cik
                ]
                fund_warnings.extend(
                    w for w in quality.get("freshness_warnings", [])
                    if w["fund"]["cik"] == cik
                )
                fund_warnings.extend(
                    w for w in quality.get("duplicate_label_warnings", [])
                    if any(f["cik"] == cik for f in w.get("funds", []))
                )
                frow = s.fund_row(cik) or {}
                positions = sorted(pf.positions.values(), key=lambda p: p.value_usd, reverse=True)
                changes = sorted(
                    diff.changes,
                    key=lambda c: (c.move.value == Move.HOLD.value,
                                   -max(c.curr_value, c.prev_value)),
                )
                if not include_holds:
                    changes = [c for c in changes if c.move != Move.HOLD]
                counts = {m.value: len(diff.by_move(m)) for m in Move}
                return jsonify({
                    "meta": {
                        "api": "13flow-pro",
                        "version": "v1",
                        "git_sha": _git_sha(),
                        "methodology": pro_methodology(quality["parameters"]["aum_jump_threshold"]),
                        "request": {
                            "basis": basis,
                            "include_holds": include_holds,
                            "limit_positions": limit_positions,
                            "limit_moves": limit_moves,
                        },
                    },
                    "fund": {
                        "cik": cik,
                        "label": pf.fund_label,
                        "manager": frow.get("manager"),
                    },
                    "filing": filing_payload(current_filing),
                    "previous_filing": filing_payload(prev_filing),
                    "portfolio": {
                        "report_date": pf.report_date,
                        "form": pf.form,
                        "aum": pf.total_value,
                        "n_positions": len(pf.positions),
                        "positions_total": len(positions),
                        "positions_returned": min(len(positions), limit_positions),
                        "positions": [position_payload(p) for p in positions[:limit_positions]],
                    },
                    "moves": {
                        "previous_report_date": prev_q,
                        "current_report_date": pf.report_date,
                        "counts": counts,
                        "changes_total": len(changes),
                        "changes_returned": min(len(changes), limit_moves),
                        "changes": [change_payload(c) for c in changes[:limit_moves]],
                    },
                    "quality": {
                        "summary": {
                            "fund_warnings": len(fund_warnings),
                            "global_aum_jump_warnings": quality["summary"]["aum_jump_warnings"],
                            "global_stale_funds": quality["summary"]["stale_funds"],
                            "global_duplicate_labels": quality["summary"]["duplicate_labels"],
                            "global_unit_scale_candidates": quality["summary"]["unit_scale_candidates"],
                        },
                        "warnings": fund_warnings,
                    },
                })
            finally:
                s.close()

        @app.get("/api/pro/v1/data-quality")
        @pro_required("quality:read")
        def pro_data_quality_ep():
            threshold = clean_float(request.args.get("threshold"), 100.0, 2.0, 10000.0)
            limit = clean_int(request.args.get("limit"), 100, 1, 500)
            s = store()
            try:
                active = _public_active_ciks(s)
                report = data_quality_report(
                    s, aum_jump_threshold=threshold, limit=limit,
                    active_ciks=active,
                )
                report["quality_gate"] = quality_gate_report(
                    s, active_ciks=active, aum_jump_threshold=threshold,
                )
                return jsonify({
                    "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                    "report": report,
                })
            finally:
                s.close()

        @app.get("/api/pro/v1/watchlist")
        @pro_required("funds:read")
        def pro_watchlist_ep():
            payload = _watchlist_payload(request.args.get("tickers") or "", limit=50)
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": payload,
            })

        @app.get("/api/pro/v1/watchlist/discover")
        @pro_required("funds:read")
        def pro_watchlist_discover_ep():
            payload = _watchlist_discovery_payload(
                clean_int(request.args.get("limit"), 50, 1, 100),
                _watchlist_discovery_filters(request.args),
            )
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": payload,
            })

        @app.get("/api/pro/v1/workspace/overview")
        @pro_required("workspace:write")
        def pro_workspace_overview_ep():
            key = request.pro_api_key
            with ProAPIStore(pro_db_path) as ps:
                summary = ps.workspace_summary(key.key_id)
                recent_alerts = ps.list_workspace_alerts(key.key_id, status="open", limit=10)
                recent_activity = ps.list_workspace_activity(key.key_id, limit=10)
                watchlists = ps.list_watchlists(key.key_id)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "workspace_scope": "api_key",
                    "ui_exposed": False,
                    "automation": "manual_snapshot_only",
                    "workspace_limits": _pro_workspace_limits_payload(),
                },
                "summary": summary,
                "recent_alerts": recent_alerts,
                "recent_activity": recent_activity,
                "watchlists": watchlists[:50],
            })

        @app.get("/api/pro/v1/workspace/export")
        @pro_required("workspace:write")
        def pro_workspace_export_ep():
            from werkzeug.exceptions import BadRequest
            key = request.pro_api_key
            fmt = str(request.args.get("format") or "json").strip().lower()
            if fmt not in {"json", "csv"}:
                raise BadRequest("format must be json or csv")
            include_signals = clean_bool(request.args.get("include_signals"), False)
            with ProAPIStore(pro_db_path) as ps:
                payload = _workspace_export_payload(ps, key.key_id, include_signals=include_signals)
            payload["meta"]["format"] = fmt
            if fmt == "json":
                return jsonify(payload)
            csv_body = _workspace_export_csv(payload)
            filename = "13flow-workspace-export.csv"
            return Response(
                csv_body,
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        @app.get("/api/pro/v1/workspace/report")
        @pro_required("workspace:write")
        def pro_workspace_report_ep():
            key = request.pro_api_key
            watchlist_id = (
                _clean_workspace_id(request.args.get("watchlist_id"))
                if request.args.get("watchlist_id") else None
            )
            with ProAPIStore(pro_db_path) as ps:
                payload = _workspace_report_payload(
                    ps, key.key_id, watchlist_id=watchlist_id,
                )
            if payload is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify(payload)

        @app.get("/api/pro/v1/workspace/activity")
        @pro_required("workspace:write")
        def pro_workspace_activity_ep():
            key = request.pro_api_key
            limit = clean_int(request.args.get("limit"), 50, 1, 100)
            event_type = _clean_workspace_activity_filter(request.args.get("event_type"))
            entity_type = _clean_workspace_activity_filter(request.args.get("entity_type"))
            with ProAPIStore(pro_db_path) as ps:
                activity = ps.list_workspace_activity(
                    key.key_id, limit=limit, event_type=event_type, entity_type=entity_type,
                )
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "limit": limit,
                    "event_type": event_type,
                    "entity_type": entity_type,
                },
                "activity": activity,
            })

        @app.get("/api/pro/v1/workspace/alerts")
        @pro_required("workspace:write")
        def pro_workspace_alerts_ep():
            key = request.pro_api_key
            status = _clean_workspace_alert_status(request.args.get("status"), allow_all=True)
            limit = clean_int(request.args.get("limit"), 50, 1, 100)
            watchlist_id = (
                _clean_workspace_id(request.args.get("watchlist_id"))
                if request.args.get("watchlist_id") else None
            )
            with ProAPIStore(pro_db_path) as ps:
                alerts = ps.list_workspace_alerts(
                    key.key_id, status=status, limit=limit, watchlist_id=watchlist_id,
                )
                summary = ps.workspace_alert_summary(key.key_id)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "status": status or "all",
                    "limit": limit,
                },
                "summary": summary,
                "alerts": alerts,
            })

        @app.patch("/api/pro/v1/workspace/alerts/<alert_id>")
        @pro_required("workspace:write")
        def pro_workspace_alert_update_ep(alert_id):
            key = request.pro_api_key
            alert_id = _clean_workspace_id(alert_id)
            payload = request.get_json(silent=True) or {}
            status = _clean_workspace_alert_status(payload.get("status"))
            with ProAPIStore(pro_db_path) as ps:
                alert = ps.update_workspace_alert_status(key.key_id, alert_id, status)
                if alert is not None:
                    event_type = "alert.reopened" if status == "open" else f"alert.{status}"
                    ps.record_workspace_activity(
                        key.key_id,
                        event_type,
                        "alert",
                        alert["id"],
                        f"Alert {status}: {alert['ticker']}",
                        detail={
                            "ticker": alert["ticker"],
                            "action": alert["action"],
                            "status": status,
                            "watchlist_id": alert["watchlist_id"],
                            "snapshot_id": alert["snapshot_id"],
                        },
                    )
            if alert is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "alert": alert,
            })

        @app.get("/api/pro/v1/workspace/watchlists")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_list_ep():
            key = request.pro_api_key
            items = _pro_store_call(lambda ps: ps.list_watchlists(key.key_id))
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "workspace_scope": "api_key",
                    "ui_exposed": False,
                    "workspace_limits": _pro_workspace_limits_payload(),
                },
                "watchlists": items,
            })

        @app.post("/api/pro/v1/workspace/watchlists")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_create_ep():
            key = request.pro_api_key
            payload = _clean_saved_watchlist_payload(request.get_json(silent=True) or {})
            with ProAPIStore(pro_db_path) as ps:
                try:
                    item = ps.create_watchlist(
                        key.key_id,
                        payload["name"],
                        payload["tickers"],
                        filters=payload["filters"],
                        alert_policy=payload["alert_policy"],
                        notes=payload["notes"],
                        max_watchlists=pro_workspace_max_watchlists,
                    )
                except WorkspaceQuotaExceeded as e:
                    return jsonify({
                        "error": "workspace_quota_exceeded",
                        "detail": str(e),
                        "workspace_limits": _pro_workspace_limits_payload(),
                    }), 409
                ps.record_workspace_activity(
                    key.key_id,
                    "watchlist.created",
                    "watchlist",
                    item["id"],
                    f"Watchlist created: {item['name']}",
                    detail={
                        "name": item["name"],
                        "tickers": item["tickers"],
                        "filters": item["filters"],
                    },
                )
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": item,
            }), 201

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_get_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            item = _pro_store_call(lambda ps: ps.get_watchlist(key.key_id, watchlist_id))
            if item is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": item,
            })

        @app.put("/api/pro/v1/workspace/watchlists/<watchlist_id>")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_put_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            payload = _clean_saved_watchlist_payload(request.get_json(silent=True) or {})
            with ProAPIStore(pro_db_path) as ps:
                item = ps.update_watchlist(
                    key.key_id,
                    watchlist_id,
                    payload["name"],
                    payload["tickers"],
                    filters=payload["filters"],
                    alert_policy=payload["alert_policy"],
                    notes=payload["notes"],
                )
                if item is not None:
                    ps.record_workspace_activity(
                        key.key_id,
                        "watchlist.updated",
                        "watchlist",
                        watchlist_id,
                        f"Watchlist updated: {item['name']}",
                        detail={
                            "name": item["name"],
                            "tickers": item["tickers"],
                            "filters": item["filters"],
                        },
                    )
            if item is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": item,
            })

        def _delete_saved_watchlist_response(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            with ProAPIStore(pro_db_path) as ps:
                item = ps.get_watchlist(key.key_id, watchlist_id)
                if item is None:
                    return jsonify({"error": "not_found"}), 404
                deleted = ps.delete_watchlist(key.key_id, watchlist_id)
                if deleted:
                    ps.record_workspace_activity(
                        key.key_id,
                        "watchlist.deleted",
                        "watchlist",
                        watchlist_id,
                        f"Watchlist deleted: {item['name']}",
                        detail={
                            "name": item["name"],
                            "tickers": item["tickers"],
                        },
                    )
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "deleted": True,
                "id": watchlist_id,
            })

        @app.delete("/api/pro/v1/workspace/watchlists/<watchlist_id>")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_delete_ep(watchlist_id):
            return _delete_saved_watchlist_response(watchlist_id)

        @app.post("/api/pro/v1/workspace/watchlists/<watchlist_id>/delete")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_post_delete_ep(watchlist_id):
            return _delete_saved_watchlist_response(watchlist_id)

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>/preview")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_preview_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            item = _pro_store_call(lambda ps: ps.get_watchlist(key.key_id, watchlist_id))
            if item is None:
                return jsonify({"error": "not_found"}), 404
            payload = _watchlist_payload(item["tickers"], limit=50)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "saved_watchlist_id": watchlist_id,
                    "saved_watchlist_name": item["name"],
                },
                "watchlist": payload,
            })

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>/signals")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_signals_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            item = _pro_store_call(lambda ps: ps.get_watchlist(key.key_id, watchlist_id))
            if item is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "saved_watchlist_id": watchlist_id,
                    "saved_watchlist_name": item["name"],
                },
                "signals": _saved_watchlist_signals_payload(item),
            })

        @app.post("/api/pro/v1/workspace/watchlists/<watchlist_id>/signals/snapshot")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_signals_snapshot_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            with ProAPIStore(pro_db_path) as ps:
                item = ps.get_watchlist(key.key_id, watchlist_id)
                if item is None:
                    return jsonify({"error": "not_found"}), 404
                previous_rows = ps.list_signal_snapshots(
                    key.key_id, watchlist_id, limit=1, include_signals=True,
                )
                previous = previous_rows[0] if previous_rows else None
                signals = _saved_watchlist_signals_payload(item)
                snapshot = ps.create_signal_snapshot(
                    key.key_id, watchlist_id, signals, max_snapshots=100,
                )
                alerts = ps.upsert_workspace_alerts(
                    key.key_id, watchlist_id, snapshot["id"], signals,
                )
                delta = _saved_watchlist_signal_delta(signals, previous)
                ps.record_workspace_activity(
                    key.key_id,
                    "signals.snapshot",
                    "watchlist",
                    watchlist_id,
                    f"Signal snapshot: {item['name']}",
                    detail={
                        "snapshot_id": snapshot["id"],
                        "signal_count": len(snapshot["tickers"]),
                        "alerts": alerts,
                        "delta": {
                            "added": len(delta["added_tickers"]),
                            "removed": len(delta["removed_tickers"]),
                            "changed_actions": len(delta["changed_actions"]),
                            "changed_scores": len(delta["changed_scores"]),
                        },
                    },
                )
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "saved_watchlist_id": watchlist_id,
                    "saved_watchlist_name": item["name"],
                    "history_retention_snapshots": 100,
                },
                "snapshot": snapshot,
                "delta": delta,
                "alerts": alerts,
            }), 201

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>/signals/history")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_signals_history_ep(watchlist_id):
            key = request.pro_api_key
            watchlist_id = _clean_workspace_id(watchlist_id)
            limit = clean_int(request.args.get("limit"), 20, 1, 100)
            include_signals = clean_bool(request.args.get("include_signals"), False)
            with ProAPIStore(pro_db_path) as ps:
                item = ps.get_watchlist(key.key_id, watchlist_id)
                if item is None:
                    return jsonify({"error": "not_found"}), 404
                history = ps.list_signal_snapshots(
                    key.key_id, watchlist_id, limit=limit, include_signals=include_signals,
                )
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "saved_watchlist_id": watchlist_id,
                    "saved_watchlist_name": item["name"],
                    "include_signals": include_signals,
                    "limit": limit,
                },
                "history": history,
            })

        def _admin_key_public_row(row: dict) -> dict:
            return {
                "id": row.get("key_id") or row.get("id"),
                "label": row.get("label") or "",
                "contact_email": row.get("contact_email") or "",
                "scopes": str(row.get("scopes") or "").split(),
                "tier": row.get("tier") or "pro",
                "rate_per_min": int(row.get("rate_per_min") or 0),
                "rate_per_day": int(row.get("rate_per_day") or 0),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "rotation_due_at": row.get("rotation_due_at"),
                "revoked_at": row.get("revoked_at"),
                "last_used_at": row.get("last_used_at"),
                "revoked": bool(row.get("revoked_at")),
            }

        def _admin_keys_payload(ps: ProAPIStore) -> dict:
            return {
                "keys": [_admin_key_public_row(row) for row in ps.list_keys()],
                "privacy": {
                    "tokens_included": False,
                    "token_hashes_exposed": False,
                    "payloads_logged": False,
                },
            }

        @app.get("/api/pro/v1/admin/health")
        @pro_required("admin:read")
        def pro_admin_health_ep():
            key = request.pro_api_key
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                },
                "health": health,
            })

        @app.get("/api/pro/v1/admin/keys")
        @pro_required("admin:read")
        def pro_admin_keys_ep():
            key = request.pro_api_key
            with ProAPIStore(pro_db_path) as ps:
                keys = _admin_keys_payload(ps)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "key_management": keys,
            })

        @app.post("/api/pro/v1/admin/keys")
        @pro_required("admin:write")
        def pro_admin_create_key_ep():
            key = request.pro_api_key
            payload = request.get_json(silent=True) or {}
            label = str(payload.get("label") or "").strip()[:120]
            contact_email = str(payload.get("contact_email") or "").strip()[:200]
            scopes_raw = payload.get("scopes") or ["funds:read", "quality:read", "workspace:write"]
            if isinstance(scopes_raw, str):
                scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
            else:
                scopes = [str(s).strip() for s in scopes_raw if str(s).strip()]
            allowed_scopes = {"funds:read", "quality:read", "workspace:write"}
            scopes = list(dict.fromkeys(scopes or ["funds:read", "quality:read"]))
            if not label:
                return jsonify({"error": "label_required"}), 400
            if not contact_email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", contact_email):
                return jsonify({"error": "valid_contact_email_required"}), 400
            if any(scope not in allowed_scopes for scope in scopes):
                return jsonify({"error": "invalid_scope", "allowed_scopes": sorted(allowed_scopes)}), 400
            rate_per_min = clean_int(payload.get("rate_per_min"), 120, 1, 1000)
            rate_per_day = clean_int(payload.get("rate_per_day"), 10000, 1, 1000000)
            expires_days = clean_int(payload.get("expires_days"), 30, 1, 365)
            rotation_days = clean_int(payload.get("rotation_days"), 21, 1, 365)
            with ProAPIStore(pro_db_path) as ps:
                token, created = ps.create_key(
                    label=label,
                    contact_email=contact_email,
                    scopes=scopes,
                    rate_per_min=rate_per_min,
                    rate_per_day=rate_per_day,
                    expires_days=expires_days,
                    rotation_days=rotation_days,
                    actor="admin_web",
                )
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:write",
                    "read_only": False,
                },
                "created_key": {
                    "id": created.key_id,
                    "label": created.label,
                    "contact_email": contact_email,
                    "scopes": list(created.scopes),
                    "rate_per_min": created.rate_per_min,
                    "rate_per_day": created.rate_per_day,
                    "expires_at": created.expires_at,
                    "rotation_due_at": created.rotation_due_at,
                    "token": token,
                    "token_shown_once": True,
                    "token_stored": False,
                },
            }), 201

        @app.post("/api/pro/v1/admin/keys/<key_id>/revoke")
        @pro_required("admin:write")
        def pro_admin_revoke_key_ep(key_id):
            key = request.pro_api_key
            safe_key_id = str(key_id or "").strip()
            if safe_key_id == key.key_id:
                return jsonify({"error": "cannot_revoke_current_admin_key"}), 400
            with ProAPIStore(pro_db_path) as ps:
                revoked = ps.revoke_key(safe_key_id, actor="admin_web")
                keys = _admin_keys_payload(ps)
            if not revoked:
                return jsonify({"error": "key_not_found_or_already_revoked"}), 404
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:write",
                    "read_only": False,
                },
                "revoked_key_id": safe_key_id,
                "key_management": keys,
            })

        @app.get("/api/pro/v1/admin/ops")
        @pro_required("admin:read")
        def pro_admin_ops_ep():
            key = request.pro_api_key
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
            ops = _pro_admin_ops_payload(live, health, automation)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "ops": ops,
            })

        @app.get("/api/pro/v1/admin/pilot-fulfillment")
        @pro_required("admin:read")
        def pro_admin_pilot_fulfillment_ep():
            key = request.pro_api_key
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
            ops = _pro_admin_ops_payload(live, health, automation)
            fulfillment = _pro_admin_pilot_fulfillment_payload(live, health, ops)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "pilot_fulfillment": fulfillment,
            })

        @app.get("/api/pro/v1/admin/buyer-handoff")
        @pro_required("admin:read")
        def pro_admin_buyer_handoff_ep():
            key = request.pro_api_key
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
            ops = _pro_admin_ops_payload(live, health, automation)
            handoff = _pro_admin_buyer_handoff_payload(live, health, ops)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "buyer_handoff": handoff,
            })

        @app.get("/api/pro/v1/admin/release-readiness")
        @pro_required("admin:read")
        def pro_admin_release_readiness_ep():
            key = request.pro_api_key
            key_id = str(request.args.get("key_id") or "").strip() or None
            days = clean_int(request.args.get("days"), 7, 1, 30)
            include = str(request.args.get("include") or "").strip().lower()
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
                closeout = ps.pilot_closeout_report(key_id=key_id, days=days, key_limit=10)
            ops = _pro_admin_ops_payload(live, health, automation)
            fulfillment = _pro_admin_pilot_fulfillment_payload(live, health, ops)
            handoff = _pro_admin_buyer_handoff_payload(live, health, ops)
            closeout["public_context"] = {
                "public_state": live.get("public_state"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "ops_status": ops.get("status"),
                "quality_status": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("status"),
                "trusted_funds": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("trusted_funds"),
            }
            renewal = _pro_admin_pilot_renewal_payload(closeout, fulfillment)
            release = _pro_admin_release_readiness_payload(ops, fulfillment, handoff, closeout, renewal)
            payload = {
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "release_readiness": release,
            }
            if include in {"surface", "all"}:
                closeout["operator_next_actions"] = [
                    "Share only the closeout summary and issued key metadata, never the token.",
                    "Use expand_candidate as a sales follow-up signal, not as investment-performance evidence.",
                    "If verdict is hold, resolve lifecycle, errors or missing usage before expanding the pilot.",
                    "Keep data-quality and legal evidence links visible in renewal notes.",
                ]
                payload.update({
                    "ops": ops,
                    "pilot_fulfillment": fulfillment,
                    "buyer_handoff": handoff,
                    "pilot_closeout": closeout,
                    "pilot_renewal": renewal,
                    "pilot_request_assist": pilot_request_assist_payload(None),
                })
            return jsonify(payload)

        @app.get("/api/pro/v1/admin/pilot-closeout")
        @pro_required("admin:read")
        def pro_admin_pilot_closeout_ep():
            key = request.pro_api_key
            key_id = str(request.args.get("key_id") or "").strip() or None
            days = clean_int(request.args.get("days"), 7, 1, 30)
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
                closeout = ps.pilot_closeout_report(key_id=key_id, days=days, key_limit=10)
            ops = _pro_admin_ops_payload(live, health, automation)
            closeout["public_context"] = {
                "public_state": live.get("public_state"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "ops_status": ops.get("status"),
                "quality_status": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("status"),
                "trusted_funds": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("trusted_funds"),
            }
            closeout["operator_next_actions"] = [
                "Share only the closeout summary and issued key metadata, never the token.",
                "Use expand_candidate as a sales follow-up signal, not as investment-performance evidence.",
                "If verdict is hold, resolve lifecycle, errors or missing usage before expanding the pilot.",
                "Keep data-quality and legal evidence links visible in renewal notes.",
            ]
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "pilot_closeout": closeout,
            })

        @app.get("/api/pro/v1/admin/pilot-renewal")
        @pro_required("admin:read")
        def pro_admin_pilot_renewal_ep():
            key = request.pro_api_key
            key_id = str(request.args.get("key_id") or "").strip() or None
            days = clean_int(request.args.get("days"), 7, 1, 30)
            live = live_status_payload()
            with ProAPIStore(pro_db_path) as ps:
                health = ps.admin_health()
                automation = ps.workspace_automation_summary(max_due=25)
                closeout = ps.pilot_closeout_report(key_id=key_id, days=days, key_limit=10)
            ops = _pro_admin_ops_payload(live, health, automation)
            fulfillment = _pro_admin_pilot_fulfillment_payload(live, health, ops)
            renewal = _pro_admin_pilot_renewal_payload(closeout, fulfillment)
            renewal["public_context"] = {
                "public_state": live.get("public_state"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "ops_status": ops.get("status"),
                "quality_status": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("status"),
                "trusted_funds": ((ops.get("public_data") or {}).get("quality_summary") or {}).get("trusted_funds"),
            }
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                },
                "pilot_renewal": renewal,
            })

        @app.route("/api/pro/v1/admin/pilot-request-assist", methods=["GET", "POST"])
        @pro_required("admin:read")
        def pro_admin_pilot_request_assist_ep():
            key = request.pro_api_key
            note = request.get_json(silent=True) if request.method == "POST" else None
            assist = pilot_request_assist_payload(note if isinstance(note, dict) else None)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "admin_key_id": key.key_id,
                    "scope": "admin:read",
                    "read_only": True,
                    "request_persisted": False,
                },
                "pilot_request_assist": assist,
            })

        @app.get("/api/pro/v1/openapi.json")
        def pro_openapi_ep():
            return jsonify(pro_openapi_doc())

    @app.get("/api/config")
    def config_ep():
        # Lets the single-page dashboard adapt to the build it is served by.
        return jsonify({"open": open_mode,
                        "demo": demo_mode,
                        "public_state": "DEMO" if demo_mode else "LIVE",
                        "features": {"auth": not open_mode,
                                     "alerts": not open_mode,
                                     "billing": not open_mode,
                                     "pro_api": pro_enabled}})

    @app.get("/api/version")
    @app.get("/healthz")
    def version_ep():
        return jsonify({
            "app": "13flow",
            "git_sha": _git_sha(),
            "commit": _git_sha(),
            "generated_at": _now_iso(),
            "open": open_mode,
            "demo": demo_mode,
            "public_state": "DEMO" if demo_mode else "LIVE",
        })

    @app.get("/api/methodology/confluence-v1")
    def confluence_v1_methodology_ep():
        from .research import confluence_v1_spec
        return jsonify(confluence_v1_spec(_git_sha()))

    @app.get("/api/signals/confluence/history")
    def confluence_history_ep():
        from .research import HISTORY_FILENAME, read_signal_history
        limit = clean_int(request.args.get("limit"), 100, 1, 1000)
        ticker = (request.args.get("ticker") or "").strip().upper() or None
        window = request.args.get("window")
        window_days = clean_int(window, 0, 7, 365) if window else None
        history_path = os.path.join(_cache_dir, HISTORY_FILENAME)
        rows = read_signal_history(
            history_path,
            limit=limit,
            ticker=ticker,
            window_days=window_days,
        )
        return jsonify({
            "metadata": {
                "append_only": True,
                "source": "confluence-history.jsonl",
                "score_version": "confluence_v1",
                "history_path_configured": bool(_cache_dir),
                "public_state": "DEMO" if demo_mode else "LIVE",
            },
            "count": len(rows),
            "history": rows,
        })

    def live_status_payload() -> dict:
        return _cached_public_payload("live_status", _live_status_payload)

    def _live_status_payload() -> dict:
        generated_at = _now_iso()
        s = store()
        try:
            active = _public_active_ciks(s)
            active_values = tuple(sorted(active))
            funds = len(active)
            if active_values:
                placeholders = _placeholders(active_values)
                filings = s.conn.execute(
                    f"SELECT COUNT(*) c FROM filings WHERE cik IN ({placeholders})",
                    active_values,
                ).fetchone()["c"] or 0
                latest_filing_date = s.conn.execute(
                    f"SELECT MAX(filing_date) d FROM filings WHERE cik IN ({placeholders})",
                    active_values,
                ).fetchone()["d"]
                accession_filter = f"WHERE f.cik IN ({placeholders})"
                accession_args = active_values
            else:
                filings = s.conn.execute("SELECT COUNT(*) c FROM filings").fetchone()["c"] or 0
                latest_filing_date = s.conn.execute("SELECT MAX(filing_date) d FROM filings").fetchone()["d"]
                accession_filter = ""
                accession_args = ()
            latest_rows = _latest_filings_count(s, active)
            latest = _latest_filings_date(s, "MAX", active)
            earliest = _latest_filings_date(s, "MIN", active)
            accession_rows = s.conn.execute(
                f"""SELECT f.accession, f.cik, fn.label, f.form, f.report_date, f.filing_date
                   FROM latest_filings lf
                   JOIN filings f ON f.accession=lf.accession
                   LEFT JOIN funds fn ON fn.cik=f.cik
                   {accession_filter}
                   ORDER BY f.report_date DESC, f.filing_date DESC, f.accession DESC
                   LIMIT 12""",
                accession_args,
            ).fetchall()
            coverage = s.coverage(latest, active) if latest else {
                "overall_value_share": None,
                "value_unresolved": None,
                "per_fund": [],
            }
            quality_report = data_quality_report(s, limit=1, active_ciks=active)
            quality = quality_report["summary"]
            gate = quality_gate_report(s, active_ciks=active)
        finally:
            s.close()
        ready = bool(funds and filings and latest_rows)
        data_mode = "demo" if demo_mode else ("live_edgar" if ready else "degraded")
        source = "DEMO SAMPLE" if demo_mode else "SEC EDGAR"
        return {
            "app": "13flow",
            "generated_at": generated_at,
            "data_mode": data_mode,
            "public_state": "DEMO" if demo_mode else ("LIVE" if ready else "DEGRADED"),
            "source": source,
            "uses_synthetic_data": bool(demo_mode),
            "git_sha": _git_sha(),
            "commit": _git_sha(),
            "open": open_mode,
            "auth_enabled": not open_mode,
            "checkout_enabled": (not open_mode),
            "latest_13f_quarter": latest,
            "data_as_of": latest_filing_date,
            "period_13f": {
                "from": earliest,
                "to": latest,
            },
            "coverage": {
                "overall_value_share": coverage.get("overall_value_share"),
                "value_unresolved": coverage.get("value_unresolved"),
                "funds_reported": len(coverage.get("per_fund") or []),
            },
            "accessions": {
                "latest_count": latest_rows,
                "sample": [dict(r) for r in accession_rows],
            },
            "counts": {
                "funds": funds,
                "filings": filings,
                "latest_filings": latest_rows,
            },
            "quality_summary": {
                "status": quality["status"],
                "aum_jump_warnings": quality["aum_jump_warnings"],
                "stale_funds": quality["stale_funds"],
                "duplicate_labels": quality["duplicate_labels"],
                "unit_scale_candidates": quality["unit_scale_candidates"],
                "review_items": quality["review_items"],
                "quality_gate_status": gate["summary"]["status"],
                "trusted_funds": gate["summary"]["trusted_funds"],
                "signal_eligible_funds": gate["summary"]["signal_eligible_funds"],
                "degraded_funds": gate["summary"]["degraded_funds"],
                "quarantined_funds": gate["summary"]["quarantined_funds"],
            },
            "public_endpoints": ["/api/live-status", "/api/version", "/api/funds", "/api/data-quality"],
        }

    @app.get("/api/live-status")
    def live_status_ep():
        return jsonify(live_status_payload())

    def product_status_payload() -> dict:
        return _cached_public_payload("product_status", _product_status_payload)

    def _product_status_payload() -> dict:
        live = live_status_payload()
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "public_state": live["public_state"],
            "data": {
                "source": live["source"],
                "uses_synthetic_data": live["uses_synthetic_data"],
                "latest_13f_quarter": live["latest_13f_quarter"],
                "data_as_of": live.get("data_as_of"),
                "period_13f": live["period_13f"],
                "counts": live["counts"],
                "coverage": live["coverage"],
                "quality_summary": live["quality_summary"],
                "coverage_boundary": {
                    "form_13f": (
                        "Delayed long US reportable securities. It is not a complete "
                        "portfolio: no shorts, most non-US holdings, bonds, full derivative "
                        "books, intra-quarter trading or confidential-treatment omissions."
                    ),
                    "form_4": (
                        "Current Confluence/validation rails use normalized Table I Form 4 "
                        "transactions. Table II derivatives, 10b5-1 plan flags, multi-owner "
                        "attribution and weighted-average price footnotes are not fully "
                        "modeled in the live score yet."
                    ),
                    "insider_universe": (
                        "Production Confluence scans Form 4 for a bounded issuer universe "
                        "driven by tracked 13F activity; insider-only and distribution "
                        "quadrants are not exhaustive."
                    ),
                },
            },
            "commercial_readiness": {
                "public_site": "live" if live["public_state"] == "LIVE" else "not_live",
                "public_api": "live_read_only",
                "pro_api": (
                    "enabled_in_this_service"
                    if pro_enabled
                    else "separate_service_expected_on_/api/pro/v1_with_api_key"
                ),
                "mcp": "available_read_only",
                "x402": "not_enabled",
                "alerts": "implemented_operator_runbook_required",
            },
            "validation": {
                "status": "mechanical_evidence_ready_for_review_metrics_unreviewed",
                "score_claim": "ordinal_heuristic_not_probability_not_expected_return",
                "current_artifact": {
                    "scope": "25-ticker mature 13F + Form 4 joined validation artifact",
                    "path": "/var/lib/13flow/confluence_features_liquid25_v2_mature.csv",
                    "feature_scope": "13f_form4_joined",
                    "features_sha256": "3ab0cebaf893520580d5dc9ae338dbcb5c8344efdb6aeb990dc4af7936f456b9",
                    "dataset_sha256": "3ab0cebaf893520580d5dc9ae338dbcb5c8344efdb6aeb990dc4af7936f456b9",
                    "prices_sha256": "not_embedded_in_status_payload",
                    "price_source": "local_csv:validation_prices_liquid25_massive.csv",
                    "form4_source": "local_csv:validation_form4_liquid25_v2.csv",
                    "schema_status": "valid_minimum_schema",
                    "metrics_status": "minimum_schema_valid_metrics_unreviewed",
                    "evidence_review_status": "mechanical_evidence_ready_for_review",
                    "date_range": {"from": "2024-11-13", "to": "2025-12-29"},
                    "row_count": 125,
                    "ticker_count": 25,
                    "row_error_count": 0,
                    "forward_return_coverage": {
                        "forward_return_20d": 1.0,
                        "forward_return_60d": 1.0,
                        "forward_return_120d": 1.0,
                    },
                    "rows_with_form4_accessions": 101,
                    "rows_with_open_market_buyers": 14,
                    "tickers_with_open_market_buyers": 9,
                    "metrics_reviewed": False,
                    "public_validation_claim": False,
                    "publishable_as_full_validation": False,
                },
                "metrics_snapshot": {
                    "horizon_days": 60,
                    "split": "test",
                    "model": "full_score",
                    "n": 113,
                    "rank_ic": -0.003655,
                    "rank_ic_permutation_p": 0.964072,
                    "top_bottom_spread": 0.026515,
                    "top_bottom_spread_ci95": [-0.078722, 0.110038],
                    "hit_rate": 0.5,
                    "mean_forward_return": 0.004804,
                    "interpretation": (
                        "weak_or_neutral_descriptive_metrics; this is not a validated "
                        "alpha, forecast, probability or expected-return claim"
                    ),
                },
                "blocked_by": (
                    "A 25-ticker mature 13F + Form 4 joined artifact is mechanically "
                    "schema-valid and ready for human review, but metrics remain "
                    "unreviewed and are not a public alpha or validation claim."
                ),
                "required_next_artifact": (
                    "broader/full-universe adjusted-price and normalized Form 4 artifacts "
                    "with reviewed price source, delisting treatment, costs, liquidity and "
                    "no-lookahead controls"
                ),
            },
            "offer_boundary": {
                "sell_now": [
                    "verifiable SEC EDGAR-derived 13F data",
                    "read-only public API",
                    "scoped Pro API keys with audit and rate limits",
                    "MCP read-only integration with Pro tools failing closed",
                    "data-quality warnings and methodology contracts",
                    "25-ticker mature Form 4 joined mechanical evidence pack ready for human review",
                ],
                "do_not_claim_yet": [
                    "validated alpha",
                    "probabilistic score",
                    "expected-return model",
                    "complete insider-only/distribution universe",
                    "x402-paid access in production",
                ],
            },
            "operator_policy": {
                "external_api_safety": (
                    "Small samples first, explicit sleeps/backoff, resumable exports, "
                    "and no repeated failed provider loops from production."
                ),
                "deployment_gate": "deploy smoke must pass before claiming live state",
            },
        }

    @app.get("/api/product-status")
    def product_status_ep():
        return jsonify(product_status_payload())

    def core_v1_boundary_payload() -> dict:
        return {
            "status": "controlled_pilot_core_v1",
            "source": "docs/CORE_V1_BOUNDARY.md",
            "sales_motion": "controlled_pilot_only",
            "change_rule": "prefer extending existing contracts over adding new surfaces",
            "operator_review_required": True,
            "public_open_build": {
                "read_only": True,
                "browser_accounts": False,
                "self_serve_checkout": False,
                "public_submission_endpoint": False,
                "token_collection": False,
            },
            "pro_boundary": {
                "operator_issued_keys": True,
                "web_worker_creates_tokens": False,
                "tokens_included_in_payloads": False,
                "default_customer_scopes": ["funds:read", "quality:read", "workspace:write"],
                "forbidden_customer_scopes": ["admin:read"],
            },
            "routine_publication": {
                "mode": "automated_fail_closed",
                "manual_13f_review_required": False,
                "signal_rule": "only trusted funds are eligible for scores, consensus and watchlist signals",
            },
            "keep_out": [
                "public self-serve checkout",
                "public prospect PII persistence",
                "automated billing or CRM workflow",
                "production x402 settlement",
                "validated alpha, expected-return or probability claims",
                "new dashboards when existing admin, buyer, onboarding or workspace surfaces can carry the need",
            ],
            "required_gates": [
                "pytest offline suite",
                "public smoke",
                "Pro workspace smoke",
                "Pro key lifecycle smoke",
                "encrypted Pro DB backup restore verification",
            ],
        }

    def commercial_readiness_payload() -> dict:
        return _cached_public_payload("commercial_readiness", _commercial_readiness_payload)

    def _commercial_readiness_payload() -> dict:
        live = live_status_payload()
        product = product_status_payload()
        quality = live.get("quality_summary") or {}
        validation = product.get("validation") or {}
        artifact = validation.get("current_artifact") or {}
        readiness = product.get("commercial_readiness") or {}
        hard_blocks = []
        if live.get("public_state") != "LIVE" or live.get("uses_synthetic_data"):
            hard_blocks.append("public data surface is not LIVE SEC EDGAR")
        if int((live.get("counts") or {}).get("funds") or 0) <= 0:
            hard_blocks.append("no tracked fund is available")
        if int(quality.get("trusted_funds") or 0) <= 0:
            hard_blocks.append("no trusted fund is eligible for signal surfaces")
        if artifact.get("public_validation_claim") is not False:
            hard_blocks.append("validation boundary is not explicit enough")
        soft_blocks = []
        if validation.get("status") != "mechanical_evidence_ready_for_review_metrics_unreviewed":
            soft_blocks.append("validation artifact state changed and should be reviewed")
        if readiness.get("x402") != "not_enabled":
            soft_blocks.append("x402 status changed; payment flow requires a fresh security check")
        if int(quality.get("review_items") or 0) > 0:
            soft_blocks.append("data-quality review items remain visible and must stay disclosed")
        status = "controlled_pilot_ready" if not hard_blocks else "not_ready"
        if status == "controlled_pilot_ready" and soft_blocks:
            status = "controlled_pilot_ready_with_disclosures"
        public_checks = [
            {
                "id": "public_live_data",
                "status": "pass" if live.get("public_state") == "LIVE" and not live.get("uses_synthetic_data") else "fail",
                "evidence": "/api/live-status",
                "summary": f"public_state={live.get('public_state')} uses_synthetic_data={live.get('uses_synthetic_data')}",
            },
            {
                "id": "data_quality_gate",
                "status": "pass" if int(quality.get("trusted_funds") or 0) > 0 else "fail",
                "evidence": "/api/data-quality",
                "summary": (
                    f"trusted={quality.get('trusted_funds')} quarantined={quality.get('quarantined_funds')} "
                    f"review_items={quality.get('review_items')}"
                ),
            },
            {
                "id": "validation_boundary",
                "status": "pass" if artifact.get("public_validation_claim") is False and artifact.get("publishable_as_full_validation") is False else "fail",
                "evidence": "/validation",
                "summary": validation.get("status"),
            },
            {
                "id": "pro_surface",
                "status": "pass",
                "evidence": "/api/pro/v1/openapi.json",
                "summary": readiness.get("pro_api"),
            },
            {
                "id": "workspace_surface",
                "status": "pass",
                "evidence": "/pro/workspace",
                "summary": "watchlists, snapshots, alerts, reports and exports are implemented behind Pro key scopes",
            },
            {
                "id": "admin_health_surface",
                "status": "pass",
                "evidence": "/api/pro/v1/admin/health",
                "summary": "admin health requires admin:read; the web panel is hidden unless server-side admin auth is configured",
            },
        ]
        external_checks = [
            {
                "id": "public_smoke",
                "status": "external_required",
                "command": "sudo EXPECTED_SHA=$SHA /opt/13flow/deploy/smoke-public.sh",
            },
            {
                "id": "pro_workspace_smoke",
                "status": "external_required",
                "command": "sudo EXPECTED_SHA=$SHA PRO_TOKEN=\"$PRO_TOKEN\" /opt/13flow/deploy/smoke-pro-workspace.sh",
            },
            {
                "id": "encrypted_backup_restore",
                "status": "external_required",
                "command": "/opt/13flow/deploy/prepare-pro-backup-restore-check.sh and deploy/verify-pro-db-backup.sh on restore host",
            },
            {
                "id": "snapshot_timer",
                "status": "external_required",
                "command": "systemctl list-timers | grep 13flow-pro-workspace-snapshot",
            },
        ]
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "status": status,
            "sales_motion": "controlled_pilot_only",
            "self_serve_checkout": False,
            "public_quote_ready": False,
            "core_v1_boundary": core_v1_boundary_payload(),
            "hard_blocks": hard_blocks,
            "soft_blocks": soft_blocks,
            "public_checks": public_checks,
            "external_checks": external_checks,
            "snapshot": {
                "public_state": live.get("public_state"),
                "data_as_of": live.get("data_as_of"),
                "latest_13f_quarter": live.get("latest_13f_quarter"),
                "counts": live.get("counts"),
                "quality_gate": {
                    "status": quality.get("quality_gate_status"),
                    "trusted_funds": quality.get("trusted_funds"),
                    "signal_eligible_funds": quality.get("signal_eligible_funds"),
                    "quarantined_funds": quality.get("quarantined_funds"),
                    "review_items": quality.get("review_items"),
                    "human_review_required_for_routine_publication": False,
                },
                "pro_api": readiness.get("pro_api"),
                "mcp": readiness.get("mcp"),
                "x402": readiness.get("x402"),
                "validation_status": validation.get("status"),
            },
            "sell_now": product["offer_boundary"]["sell_now"],
            "do_not_claim_yet": product["offer_boundary"]["do_not_claim_yet"],
            "runbook": {
                "before_demo": [
                    "Confirm /api/version SHA matches deployed commit.",
                    "Run public smoke.",
                    "Run Pro workspace smoke with a scoped QA key.",
                    "Open /readiness and confirm admin health with protected admin credentials.",
                    "Keep validation and quality disclosures visible in buyer material.",
                ],
                "operator_boundary": (
                    "This endpoint does not execute systemctl, read backup files or certify smokes. "
                    "External checks must be run by the operator."
                ),
            },
        }

    @app.get("/api/commercial-readiness")
    def commercial_readiness_ep():
        return jsonify(commercial_readiness_payload())

    def security_posture_payload() -> dict:
        return _cached_public_payload("security_posture", _security_posture_payload)

    def _security_posture_payload() -> dict:
        live = live_status_payload()
        readiness = commercial_readiness_payload()
        quality = (readiness.get("snapshot") or {}).get("quality_gate") or {}
        workspace_limits = _pro_workspace_limits_payload()
        hard_blocks = list(readiness.get("hard_blocks") or [])
        status = "controlled_pilot_security_ready" if not hard_blocks else "security_review_required"
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "status": status,
            "core_v1_boundary": core_v1_boundary_payload(),
            "scope": (
                "Security posture for a controlled technical pilot. This is an operator "
                "evidence pack, not a third-party penetration test, SOC 2 report, "
                "investment-advice review or managed-service SLA."
            ),
            "public_surface": {
                "mode": "read_only_open_build",
                "synthetic_data": bool(live.get("uses_synthetic_data")),
                "auth_billing_accounts": "not registered in open mode",
                "mutation_policy": "public endpoints are GET-oriented; Pro writes live only behind the separate Pro service",
                "headers": [
                    "nosniff baseline",
                    "DENY frame policy",
                    "no-referrer baseline",
                    "per-response CSP with no third-party scripts",
                ],
                "evidence": ["/api/version", "/api/config", "/api/live-status", "/api/openapi.json"],
            },
            "pro_surface": {
                "service_boundary": "separate /api/pro/v1 service with scoped API keys",
                "credential_headers": ["Authorization: Bearer <token>", "X-13FLOW-Key: <token>"],
                "token_in_url_allowed": False,
                "browser_storage": "diagnostic/cockpit pages use sessionStorage only; not localStorage",
                "cache_policy": "private no-store for authenticated Pro responses",
                "audit": "accepted, denied and rate-limited Pro requests create audit rows without storing plaintext tokens",
                "rate_limits": "persistent per-key minute/day quotas",
                "workspace_limits": workspace_limits,
                "evidence": ["/api/pro/v1/status", "/api/pro/v1/usage", "/api/pro/v1/onboarding", "/pro/onboarding"],
            },
            "mcp_surface": {
                "transport": "streamable-http",
                "public_path": "/api/mcp",
                "host_origin_policy": "MCP server enforces Host/Origin validation and a bounded request body",
                "rate_limit": "per-client rate limit in MCP process",
                "pro_tool_boundary": "Pro tools fail closed without payment or a valid key",
                "evidence": ["/methodology/mcp", "/developers"],
            },
            "data_quality": {
                "mode": "automated_fail_closed",
                "manual_13f_review_required_for_routine_publication": False,
                "signal_rule": "only trusted funds are eligible for scores, consensus and watchlist signals",
                "trusted_funds": quality.get("trusted_funds"),
                "signal_eligible_funds": quality.get("signal_eligible_funds"),
                "quarantined_funds": quality.get("quarantined_funds"),
                "review_items": quality.get("review_items"),
                "evidence": ["/coverage", "/api/data-quality"],
            },
            "operations": {
                "deploy_gate": "public and Pro workspace smoke tests must pass after each deploy",
                "backup": "Pro DB backup is encrypted and restore verification is performed off-host with the private key",
                "external_checks_required": readiness.get("external_checks") or [],
                "operator_boundary": (
                    "This endpoint does not read systemd, Apache configs, journal logs, "
                    "backup files or secrets. Those remain operator-side checks."
                ),
            },
            "privacy": {
                "public_accounts": False,
                "self_serve_checkout": False,
                "tokens_echoed": False,
                "secrets_in_payloads": False,
                "payload_policy": "security posture exposes controls and evidence links, never keys, hashes, IPs, user agents or request bodies",
            },
            "non_claims": [
                "third-party penetration test",
                "SOC 2, ISO 27001 or regulated outsourcing certification",
                "public self-serve payment flow",
                "managed-service SLA",
                "validated alpha or investment advice",
                "complete coverage of non-13F assets, shorts or intra-quarter trades",
            ],
            "buyer_security_questions": [
                "Who owns token custody and rotation on the buyer side?",
                "Which scopes are needed for the pilot and which are deliberately excluded?",
                "What request volume and burst profile should be configured before go-live?",
                "Does the buyer require IP allow-listing, contractual DPA clauses or a custom retention policy?",
                "Which evidence links must be archived before recurring access begins?",
            ],
            "evidence_links": [
                {"label": "Commercial readiness", "href": "/api/commercial-readiness"},
                {"label": "Security posture", "href": "/api/security-posture"},
                {"label": "Coverage and quality", "href": "/coverage"},
                {"label": "Validation boundary", "href": "/validation"},
                {"label": "Public OpenAPI", "href": "/api/openapi.json"},
                {"label": "Pro OpenAPI", "href": "/api/pro/v1/openapi.json"},
                {"label": "Pro terms", "href": "/legal/pro-api"},
            ],
        }

    @app.get("/api/security-posture")
    def security_posture_ep():
        return jsonify(security_posture_payload())

    def pilot_intake_payload() -> dict:
        return _cached_public_payload("pilot_intake", _pilot_intake_payload)

    def _pilot_intake_payload() -> dict:
        readiness = commercial_readiness_payload()
        security = security_posture_payload()
        offer = pro_offer_payload()
        defaults = offer.get("default_limits") or {}
        package_names = [p.get("name") for p in (offer.get("plans") or []) if p.get("name")]
        required_fields = [
            {
                "id": "organization",
                "label": "Organization name",
                "required": True,
                "sensitive": False,
                "purpose": "commercial qualification and contract/admin record",
            },
            {
                "id": "billing_contact",
                "label": "Billing/security contact",
                "required": True,
                "sensitive": "business_contact",
                "purpose": "manual pilot coordination, token delivery and rotation planning",
            },
            {
                "id": "workflow",
                "label": "Intended workflow",
                "required": True,
                "allowed_values": ["research desk", "data pipeline", "MCP agent", "monitoring", "internal dashboard"],
                "purpose": "scope the pilot and avoid unused permissions",
            },
            {
                "id": "requested_scopes",
                "label": "Requested scopes",
                "required": True,
                "allowed_values": ["funds:read", "quality:read", "workspace:write", "admin:read"],
                "purpose": "least-privilege key issuance",
            },
            {
                "id": "expected_volume",
                "label": "Expected request volume",
                "required": True,
                "default_limits": defaults,
                "purpose": "quota sizing before any recurring access",
            },
            {
                "id": "token_custody",
                "label": "Token custody owner",
                "required": True,
                "purpose": "rotation, revocation and incident response",
            },
            {
                "id": "legal_acknowledgement",
                "label": "Research-screen acknowledgement",
                "required": True,
                "must_acknowledge": "13FLOW is a research screen, not investment advice, not a performance claim and not a public price quote.",
            },
        ]
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "status": "operator_review_required",
            "sales_motion": readiness.get("sales_motion"),
            "self_serve_checkout": False,
            "public_submission_endpoint": None,
            "public_form_submission": False,
            "core_v1_boundary": core_v1_boundary_payload(),
            "privacy": {
                "server_side_pii_storage": False,
                "browser_storage": "none required; the public page renders a copyable template only",
                "token_collection": False,
                "secret_collection": False,
                "recommended_channel": "operator-selected secure channel outside the public site",
                "retention_note": "archive only the final operator note needed for pilot administration and legal evidence",
            },
            "pilot_packages": package_names,
            "default_limits": defaults,
            "required_fields": required_fields,
            "operator_note_schema": {
                "package": " | ".join(package_names),
                "organization": "required",
                "billing_contact": "required business contact",
                "workflow": "research desk | data pipeline | MCP agent | monitoring | internal dashboard",
                "requested_scopes": "least privilege; no admin:read for customers",
                "expected_volume": "requests per minute, requests per day, burst profile",
                "token_delivery_channel": "secure channel selected by operator",
                "rotation_due_at": "set before key delivery",
                "security_requirements": "IP allow-listing, DPA, retention or custom terms if needed",
                "boundary_ack": "research screen; no investment advice; no public price quote; no SLA unless custom contract",
            },
            "operator_note_template": [
                "13FLOW PILOT INTAKE",
                "package: <Technical pilot review | API integration review | MCP integration review>",
                "organization: <legal or operating name>",
                "billing_contact: <business contact>",
                "security_contact: <optional business contact>",
                "workflow: <research desk | data pipeline | MCP agent | monitoring | internal dashboard>",
                "requested_scopes: <funds:read quality:read workspace:write>",
                "expected_volume: <per minute / per day / burst profile>",
                "token_delivery_channel: <secure channel>",
                "rotation_due_at: <YYYY-MM-DD>",
                "security_requirements: <IP allow-listing / DPA / retention / none>",
                "boundary_ack: research screen; not investment advice; no public price quote; no SLA unless custom contract",
                "operator_decision: <decline | issue bounded pilot key | request more info>",
            ],
            "pre_issue_checks": [
                "Run public smoke and Pro workspace smoke on the deployed SHA.",
                "Confirm /api/security-posture status is controlled_pilot_security_ready.",
                "Confirm requested scopes are least-privilege and exclude admin:read for customers.",
                "Set expiry and rotation_due_at before token delivery.",
                "Record key id after the first successful /api/pro/v1/status call.",
            ],
            "evidence_links": [
                {"label": "Pilot intake page", "href": "/pilot"},
                {"label": "Pilot intake Markdown", "href": "/api/pilot-intake.md"},
                {"label": "Buyer pack", "href": "/buyer-pack"},
                {"label": "Security posture", "href": "/security"},
                {"label": "Commercial readiness", "href": "/readiness"},
                {"label": "Pro terms", "href": "/legal/pro-api"},
            ],
        }

    @app.get("/api/pilot-intake")
    def pilot_intake_ep():
        return jsonify(pilot_intake_payload())

    def pilot_intake_markdown(payload: dict) -> str:
        def bullets(items) -> str:
            return "\n".join(f"- {str(item)}" for item in (items or [])) or "- None"
        field_lines = []
        for field in payload.get("required_fields") or []:
            req = "required" if field.get("required") else "optional"
            sensitive = field.get("sensitive")
            suffix = f"; sensitive={sensitive}" if sensitive else ""
            field_lines.append(f"- {field.get('id')}: {field.get('label')} ({req}{suffix})")
        link_lines = [
            f"- [{item.get('label')}]({item.get('href')})"
            for item in (payload.get("evidence_links") or [])
        ]
        return "\n".join([
            "# 13FLOW Pilot Intake",
            "",
            f"Generated: {payload.get('generated_at')}",
            f"Git SHA: {payload.get('git_sha')}",
            f"Status: {payload.get('status')}",
            f"Sales motion: {payload.get('sales_motion')}",
            f"Self-serve checkout: {str(payload.get('self_serve_checkout')).lower()}",
            f"Public form submission: {str(payload.get('public_form_submission')).lower()}",
            "",
            "## Privacy Boundary",
            "",
            f"- Server-side PII storage: {str((payload.get('privacy') or {}).get('server_side_pii_storage')).lower()}",
            f"- Token collection: {str((payload.get('privacy') or {}).get('token_collection')).lower()}",
            f"- Secret collection: {str((payload.get('privacy') or {}).get('secret_collection')).lower()}",
            f"- Recommended channel: {(payload.get('privacy') or {}).get('recommended_channel')}",
            "",
            "## Required Fields",
            "",
            "\n".join(field_lines) or "- None",
            "",
            "## Operator Note Template",
            "",
            "```text",
            "\n".join(str(x) for x in (payload.get("operator_note_template") or [])),
            "```",
            "",
            "## Pre-Issue Checks",
            "",
            bullets(payload.get("pre_issue_checks")),
            "",
            "## Evidence Links",
            "",
            "\n".join(link_lines) or "- None",
            "",
        ])

    def pilot_request_assist_payload(request_note: Optional[dict] = None) -> dict:
        intake = pilot_intake_payload()
        allowed_scopes = ["funds:read", "quality:read", "workspace:write"]
        allowed_workflows = ["research desk", "data pipeline", "MCP agent", "monitoring", "internal dashboard"]
        note = request_note if isinstance(request_note, dict) else {}
        sanitized = {}
        redacted_fields = []
        for key in (
            "organization",
            "billing_contact",
            "security_contact",
            "workflow",
            "package",
            "requested_scopes",
            "expected_volume",
            "token_delivery_channel",
            "security_requirements",
            "boundary_ack",
        ):
            value, redacted = redact_public_secret_like_text(note.get(key))
            sanitized[key] = value
            if redacted:
                redacted_fields.append(key)
        requested_scopes = [
            s.strip()
            for s in re.split(r"[\s,;]+", sanitized.get("requested_scopes") or "")
            if s.strip()
        ]
        invalid_scopes = sorted({s for s in requested_scopes if s not in allowed_scopes})
        recommended_scopes = [s for s in allowed_scopes if s in requested_scopes] or allowed_scopes
        required = ["organization", "billing_contact", "workflow", "requested_scopes", "expected_volume", "boundary_ack"]
        missing = [key for key in required if not sanitized.get(key)]
        risk_flags = []
        if invalid_scopes:
            risk_flags.append("requested scopes include values outside the customer allow-list")
        if "admin:read" in requested_scopes:
            risk_flags.append("admin:read must never be issued to customers")
        if sanitized.get("workflow") and sanitized["workflow"] not in allowed_workflows:
            risk_flags.append("workflow needs operator normalization")
        if redacted_fields:
            risk_flags.append("secret-like material was redacted from the request note")
        if "not investment advice" not in sanitized.get("boundary_ack", "").lower():
            risk_flags.append("research-screen acknowledgement is incomplete")
        status = "ready_for_operator_review" if not missing and not invalid_scopes and not redacted_fields else "needs_more_info"
        operator_note = [
            "13FLOW ASSISTED PILOT REQUEST",
            f"organization: {sanitized.get('organization') or '<missing>'}",
            f"billing_contact: {sanitized.get('billing_contact') or '<missing>'}",
            f"security_contact: {sanitized.get('security_contact') or '<optional>'}",
            f"workflow: {sanitized.get('workflow') or '<missing>'}",
            f"package: {sanitized.get('package') or '<operator selects package>'}",
            f"requested_scopes: {','.join(recommended_scopes)}",
            f"expected_volume: {sanitized.get('expected_volume') or '<missing>'}",
            f"token_delivery_channel: {sanitized.get('token_delivery_channel') or '<secure channel selected by operator>'}",
            f"security_requirements: {sanitized.get('security_requirements') or '<none provided>'}",
            f"boundary_ack: {sanitized.get('boundary_ack') or '<missing>'}",
            "operator_decision: <decline | issue bounded pilot key | request more info>",
        ]
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "status": status,
            "read_only": True,
            "public_page": "/pilot/request",
            "public_submission_endpoint": None,
            "server_side_pii_storage": False,
            "request_persisted": False,
            "web_worker_creates_tokens": False,
            "tokens_collected": False,
            "secrets_collected": False,
            "input_schema": {
                "required": required,
                "optional": ["security_contact", "package", "token_delivery_channel", "security_requirements"],
                "allowed_workflows": allowed_workflows,
                "allowed_customer_scopes": allowed_scopes,
                "forbidden_customer_scopes": ["admin:read"],
            },
            "sample_request": {
                "organization": "Example Capital",
                "billing_contact": "ops@example.invalid",
                "security_contact": "security@example.invalid",
                "workflow": "research desk",
                "package": (intake.get("pilot_packages") or ["Technical pilot review"])[0],
                "requested_scopes": "funds:read,quality:read,workspace:write",
                "expected_volume": "60/min, 5000/day, no burst automation before approval",
                "token_delivery_channel": "operator-approved secure channel",
                "security_requirements": "no custom requirement for pilot",
                "boundary_ack": "13FLOW is a research screen, not investment advice.",
            },
            "sanitized_request": sanitized,
            "missing_fields": missing,
            "invalid_scopes": invalid_scopes,
            "redacted_fields": redacted_fields,
            "risk_flags": risk_flags,
            "recommended_scopes": recommended_scopes,
            "operator_note": operator_note,
            "operator_checklist": [
                "Confirm the requester is a business buyer and the contact channel is acceptable.",
                "Confirm the requested workflow maps to a bounded Pro pilot package.",
                "Reject or normalize any scope outside funds:read, quality:read and workspace:write.",
                "Run public smoke, Pro workspace smoke and Pro key lifecycle smoke on the deployed SHA.",
                "Use admin pilot fulfillment to create a bounded key only after operator review.",
                "Deliver the token once through the selected secure channel; never paste it into the public site.",
            ],
            "admin_transform": {
                "endpoint": "/api/pro/v1/admin/pilot-request-assist",
                "method": "POST",
                "stores_request": False,
                "requires_scope": "admin:read",
            },
            "privacy": {
                "browser_storage": "none; public assistant renders a copyable note in the page only",
                "server_side_pii_storage": False,
                "tokens_echoed": False,
                "token_hashes_exposed": False,
                "payloads_logged": False,
            },
        }

    @app.get("/api/pilot-request-assist")
    def pilot_request_assist_ep():
        return jsonify(pilot_request_assist_payload())

    @app.get("/api/pilot-intake.md")
    def pilot_intake_markdown_ep():
        body = pilot_intake_markdown(pilot_intake_payload())
        return Response(
            body,
            content_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="13flow-pilot-intake.md"'},
        )

    def buyer_pack_payload() -> dict:
        return _cached_public_payload("buyer_pack", _buyer_pack_payload)

    def _buyer_pack_payload() -> dict:
        product = product_status_payload()
        readiness = commercial_readiness_payload()
        security = security_posture_payload()
        intake = pilot_intake_payload()
        offer = pro_offer_payload()
        snapshot = readiness.get("snapshot") or {}
        quality = snapshot.get("quality_gate") or {}
        validation = product.get("validation") or {}
        artifact = validation.get("current_artifact") or {}
        commercial = offer.get("commercial_model") or {}
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "title": "13FLOW buyer review pack",
            "status": readiness.get("status"),
            "sales_motion": readiness.get("sales_motion"),
            "public_quote_ready": readiness.get("public_quote_ready"),
            "self_serve_checkout": readiness.get("self_serve_checkout"),
            "core_v1_boundary": core_v1_boundary_payload(),
            "one_liner": (
                "Source-linked SEC EDGAR-derived 13F research surfaces, scoped Pro API "
                "access, workspace tooling and explicit validation boundaries for a "
                "controlled technical pilot."
            ),
            "audience": offer["offer"]["audience"],
            "proof_points": [
                "LIVE public EDGAR-derived data surface with synthetic mode disabled.",
                "Trusted-fund quality gate and quarantined-fund exclusion are machine-readable.",
                "Pro API keys are scoped, rate-limited, audited and no-store.",
                "Workspace watchlists, snapshots, alerts, reports and exports are available behind Pro scopes.",
                "MCP Pro tools fail closed without payment or a valid key.",
                "Security posture separates implemented controls from external operator checks and non-claims.",
                "Validation page separates mechanical evidence from alpha claims.",
            ],
            "snapshot": {
                "public_state": snapshot.get("public_state"),
                "data_as_of": snapshot.get("data_as_of"),
                "latest_13f_quarter": snapshot.get("latest_13f_quarter"),
                "funds": (snapshot.get("counts") or {}).get("funds"),
                "trusted_funds": quality.get("trusted_funds"),
                "signal_eligible_funds": quality.get("signal_eligible_funds"),
                "quarantined_funds": quality.get("quarantined_funds"),
                "review_items": quality.get("review_items"),
                "validation_status": snapshot.get("validation_status"),
                "artifact_rows": artifact.get("row_count"),
                "artifact_tickers": artifact.get("ticker_count"),
            },
            "pilot_packages": commercial.get("recommended_packages") or [],
            "buyer_checklist": offer["buyer_checklist"],
            "qualification_questions": offer["sales_packet"]["qualification_questions"],
            "pilot_handoff": offer["sales_packet"]["pilot_handoff"],
            "evidence_links": [
                {"label": "Commercial readiness", "href": "/api/commercial-readiness"},
                {"label": "Pilot intake", "href": "/pilot"},
                {"label": "Security posture", "href": "/security"},
                {"label": "Product status", "href": "/api/product-status"},
                {"label": "Coverage and quality", "href": "/coverage"},
                {"label": "Validation boundary", "href": "/validation"},
                {"label": "Public status", "href": "/status"},
                {"label": "Pro offer", "href": "/api/pro-offer"},
                {"label": "Pro OpenAPI", "href": "/api/pro/v1/openapi.json"},
                {"label": "Onboarding diagnostic", "href": "/pro/onboarding"},
                {"label": "Workspace cockpit", "href": "/pro/workspace"},
            ],
            "sell_now": readiness.get("sell_now") or [],
            "do_not_claim_yet": readiness.get("do_not_claim_yet") or [],
            "next_steps": [
                "Review the evidence links and current validation boundary.",
                "Complete the pilot intake operator note before issuing any key.",
                "Answer the qualification questions and confirm expected request volume.",
                "Run the public readiness and Pro OpenAPI checks.",
                "Use /pro/onboarding with the issued key before wiring production code.",
                "Start with a bounded technical pilot before any recurring access discussion.",
            ],
            "terms_boundary": {
                "pricing": commercial.get("pricing_status"),
                "redistribution": "not included without a custom agreement",
                "investment_advice": False,
                "managed_service_sla": False,
                "operator_review_required": True,
            },
            "security_boundary": {
                "status": security.get("status"),
                "scope": security.get("scope"),
                "non_claims": security.get("non_claims"),
            },
            "pilot_intake": {
                "status": intake.get("status"),
                "public_form_submission": intake.get("public_form_submission"),
                "server_side_pii_storage": (intake.get("privacy") or {}).get("server_side_pii_storage"),
                "required_fields": [f.get("id") for f in (intake.get("required_fields") or [])],
            },
        }

    @app.get("/api/buyer-pack")
    def buyer_pack_ep():
        return jsonify(buyer_pack_payload())

    def buyer_pack_markdown(payload: dict) -> str:
        snapshot = payload.get("snapshot") or {}
        terms = payload.get("terms_boundary") or {}
        def bullets(items) -> str:
            return "\n".join(f"- {str(item)}" for item in (items or [])) or "- None"
        def link_bullets(items) -> str:
            return "\n".join(
                f"- [{item.get('label')}]({item.get('href')})"
                for item in (items or [])
            ) or "- None"
        packages = []
        for pkg in payload.get("pilot_packages") or []:
            packages.append(
                f"- {pkg.get('name')}: {pkg.get('term')}; "
                f"price={pkg.get('price_eur_per_month')}; "
                f"sell_when={pkg.get('sell_when')}"
            )
        return "\n".join([
            "# 13FLOW Buyer Review Pack",
            "",
            f"Generated: {payload.get('generated_at')}",
            f"Git SHA: {payload.get('git_sha')}",
            f"Status: {payload.get('status')}",
            f"Sales motion: {payload.get('sales_motion')}",
            f"Public quote ready: {str(payload.get('public_quote_ready')).lower()}",
            f"Self-serve checkout: {str(payload.get('self_serve_checkout')).lower()}",
            "",
            "## Summary",
            "",
            str(payload.get("one_liner") or ""),
            "",
            "## Current Snapshot",
            "",
            f"- Public state: {snapshot.get('public_state')}",
            f"- Data as of: {snapshot.get('data_as_of')}",
            f"- Latest 13F quarter: {snapshot.get('latest_13f_quarter')}",
            f"- Funds: {snapshot.get('funds')}",
            f"- Trusted funds: {snapshot.get('trusted_funds')}",
            f"- Signal eligible funds: {snapshot.get('signal_eligible_funds')}",
            f"- Quarantined funds: {snapshot.get('quarantined_funds')}",
            f"- Validation status: {snapshot.get('validation_status')}",
            f"- Validation rows: {snapshot.get('artifact_rows')}",
            f"- Validation tickers: {snapshot.get('artifact_tickers')}",
            "",
            "## Proof Points",
            "",
            bullets(payload.get("proof_points")),
            "",
            "## Pilot Packages",
            "",
            "\n".join(packages) or "- Not publicly quoted",
            "",
            "## Buyer Checklist",
            "",
            bullets(payload.get("buyer_checklist")),
            "",
            "## Qualification Questions",
            "",
            bullets(payload.get("qualification_questions")),
            "",
            "## Pilot Handoff",
            "",
            bullets(payload.get("pilot_handoff")),
            "",
            "## Evidence Links",
            "",
            link_bullets(payload.get("evidence_links")),
            "",
            "## Do Not Claim Yet",
            "",
            bullets(payload.get("do_not_claim_yet")),
            "",
            "## Terms Boundary",
            "",
            f"- Pricing: {terms.get('pricing')}",
            f"- Redistribution: {terms.get('redistribution')}",
            f"- Investment advice: {str(terms.get('investment_advice')).lower()}",
            f"- Managed-service SLA: {str(terms.get('managed_service_sla')).lower()}",
            f"- Operator review required: {str(terms.get('operator_review_required')).lower()}",
            "",
            "This pack is not investment advice, not a performance claim and not a public price quote.",
            "",
        ])

    @app.get("/api/buyer-pack.md")
    def buyer_pack_markdown_ep():
        body = buyer_pack_markdown(buyer_pack_payload())
        return Response(
            body,
            content_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="13flow-buyer-pack.md"'},
        )

    def coverage_quality_payload() -> dict:
        live = live_status_payload()
        s = store()
        try:
            active = _public_active_ciks(s)
            report = data_quality_report(s, limit=500, active_ciks=active)
            gate = quality_gate_report(s, active_ciks=active)
        finally:
            s.close()
        excluded = [f for f in gate.get("funds", []) if not f.get("signal_eligible")]
        trusted = [f for f in gate.get("funds", []) if f.get("signal_eligible")]
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "public_state": live.get("public_state"),
            "data_as_of": live.get("data_as_of"),
            "latest_13f_quarter": live.get("latest_13f_quarter"),
            "counts": live.get("counts") or {},
            "summary": gate.get("summary") or {},
            "policy": gate.get("policy") or {},
            "excluded_funds": excluded,
            "trusted_sample": trusted[:12],
            "quality_report_summary": report.get("summary") or {},
            "source_links": {
                "data_quality_api": "/api/data-quality",
                "funds_api": "/api/funds",
                "methodology": "/methodology",
                "live_status": "/api/live-status",
            },
            "commercial_boundary": {
                "signals_use_trusted_funds_only": True,
                "human_review_required_for_routine_publication": False,
                "fail_closed": True,
                "not_a_performance_claim": True,
            },
        }

    @app.get("/coverage")
    def coverage_page():
        payload = coverage_quality_payload()
        summary = payload["summary"]
        policy = payload["policy"]
        excluded = payload["excluded_funds"]
        trusted = payload["trusted_sample"]
        counts = payload["counts"]
        metrics = [
            ("Active funds", summary.get("active_funds")),
            ("Trusted funds", summary.get("trusted_funds")),
            ("Signal eligible", summary.get("signal_eligible_funds")),
            ("Excluded", len(excluded)),
            ("Stale", summary.get("stale_funds")),
            ("Degraded", summary.get("degraded_funds")),
            ("Quarantined", summary.get("quarantined_funds")),
            ("Latest 13F", payload.get("latest_13f_quarter")),
        ]
        metrics_html = "".join(
            f"<div class=\"doc-metric\"><b>{html_escape(str(value if value is not None else '-'))}</b>"
            f"<span>{html_escape(label)}</span></div>"
            for label, value in metrics
        )
        def reason_text(item: dict) -> str:
            reasons = []
            for reason in item.get("reasons") or []:
                code = reason.get("code") or "unknown"
                detail = {
                    k: v for k, v in reason.items()
                    if k != "code" and v is not None
                }
                if detail:
                    reasons.append(f"{code}: " + ", ".join(f"{k}={v}" for k, v in detail.items()))
                else:
                    reasons.append(code)
            return "; ".join(reasons) or "excluded by automated quality gate"
        excluded_rows = "".join(
            "<tr>"
            f"<td>{html_escape(f.get('label') or '-')}<br><span class=\"meta\">CIK {html_escape(f.get('cik') or '-')}</span></td>"
            f"<td><span class=\"pill\">{html_escape(f.get('status') or '-')}</span></td>"
            f"<td>{html_escape(((f.get('latest_filing') or {}).get('report_date')) or '-')}</td>"
            f"<td>{html_escape(reason_text(f))}</td>"
            "</tr>"
            for f in excluded
        ) or "<tr><td colspan=\"4\">No excluded fund in the current quality gate.</td></tr>"
        trusted_rows = "".join(
            "<tr>"
            f"<td>{html_escape(f.get('label') or '-')}<br><span class=\"meta\">CIK {html_escape(f.get('cik') or '-')}</span></td>"
            f"<td>{html_escape(((f.get('latest_filing') or {}).get('report_date')) or '-')}</td>"
            f"<td>{html_escape(str(f.get('series_points') or 0))}</td>"
            "</tr>"
            for f in trusted
        )
        source_links = payload["source_links"]
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Coverage & quality</div>"
            "<h1>Trusted Fund Coverage</h1>"
            "<p class=\"doc-lede\">13FLOW publishes current signals only from funds that pass an automated fail-closed quality gate. "
            "Stale, degraded or quarantined funds remain visible for audit, but they are excluded from product scores, consensus screens and watchlists.</p></div>"
            "<aside class=\"doc-panel\"><h3>Commercial boundary</h3>"
            "<p>This is not a performance claim. It is an operating disclosure: the signal universe is deliberately narrower than the raw database when a fund is stale or fails quality checks.</p>"
            f"<p><span class=\"pill\">status:{html_escape(str(summary.get('status') or '-'))}</span>"
            f"<span class=\"pill\">mode:{html_escape(str(policy.get('mode') or '-'))}</span>"
            f"<span class=\"pill\">human_review:false</span></p></aside></section>"
            f"<section class=\"doc-metrics\">{metrics_html}</section>"
            "<section class=\"doc-section\"><h2>Signal Eligibility Rule</h2>"
            f"<p>{html_escape(policy.get('signal_rule') or 'Only trusted funds are eligible for product signals.')}</p>"
            "<div class=\"mini-list\">"
            "<div><b>Trusted</b> eligible for product signals.</div>"
            "<div><b>Stale</b> visible for audit/history, excluded from current signals.</div>"
            "<div><b>Degraded</b> visible for audit, excluded from product signals.</div>"
            "<div><b>Quarantined</b> retained in DB, excluded from product signals.</div>"
            "</div></section>"
            "<section class=\"doc-section\"><h2>Excluded Funds</h2>"
            "<p>These funds are not used in current scores, consensus screens or watchlist discovery until they pass the gate again.</p>"
            "<table><thead><tr><th>Fund</th><th>Status</th><th>Latest fund quarter</th><th>Reason</th></tr></thead>"
            f"<tbody>{excluded_rows}</tbody></table></section>"
            "<section class=\"doc-section\"><h2>Trusted Sample</h2>"
            "<p>Sample of funds currently eligible for product signals. The full machine-readable list is exposed in the data-quality API.</p>"
            "<table><thead><tr><th>Fund</th><th>Latest fund quarter</th><th>Series points</th></tr></thead>"
            f"<tbody>{trusted_rows}</tbody></table></section>"
            "<section class=\"doc-section\"><h2>Audit Links</h2>"
            "<p>"
            f"<a class=\"pill\" href=\"{html_escape(source_links['data_quality_api'])}\">Data-quality JSON</a>"
            f"<a class=\"pill\" href=\"{html_escape(source_links['funds_api'])}\">Funds JSON</a>"
            f"<a class=\"pill\" href=\"{html_escape(source_links['live_status'])}\">Live status</a>"
            f"<a class=\"pill\" href=\"{html_escape(source_links['methodology'])}\">Methodology</a>"
            "</p>"
            f"<p class=\"meta\">public_state={html_escape(str(payload.get('public_state') or '-'))}; "
            f"data_as_of={html_escape(str(payload.get('data_as_of') or '-'))}; "
            f"funds={html_escape(str(counts.get('funds') or 0))}; generated_at={html_escape(payload['generated_at'])}</p>"
            "</section>"
        )
        return _html_response("Coverage & Quality", body)

    @app.get("/status")
    def status_page():
        live = live_status_payload()
        product = product_status_payload()
        validation = product["validation"]
        artifact = validation["current_artifact"]
        boundary = product["offer_boundary"]
        counts = live["counts"]
        quality = live["quality_summary"]
        period = live["period_13f"]
        status_class = "pill"
        rows = [
            ("Runtime state", live["public_state"]),
            ("Source", live["source"]),
            ("Git SHA", live["git_sha"]),
            ("Generated at", live["generated_at"]),
            ("Data as of", live.get("data_as_of") or "unknown"),
            ("13F period", f"{period.get('from') or 'unknown'} -> {period.get('to') or 'unknown'}"),
            ("Funds", str(counts.get("funds") or 0)),
            ("Filings", str(counts.get("filings") or 0)),
            ("Latest filing rows", str(counts.get("latest_filings") or 0)),
            ("Quality status", quality.get("status") or "unknown"),
            ("AUM jump warnings", str(quality.get("aum_jump_warnings") or 0)),
            ("Unit-scale candidates", str(quality.get("unit_scale_candidates") or 0)),
        ]
        rows_html = "".join(
            f"<tr><td>{html_escape(k)}</td><td><code>{html_escape(v)}</code></td></tr>"
            for k, v in rows
        )
        endpoints = [
            ("/api/version", "deployed SHA and open/demo flags"),
            ("/api/live-status", "current SEC EDGAR data state and counts"),
            ("/api/product-status", "commercial readiness and validation boundary"),
            ("/api/data-quality", "operator-visible data-quality warnings"),
            ("/api/openapi.json", "public API contract"),
            ("/api/methodology/confluence-v1", "frozen Confluence v1 method contract"),
        ]
        endpoint_rows = "".join(
            f"<tr><td><a href=\"{html_escape(path)}\"><code>{html_escape(path)}</code></a></td>"
            f"<td>{html_escape(desc)}</td></tr>"
            for path, desc in endpoints
        )
        sell_now = "".join(f"<li>{html_escape(item)}</li>" for item in boundary["sell_now"])
        do_not_claim = "".join(f"<li>{html_escape(item)}</li>" for item in boundary["do_not_claim_yet"])
        coverage = live.get("coverage") or {}
        cov = coverage.get("overall_value_share")
        cov_s = "-" if cov is None else f"{float(cov) * 100:.1f}%"
        smoke = (
            "curl -fsS https://13flow.eu/api/version\n"
            "curl -fsS https://13flow.eu/api/live-status\n"
            "curl -fsS https://13flow.eu/app >/dev/null\n"
            "curl -fsS https://13flow.eu/pro >/dev/null"
        )
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Production trust console</div>"
            "<h1>Status</h1>"
            "<p class=\"doc-lede\">Human-readable evidence page for the currently served 13FLOW build. "
            "Use this page to distinguish deployed production state from stale local audits or screenshots, "
            "then follow the smoke checks before claiming a release is live.</p>"
            "<div class=\"actions\"><a class=\"pill cta\" href=\"/api/live-status\">Live JSON</a>"
            "<a class=\"pill\" href=\"/api/product-status\">Product contract</a>"
            "<a class=\"pill\" href=\"/validation\">Validation</a></div></div>"
            "<aside class=\"doc-panel\"><h3>Evidence status</h3>"
            f"<p><span class=\"{status_class}\">{html_escape(live['public_state'])}</span></p>"
            f"<p class=\"meta\">uses_synthetic_data={str(live['uses_synthetic_data']).lower()}</p>"
            f"<p class=\"meta\">commit={html_escape(live['git_sha'])}</p>"
            f"<p class=\"meta\">generated={html_escape(live['generated_at'])}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(counts.get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(counts.get('filings') or 0))}</b><span>SEC filings</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(cov_s)}</b><span>value coverage</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(live.get('latest_13f_quarter') or 'unknown')}</b><span>latest 13F quarter</span></div>"
            "</section>"
            "<section class=\"runbook\">"
            "<div class=\"runstep\"><b>01</b><span>Confirm SHA from /api/version after deploy.</span></div>"
            "<div class=\"runstep\"><b>02</b><span>Check /api/live-status for LIVE and no synthetic data.</span></div>"
            "<div class=\"runstep\"><b>03</b><span>Open app, Pro and docs pages through Apache.</span></div>"
            "<div class=\"runstep\"><b>04</b><span>Only then call the release verified.</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Runtime proof</h2>"
            f"<table><thead><tr><th>Field</th><th>Current value</th></tr></thead><tbody>{rows_html}</tbody></table>"
            "</section>"
            "<section class=\"doc-section\"><h2>Operator smoke commands</h2>"
            f"<pre><code>{html_escape(smoke)}</code></pre>"
            f"<p class=\"meta\">{html_escape(product['operator_policy']['deployment_gate'])}</p></section>"
            "<section class=\"doc-section\"><h2>Verification endpoints</h2>"
            f"<table><thead><tr><th>Endpoint</th><th>Use</th></tr></thead><tbody>{endpoint_rows}</tbody></table>"
            "</section>"
            "<section class=\"doc-section\"><h2>Validation artifact</h2>"
            f"<p class=\"callout\"><strong>Boundary:</strong> {html_escape(validation['blocked_by'])}</p>"
            f"<p class=\"meta\">scope={html_escape(artifact['scope'])}</p>"
            f"<p class=\"meta\">path={html_escape(str(artifact.get('path') or ''))}</p>"
            f"<p class=\"meta\">schema_status={html_escape(str(artifact.get('schema_status') or 'unknown'))}</p>"
            f"<p class=\"meta\">evidence_review_status={html_escape(str(artifact.get('evidence_review_status') or 'unknown'))}</p>"
            f"<p class=\"meta\">metrics_status={html_escape(str(artifact.get('metrics_status') or 'unknown'))}</p>"
            f"<p class=\"meta\">rows={html_escape(str(artifact.get('row_count') or 'unknown'))}; "
            f"tickers={html_escape(str(artifact.get('ticker_count') or 'unknown'))}; "
            f"row_errors={html_escape(str(artifact.get('row_error_count') if artifact.get('row_error_count') is not None else 'unknown'))}</p>"
            f"<p><code>features_sha256={html_escape(artifact['features_sha256'])}</code></p>"
            f"<p><code>prices_sha256={html_escape(artifact['prices_sha256'])}</code></p>"
            f"<p>Publishable as full validation: <code>{str(artifact['publishable_as_full_validation']).lower()}</code></p>"
            f"<p>Public validation claim: <code>{str(artifact.get('public_validation_claim')).lower()}</code></p>"
            "</section>"
            "<section class=\"split\">"
            "<div class=\"doc-section\"><h2>Sell now</h2><ul>" + sell_now + "</ul></div>"
            "<div class=\"doc-section\"><h2>Do not claim yet</h2><ul>" + do_not_claim + "</ul></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Operational policy</h2>"
            f"<p>{html_escape(product['operator_policy']['external_api_safety'])}</p>"
            "<p><a class=\"pill\" href=\"/developers\">Developer docs</a> "
            "<a class=\"pill\" href=\"/methodology/app\">Application methodology</a> "
            "<a class=\"pill\" href=\"/methodology/mcp\">MCP methodology</a></p></section>"
        )
        return _html_response("Status", body)

    @app.get("/readiness")
    def readiness_page():
        payload = commercial_readiness_payload()
        snapshot = payload["snapshot"]
        quality = snapshot["quality_gate"]
        public_rows = "".join(
            f"<tr><td>{html_escape(item['id'])}</td>"
            f"<td><span class=\"pill\">{html_escape(item['status'])}</span></td>"
            f"<td>{html_escape(item.get('summary') or '')}</td>"
            f"<td><a href=\"{html_escape(item['evidence'])}\">{html_escape(item['evidence'])}</a></td></tr>"
            for item in payload["public_checks"]
        )
        external_rows = "".join(
            f"<tr><td>{html_escape(item['id'])}</td>"
            f"<td><span class=\"pill\">{html_escape(item['status'])}</span></td>"
            f"<td><code>{html_escape(item['command'])}</code></td></tr>"
            for item in payload["external_checks"]
        )
        sell_now = "".join(f"<li>{html_escape(item)}</li>" for item in payload["sell_now"])
        do_not = "".join(f"<li>{html_escape(item)}</li>" for item in payload["do_not_claim_yet"])
        hard = "".join(f"<li>{html_escape(item)}</li>" for item in payload["hard_blocks"]) or "<li>None for controlled pilot readiness.</li>"
        soft = "".join(f"<li>{html_escape(item)}</li>" for item in payload["soft_blocks"]) or "<li>No current disclosure warning beyond standard product boundaries.</li>"
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Commercial readiness</div>"
            "<h1>Readiness Checklist</h1>"
            "<p class=\"doc-lede\">Operator-facing summary for deciding whether 13FLOW can be shown or sold as a controlled Pro pilot today.</p></div>"
            f"<aside class=\"doc-panel\"><h3>Status</h3><p><span class=\"pill\">{html_escape(payload['status'])}</span></p>"
            f"<p class=\"meta\">sales_motion={html_escape(payload['sales_motion'])} · public_quote_ready={str(payload['public_quote_ready']).lower()}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str((snapshot.get('counts') or {}).get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(quality.get('trusted_funds') or 0))}</b><span>trusted funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(quality.get('quarantined_funds') or 0))}</b><span>quarantined funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(snapshot.get('latest_13f_quarter') or '-')}</b><span>latest 13F</span></div>"
            "</section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Hard Blocks</h2><ul>" + hard + "</ul></section>"
            "<section class=\"panel\"><h2>Disclosures</h2><ul>" + soft + "</ul></section></div>"
            "<h2>Public Checks</h2>"
            f"<table><thead><tr><th>Check</th><th>Status</th><th>Summary</th><th>Evidence</th></tr></thead><tbody>{public_rows}</tbody></table>"
            "<h2>External Operator Checks</h2>"
            f"<table><thead><tr><th>Check</th><th>Status</th><th>Command</th></tr></thead><tbody>{external_rows}</tbody></table>"
            "<div class=\"split\"><section class=\"panel\"><h2>Sell Now</h2><ul>" + sell_now + "</ul></section>"
            "<section class=\"panel\"><h2>Do Not Claim Yet</h2><ul>" + do_not + "</ul></section></div>"
            "<p class=\"lede\"><a class=\"pill\" href=\"/api/commercial-readiness\">Machine-readable readiness</a> "
            "<a class=\"pill\" href=\"/api/product-status\">Product status</a> "
            "<a class=\"pill\" href=\"/security\">Security posture</a> "
            "<a class=\"pill\" href=\"/validation\">Validation boundary</a></p>"
        )
        return _html_response("Commercial Readiness", body)

    @app.get("/security")
    def security_page():
        payload = security_posture_payload()
        public = payload["public_surface"]
        pro = payload["pro_surface"]
        mcp = payload["mcp_surface"]
        quality = payload["data_quality"]
        ops = payload["operations"]
        privacy = payload["privacy"]
        public_controls = "".join(f"<li>{html_escape(item)}</li>" for item in public.get("headers") or [])
        non_claims = "".join(f"<li>{html_escape(item)}</li>" for item in payload.get("non_claims") or [])
        questions = "".join(f"<li>{html_escape(item)}</li>" for item in payload.get("buyer_security_questions") or [])
        evidence = "".join(
            f"<li><a href=\"{html_escape(item['href'], quote=True)}\">{html_escape(item['label'])}</a></li>"
            for item in payload.get("evidence_links") or []
        )
        external = "".join(
            f"<tr><td>{html_escape(item.get('id') or '-')}</td>"
            f"<td><span class=\"pill\">{html_escape(item.get('status') or '-')}</span></td>"
            f"<td><code>{html_escape(item.get('command') or '-')}</code></td></tr>"
            for item in ops.get("external_checks_required") or []
        )
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Security posture</div>"
            "<h1>Controlled Pilot Security</h1>"
            "<p class=\"doc-lede\">Evidence pack for a controlled Pro pilot: implemented controls, operator-side checks and explicit non-claims. "
            "It is designed for security review without exposing secrets, tokens, hashes, IPs, user agents or request bodies.</p></div>"
            f"<aside class=\"doc-panel\"><h3>Status</h3><p><span class=\"pill\">{html_escape(payload['status'])}</span></p>"
            f"<p class=\"meta\">generated={html_escape(payload['generated_at'])}</p>"
            f"<p class=\"meta\">sha={html_escape(payload['git_sha'])}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{str(public.get('synthetic_data')).lower()}</b><span>synthetic data</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(quality.get('trusted_funds') or 0))}</b><span>trusted funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(pro['workspace_limits']['max_watchlists_per_key']))}</b><span>watchlists/key</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(pro['workspace_limits']['max_request_bytes']))}</b><span>max request bytes</span></div>"
            "</section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Public Surface</h2>"
            f"<p><span class=\"pill\">{html_escape(public['mode'])}</span><span class=\"pill\">auth:{html_escape(public['auth_billing_accounts'])}</span></p>"
            f"<p>{html_escape(public['mutation_policy'])}</p><ul>{public_controls}</ul></section>"
            "<section class=\"panel\"><h2>Pro Surface</h2>"
            f"<p><span class=\"pill\">token_in_url:{str(pro['token_in_url_allowed']).lower()}</span><span class=\"pill\">storage:sessionStorage</span></p>"
            f"<p>{html_escape(pro['service_boundary'])}</p><p class=\"meta\">{html_escape(pro['audit'])}</p></section></div>"
            "<div class=\"split\"><section class=\"panel\"><h2>MCP Boundary</h2>"
            f"<p>{html_escape(mcp['host_origin_policy'])}</p><p>{html_escape(mcp['pro_tool_boundary'])}</p></section>"
            "<section class=\"panel\"><h2>Data Quality Gate</h2>"
            f"<p><span class=\"pill\">mode:{html_escape(quality['mode'])}</span><span class=\"pill\">human_review:{str(quality['manual_13f_review_required_for_routine_publication']).lower()}</span></p>"
            f"<p>{html_escape(quality['signal_rule'])}</p></section></div>"
            "<section class=\"doc-section\"><h2>Operator Checks</h2>"
            f"<p>{html_escape(ops['operator_boundary'])}</p>"
            f"<table><thead><tr><th>Check</th><th>Status</th><th>Command</th></tr></thead><tbody>{external}</tbody></table></section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Privacy</h2>"
            f"<p><span class=\"pill\">tokens_echoed:{str(privacy['tokens_echoed']).lower()}</span>"
            f"<span class=\"pill\">secrets_in_payloads:{str(privacy['secrets_in_payloads']).lower()}</span></p>"
            f"<p>{html_escape(privacy['payload_policy'])}</p></section>"
            "<section class=\"panel\"><h2>Non-Claims</h2><ul>" + non_claims + "</ul></section></div>"
            "<div class=\"split\" style=\"margin-top:18px\"><section class=\"panel\"><h2>Buyer Security Questions</h2><ul>" + questions + "</ul></section>"
            "<section class=\"panel\"><h2>Evidence Links</h2><ul>" + evidence + "</ul></section></div>"
            "<p class=\"lede\"><a class=\"pill\" href=\"/api/security-posture\">Machine-readable security posture</a> "
            "<a class=\"pill\" href=\"/readiness\">Commercial readiness</a> "
            "<a class=\"pill\" href=\"/legal/pro-api\">Pro terms</a></p>"
        )
        return _html_response("Security Posture", body)

    @app.get("/pilot")
    def pilot_intake_page():
        payload = pilot_intake_payload()
        privacy = payload["privacy"]
        fields = "".join(
            "<tr>"
            f"<td><code>{html_escape(field.get('id') or '-')}</code></td>"
            f"<td>{html_escape(field.get('label') or '-')}</td>"
            f"<td><span class=\"pill\">required:{str(field.get('required')).lower()}</span></td>"
            f"<td>{html_escape(str(field.get('purpose') or field.get('must_acknowledge') or '-'))}</td>"
            "</tr>"
            for field in payload.get("required_fields") or []
        )
        checks = "".join(f"<li>{html_escape(item)}</li>" for item in payload.get("pre_issue_checks") or [])
        links = "".join(
            f"<li><a href=\"{html_escape(item['href'], quote=True)}\">{html_escape(item['label'])}</a></li>"
            for item in payload.get("evidence_links") or []
        )
        note = "\n".join(str(x) for x in payload.get("operator_note_template") or [])
        packages = "".join(
            f"<span class=\"pill\">{html_escape(str(pkg))}</span>"
            for pkg in payload.get("pilot_packages") or []
        )
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Pilot intake</div>"
            "<h1>Controlled Pilot Intake</h1>"
            "<p class=\"doc-lede\">Qualification pack for issuing a bounded Pro pilot key. "
            "The public site does not submit this data, store buyer PII, collect tokens or create self-serve checkout.</p></div>"
            f"<aside class=\"doc-panel\"><h3>Status</h3><p><span class=\"pill\">{html_escape(payload['status'])}</span></p>"
            f"<p class=\"meta\">public_form_submission={str(payload['public_form_submission']).lower()}</p>"
            f"<p class=\"meta\">server_side_pii_storage={str(privacy['server_side_pii_storage']).lower()}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{str(payload['self_serve_checkout']).lower()}</b><span>self-serve checkout</span></div>"
            f"<div class=\"doc-metric\"><b>{str(payload['public_form_submission']).lower()}</b><span>public submit</span></div>"
            f"<div class=\"doc-metric\"><b>{str(privacy['token_collection']).lower()}</b><span>token collection</span></div>"
            f"<div class=\"doc-metric\"><b>{str(privacy['secret_collection']).lower()}</b><span>secret collection</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Pilot Packages</h2><p>" + packages + "</p></section>"
            "<section class=\"doc-section\"><h2>Required Fields</h2>"
            f"<table><thead><tr><th>ID</th><th>Field</th><th>Required</th><th>Purpose</th></tr></thead><tbody>{fields}</tbody></table></section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Pre-Issue Checks</h2><ul>" + checks + "</ul></section>"
            "<section class=\"panel\"><h2>Privacy Boundary</h2>"
            f"<p><span class=\"pill\">storage:{html_escape(str(privacy['browser_storage']))}</span></p>"
            f"<p>{html_escape(privacy['retention_note'])}</p>"
            f"<p class=\"meta\">recommended_channel={html_escape(privacy['recommended_channel'])}</p></section></div>"
            "<section class=\"doc-section\"><h2>Operator Note Template</h2>"
            f"<pre><code>{html_escape(note)}</code></pre></section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Evidence Links</h2><ul>" + links + "</ul></section>"
            "<section class=\"panel\"><h2>Boundary</h2>"
            "<p>Operator review is required before any key is issued. This page is not a contract, not investment advice and not a public price quote.</p></section></div>"
            "<p class=\"lede\"><a class=\"pill\" href=\"/api/pilot-intake\">Machine-readable pilot intake</a> "
            "<a class=\"pill\" href=\"/api/pilot-intake.md\">Markdown export</a> "
            "<a class=\"pill\" href=\"/buyer-pack\">Buyer pack</a> "
            "<a class=\"pill\" href=\"/security\">Security posture</a></p>"
        )
        return _html_response("Pilot Intake", body)

    @app.get("/pilot/request")
    def pilot_request_page():
        body = """
<style>
.request-app{display:grid;gap:14px}
.request-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;align-items:start}
.request-panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;min-width:0}
.request-panel h2,.request-panel h3{font-size:18px;margin:0 0 10px}
.request-form{display:grid;gap:10px}
.request-form label{display:grid;gap:5px;color:var(--faint);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.request-form input,.request-form select,.request-form textarea{width:100%;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font:inherit;padding:10px 11px;letter-spacing:0}
.request-form textarea{min-height:76px;resize:vertical}
.request-actions{display:flex;gap:8px;flex-wrap:wrap}
.request-button{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font-weight:800;padding:10px 12px;min-height:42px;cursor:pointer}
.request-button.primary{background:var(--accent);border-color:var(--accent);color:#06140f}
.request-status{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);color:var(--muted);padding:10px 12px;font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.request-output{white-space:pre-wrap;overflow:auto;max-height:520px;border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:12px;font-family:var(--mono);font-size:12px;color:var(--muted)}
@media(max-width:900px){.request-grid{grid-template-columns:1fr}}
</style>
<section class="doc-hero"><div class="doc-copy"><div class="kicker">Pilot request</div>
<h1>Assisted Pilot Request</h1>
<p class="doc-lede">Client-side request builder for a controlled Pro pilot. The form does not submit to 13FLOW, store buyer PII, collect tokens or create keys.</p></div>
<aside class="doc-panel"><h3>Boundary</h3><p><span class="pill">server_side_pii_storage:false</span><span class="pill">public_submission_endpoint:none</span><span class="pill">tokens_collected:false</span></p></aside></section>
<main class="request-app" data-pilot-request-app>
  <section class="request-grid">
    <section class="request-panel">
      <h2>Request Details</h2>
      <form id="pilotRequestForm" class="request-form">
        <label>Organization <input name="organization" autocomplete="organization" maxlength="160" required></label>
        <label>Business contact <input name="billing_contact" autocomplete="email" maxlength="200" required></label>
        <label>Security contact <input name="security_contact" autocomplete="email" maxlength="200"></label>
        <label>Workflow <select name="workflow"><option>research desk</option><option>data pipeline</option><option>MCP agent</option><option>monitoring</option><option>internal dashboard</option></select></label>
        <label>Package <select name="package" id="pilotPackageSelect"></select></label>
        <label>Requested scopes <input name="requested_scopes" value="funds:read,quality:read,workspace:write" maxlength="200" required></label>
        <label>Expected volume <input name="expected_volume" placeholder="60/min, 5000/day, no burst automation before approval" maxlength="220" required></label>
        <label>Secure delivery channel <input name="token_delivery_channel" placeholder="operator-approved secure channel" maxlength="220"></label>
        <label>Security requirements <textarea name="security_requirements" placeholder="IP allow-listing, DPA, retention, custom terms, or none"></textarea></label>
        <label>Boundary acknowledgement <textarea name="boundary_ack" required>13FLOW is a research screen, not investment advice.</textarea></label>
        <div class="request-actions"><button class="request-button primary" type="submit">Generate note</button><button id="pilotCopyNote" class="request-button" type="button">Copy note</button></div>
      </form>
    </section>
    <section class="request-panel">
      <h2>Operator Note</h2>
      <div id="pilotRequestStatus" class="request-status">No note generated.</div>
      <pre id="pilotRequestOutput" class="request-output">Fill the form to generate a copyable operator note.</pre>
    </section>
  </section>
  <section class="request-panel"><h2>Privacy</h2><p><span class="pill">no browser storage</span><span class="pill">no public submit</span><span class="pill">admin review required</span></p><p>This page is a formatter. Send the generated note only through the operator-selected channel.</p></section>
</main>
"""
        script = r"""
(() => {
  const app = document.querySelector("[data-pilot-request-app]");
  if (!app) return;
  const $ = (id) => document.getElementById(id);
  const escText = (value) => String(value || "").trim();
  let latestNote = "";
  function setStatus(message, bad=false) {
    const node = $("pilotRequestStatus");
    node.textContent = message;
    node.style.borderColor = bad ? "rgba(239,106,82,.55)" : "var(--line-soft)";
  }
  function formPayload(form) {
    const data = new FormData(form);
    return Object.fromEntries(Array.from(data.entries()).map(([k, v]) => [k, escText(v)]));
  }
  function renderLocalNote(payload) {
    const scopes = payload.requested_scopes || "funds:read,quality:read,workspace:write";
    return [
      "13FLOW ASSISTED PILOT REQUEST",
      `organization: ${payload.organization || "<missing>"}`,
      `billing_contact: ${payload.billing_contact || "<missing>"}`,
      `security_contact: ${payload.security_contact || "<optional>"}`,
      `workflow: ${payload.workflow || "<missing>"}`,
      `package: ${payload.package || "<operator selects package>"}`,
      `requested_scopes: ${scopes}`,
      `expected_volume: ${payload.expected_volume || "<missing>"}`,
      `token_delivery_channel: ${payload.token_delivery_channel || "<secure channel selected by operator>"}`,
      `security_requirements: ${payload.security_requirements || "<none provided>"}`,
      `boundary_ack: ${payload.boundary_ack || "<missing>"}`,
      "operator_decision: <decline | issue bounded pilot key | request more info>",
    ].join("\n");
  }
  async function loadContract() {
    const res = await fetch("/api/pilot-request-assist", {headers: {"Accept": "application/json"}});
    const data = await res.json();
    const select = $("pilotPackageSelect");
    const packages = ((data.input_schema || {}).packages || data.pilot_packages || []);
    const samplePackage = ((data.sample_request || {}).package || "Technical pilot review");
    const values = packages.length ? packages : [samplePackage, "API integration review", "MCP integration review"];
    select.innerHTML = values.map((x) => `<option>${String(x).replace(/[&<>"]/g, "")}</option>`).join("");
  }
  $("pilotRequestForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = formPayload(event.currentTarget);
    latestNote = renderLocalNote(payload);
    $("pilotRequestOutput").textContent = latestNote;
    setStatus("Generated locally. Nothing was submitted to 13FLOW.");
  });
  $("pilotCopyNote").addEventListener("click", async () => {
    if (!latestNote) latestNote = $("pilotRequestOutput").textContent;
    try {
      await navigator.clipboard.writeText(latestNote);
      setStatus("Operator note copied.");
    } catch (_) {
      setStatus("Copy unavailable in this browser; select the note manually.", true);
    }
  });
  loadContract().catch(() => setStatus("Assistant contract unavailable; local form still works.", true));
})();
"""
        return _html_response("Pilot Request", body, script=script)

    @app.get("/buyer-pack")
    def buyer_pack_page():
        payload = buyer_pack_payload()
        snapshot = payload["snapshot"]
        terms = payload["terms_boundary"]
        proof_points = "".join(f"<li>{html_escape(item)}</li>" for item in payload["proof_points"])
        checklist = "".join(f"<li>{html_escape(item)}</li>" for item in payload["buyer_checklist"])
        questions = "".join(f"<li>{html_escape(item)}</li>" for item in payload["qualification_questions"])
        handoff = "".join(f"<li>{html_escape(item)}</li>" for item in payload["pilot_handoff"])
        next_steps = "".join(f"<li>{html_escape(item)}</li>" for item in payload["next_steps"])
        do_not = "".join(f"<li>{html_escape(item)}</li>" for item in payload["do_not_claim_yet"])
        evidence = "".join(
            f"<li><a href=\"{html_escape(item['href'], quote=True)}\">{html_escape(item['label'])}</a></li>"
            for item in payload["evidence_links"]
        )
        packages = "".join(
            "<article class=\"card\">"
            f"<h3>{html_escape(pkg.get('name') or 'Pilot')}</h3>"
            f"<p>{html_escape(pkg.get('term') or 'bounded evaluation')}</p>"
            f"<p><span class=\"pill\">{html_escape(pkg.get('price_eur_per_month') or 'not publicly quoted')}</span></p>"
            f"<p class=\"meta\">{html_escape(pkg.get('sell_when') or '')}</p>"
            "</article>"
            for pkg in payload["pilot_packages"]
        )
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Buyer pack</div>"
            "<h1>13FLOW Buyer Review Pack</h1>"
            f"<p class=\"doc-lede\">{html_escape(payload['one_liner'])}</p></div>"
            f"<aside class=\"doc-panel\"><h3>Status</h3><p><span class=\"pill\">{html_escape(payload['status'])}</span></p>"
            f"<p class=\"meta\">sales_motion={html_escape(payload['sales_motion'])} · public_quote_ready={str(payload['public_quote_ready']).lower()}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('trusted_funds') or 0))}</b><span>trusted funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('latest_13f_quarter') or '-'))}</b><span>latest 13F</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('artifact_tickers') or 0))}</b><span>validation tickers</span></div>"
            "</section>"
            "<div class=\"split\"><section class=\"panel\"><h2>Proof Points</h2><ul>" + proof_points + "</ul></section>"
            "<section class=\"panel\"><h2>Do Not Claim Yet</h2><ul>" + do_not + "</ul></section></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Pilot Package</h2><div class=\"grid\">" + packages + "</div></div>"
            "<div class=\"split\" style=\"margin-top:18px\"><section class=\"panel\"><h2>Buyer Checklist</h2><ul>" + checklist + "</ul></section>"
            "<section class=\"panel\"><h2>Qualification Questions</h2><ul>" + questions + "</ul></section></div>"
            "<div class=\"split\" style=\"margin-top:18px\"><section class=\"panel\"><h2>Pilot Handoff</h2><ul>" + handoff + "</ul></section>"
            "<section class=\"panel\"><h2>Next Steps</h2><ul>" + next_steps + "</ul></section></div>"
            "<div class=\"split\" style=\"margin-top:18px\"><section class=\"panel\"><h2>Evidence Links</h2><ul>" + evidence + "</ul></section>"
            "<section class=\"panel\"><h2>Terms Boundary</h2>"
            f"<p><span class=\"pill\">pricing:{html_escape(str(terms.get('pricing')))}</span></p>"
            f"<p><span class=\"pill\">redistribution:{html_escape(str(terms.get('redistribution')))}</span></p>"
            f"<p><span class=\"pill\">operator_review:{str(terms.get('operator_review_required')).lower()}</span></p>"
            "<p class=\"meta\">This pack is not investment advice, not a performance claim and not a public price quote.</p></section></div>"
            "<p class=\"lede\"><a class=\"pill\" href=\"/api/buyer-pack\">Machine-readable buyer pack</a> "
            "<a class=\"pill\" href=\"/api/buyer-pack.md\">Markdown export</a> "
            "<a class=\"pill\" href=\"/buyer-pack/print\">Printable pack</a> "
            "<a class=\"pill\" href=\"/security\">Security posture</a> "
            "<a class=\"pill\" href=\"/pro/onboarding\">Pro onboarding diagnostic</a> "
            "<a class=\"pill\" href=\"/readiness\">Commercial readiness</a></p>"
        )
        return _html_response("Buyer Review Pack", body)

    @app.get("/buyer-pack/print")
    def buyer_pack_print_page():
        payload = buyer_pack_payload()
        snapshot = payload["snapshot"]
        terms = payload["terms_boundary"]
        def list_items(items) -> str:
            return "".join(f"<li>{html_escape(str(item))}</li>" for item in (items or []))
        evidence = "".join(
            f"<li>{html_escape(item['label'])}: <code>{html_escape(item['href'])}</code></li>"
            for item in payload.get("evidence_links") or []
        )
        packages = "".join(
            "<tr>"
            f"<td>{html_escape(pkg.get('name') or '-')}</td>"
            f"<td>{html_escape(pkg.get('term') or '-')}</td>"
            f"<td>{html_escape(str(pkg.get('price_eur_per_month') or 'not publicly quoted'))}</td>"
            f"<td>{html_escape(pkg.get('sell_when') or '-')}</td>"
            "</tr>"
            for pkg in payload.get("pilot_packages") or []
        )
        body = (
            "<style>@media print{.topnav,.site-footer,.print-actions{display:none!important}.wrap{max-width:none;padding:0}body{background:#fff;color:#111}.doc-section,.doc-panel,.panel{break-inside:avoid;border-color:#bbb;background:#fff;color:#111}.doc-section p,.panel p,.doc-lede,li{color:#222}.pill{border-color:#777;color:#111}.doc-copy h1{font-size:38px}.doc-metrics{grid-template-columns:repeat(4,1fr)}}"
            ".print-cover{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:24px;margin-bottom:14px}.print-cover h1{font-size:46px;margin-bottom:10px}.print-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}</style>"
            "<section class=\"print-cover\"><div class=\"kicker\">Shareable buyer pack</div>"
            "<h1>13FLOW Buyer Review Pack</h1>"
            f"<p class=\"doc-lede\">{html_escape(payload['one_liner'])}</p>"
            f"<p class=\"meta\">generated={html_escape(payload['generated_at'])}; git_sha={html_escape(payload['git_sha'])}</p>"
            "<p class=\"print-actions\"><span class=\"pill cta\">PDF-ready printable view</span>"
            "<a class=\"pill\" href=\"/api/buyer-pack.md\">Markdown export</a>"
            "<a class=\"pill\" href=\"/api/buyer-pack\">JSON contract</a></p></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('trusted_funds') or 0))}</b><span>trusted funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('latest_13f_quarter') or '-'))}</b><span>latest 13F</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(snapshot.get('artifact_tickers') or 0))}</b><span>validation tickers</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Proof Points</h2><ul>"
            + list_items(payload.get("proof_points")) + "</ul></section>"
            "<section class=\"doc-section\"><h2>Pilot Packages</h2>"
            "<table><thead><tr><th>Name</th><th>Term</th><th>Price</th><th>Sell when</th></tr></thead>"
            f"<tbody>{packages}</tbody></table></section>"
            "<div class=\"split\"><section class=\"doc-section\"><h2>Buyer Checklist</h2><ul>"
            + list_items(payload.get("buyer_checklist")) + "</ul></section>"
            "<section class=\"doc-section\"><h2>Qualification Questions</h2><ul>"
            + list_items(payload.get("qualification_questions")) + "</ul></section></div>"
            "<div class=\"split\"><section class=\"doc-section\"><h2>Pilot Handoff</h2><ul>"
            + list_items(payload.get("pilot_handoff")) + "</ul></section>"
            "<section class=\"doc-section\"><h2>Do Not Claim Yet</h2><ul>"
            + list_items(payload.get("do_not_claim_yet")) + "</ul></section></div>"
            "<section class=\"doc-section\"><h2>Evidence Links</h2><ul>"
            f"{evidence}</ul></section>"
            "<section class=\"doc-section\"><h2>Terms Boundary</h2>"
            f"<p><span class=\"pill\">pricing:{html_escape(str(terms.get('pricing')))}</span>"
            f"<span class=\"pill\">redistribution:{html_escape(str(terms.get('redistribution')))}</span>"
            f"<span class=\"pill\">investment_advice:{str(terms.get('investment_advice')).lower()}</span>"
            f"<span class=\"pill\">sla:{str(terms.get('managed_service_sla')).lower()}</span></p>"
            "<p class=\"callout\"><strong>Boundary:</strong> This pack is not investment advice, not a performance claim and not a public price quote.</p>"
            "</section>"
        )
        return _html_response("Printable Buyer Pack", body)

    @app.get("/validation")
    def validation_page():
        product = product_status_payload()
        validation = product["validation"]
        artifact = validation["current_artifact"]
        metrics = validation["metrics_snapshot"]

        def pct(value: object) -> str:
            try:
                return f"{float(value) * 100:.1f}%"
            except (TypeError, ValueError):
                return "-"

        proof_rows = [
            ("Artifact status", artifact["schema_status"]),
            ("Evidence review", artifact["evidence_review_status"]),
            ("Metrics status", artifact["metrics_status"]),
            ("Feature scope", artifact["feature_scope"]),
            ("Rows", str(artifact["row_count"])),
            ("Tickers", str(artifact["ticker_count"])),
            ("Row errors", str(artifact["row_error_count"])),
            ("Rows with Form 4 accessions", str(artifact["rows_with_form4_accessions"])),
            ("Rows with open-market buyers", str(artifact["rows_with_open_market_buyers"])),
            ("Tickers with open-market buyers", str(artifact["tickers_with_open_market_buyers"])),
            ("20d forward-return coverage", pct(artifact["forward_return_coverage"]["forward_return_20d"])),
            ("60d forward-return coverage", pct(artifact["forward_return_coverage"]["forward_return_60d"])),
            ("120d forward-return coverage", pct(artifact["forward_return_coverage"]["forward_return_120d"])),
        ]
        proof_html = "".join(
            f"<tr><td>{html_escape(k)}</td><td><code>{html_escape(v)}</code></td></tr>"
            for k, v in proof_rows
        )

        metric_rows = [
            ("Split", metrics["split"]),
            ("Model", metrics["model"]),
            ("Horizon", f"{metrics['horizon_days']} trading days"),
            ("n", str(metrics["n"])),
            ("Rank IC", str(metrics["rank_ic"])),
            ("Permutation p-value", str(metrics["rank_ic_permutation_p"])),
            ("Top-bottom spread", str(metrics["top_bottom_spread"])),
            ("Top-bottom spread CI95", f"{metrics['top_bottom_spread_ci95'][0]} -> {metrics['top_bottom_spread_ci95'][1]}"),
            ("Hit rate", pct(metrics["hit_rate"])),
            ("Mean forward return", pct(metrics["mean_forward_return"])),
        ]
        metrics_html = "".join(
            f"<tr><td>{html_escape(k)}</td><td><code>{html_escape(v)}</code></td></tr>"
            for k, v in metric_rows
        )

        proves = [
            "The local feature table is mechanically schema-valid.",
            "The 25-ticker artifact joins 13F and reviewed Form 4 accessions.",
            "Forward returns are complete for the 20d, 60d and 120d horizons on the mature window.",
            "The artifact is ready for human methodology review.",
        ]
        does_not = [
            "It does not prove validated alpha.",
            "It does not provide a probability, price target or expected-return model.",
            "It does not cover the full historical universe.",
            "It does not complete price-source, delisting, liquidity, costs or no-lookahead review.",
        ]
        proves_html = "".join(f"<li>{html_escape(item)}</li>" for item in proves)
        does_not_html = "".join(f"<li>{html_escape(item)}</li>" for item in does_not)
        sources = [
            ("/api/product-status", "Machine-readable validation boundary"),
            ("/api/methodology/app", "Application methodology contract"),
            ("/api/methodology/confluence-v1", "Frozen Confluence v1 contract"),
            ("/status", "Deployment and runtime proof"),
            ("/pro", "Commercial access boundary"),
        ]
        source_html = "".join(
            f"<tr><td><a href=\"{html_escape(path)}\"><code>{html_escape(path)}</code></a></td>"
            f"<td>{html_escape(desc)}</td></tr>"
            for path, desc in sources
        )

        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Evidence, not hype</div>"
            "<h1>Validation</h1>"
            "<p class=\"doc-lede\">Current Confluence evidence pack for 13FLOW. "
            "This page is intentionally conservative: it separates mechanical dataset readiness "
            "from any alpha or investment-performance claim, so a buyer can see exactly what is ready "
            "and what still needs review.</p>"
            "<div class=\"actions\"><a class=\"pill cta\" href=\"/api/product-status\">Product status JSON</a>"
            "<a class=\"pill\" href=\"/api/methodology/confluence-v1\">Confluence v1 contract</a>"
            "<a class=\"pill\" href=\"/methodology/app\">Methodology</a></div></div>"
            "<aside class=\"doc-panel\"><h3>Current status</h3>"
            f"<p><span class=\"pill\">{html_escape(validation['status'])}</span></p>"
            f"<p>{html_escape(validation['blocked_by'])}</p>"
            f"<p>Public validation claim: <code>{str(artifact['public_validation_claim']).lower()}</code></p>"
            f"<p>Publishable as full validation: <code>{str(artifact['publishable_as_full_validation']).lower()}</code></p>"
            f"<p class=\"meta\">score_claim={html_escape(validation['score_claim'])}</p></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(artifact['row_count']))}</b><span>feature rows</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(artifact['ticker_count']))}</b><span>tickers</span></div>"
            f"<div class=\"doc-metric\"><b>{pct(artifact['forward_return_coverage']['forward_return_60d'])}</b><span>60d return coverage</span></div>"
            f"<div class=\"doc-metric\"><b>{pct(metrics['hit_rate'])}</b><span>60d hit rate</span></div>"
            "</section>"
            "<section class=\"runbook\">"
            "<div class=\"runstep\"><b>01 · Scope</b><span>25 liquid tickers with mature 13F plus Form 4 evidence.</span></div>"
            "<div class=\"runstep\"><b>02 · Gate</b><span>Schema and row checks pass before any public claim.</span></div>"
            "<div class=\"runstep\"><b>03 · Metrics</b><span>Descriptive results are weak or neutral, not promotional.</span></div>"
            "<div class=\"runstep\"><b>04 · Next</b><span>Full-universe adjusted-price review remains the required artifact.</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Mechanical Evidence</h2>"
            "<p class=\"callout\"><strong>Readable takeaway:</strong> the evidence pack is structurally ready for review, "
            "but it is not yet a published validation study.</p>"
            f"<table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>{proof_html}</tbody></table></section>"
            "<section class=\"doc-section\"><h2>Descriptive Metrics</h2>"
            "<p class=\"lede\">These 60-day metrics are weak or neutral. They are useful as a "
            "review checkpoint, not as a performance promise.</p>"
            f"<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>{metrics_html}</tbody></table>"
            f"<p class=\"meta\">{html_escape(metrics['interpretation'])}</p></section>"
            "<section class=\"split\">"
            "<div class=\"doc-section\"><h2>What this proves</h2><ul>" + proves_html + "</ul></div>"
            "<div class=\"doc-section\"><h2>What this does not prove</h2><ul>" + does_not_html + "</ul></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>How to use this page</h2>"
            "<div class=\"mini-list\">"
            "<div><b>For analysts:</b> use the validation boundary to decide how much weight to give a Confluence signal before reading the filings.</div>"
            "<div><b>For buyers:</b> use the status, artifact hashes and source links as due-diligence evidence before requesting Pro access.</div>"
            "<div><b>For operators:</b> do not market output as validated alpha until the next artifact covers broader history, adjusted prices and no-lookahead controls.</div>"
            "</div></section>"
            "<section class=\"doc-section\"><h2>Verification Links</h2>"
            f"<table><thead><tr><th>Surface</th><th>Use</th></tr></thead><tbody>{source_html}</tbody></table></section>"
        )
        return _html_response("Validation", body)

    def pro_offer_payload() -> dict:
        return _cached_public_payload("pro_offer", _pro_offer_payload)

    def _pro_offer_payload() -> dict:
        status = product_status_payload()
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "offer": {
                "name": "13FLOW Pro API",
                "positioning": (
                    "Institutional read-only API and MCP access over SEC EDGAR-derived "
                    "13F data, methodology contracts, data-quality warnings and signal "
                    "history."
                ),
                "audience": [
                    "family offices",
                    "asset managers",
                    "research desks",
                    "data teams",
                    "automated agent workflows",
                ],
                "access_model": "operator_issued_api_key",
                "self_serve_checkout": False,
                "human_page": "/pro",
                "runbook": "operator_cli_and_admin_readiness_gate",
                "contact": {
                    "email": "admin@toonux.com",
                    "mailto": (
                        "mailto:admin@toonux.com?subject=13FLOW%20Pro%20API%20access"
                    ),
                    "expected_response": "operator review before any token is issued",
                },
            },
            "core_v1_boundary": core_v1_boundary_payload(),
            "plans": [
                {
                    "name": "Technical pilot review",
                    "fit": "one bounded evaluator checking whether 13FLOW fits a real workflow",
                    "commercial_model": "not publicly priced",
                    "includes": [
                        "one scoped API key",
                        "conservative default limits",
                        "status, funds, bounded fund detail and data-quality endpoints",
                        "bounded first probes with operator verification",
                    ],
                    "success_criteria": [
                        "status and funds probes pass",
                        "one bounded fund detail is ingested client-side",
                        "client accepts current validation boundary",
                    ],
                },
                {
                    "name": "API integration review",
                    "fit": "internal dashboard, notebook or data pipeline evaluation after the first pilot probes",
                    "commercial_model": "not publicly priced",
                    "includes": [
                        "institution-labelled API key",
                        "documented scopes, limits and rotation policy",
                        "request audit trail",
                        "data-quality warnings surfaced as first-class output",
                    ],
                    "success_criteria": [
                        "client workflow handles pagination and bounded payloads",
                        "audit rows are verified after first integration",
                        "rotation date is documented",
                    ],
                },
                {
                    "name": "MCP integration review",
                    "fit": "agent workflow evaluation where Pro tools must fail closed without a key",
                    "commercial_model": "not publicly priced",
                    "includes": [
                        "MCP product-status and Pro tool probes",
                        "fail-closed behavior without a valid Pro key",
                        "read-only access pattern suitable for agent workflows",
                    ],
                    "success_criteria": [
                        "MCP public tools respond",
                        "Pro MCP tool succeeds with a valid key",
                        "Pro MCP tool fails closed without credential",
                    ],
                },
            ],
            "buyer_checklist": [
                "organization name and billing contact",
                "intended workflow: research desk, data pipeline, MCP agent, monitoring",
                "required scopes and expected request volume",
                "preferred token delivery channel",
                "expiry, rotation and revocation expectations",
                "confirmation that 13FLOW is a research screen, not investment advice",
            ],
            "commercial_model": {
                "pricing_currency": "not_publicly_quoted",
                "pricing_status": "paused_until_terms_and_capacity_are_ready",
                "principle": (
                    "Do not publish package pricing yet. 13FLOW is an operator-reviewed, "
                    "limited-capacity research service; sell only a bounded technical "
                    "pilot after the buyer accepts the validation, support and redistribution "
                    "boundaries. Do not position it as cheap raw SEC data."
                ),
                "ideal_customer_profiles": [
                    {
                        "name": "research desk",
                        "pain": "manual 13F checks, spreadsheet reconciliation and source-link verification",
                        "buyer": "small asset manager, family office, independent research desk",
                    },
                    {
                        "name": "data pipeline owner",
                        "pain": "needs a stable, bounded API over 13F portfolios with quality warnings",
                        "buyer": "data team inside a fund, advisory shop or analytics vendor",
                    },
                    {
                        "name": "agent workflow builder",
                        "pain": "needs MCP-accessible, read-only institutional ownership context with fail-closed Pro tools",
                        "buyer": "AI/automation team building internal research agents",
                    },
                ],
                "recommended_packages": [
                    {
                        "name": "Reviewed technical pilot",
                        "price_eur_per_month": "not publicly quoted",
                        "term": "short, bounded evaluation only",
                        "included_keys": 1,
                        "included_limits": {"per_min": 120, "per_day": 10000},
                        "support": "best-effort operator availability; no SLA",
                        "sell_when": "a serious evaluator has a bounded workflow and accepts the no-alpha/no-SLA boundary",
                    },
                ],
                "do_not_discount_below": {
                    "full_live_api_access_eur_per_month": None,
                    "reason": "public pricing is paused; quote nothing until pilot terms, capacity and support boundaries are explicit",
                },
                "pricing_policy": {
                    "strategy": "bounded_operator_review_before_any_quote",
                    "do_not_compete_on": [
                        "generic SEC filing download volume",
                        "self-serve retail portfolio widgets",
                        "unvalidated alpha claims",
                    ],
                    "compete_on": [
                        "13F plus Form 4 confluence workflow",
                        "source-linked methodology and validation boundary",
                        "operator-issued keys with audit, limits and fail-closed MCP tools",
                        "evidence pack suitable for a professional buyer review",
                    ],
                    "discount_rule": "do not negotiate public packages; reduce to a smaller technical pilot or decline",
                },
                "market_context": [
                    {
                        "category": "official_source",
                        "provider": "SEC EDGAR",
                        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
                        "observed_offer": "official EDGAR JSON APIs and nightly bulk files",
                        "risk_if_competing_directly": "free official source makes raw filing resale indefensible",
                        "thirteenflow_response": "sell normalized workflow, quality warnings, status evidence and support around the official data",
                    },
                    {
                        "category": "generic_sec_api_vendor",
                        "provider": "unnamed third-party SEC API vendor",
                        "source_url": "",
                        "observed_offer": "broad SEC API suite with self-serve API access",
                        "risk_if_competing_directly": "a generic 13F or Form 4 endpoint would be compared against mature API vendors",
                        "thirteenflow_response": "position as a narrower research product: 13F, Form 4 validation, Confluence boundary, MCP and audit-ready onboarding",
                    },
                    {
                        "category": "generic_alternative_data_platform",
                        "provider": "unnamed alternative-data platform",
                        "source_url": "",
                        "observed_offer": "alternative-data APIs and retail-facing research surfaces",
                        "risk_if_competing_directly": "broad alternative-data UX is hard to beat with a narrower raw-data catalogue",
                        "thirteenflow_response": "stay professional and evidence-first: fewer claims, stronger method boundary, scoped Pro API and verifiable MCP behavior",
                    },
                    {
                        "category": "generic_free_curated_portfolio_site",
                        "provider": "unnamed free curated portfolio site",
                        "source_url": "",
                        "observed_offer": "free curated investor portfolios and insider-buy screens",
                        "risk_if_competing_directly": "free curation absorbs casual retail interest",
                        "thirteenflow_response": "avoid retail checkout; sell machine-readable proof, API access, auditability and buyer-specific workflows",
                    },
                ],
                "qualification_filter": {
                    "good_fit": [
                        "professional buyer with a repeatable 13F research workflow",
                        "needs API or MCP access rather than screenshots",
                        "accepts the current no-alpha validation boundary",
                        "values audit trail, source links and methodology stability",
                    ],
                    "bad_fit": [
                        "wants cheap raw SEC access only",
                        "requires a public self-serve checkout today",
                        "expects investment advice, price targets or validated alpha",
                        "needs redistribution without a custom contract",
                    ],
                },
                "evidence_pack": [
                    "/validation",
                    "/status",
                    "/api/product-status",
                    "/api/live-status",
                    "/api/pro-offer",
                    "/api/openapi.json",
                    "/api/pro/v1/openapi.json",
                    "/api/methodology/confluence-v1",
                ],
            },
            "sales_packet": {
                "qualification_questions": [
                    "Which desk, product or automated workflow will consume 13FLOW?",
                    "Which 13F managers, tickers or watchlists matter first?",
                    "Is the first use case human research, internal dashboarding, or agent/MCP automation?",
                    "What request volume do you expect during pilot and production use?",
                    "Who owns security review, token custody and rotation?",
                    "Do you need bounded fund detail only, or full data-quality metadata as well?",
                ],
                "lead_reply_template": (
                    "Thanks for the 13FLOW Pro API request.\n\n"
                    "Before I issue a scoped pilot key, please confirm:\n"
                    "- Organization / billing contact:\n"
                    "- Workflow: research desk, data pipeline, MCP agent, monitoring, or other\n"
                    "- Priority funds, tickers or watchlists:\n"
                    "- Expected request volume during pilot:\n"
                    "- Required scopes: funds:read, quality:read, or both\n"
                    "- Preferred secure token delivery channel:\n"
                    "- Rotation / expiry expectation:\n"
                    "- You accept the current validation boundary: no validated alpha, probability, or expected-return claim yet\n"
                ),
                "operator_note_schema": {
                    "organization": "",
                    "contact": "",
                    "package": "Technical pilot review | API integration review | MCP integration review",
                    "workflow": "",
                    "scopes": ["funds:read", "quality:read"],
                    "rate_limits": {"per_min": 120, "per_day": 10000},
                    "token_delivery_channel": "",
                    "expiry_or_rotation_date": "",
                    "key_id": "",
                    "first_probe_status": "pending",
                    "audit_verified_at": "",
                    "boundary_acknowledged": False,
                },
                "pilot_handoff": [
                    "Send the Pro OpenAPI URL and three curl probes.",
                    "Ask the buyer to run status, funds and one bounded fund-detail call.",
                    "Confirm the buyer can parse truncation counters and data-quality warnings.",
                    "Verify the key id in api_audit after the first successful calls.",
                    "Document the rotation date before moving from pilot to recurring use.",
                ],
            },
            "included": [
                {
                    "capability": "Pro API",
                    "details": [
                        "API-key authentication by Authorization Bearer or X-13FLOW-Key",
                        "scopes: funds:read and quality:read",
                        "per-key rate limits and request audit",
                        "bounded payload controls on fund detail endpoints",
                    ],
                },
                {
                    "capability": "MCP",
                    "details": [
                        "read-only public tools",
                        "Pro tools gated by Pro API key",
                        "x402 path implemented but disabled until production payment details are configured",
                    ],
                },
                {
                    "capability": "Data quality and methodology",
                    "details": [
                        "read-only quality warnings, never silent corrections",
                        "frozen Confluence v1 methodology contract",
                        "append-only signal history for revisions",
                    ],
                },
                {
                    "capability": "Alerts",
                    "details": [
                        "filing-diff alert engine implemented",
                        "operator runbook and channel configuration required before managed service use",
                    ],
                },
            ],
            "not_included_yet": status["offer_boundary"]["do_not_claim_yet"],
            "default_limits": {
                "rate_per_min": 120,
                "rate_per_day": 10000,
                "max_positions_per_fund_detail": 1000,
                "max_moves_per_fund_detail": 2000,
            },
            "onboarding": [
                "Buyer sends the access request with organization, workflow, scopes and expected volume.",
                "Operator confirms fit, validation boundary, limits, expiry and secure token channel.",
                "Operator creates one API key per institution or internal service.",
                "Plaintext token is delivered once through an out-of-band secure channel.",
                "Buyer runs status, funds and bounded fund-detail probes.",
                "Operator verifies recent audit rows and documents the key id.",
                "Rotation date is scheduled and bootstrap/internal QA keys are revoked when no longer needed.",
            ],
            "operator_commands": {
                "create_key": (
                    "sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--create-api-key \"Client Label\" "
                    "--pro-db /var/lib/13flow-pro/13flow-pro.db "
                    "--api-key-scopes funds:read,quality:read "
                    "--api-key-rate-per-min 120 --api-key-rate-per-day 10000 "
                    "--api-key-rotation-days 90"
                ),
                "list_keys": (
                    "sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--list-api-keys --pro-db /var/lib/13flow-pro/13flow-pro.db"
                ),
                "revoke_key": (
                    "sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py "
                    "--revoke-api-key <key_id> --pro-db /var/lib/13flow-pro/13flow-pro.db"
                ),
                "preflight": (
                    "sudo -E /opt/13flow/.venv/bin/python /opt/13flow/run.py --preflight "
                    "--db /var/lib/13flow/13flow.db --pro-db /var/lib/13flow-pro/13flow-pro.db "
                    "--require-pro --expected-sha <sha>"
                ),
            },
            "client_probes": {
                "status": "curl -H \"Authorization: Bearer $TOKEN\" https://13flow.eu/api/pro/v1/status",
                "funds": "curl -H \"Authorization: Bearer $TOKEN\" https://13flow.eu/api/pro/v1/funds",
                "fund_detail_bounded": (
                    "curl -H \"Authorization: Bearer $TOKEN\" "
                    "\"https://13flow.eu/api/pro/v1/fund/0001067983?include_holds=0&limit_positions=20&limit_moves=50\""
                ),
                "mcp_product_status": (
                    "curl -fsS https://13flow.eu/api/mcp -H 'Content-Type: application/json' "
                    "-H 'Accept: application/json, text/event-stream' "
                    "--data '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\","
                    "\"params\":{\"name\":\"get_product_status\",\"arguments\":{}}}'"
                ),
            },
            "security": {
                "token_storage": "plaintext token shown once; server-bound token hash stored at rest in production",
                "audit": "one api_audit row per accepted, denied or rate-limited Pro request",
                "cache": "Pro responses are private/no-store and vary by credential header",
                "service_split": "public web service has no Pro DB write path",
            },
            "truth_boundary": status["validation"],
        }

    @app.get("/api/pro-offer")
    def pro_offer_ep():
        return jsonify(pro_offer_payload())

    # ---- dashboard ------------------------------------------------------
    def _dashboard_live_status() -> dict[str, str]:
        status = live_status_payload()
        counts = status["counts"]
        quality = status["quality_summary"]
        latest_s = status["latest_13f_quarter"] or "n/a"
        coverage = status.get("coverage") or {}
        cov = coverage.get("overall_value_share")
        cov_s = f"{cov * 100:.1f}%" if isinstance(cov, (int, float)) else "n/a"
        if status["public_state"] == "LIVE":
            src_text = "LIVE · EDGAR"
            prefix = "Live data status: LIVE EDGAR."
        elif status["public_state"] == "DEMO":
            src_text = "DEMO SAMPLE"
            prefix = "Live data status: DEMO SAMPLE."
        else:
            src_text = "DEGRADED"
            prefix = "Live data status: DEGRADED."
        detail = (
            f"State: {status['public_state']} · Source: {status['source']} · "
            f"{counts['funds']} funds · {counts['filings']} filings · "
            f"{counts['latest_filings']} latest rows · latest 13F {latest_s} · "
            f"coverage {cov_s} · commit {status['git_sha'][:12]}"
        )
        crawler = (
            f"{prefix} /api/version SHA {status['git_sha']}; generated_at={status['generated_at']}; "
            f"data_as_of={status.get('data_as_of') or 'n/a'}; "
            f"period_13f={status['period_13f']['from']}..{status['period_13f']['to']}; "
            f"uses_synthetic_data={str(status['uses_synthetic_data']).lower()}; "
            f"coverage={cov_s}; latest_accessions={status['accessions']['latest_count']}; "
            f"/api/funds serves {counts['funds']} funds; latest 13F quarter {latest_s}; "
            f"data quality has {quality['aum_jump_warnings']} AUM warnings and "
            f"{quality['unit_scale_candidates']} unit-scale candidates."
        )
        return {
            "src_text": src_text,
            "src_detail": detail,
            "crawler": crawler,
        }

    def _inject_dashboard_live_status(markup: str) -> str:
        try:
            status = _dashboard_live_status()
            is_live = True
        except Exception as e:  # noqa: BLE001
            app.logger.warning("dashboard live-status injection failed: %s", e)
            is_live = False
            status = {
                "src_text": "VERIFYING DATA",
                "src_detail": "Source: SEC EDGAR · public API verification pending",
                "crawler": (
                    "Live data status pending. Public verification endpoints: "
                    "/api/version, /api/funds, /api/data-quality."
                ),
            }
        if is_live:
            markup = markup.replace(
                'id="srcBadge" class="badge"',
                'id="srcBadge" class="badge live"',
            )
        markup = markup.replace(
            '<span id="srcText">VERIFYING DATA</span>',
            f'<span id="srcText">{html_escape(status["src_text"])}</span>',
        )
        markup = markup.replace(
            '<div id="srcDetail" style="margin-top:10px">Source: SEC EDGAR · public API verification pending</div>',
            f'<div id="srcDetail" style="margin-top:10px">{html_escape(status["src_detail"])}</div>',
        )
        markup = markup.replace(
            '<div id="crawlerState" class="crawler-state">Live data status pending. Public verification endpoints: /api/version, /api/funds, /api/data-quality.</div>',
            f'<div id="crawlerState" class="crawler-state">{html_escape(status["crawler"])}</div>',
        )
        if open_mode:
            markup = _strip_open_build_dashboard(markup)
        return markup

    def _strip_open_build_dashboard(markup: str) -> str:
        """Keep crawler-visible open builds free of auth, checkout, and alert upsell UI."""
        markup = re.sub(
            r'\s*<div class="nav-item" data-view="alerts"><span class="ico">!</span> Alerts</div>',
            "",
            markup,
        )
        markup = re.sub(
            r'\s*<div class="modal" id="authModal">.*?</div>\s*<div class="modal" id="upgradeModal">',
            '\n<div class="modal" id="upgradeModal">',
            markup,
            flags=re.S,
        )
        markup = re.sub(
            r'\s*<div class="modal" id="upgradeModal">.*?</div>\s*<div id="toast"',
            '\n<div id="toast"',
            markup,
            flags=re.S,
        )
        for old, new in (
            ('data-view="alerts"', 'data-view="open-disabled"'),
            ("Sign in", "Open build"),
            ("Upgrade to Pro", "API access"),
            ("Continue to checkout", "Checkout disabled"),
            ("€12", "Pro API"),
        ):
            markup = markup.replace(old, new)
        return markup

    def _serve_html(path):
        # Serve a local HTML file with a strict, per-request nonce CSP. The page's single
        # inline <script> gets the nonce; inline event-handler attributes are NOT used, so
        # script-src needs no 'unsafe-inline'. (Inline style attributes remain, hence
        # style-src 'unsafe-inline' — a far weaker allowance than for scripts.)
        if not os.path.exists(path):
            return Response("not found", status=404)
        nonce = secrets.token_urlsafe(16)
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        if os.path.abspath(path) == os.path.abspath(dash):
            html = _inject_dashboard_live_status(html)
        html = html.replace("<script>", f'<script nonce="{nonce}">')
        resp = Response(html, mimetype="text/html; charset=utf-8")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return resp

    def _html_response(title: str, body: str, script: str = "") -> Response:
        nonce = secrets.token_urlsafe(16)
        nav = (
            '<nav class="topnav"><a class="brand" href="/"><svg width="34" height="34" viewBox="0 0 64 64" fill="none" aria-hidden="true">'
            '<path d="M7 15 C 22 15, 22 32, 37 32" stroke="#19c187" stroke-width="7.5" stroke-linecap="round"/>'
            '<path d="M7 49 C 22 49, 22 32, 37 32" stroke="#e0a534" stroke-width="7.5" stroke-linecap="round"/>'
            '<path d="M34 32 L 57 32" stroke="#19c187" stroke-width="8.5" stroke-linecap="round"/>'
            '<circle cx="35" cy="32" r="6.4" fill="#0c1611"/><circle cx="35" cy="32" r="2.7" fill="#fff"/></svg>'
            '<span class="wm">13<span>FL</span><b>OW</b></span></a>'
            '<div class="navlinks"><a class="primary" href="/app">Cockpit</a>'
            '<a href="/signals">Signals</a><a href="/funds">Funds</a><a href="/stocks">Stocks</a>'
            '<a href="/developers">API</a><a href="/pro">Pro</a></div></nav>'
        )
        footer = (
            '<footer class="site-footer"><div class="foot-grid">'
            '<div><h4>13FLOW</h4><p>SEC EDGAR-derived 13F and Form 4 research surfaces '
            'for analysts, APIs and agent workflows.</p></div>'
            '<div><h4>Product</h4><a href="/app">Cockpit</a><a href="/signals">Signals</a>'
            '<a href="/funds">Funds</a><a href="/stocks">Stocks</a><a href="/pro">Pro API</a>'
            '<a href="/developers">API docs</a></div>'
            '<div><h4>Trust</h4><a href="/status">Status</a><a href="/coverage">Coverage</a>'
            '<a href="/validation">Validation</a><a href="/security">Security</a>'
            '<a href="/methodology">Methodology</a><a href="/methodology/app">Application method</a>'
            '<a href="/methodology/mcp">MCP method</a></div>'
            '<div><h4>Company</h4><a href="/pilot">Pilot intake</a><a href="/buyer-pack">Buyer pack</a>'
            '<a href="/about">About</a><a href="/faq">FAQ</a><a href="/legal">Legal</a>'
            '<a href="/legal/pro-api">Pro terms</a></div>'
            '</div><div class="fine"><span>Public filings research. Not investment advice.</span>'
            '<span>Built by <a href="https://l0g.fr/" rel="noopener">l0g</a> · Source: SEC EDGAR · LIVE state exposed at /api/live-status</span></div></footer>'
        )
        script_tag = (
            f'<script nonce="{nonce}">{script}</script>'
            if script
            else f'<script nonce="{nonce}"></script>'
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(title)} · 13FLOW</title><link href="/assets/fonts/13flow-fonts.css" rel="stylesheet">
<style>
:root{{--bg:#0c1611;--panel:#13241c;--panel-2:#16291f;--panel-3:#101f18;--line:#1f3329;--line-soft:#182a20;--text:#eaf5ef;--muted:#a9c4b7;--faint:#6f897d;--accent:#19c187;--amber:#e0a534;--danger:#ef6a52;--sans:'Hanken Grotesk',system-ui,sans-serif;--display:'Bricolage Grotesque',system-ui,sans-serif;--mono:'Geist Mono',ui-monospace,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55;letter-spacing:0;background-image:linear-gradient(180deg,rgba(255,255,255,.025),transparent 420px)}}a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1180px;margin:0 auto;padding:22px 24px 0}}.topnav{{position:sticky;top:0;z-index:10;display:flex;gap:18px;align-items:center;margin:0 -24px 34px;padding:14px 24px;border-bottom:1px solid var(--line);background:rgba(12,22,17,.92);backdrop-filter:blur(14px)}}.navlinks{{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin-left:auto}}.navlinks a{{color:var(--muted);font-weight:650;font-size:13px;padding:7px 10px;border-radius:8px}}.navlinks a:hover{{color:var(--text);background:var(--panel-2)}}.navlinks a.primary{{color:#06140f;background:var(--accent)}}.brand{{font-family:var(--display);font-size:24px;font-weight:800;color:var(--text);margin-right:auto;letter-spacing:0}}.brand span{{color:var(--accent)}}.brand b{{color:var(--amber)}}h1{{font-family:var(--display);font-size:44px;line-height:1.02;margin:0 0 10px;letter-spacing:0}}h2,h3{{font-family:var(--display);letter-spacing:0}}.lede{{color:var(--muted);max-width:780px;margin:0 0 24px;font-size:16px}}.hero{{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);gap:18px;align-items:stretch;margin-bottom:18px}}.hero .panel{{min-height:100%}}.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}}.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px}}.card{{display:block;color:var(--text)}}.card:hover{{border-color:var(--accent);background:var(--panel-2)}}.card h2,.card h3{{margin:0 0 6px}}.card p,.panel p,.panel li{{color:var(--muted)}}.card a,.panel a,code{{overflow-wrap:anywhere}}.panel,.meta{{overflow-wrap:anywhere;word-break:break-word}}.meta,.num{{font-family:var(--mono)}}.meta{{font-size:12px;color:var(--faint)}}.num{{font-size:13px}}pre{{white-space:pre-wrap;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto}}code{{font-family:var(--mono)}}table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}th:first-child,td:first-child{{text-align:left}}th{{font-family:var(--mono);font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}}td{{font-size:14px}}.pill{{display:inline-block;max-width:100%;border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-family:var(--mono);font-size:11px;color:var(--muted);margin:2px 5px 2px 0;overflow-wrap:anywhere;word-break:break-word;white-space:normal}}a.pill,.pill.cta{{color:#06140f;background:var(--accent);border-color:var(--accent);font-weight:700}}.sec{{font-family:var(--mono);font-size:11px}}.status-strip{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:18px}}.status-strip div{{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);padding:12px}}.home-hero{{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(360px,.95fr);gap:26px;align-items:center;margin:8px 0 20px;min-height:520px}}.home-copy{{padding:20px 0 28px}}.home-eyebrow{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}}.home-eyebrow span{{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);background:var(--panel-2);border-radius:8px;padding:6px 9px}}.home-copy h1,.home-title{{font-family:var(--display);font-size:72px;line-height:.92;margin:0 0 18px;letter-spacing:0;max-width:760px;overflow-wrap:anywhere}}.home-title .mark{{color:var(--accent)}}.home-lede{{font-size:20px;line-height:1.48;color:var(--muted);max-width:660px;margin:0}}.home-proof{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:26px;max-width:720px}}.proof-item{{border-top:1px solid var(--line);padding-top:11px}}.proof-item b{{display:block;font-family:var(--mono);font-size:22px;line-height:1.1;color:var(--text)}}.proof-item span{{display:block;font-size:12px;color:var(--faint);margin-top:4px}}.home-actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:28px}}.home-actions .button{{display:inline-flex;align-items:center;justify-content:center;min-height:40px;border-radius:8px;padding:10px 14px;font-weight:800;color:#06140f;background:var(--accent);border:1px solid var(--accent)}}.home-actions .button.secondary{{background:transparent;color:var(--text);border-color:var(--line)}}.home-actions .button.secondary:hover{{border-color:var(--accent);color:var(--accent)}}.cockpit-shot{{border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,var(--panel),var(--panel-3));box-shadow:0 28px 70px -34px rgba(0,0,0,.85);overflow:hidden}}.shot-top{{display:flex;justify-content:space-between;gap:12px;align-items:center;border-bottom:1px solid var(--line);padding:14px 16px}}.shot-title{{font-family:var(--display);font-weight:800;font-size:17px}}.shot-live{{font-family:var(--mono);font-size:10px;color:var(--accent);border:1px solid rgba(25,193,135,.32);border-radius:8px;padding:5px 8px;background:rgba(25,193,135,.08)}}.shot-grid{{display:grid;grid-template-columns:1.1fr .9fr;gap:1px;background:var(--line)}}.quadrant{{background:var(--panel);padding:18px;min-height:284px;position:relative}}.axis{{position:absolute;font-family:var(--mono);font-size:10px;color:var(--faint)}}.axis.x{{bottom:12px;left:18px;right:18px;display:flex;justify-content:space-between}}.axis.y{{top:18px;right:16px}}.bubble{{position:absolute;width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font-family:var(--mono);font-size:11px;font-weight:800;color:#06140f;background:var(--accent);box-shadow:0 0 0 8px rgba(25,193,135,.08)}}.bubble.b2{{width:42px;height:42px;left:54%;top:24%;background:var(--amber)}}.bubble.b1{{left:66%;top:44%}}.bubble.b3{{width:36px;height:36px;left:31%;top:54%;background:#7fb89d;color:#081310}}.watchlist{{background:var(--panel);padding:16px}}.watch-row{{display:grid;grid-template-columns:52px 1fr auto;gap:10px;align-items:center;border-bottom:1px solid var(--line-soft);padding:10px 0}}.watch-row:last-child{{border-bottom:0}}.watch-row b{{font-family:var(--mono);font-size:12px;color:var(--accent)}}.watch-row span{{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.watch-row i{{font-family:var(--mono);font-style:normal;font-size:11px;color:var(--faint)}}.trust-band{{display:grid;grid-template-columns:1.1fr repeat(3,.8fr);gap:10px;margin:18px 0 26px}}.trust-band div{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}}.trust-band b{{display:block;font-family:var(--mono);font-size:13px;color:var(--text)}}.trust-band span{{display:block;color:var(--muted);font-size:12px;margin-top:5px}}.section-head{{display:flex;justify-content:space-between;gap:18px;align-items:end;margin:34px 0 14px}}.section-head h2{{font-size:28px;line-height:1.05;margin:0}}.section-head p{{margin:0;color:var(--muted);max-width:520px}}.journey{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.journey .step{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:18px;display:block;color:var(--text)}}.journey .step:hover{{border-color:var(--accent);background:var(--panel-2)}}.step .n{{font-family:var(--mono);font-size:11px;color:var(--accent);margin-bottom:10px}}.step h3{{font-size:19px;margin:0 0 8px}}.step p{{margin:0;color:var(--muted)}}.boundary{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}.boundary .panel h3{{margin-top:0}}.boundary ul{{padding-left:18px;margin:0}}.doc-hero{{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(300px,.9fr);gap:18px;align-items:stretch;margin:8px 0 18px}}.doc-hero>*,.doc-copy,.doc-panel,.doc-section,.doc-card,.runstep{{min-width:0}}.doc-copy{{padding:18px 0}}.doc-copy h1{{font-size:58px;line-height:.96;margin-bottom:14px;overflow-wrap:anywhere}}.doc-lede{{font-size:19px;line-height:1.5;color:var(--muted);max-width:720px;margin:0}}.doc-panel{{border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,var(--panel),var(--panel-3));padding:18px;box-shadow:0 24px 58px -36px rgba(0,0,0,.75);overflow-wrap:anywhere}}.doc-panel h3{{margin:0 0 12px;font-size:18px}}.doc-metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:18px 0 24px}}.doc-metric{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;min-width:0}}.doc-metric b{{display:block;font-family:var(--mono);font-size:21px;color:var(--text);line-height:1.1;overflow-wrap:anywhere}}.doc-metric span{{display:block;color:var(--faint);font-size:12px;margin-top:6px}}.doc-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}}.doc-card{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:18px;display:block;color:var(--text)}}.doc-card:hover{{border-color:var(--accent);background:var(--panel-2)}}.doc-card h3{{margin:0 0 8px;font-size:19px}}.doc-card p{{margin:0;color:var(--muted)}}.doc-section{{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:20px;overflow-wrap:anywhere}}.doc-section h2{{font-size:24px;margin:0 0 10px}}.doc-section p{{color:var(--muted)}}.doc-section ul{{margin:10px 0 0;padding-left:19px}}.doc-section li{{margin:7px 0}}.runbook{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}}.runstep{{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:14px}}.runstep b{{display:block;font-family:var(--mono);font-size:11px;color:var(--accent);margin-bottom:8px}}.runstep span{{display:block;color:var(--muted);font-size:13px}}.callout{{border-left:3px solid var(--accent);background:var(--panel-2);border-radius:8px;padding:14px 16px;color:var(--muted)}}.callout strong{{color:var(--text)}}.split{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}.mini-list{{display:grid;gap:8px;margin-top:12px}}.mini-list div{{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:11px 12px;color:var(--muted)}}.mini-list b{{color:var(--text)}}.site-footer{{margin-top:46px;border-top:1px solid var(--line);padding:28px 0 34px;color:var(--muted)}}.foot-grid{{display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:26px}}.site-footer h4{{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin:0 0 10px}}.site-footer p{{margin:0;color:var(--muted);font-size:13px;line-height:1.55;max-width:38ch}}.site-footer a{{display:block;color:var(--text);font-weight:600;font-size:13px;margin:7px 0}}.site-footer a:hover{{color:var(--accent)}}.fine{{border-top:1px solid var(--line-soft);margin-top:24px;padding-top:16px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--faint)}}@media(max-width:980px){{.home-hero,.doc-hero{{grid-template-columns:minmax(0,1fr);min-height:0}}.trust-band,.doc-metrics{{grid-template-columns:1fr 1fr}}.journey,.doc-grid{{grid-template-columns:1fr}}.boundary,.split{{grid-template-columns:1fr}}.runbook{{grid-template-columns:1fr 1fr}}}}@media(max-width:860px){{.hero{{grid-template-columns:1fr}}.status-strip{{grid-template-columns:1fr}}.home-proof{{grid-template-columns:1fr 1fr}}.shot-grid{{grid-template-columns:1fr}}.quadrant{{min-height:240px}}}}@media(max-width:760px){{.wrap{{padding:0 16px}}.topnav{{position:relative;display:block;margin:0 -16px 22px;padding:12px 16px}}.brand{{display:block;margin:0 0 10px}}.navlinks{{margin-left:0;display:flex;flex-wrap:nowrap;overflow-x:auto;gap:6px;padding-bottom:4px}}.navlinks a{{white-space:nowrap;flex:0 0 auto}}.foot-grid{{grid-template-columns:1fr}}h1{{font-size:34px}}.home-copy{{padding:4px 0 20px}}.home-copy h1,.home-title,.doc-copy h1{{font-size:48px}}.home-lede,.doc-lede{{font-size:17px;line-height:1.42}}.home-proof{{grid-template-columns:1fr 1fr;margin-top:20px}}.trust-band,.doc-metrics{{grid-template-columns:1fr}}.home-actions{{margin-top:20px}}.home-actions .button{{flex:1 1 155px}}.cockpit-shot{{margin-top:4px}}.runbook{{grid-template-columns:1fr}}table{{display:block;overflow-x:auto}}}}
.navlinks a[href="/pro"]{{color:#06140f;background:var(--amber);border:1px solid rgba(224,165,52,.35);font-weight:800}}.topnav{{box-shadow:0 16px 44px -34px rgba(0,0,0,.9)}}.brand{{font-size:26px}}.home-hero{{grid-template-columns:minmax(0,1fr) minmax(390px,.92fr);gap:30px;align-items:stretch;min-height:560px;margin-top:4px}}.home-copy{{display:flex;flex-direction:column;justify-content:center;border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.008));padding:34px}}.home-copy h1{{font-size:78px;line-height:.9}}.home-lede{{max-width:710px;color:#c3d4cc}}.proof-item b{{font-size:20px;white-space:nowrap}}.home-actions .button{{box-shadow:0 14px 34px -24px rgba(25,193,135,.75)}}.home-actions .button.secondary{{box-shadow:none;background:rgba(255,255,255,.025)}}.cockpit-shot{{height:100%;display:flex;flex-direction:column}}.shot-grid{{flex:1}}.quadrant{{min-height:330px}}.watch-row{{grid-template-columns:74px minmax(0,1fr) auto;align-items:start}}.watch-row span{{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.25}}.trust-band div:first-child{{background:linear-gradient(180deg,rgba(25,193,135,.11),var(--panel));border-color:rgba(25,193,135,.28)}}.purchase-hero{{display:grid;grid-template-columns:minmax(0,1fr) minmax(360px,.76fr);gap:18px;margin:4px 0 18px;align-items:stretch}}.purchase-copy{{border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.01));padding:30px;min-width:0}}.purchase-copy h1{{font-size:62px;line-height:.96;max-width:720px}}.purchase-copy .lede{{font-size:18px;max-width:760px;color:#c3d4cc}}.purchase-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}.purchase-actions a{{display:inline-flex;min-height:40px;align-items:center;justify-content:center;border-radius:8px;padding:10px 14px;font-weight:800;border:1px solid var(--line);color:var(--text)}}.purchase-actions a:first-child{{background:var(--accent);border-color:var(--accent);color:#06140f}}.purchase-panel{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:20px;display:grid;gap:12px;align-content:start}}.purchase-panel h3{{margin:0;font-size:20px}}.proof-line{{display:grid;grid-template-columns:108px 1fr;gap:12px;align-items:start;border-top:1px solid var(--line-soft);padding-top:12px}}.proof-line:first-of-type{{border-top:0;padding-top:0}}.proof-line b{{font-family:var(--mono);font-size:12px;color:var(--accent)}}.proof-line span{{color:var(--muted);font-size:13px}}.buyer-strip{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}}.buyer-strip div{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}}.buyer-strip b{{display:block;font-family:var(--mono);font-size:12px;color:var(--text)}}.buyer-strip span{{display:block;color:var(--muted);font-size:12px;margin-top:5px}}.decision-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px}}.decision-grid .card{{min-height:100%}}.section-band{{border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.02);padding:20px;margin-top:18px}}.section-band h2{{margin-top:0}}@media(max-width:980px){{.home-hero,.purchase-hero{{grid-template-columns:1fr;min-height:0}}.buyer-strip,.decision-grid{{grid-template-columns:1fr 1fr}}.home-copy{{padding:24px}}}}@media(max-width:760px){{.home-copy h1,.purchase-copy h1{{font-size:46px}}.home-copy,.purchase-copy{{padding:20px}}.buyer-strip,.decision-grid{{grid-template-columns:1fr}}}}
.wrap{{max-width:1120px}}body{{background-image:radial-gradient(50rem 34rem at 82% -6%,rgba(25,193,135,.11),transparent 60%),radial-gradient(46rem 32rem at 4% 4%,rgba(224,165,52,.075),transparent 58%),linear-gradient(180deg,rgba(255,255,255,.025),transparent 420px);background-attachment:fixed}}.topnav{{margin-bottom:42px;border-bottom:0;background:transparent;box-shadow:none;backdrop-filter:none}}.brand{{display:flex;align-items:center;gap:11px;letter-spacing:0;font-size:25px}}.brand .wm{{display:inline;font-family:var(--display);font-weight:800;letter-spacing:0}}.brand svg{{filter:drop-shadow(0 2px 9px rgba(25,193,135,.28));flex:0 0 auto}}.navlinks a{{border-radius:999px;padding:8px 14px;font-size:13.5px}}.navlinks a.primary{{border-radius:999px}}.navlinks a[href="/pro"]{{border-radius:999px}}.home-hero{{grid-template-columns:minmax(0,1fr) minmax(430px,.9fr);gap:36px;min-height:520px;align-items:center}}.home-copy{{border:0;background:transparent;padding:18px 0;display:block}}.home-copy h1{{font-size:82px;line-height:.88;letter-spacing:0;margin-bottom:20px}}.home-lede{{font-size:21px;line-height:1.5;max-width:670px;color:#c8d8d0}}.home-eyebrow span{{border-radius:999px;background:rgba(19,36,28,.82);border-color:var(--line)}}.home-proof{{max-width:640px;margin-top:30px}}.proof-item{{border-top-color:#274033}}.home-actions .button{{border-radius:999px;min-height:48px;padding:12px 20px;font-size:15px}}.home-actions .button.secondary{{border-radius:999px}}.cockpit-shot{{border-radius:18px;box-shadow:0 1px 2px rgba(0,0,0,.35),0 22px 54px -28px rgba(0,0,0,.85)}}.shot-top{{padding:18px 20px}}.shot-title{{font-size:20px}}.shot-grid{{grid-template-columns:1fr 1fr}}.quadrant{{min-height:314px}}.watch-row{{grid-template-columns:70px minmax(0,1fr)}}.watch-row i{{display:none}}.trust-band{{margin-top:30px}}.trust-band div,.buyer-strip div,.journey .step,.purchase-panel,.purchase-copy,.section-band,.card,.panel,.doc-section,.doc-panel,.doc-card{{border-radius:16px}}.buyer-strip div,.journey .step,.trust-band div{{position:relative;overflow:hidden}}.buyer-strip div:before,.journey .step:before{{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,var(--accent),transparent)}}.buyer-strip div:nth-child(2):before,.journey .step:nth-child(2):before{{background:linear-gradient(90deg,var(--amber),transparent)}}.buyer-strip div:nth-child(3):before,.journey .step:nth-child(3):before{{background:linear-gradient(90deg,var(--accent),var(--amber))}}.section-head{{margin-top:42px}}.section-head h2{{font-size:34px;line-height:1}}.purchase-copy{{background:transparent;border:0;padding:18px 0}}.purchase-copy h1{{font-size:68px;line-height:.95}}.purchase-panel{{padding:24px}}.purchase-actions a{{border-radius:999px;min-height:46px;padding:11px 18px}}@media(max-width:980px){{.home-hero{{grid-template-columns:1fr;min-height:0}}.topnav{{margin-bottom:24px}}}}@media(max-width:760px){{.home-copy h1,.purchase-copy h1{{font-size:52px}}.home-lede{{font-size:18px}}.topnav{{background:transparent}}.brand{{font-size:23px}}.brand svg{{width:30px;height:30px}}.navlinks{{flex-wrap:wrap;overflow-x:visible}}.navlinks a{{white-space:normal}}.watch-row{{grid-template-columns:1fr;gap:4px}}}}
.home-copy h1{{display:inline-block;font-size:70px;background:linear-gradient(90deg,var(--text) 0%,var(--accent) 54%,var(--amber) 100%);-webkit-background-clip:text;background-clip:text;color:transparent}}@media(max-width:760px){{.home-copy h1{{font-size:44px}}}}
.fine a{{display:inline;margin:0;font-size:inherit;color:var(--muted)}}
</style></head><body><div class="wrap">{nav}{body}{footer}</div>{script_tag}</body></html>"""
        resp = Response(html, mimetype="text/html; charset=utf-8")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return resp

    def _fmt_usd_html(x) -> str:
        try:
            v = float(x or 0)
        except (TypeError, ValueError):
            return "-"
        av = abs(v)
        if av >= 1e9:
            return f"${v / 1e9:,.1f}B"
        if av >= 1e6:
            return f"${v / 1e6:,.1f}M"
        if av >= 1e3:
            return f"${v / 1e3:,.0f}K"
        return f"${v:,.0f}"

    def _load_confluence_cache(window: int = 90) -> dict:
        import json
        path = os.path.join(_cache_dir, f"confluence-{window}.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return merge_methodology_metadata(
                    _enrich_confluence_cache_payload(db_path, json.load(fh)),
                    {"served_from_cache": True},
                )
        except (OSError, ValueError):
            return {"metadata": {"served_from_cache": False}, "kpis": {}, "signals": []}

    @app.get("/funds")
    def static_funds():
        s = store()
        try:
            rows = _fund_rows(s, _public_active_ciks(s))
            cards = []
            for r in rows:
                series = s.fund_value_timeline(r["cik"])
                latest = series[-1] if series else {}
                cards.append(
                    f'<a class="card" href="/funds/{html_escape(r["cik"])}">'
                    f'<h2>{html_escape(r["label"])}</h2>'
                    f'<div class="meta">{html_escape(r["manager"] or "")}</div>'
                    f'<p class="num">{html_escape(str(latest.get("report_date") or "-"))} · '
                    f'{_fmt_usd_html(latest.get("total_value"))} · '
                    f'{html_escape(str(latest.get("n_positions") or 0))} positions</p></a>'
                )
        finally:
            s.close()
        return _html_response("Funds", "<h1>Funds</h1><p class=\"lede\">Tracked 13F managers, latest filings and SEC source links.</p><div class=\"grid\">" + "".join(cards) + "</div>")

    @app.get("/funds/<cik>")
    def static_fund_detail(cik):
        cik = clean_cik(cik)
        s = store()
        try:
            pf = s.load_portfolio(cik)
            if pf is None:
                abort(404)
            frow = filing_row_for(s, cik, pf.report_date)
            positions = sorted(pf.positions.values(), key=lambda p: p.value_usd, reverse=True)
        finally:
            s.close()
        sec = sec_accession_url(cik, frow["accession"]) if frow else "#"
        rows = "".join(
            f"<tr><td><a href=\"/stocks/{html_escape(p.ticker or p.cusip)}\">{html_escape(p.ticker or '-')}</a>"
            f"<div class=\"meta\">{html_escape(p.cusip)}</div></td>"
            f"<td>{html_escape(p.issuer or '')}</td><td class=\"num\">{p.weight * 100:.2f}%</td>"
            f"<td class=\"num\">{_fmt_usd_html(p.value_usd)}</td></tr>"
            for p in positions[:80]
        )
        body = (
            f"<h1>{html_escape(pf.fund_label)}</h1>"
            f"<p class=\"lede\">Latest 13F portfolio for {html_escape(pf.report_date)}. "
            f"<a class=\"sec\" href=\"{html_escape(sec)}\" rel=\"noopener\" target=\"_blank\">SEC filing {html_escape(frow['accession'] if frow else '')}</a></p>"
            f"<table><thead><tr><th>Ticker</th><th>Issuer</th><th>Weight</th><th>Value</th></tr></thead><tbody>{rows}</tbody></table>"
        )
        return _html_response(pf.fund_label, body)

    @app.get("/stocks")
    def static_stocks():
        s = store()
        try:
            trusted, _gate = _trusted_active_ciks(s)
            latest = _latest_filings_date(s, "MAX", _public_active_ciks(s))
            active_values = tuple(sorted(trusted))
            active_sql = ""
            active_args: tuple[str, ...] = ()
            if active_values:
                active_sql = f" AND lf.cik IN ({_placeholders(active_values)})"
                active_args = active_values
            else:
                active_sql = " AND 1=0"
            rows = [dict(r) for r in s.conn.execute(
                f"""SELECT UPPER(h.ticker) ticker, MAX(h.issuer) issuer,
                          COUNT(DISTINCT lf.cik) holders, SUM(h.value_usd) value_usd
                   FROM latest_filings lf
                   JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                   WHERE lf.report_date=? AND h.ticker IS NOT NULL AND h.ticker<>''
                   {active_sql}
                   GROUP BY UPPER(h.ticker)
                   ORDER BY value_usd DESC LIMIT 300""",
                (latest, *active_args),
            )]
        finally:
            s.close()
        table = "".join(
            f"<tr><td><a href=\"/stocks/{html_escape(r['ticker'])}\">{html_escape(r['ticker'])}</a>"
            f"<div class=\"meta\">{html_escape(r['issuer'] or '')}</div></td>"
            f"<td class=\"num\">{r['holders']}</td><td class=\"num\">{_fmt_usd_html(r['value_usd'])}</td>"
            f"<td><a class=\"sec\" href=\"https://www.sec.gov/edgar/search/#/q={html_escape(r['ticker'])}\" rel=\"noopener\" target=\"_blank\">SEC search</a></td></tr>"
            for r in rows
        )
        return _html_response("Stocks", f"<h1>Stocks</h1><p class=\"lede\">Latest-quarter holdings by ticker across tracked funds.</p><table><thead><tr><th>Ticker</th><th>Funds</th><th>Value</th><th>SEC</th></tr></thead><tbody>{table}</tbody></table>")

    @app.get("/stocks/<ticker>")
    def static_stock_detail(ticker):
        payload = _stock_payload(ticker)
        score = payload.get("score") or {}
        confidence = payload.get("confidence") or {}
        summary = payload.get("movement_summary") or {}
        holder_rows = "".join(
            f"<tr><td><a href=\"/funds/{html_escape(r['cik'])}\">{html_escape(r['label'])}</a></td>"
            f"<td class=\"num\">{r['weight'] * 100:.2f}%</td><td class=\"num\">{_fmt_usd_html(r['value_usd'])}</td>"
            f"<td><a class=\"sec\" href=\"{html_escape(sec_accession_url(r['cik'], r['accession']))}\" rel=\"noopener\" target=\"_blank\">{html_escape(r['accession'])}</a></td></tr>"
            for r in payload["holders"]
        )

        def movement_sec_link(row):
            url = row.get("sec_filing_url")
            if not url:
                return "-"
            return f"<a class=\"sec\" href=\"{html_escape(url)}\" rel=\"noopener\" target=\"_blank\">SEC</a>"

        movement_rows = "".join(
            f"<tr><td><a href=\"/funds/{html_escape(m['cik'])}\">{html_escape(m['label'])}</a>"
            f"<div class=\"meta\">prev {html_escape(str(m.get('previous_quarter') or '-'))}</div></td>"
            f"<td><span class=\"pill\">{html_escape(m['move'])}</span></td>"
            f"<td class=\"num\">{_fmt_usd_html(m.get('prev_value_usd'))}</td>"
            f"<td class=\"num\">{_fmt_usd_html(m.get('curr_value_usd'))}</td>"
            f"<td class=\"num\">{float(m.get('curr_weight') or 0) * 100:.2f}%</td>"
            f"<td>{movement_sec_link(m)}</td></tr>"
            for m in payload.get("movements", [])[:40]
        )
        quality_pills = "".join(
            f"<span class=\"pill\">{html_escape(str(w.get('type') or 'quality'))}: "
            f"{html_escape(str((w.get('fund') or {}).get('label') or w.get('label') or 'review'))}</span>"
            for w in payload.get("quality_flags", [])[:8]
        ) or '<span class="pill">no active ticker-level quality warning</span>'
        reasons = "".join(
            f"<li>{html_escape(str(reason))}</li>"
            for reason in confidence.get("reasons", [])
        )
        body = (
            f"<h1>{html_escape(payload['ticker'])}</h1>"
            f"<p class=\"lede\">Ticker intelligence from active-registry 13F rows at {html_escape(payload['latest_13f_quarter'] or '-')}. "
            f"<a href=\"{html_escape(payload['sec_company_search'])}\" rel=\"noopener\" target=\"_blank\">SEC company search</a></p>"
            f"<div class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(score.get('score', 0)))}</b><span>Ticker Flow Score</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(payload['holder_count']))}</b><span>latest holders</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(summary.get('buyers_count') or 0))}</b><span>buyers/adders</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(confidence.get('status') or 'unknown')}</b><span>data confidence</span></div>"
            f"</div>"
            f"<div class=\"split\"><section class=\"panel\"><h2>Signal Components</h2>"
            f"<p>{html_escape(score.get('interpretation') or '')}</p>"
            f"<p><span class=\"pill\">new={html_escape(str(summary.get('new_positions') or 0))}</span>"
            f"<span class=\"pill\">trims/exits={html_escape(str(summary.get('sellers_count') or 0))}</span>"
            f"<span class=\"pill\">conviction funds={html_escape(str(summary.get('conviction_funds') or 0))}</span>"
            f"<span class=\"pill\">avg weight={float(summary.get('avg_weight_pct') or 0):.2f}%</span></p></section>"
            f"<section class=\"panel\"><h2>Data Confidence</h2><ul>{reasons}</ul><p>{quality_pills}</p></section></div>"
            f"<h2>Quarter Moves</h2>"
            f"<table><thead><tr><th>Fund</th><th>Move</th><th>Previous value</th><th>Current value</th><th>Weight</th><th>Source</th></tr></thead><tbody>{movement_rows}</tbody></table>"
            f"<h2>Latest Holders</h2>"
            f"<table><thead><tr><th>Fund</th><th>Weight</th><th>Value</th><th>SEC filing</th></tr></thead><tbody>{holder_rows}</tbody></table>"
        )
        return _html_response(payload["ticker"], body)

    @app.get("/signals")
    def static_signals():
        payload = _load_confluence_cache(90)
        rows = "".join(
            f"<tr><td><a href=\"/signals/{html_escape(str(sig.get('ticker') or ''))}\">{html_escape(sig.get('ticker') or '')}</a>"
            f"<div class=\"meta\">{html_escape(sig.get('issuer_name') or '')}</div></td>"
            f"<td class=\"num\">{html_escape(str(sig.get('score') or 0))}</td>"
            f"<td><span class=\"pill\">{html_escape(sig.get('quadrant') or '')}</span></td>"
            f"<td>{html_escape(sig.get('rationale') or '')}</td></tr>"
            for sig in payload.get("signals", [])
        )
        return _html_response("Signals", f"<h1>Signals</h1><p class=\"lede\">Confluence v1 cached signals. The score is an ordinal research screen, not a probability or expected return.</p><table><thead><tr><th>Ticker</th><th>Score</th><th>Quadrant</th><th>Rationale</th></tr></thead><tbody>{rows}</tbody></table>")

    @app.get("/signals/<ticker>")
    def static_signal_detail(ticker):
        t = (ticker or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", t):
            abort(404)
        payload = _load_confluence_cache(90)
        sig = next((s for s in payload.get("signals", []) if str(s.get("ticker") or "").upper() == t), None)
        if sig is None:
            abort(404)
        inst = sig.get("institutional") or {}
        ins = sig.get("insider") or {}
        body = (
            f"<h1>{html_escape(t)}</h1><p class=\"lede\">{html_escape(sig.get('rationale') or '')}</p>"
            f"<div class=\"grid\"><div class=\"card\"><h3>Score</h3><p class=\"num\">{html_escape(str(sig.get('score')))} / 100</p><p><span class=\"pill\">{html_escape(sig.get('quadrant') or '')}</span></p></div>"
            f"<div class=\"card\"><h3>Institutional</h3><p>{html_escape(str(inst.get('funds_accumulating', 0)))} accumulating, {html_escape(str(inst.get('funds_trimming', 0)))} trimming</p><p class=\"num\">{_fmt_usd_html(inst.get('total_value_usd'))}</p></div>"
            f"<div class=\"card\"><h3>Insiders</h3><p>{html_escape(str(ins.get('n_buyers', 0)))} buyers · {html_escape(str(ins.get('n_c_suite_buyers', 0)))} C-suite</p><p class=\"num\">{_fmt_usd_html(ins.get('buy_value_usd'))}</p></div></div>"
            f"<p class=\"lede\"><a href=\"/stocks/{html_escape(t)}\">Latest 13F holders</a> · <a href=\"https://www.sec.gov/edgar/search/#/q={html_escape(t)}\" rel=\"noopener\" target=\"_blank\">SEC search</a></p>"
        )
        return _html_response(f"{t} signal", body)

    def app_methodology_payload() -> dict:
        status = product_status_payload()
        live = live_status_payload()
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "title": "13FLOW application methodology",
            "scope": (
                "Public read-only research interface over SEC EDGAR-derived Form 13F holdings, "
                "quality warnings, source links and Confluence v1 research screens."
            ),
            "primary_sources": [
                {
                    "name": "SEC EDGAR filing and data APIs",
                    "url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
                    "role": "official public filing source and API documentation",
                },
                {
                    "name": "SEC developer resources",
                    "url": "https://www.sec.gov/about/developer-resources",
                    "role": "official programmatic access and developer context",
                },
                {
                    "name": "13FLOW live status",
                    "url": "/api/live-status",
                    "role": "current dataset state, counts, latest quarter and quality summary",
                },
                {
                    "name": "13FLOW Confluence v1 contract",
                    "url": "/api/methodology/confluence-v1",
                    "role": "frozen research-screen methodology contract",
                },
            ],
            "pipeline": [
                "Ingest tracked Form 13F filings and source accession metadata from SEC EDGAR.",
                "Normalize CIKs, accessions, issuers, CUSIPs, tickers, reported values and share counts.",
                "Publish the market DB as a read-only artifact for public web/API use.",
                "Expose source links so users can inspect the underlying SEC filing.",
                "Emit data-quality warnings instead of silently correcting suspect jumps or unit-scale issues.",
                "Serve Confluence v1 as an ordinal screen only; it is not a forecast, probability or expected-return model.",
            ],
            "current_state": {
                "public_state": live.get("public_state"),
                "data_as_of": live.get("data_as_of"),
                "latest_13f": live.get("latest_13f_quarter"),
                "counts": live.get("counts"),
                "quality_summary": live.get("quality_summary"),
            },
            "truth_boundary": status["validation"],
            "verified_now": [
                "tracked Form 13F holdings are served from a local SEC EDGAR-derived database",
                "public runtime state exposes deployed SHA, LIVE/DEMO/DEGRADED state and dataset counts",
                "source accessions and SEC filing links remain inspectable from fund and stock pages",
                "data-quality warnings are surfaced instead of silently corrected away",
                "open public build has no browser account, retail checkout or alert upsell chrome",
                "a 25-ticker mature 13F + Form 4 joined validation artifact is mechanically schema-valid and ready for human review",
            ],
            "not_verified_yet": [
                "Confluence v1 is not validated as alpha, probability or expected-return model",
                "current Confluence validation metrics remain unreviewed and are not a public validation claim",
                "full point-in-time Form 4 plus adjusted-price validation is not yet published",
                "x402 paid production access is not enabled",
                "complete insider-only/distribution universe is not claimed",
                "institutional deployment requires buyer-specific contract, support and redistribution terms",
            ],
            "sellable_now": status["offer_boundary"]["sell_now"],
            "not_claimed": status["offer_boundary"]["do_not_claim_yet"],
            "user_interpretation": [
                "13F filings are delayed regulatory disclosures, not real-time portfolios.",
                "Reported holdings may omit non-reportable positions, shorts, derivatives or intra-quarter changes.",
                "Ticker resolution and CUSIP mapping are operational conveniences and must keep quality metadata visible.",
                "Screens can prioritize review, but they do not replace fundamental analysis or risk management.",
            ],
        }

    def mcp_methodology_payload() -> dict:
        offer = pro_offer_payload()
        return {
            "app": "13flow",
            "generated_at": _now_iso(),
            "git_sha": _git_sha(),
            "title": "13FLOW MCP methodology",
            "scope": (
                "Read-only Model Context Protocol access to public 13FLOW resources, plus gated "
                "Pro tools that require an existing Pro API key or configured paid settlement path."
            ),
            "surfaces": [
                {
                    "name": "public MCP",
                    "url": "/api/mcp",
                    "tools": ["product.status", "pro.offer", "funds.list", "funds.get", "stocks.get"],
                    "auth": "none for public read-only tools",
                },
                {
                    "name": "Pro MCP tools",
                    "url": "/api/mcp",
                    "tools": ["pro.list_funds", "pro.get_fund", "pro.data_quality"],
                    "auth": "13FLOW Pro API key; x402 path remains disabled until configured",
                },
            ],
            "contract": [
                "Public tools must not require browser auth, cookies or checkout.",
                "Pro tools must fail closed without a valid Pro API key or verified paid access.",
                "MCP responses must expose product status, validation boundary and data-quality warnings.",
                "Tool output must not claim validated alpha, probabilities or expected returns.",
                "The MCP server calls the isolated Pro API service for premium data rather than reading the Pro DB directly.",
            ],
            "security": {
                "credential_headers": ["Authorization: Bearer <token>", "X-13FLOW-Key: <token>"],
                "cache_policy": "Pro API responses are private/no-store and vary by credential header",
                "audit": "accepted, denied and rate-limited Pro API requests create audit rows",
                "x402": "implemented but disabled until production payment details are configured",
            },
            "operator_checks": [
                "Run public smoke after deployment.",
                "Verify MCP tools/list public contract.",
                "Verify Pro MCP tools fail closed without payment/key.",
                "Run a valid Pro key probe before claiming Pro MCP readiness for a buyer.",
                "Record key id, scopes, limits and rotation date in the operator note.",
            ],
            "references": [
                {"name": "Pro offer", "url": "/api/pro-offer"},
                {"name": "Product status", "url": "/api/product-status"},
                {"name": "Public OpenAPI", "url": "/api/openapi.json"},
                {"name": "Pro OpenAPI", "url": "/api/pro/v1/openapi.json"},
            ],
        }

    @app.get("/api/methodology/app")
    def app_methodology_ep():
        return jsonify(app_methodology_payload())

    @app.get("/api/methodology/mcp")
    def mcp_methodology_ep():
        return jsonify(mcp_methodology_payload())

    def _methodology_page(title: str, payload: dict, api_path: str) -> Response:
        sources = "".join(
            f"<li><a href=\"{html_escape(src['url'])}\">{html_escape(src['name'])}</a>"
            f"<div class=\"meta\">{html_escape(src.get('role') or '')}</div></li>"
            for src in payload.get("primary_sources", payload.get("references", []))
        )
        main_items = payload.get("pipeline") or payload.get("contract") or []
        bullets = "".join(f"<li>{html_escape(item)}</li>" for item in main_items)
        caveats = "".join(
            f"<li>{html_escape(item)}</li>" for item in payload.get("user_interpretation", payload.get("operator_checks", []))
        )
        boundary = payload.get("truth_boundary") or {}
        artifact = boundary.get("current_artifact") or {}
        verified = "".join(
            f"<li>{html_escape(item)}</li>" for item in payload.get("verified_now", [])
        )
        not_verified = "".join(
            f"<li>{html_escape(item)}</li>" for item in payload.get("not_verified_yet", [])
        )
        sellable = "".join(
            f"<li>{html_escape(item)}</li>" for item in payload.get("sellable_now", [])
        )
        not_claimed = "".join(
            f"<li>{html_escape(item)}</li>" for item in payload.get("not_claimed", [])
        )
        proof_panels = ""
        if verified or not_verified or sellable or not_claimed:
            proof_panels = (
                "<div class=\"grid\" style=\"margin-top:18px\">"
                "<div class=\"card\"><h3>What is verified</h3><ul>" + verified + "</ul></div>"
                "<div class=\"card\"><h3>What is not verified yet</h3><ul>" + not_verified + "</ul></div>"
                "<div class=\"card\"><h3>Sellable now</h3><ul>" + sellable + "</ul></div>"
                "<div class=\"card\"><h3>Do not claim</h3><ul>" + not_claimed + "</ul></div>"
                "</div>"
            )
        artifact_panel = ""
        if artifact:
            artifact_panel = (
                "<div class=\"panel\" style=\"margin-top:18px\"><h2>Current validation artifact</h2>"
                f"<p class=\"meta\">scope={html_escape(str(artifact.get('scope') or 'unknown'))}</p>"
                f"<p class=\"meta\">schema_status={html_escape(str(artifact.get('schema_status') or 'unknown'))}</p>"
                f"<p class=\"meta\">evidence_review_status={html_escape(str(artifact.get('evidence_review_status') or 'unknown'))}</p>"
                f"<p class=\"meta\">metrics_status={html_escape(str(artifact.get('metrics_status') or 'unknown'))}</p>"
                f"<p class=\"meta\">rows={html_escape(str(artifact.get('row_count') or 'unknown'))}; "
                f"tickers={html_escape(str(artifact.get('ticker_count') or 'unknown'))}; "
                f"row_errors={html_escape(str(artifact.get('row_error_count') if artifact.get('row_error_count') is not None else 'unknown'))}</p>"
                f"<p>Publishable as full validation: <code>{str(artifact.get('publishable_as_full_validation')).lower()}</code></p>"
                f"<p>Public validation claim: <code>{str(artifact.get('public_validation_claim')).lower()}</code></p>"
                f"<p class=\"meta\">features_sha256={html_escape(str(artifact.get('features_sha256') or ''))}</p>"
                f"<p class=\"meta\">prices_sha256={html_escape(str(artifact.get('prices_sha256') or ''))}</p></div>"
            )
        state = payload.get("current_state") or {}
        counts = state.get("counts") or {}
        quality = state.get("quality_summary") or {}
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Methodology and operating model</div>"
            f"<h1>{html_escape(title)}</h1>"
            f"<p class=\"doc-lede\">{html_escape(payload['scope'])}</p>"
            "<div class=\"actions\">"
            f"<a class=\"pill cta\" href=\"{html_escape(api_path)}\">Machine-readable contract</a>"
            "<a class=\"pill\" href=\"/validation\">Validation boundary</a>"
            "<a class=\"pill\" href=\"/developers\">Developer docs</a></div></div>"
            "<aside class=\"doc-panel\"><h3>Use this page for</h3>"
            "<div class=\"mini-list\">"
            "<div><b>Analyst onboarding:</b> understand what the signal can and cannot say.</div>"
            "<div><b>Buyer diligence:</b> inspect sources, claims and quality gates before Pro access.</div>"
            "<div><b>Agent grounding:</b> link tools to explicit contracts instead of free-form assumptions.</div>"
            "</div></aside></section>"
            "<section class=\"doc-metrics\">"
            f"<div class=\"doc-metric\"><b>{html_escape(str(counts.get('funds') or 'public'))}</b><span>tracked funds</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(counts.get('filings') or 'MCP'))}</b><span>filings or tools</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(str(quality.get('status') or 'fail-closed'))}</b><span>quality posture</span></div>"
            f"<div class=\"doc-metric\"><b>{html_escape(payload['git_sha'][:12])}</b><span>served SHA</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Method</h2>"
            f"<ul>{bullets}</ul></section>"
            "<section class=\"doc-section\"><h2>How to read 13FLOW output</h2>"
            "<div class=\"runbook\">"
            "<div class=\"runstep\"><b>01</b><span>Start from the live/status surface and confirm the data quarter.</span></div>"
            "<div class=\"runstep\"><b>02</b><span>Use fund, stock and signal pages to inspect source-linked evidence.</span></div>"
            "<div class=\"runstep\"><b>03</b><span>Read validation status before treating a score as decision support.</span></div>"
            "<div class=\"runstep\"><b>04</b><span>Keep 13F delay, Form 4 scope and quality warnings in the note.</span></div>"
            "</div></section>"
            + proof_panels +
            artifact_panel +
            "<section class=\"doc-section\"><h2>Sources and contracts</h2>"
            f"<ul>{sources}</ul></section>"
            "<section class=\"doc-section\"><h2>Interpretation boundary</h2>"
            f"<ul>{caveats}</ul>"
            f"<p class=\"meta\">Validation status: {html_escape(str(boundary.get('status') or 'see API contract'))}</p></section>"
        )
        return _html_response(title, body)

    @app.get("/methodology")
    def methodology_hub():
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\"><div class=\"kicker\">Read before you trust a signal</div>"
            "<h1>Methodology</h1>"
            "<p class=\"doc-lede\">How 13FLOW turns public SEC filings into read-only research surfaces, "
            "how the validation boundary constrains claims, and how the MCP layer exposes that context to agents.</p>"
            "<div class=\"actions\"><a class=\"pill cta\" href=\"/methodology/app\">Application methodology</a>"
            "<a class=\"pill\" href=\"/methodology/mcp\">MCP methodology</a>"
            "<a class=\"pill\" href=\"/api/methodology/confluence-v1\">Confluence v1 JSON</a></div></div>"
            "<aside class=\"doc-panel\"><h3>Core doctrine</h3>"
            "<p class=\"callout\"><strong>13FLOW is a filing evidence system.</strong> It can prioritize review, expose sources and structure workflows. It does not replace fundamental analysis, risk management or legal review.</p>"
            "</aside></section>"
            "<section class=\"doc-grid\">"
            "<a class=\"doc-card\" href=\"/methodology/app\"><h3>Application methodology</h3>"
            "<p>Data pipeline, 13F caveats, quality warnings, Confluence scope and validation boundary.</p></a>"
            "<a class=\"doc-card\" href=\"/methodology/mcp\"><h3>MCP methodology</h3>"
            "<p>Tool contract, Pro gating, fail-closed behavior, x402 readiness and agent safety.</p></a>"
            "<a class=\"doc-card\" href=\"/validation\"><h3>Validation pack</h3>"
            "<p>Mechanical evidence, descriptive metrics, current artifact hashes and explicit non-claims.</p></a>"
            "</section>"
            "<section class=\"doc-section\"><h2>How to use the tool</h2>"
            "<div class=\"runbook\">"
            "<div class=\"runstep\"><b>01 · Confirm</b><span>Open Status and verify LIVE, commit SHA and latest 13F quarter.</span></div>"
            "<div class=\"runstep\"><b>02 · Screen</b><span>Use Cockpit, Signals, Funds and Stocks to build a research queue.</span></div>"
            "<div class=\"runstep\"><b>03 · Inspect</b><span>Follow SEC accessions, manager context, Form 4 overlap and quality warnings.</span></div>"
            "<div class=\"runstep\"><b>04 · Decide</b><span>Take decisions outside 13FLOW, with your own model, risk limits and source review.</span></div>"
            "</div></section>"
            "<section class=\"split\">"
            "<div class=\"doc-section\"><h2>What 13FLOW is good at</h2><ul>"
            "<li>Making delayed SEC 13F disclosures navigable.</li>"
            "<li>Joining institutional movement with Form 4 evidence where available.</li>"
            "<li>Surfacing methodology, source links and data-quality warnings beside the signal.</li>"
            "<li>Giving humans and agents the same public contracts.</li></ul></div>"
            "<div class=\"doc-section\"><h2>What users must remember</h2><ul>"
            "<li>13F filings are delayed regulatory disclosures, not live books.</li>"
            "<li>Scores are ordinal research screens, not probabilities or expected returns.</li>"
            "<li>Validation is mechanical and conservative until broader reviewed artifacts are published.</li>"
            "<li>Redistribution and production Pro use require operator approval.</li></ul></div>"
            "</section>"
        )
        return _html_response("Methodology", body)

    @app.get("/methodology/app")
    def app_methodology_page():
        return _methodology_page("Application methodology", app_methodology_payload(), "/api/methodology/app")

    @app.get("/methodology/mcp")
    def mcp_methodology_page():
        return _methodology_page("MCP methodology", mcp_methodology_payload(), "/api/methodology/mcp")

    @app.get("/developers")
    def developers_page():
        offer = pro_offer_payload()
        limits = offer["default_limits"]
        tools = [
            ("get_live_status", "Public dataset state, counts, latest 13F quarter and quality summary."),
            ("get_product_status", "Commercial readiness, validation boundary and disabled-claim list."),
            ("get_pro_offer", "Machine-readable Pro packaging, buyer checklist and pricing model."),
            ("list_funds", "Public tracked fund list."),
            ("pro.list_funds", "Pro-only fund list; fails closed without a valid key or paid access."),
        ]
        tool_rows = "".join(
            f"<tr><td><code>{html_escape(name)}</code></td><td>{html_escape(desc)}</td></tr>"
            for name, desc in tools
        )
        curl_status = (
            "curl -fsS https://13flow.eu/status\n"
            "curl -fsS https://13flow.eu/api/live-status\n"
            "curl -fsS https://13flow.eu/api/product-status\n"
            "curl -fsS https://13flow.eu/api/pro-offer"
        )
        curl_mcp = (
            "curl -fsS https://13flow.eu/api/mcp \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -H 'Accept: application/json, text/event-stream' \\\n"
            "  --data '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'"
        )
        body = (
            "<h1>Developers</h1>"
            "<p class=\"lede\">Public read-only API and MCP entry points for source-linked SEC 13F context. "
            "Pro access is operator-issued and must fail closed without a valid credential or paid settlement.</p>"
            "<div class=\"grid\">"
            "<div class=\"card\"><h3>Status</h3><p><a href=\"/status\">/status</a></p>"
            "<p class=\"meta\">Human-readable deployed SHA, live state and validation boundary.</p></div>"
            "<div class=\"card\"><h3>Public API</h3><p><a href=\"/api/openapi.json\">/api/openapi.json</a></p>"
            "<p class=\"meta\">No browser account, no cookies, no checkout required for open endpoints.</p></div>"
            "<div class=\"card\"><h3>Pro API</h3><p><a href=\"/api/pro/v1/openapi.json\">/api/pro/v1/openapi.json</a></p>"
            f"<p class=\"meta\">Default pilot limits: {limits['rate_per_min']} / min, {limits['rate_per_day']} / day.</p></div>"
            "<div class=\"card\"><h3>MCP</h3><p><a href=\"/api/mcp\">/api/mcp</a></p>"
            "<p class=\"meta\">Streamable HTTP, public tools plus Pro tools gated by key or payment path.</p></div>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Quick checks</h2>"
            f"<pre><code>{html_escape(curl_status)}</code></pre></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>MCP tools/list</h2>"
            f"<pre><code>{html_escape(curl_mcp)}</code></pre></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Tool boundary</h2>"
            f"<table><thead><tr><th>Tool</th><th>Contract</th></tr></thead><tbody>{tool_rows}</tbody></table>"
            "<p class=\"meta\">Pro tools are intentionally visible in tools/list so agents can discover the capability, "
            "then receive a 402/401 fail-closed response without payment or key.</p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Use policy</h2>"
            "<ul><li>13FLOW is a research screen, not investment advice.</li>"
            "<li>13F filings are delayed regulatory disclosures and are not real-time portfolios.</li>"
            "<li>Do not market tool output as validated alpha, expected return or trading recommendation.</li>"
            "<li>Redistribution, bulk resale, custom limits and automated high-volume use require operator approval.</li></ul>"
            "<p><a class=\"pill\" href=\"/legal/pro-api\">Pro API terms</a> "
            "<a class=\"pill\" href=\"/methodology/mcp\">MCP methodology</a> "
            "<a class=\"pill\" href=\"/pro\">Request Pro access</a></p></div>"
        )
        return _html_response("Developers", body)

    @app.get("/legal/pro-api")
    def pro_api_terms_page():
        body = (
            "<h1>Pro API, MCP and x402 terms</h1>"
            "<p class=\"lede\">Operational terms for evaluated Pro access. These terms are intentionally strict until "
            "validation, billing, support and redistribution policies are expanded in a signed agreement.</p>"
            "<div class=\"grid\">"
            "<div class=\"card\"><h3>Access model</h3><p>Pro access is operator-reviewed and issued through scoped API keys. "
            "Self-serve checkout is disabled on the open build.</p></div>"
            "<div class=\"card\"><h3>Payment model</h3><p>x402 support is implemented as a gated path but remains disabled "
            "until production payment details are configured and verified.</p></div>"
            "<div class=\"card\"><h3>Audit model</h3><p>Accepted, denied and rate-limited Pro requests may be logged with "
            "key id, scope, endpoint, status and request metadata needed for security review.</p></div>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Capacity and support boundary</h2>"
            "<ul><li>13FLOW Pro is currently a limited-capacity, operator-reviewed technical evaluation surface.</li>"
            "<li>No public package pricing, uptime SLA, support SLA, enterprise procurement promise or managed-service guarantee is offered on the open site.</li>"
            "<li>Access can be declined, rate-limited, paused, rotated or revoked for operational, security or legal reasons.</li>"
            "<li>Any paid pilot, production use or redistribution right requires explicit written agreement before a token is issued.</li></ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Allowed use</h2>"
            "<ul><li>Internal research, dashboards, notebooks, data-quality checks and agent workflows.</li>"
            "<li>Source-linked review of SEC EDGAR-derived 13F holdings and quality warnings.</li>"
            "<li>MCP use where Pro tools fail closed without a valid key or paid access.</li></ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Restricted use</h2>"
            "<ul><li>No resale, redistribution, bulk republishing or public embedding of Pro data without written approval.</li>"
            "<li>No representation that 13FLOW provides investment advice, validated alpha, probabilities or expected returns.</li>"
            "<li>No attempts to bypass rate limits, auth, audit logging, payment checks or credential isolation.</li>"
            "<li>No storage of API keys in client-side code, public repositories or shared prompts.</li></ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Data and proof boundary</h2>"
            "<p>13FLOW structures public SEC filing data and adds quality metadata, source links and research-screen contracts. "
            "It does not own the underlying SEC filings and does not sell raw SEC access as proprietary data.</p>"
            "<p class=\"meta\">Current methodology references: <a href=\"/methodology/app\">application</a>, "
            "<a href=\"/methodology/mcp\">MCP</a>, <a href=\"/api/product-status\">product status</a>.</p></div>"
        )
        return _html_response("Pro API terms", body)

    @app.get("/pro/onboarding")
    def static_pro_onboarding():
        body = """
<style>
.onboarding-app{display:grid;gap:14px}
.onboarding-bar{display:grid;grid-template-columns:minmax(220px,1fr) auto auto;gap:8px;align-items:end;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}
.onboarding-bar label{display:grid;gap:5px;color:var(--faint);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.onboarding-bar input{width:100%;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font:inherit;padding:10px 11px;letter-spacing:0}
.onboarding-button{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font-weight:800;padding:10px 12px;min-height:42px;cursor:pointer}
.onboarding-button.primary{background:var(--accent);border-color:var(--accent);color:#06140f}
.onboarding-status{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);color:var(--muted);padding:10px 12px;font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.onboarding-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.onboarding-kpi{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;min-width:0}
.onboarding-kpi b{display:block;font-family:var(--mono);font-size:18px;line-height:1.1}
.onboarding-kpi span{display:block;color:var(--faint);font-size:11px;margin-top:4px}
.onboarding-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}
.onboarding-panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;min-width:0}
.onboarding-panel h2,.onboarding-panel h3{font-size:18px;margin:0 0 10px}
.onboarding-list{display:grid;gap:8px}
.onboarding-row{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;display:grid;gap:7px}
.onboarding-row h3{font-size:15px;margin:0;overflow-wrap:anywhere}
.onboarding-row p{margin:0;color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.onboarding-mini{font-family:var(--mono);font-size:11px;color:var(--faint)}
.onboarding-empty{color:var(--faint);font-size:13px;margin:0}
@media(max-width:900px){.onboarding-bar,.onboarding-grid{grid-template-columns:1fr}.onboarding-kpis{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.onboarding-kpis{grid-template-columns:1fr}}
</style>
<section class="doc-hero"><div class="doc-copy"><div class="kicker">Pro onboarding</div>
<h1>Integration Diagnostic</h1>
<p class="doc-lede">Validate a scoped Pro API key, inspect available capabilities and copy safe first-call checks before wiring a client workflow.</p></div>
<aside class="doc-panel"><h3>Credential boundary</h3><p>The token stays in tab session storage and is sent only as an Authorization header. It is never echoed by the diagnostic API.</p>
<p class="meta">Required base scope: funds:read</p></aside></section>
<main class="onboarding-app" data-pro-onboarding-app>
  <section class="onboarding-bar" aria-label="Pro onboarding access">
    <label>Pro API key <input id="onboardingToken" type="password" autocomplete="off" spellcheck="false" placeholder="13flow_live_..."></label>
    <button id="onboardingConnect" class="onboarding-button primary" type="button">Connect</button>
    <button id="onboardingForget" class="onboarding-button" type="button">Forget</button>
  </section>
  <div id="onboardingStatus" class="onboarding-status">Disconnected</div>
  <section class="onboarding-kpis" id="onboardingKpis">
    <div class="onboarding-kpi"><b>-</b><span>Key</span></div>
    <div class="onboarding-kpi"><b>-</b><span>Scopes</span></div>
    <div class="onboarding-kpi"><b>-</b><span>Per minute</span></div>
    <div class="onboarding-kpi"><b>-</b><span>Workspace</span></div>
  </section>
  <section class="onboarding-grid">
    <section class="onboarding-panel"><h2>Endpoint Checks</h2><div id="onboardingChecks" class="onboarding-list"><p class="onboarding-empty">No diagnostic loaded.</p></div></section>
    <section class="onboarding-panel"><h2>Next Actions</h2><div id="onboardingActions" class="onboarding-list"><p class="onboarding-empty">No diagnostic loaded.</p></div></section>
    <section class="onboarding-panel"><h2>Quick Checks</h2><div id="onboardingQuick" class="onboarding-list"><p class="onboarding-empty">No diagnostic loaded.</p></div></section>
    <section class="onboarding-panel"><h2>Security Boundary</h2><div id="onboardingSecurity" class="onboarding-list"><p class="onboarding-empty">No diagnostic loaded.</p></div></section>
  </section>
</main>
"""
        script = r"""
(() => {
  const TOKEN_KEY = "13flow.pro.onboarding.token";
  const $ = (id) => document.getElementById(id);
  const app = document.querySelector("[data-pro-onboarding-app]");
  if (!app) return;
  const state = {token: sessionStorage.getItem(TOKEN_KEY) || ""};
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (v) => Number.isFinite(Number(v)) ? String(Number(v)) : "-";
  const setStatus = (msg, bad=false) => {
    const node = $("onboardingStatus");
    node.textContent = msg;
    node.style.borderColor = bad ? "rgba(239,106,82,.55)" : "var(--line-soft)";
  };
  async function onboardingApi() {
    if (!state.token) throw new Error("Pro API key required");
    const res = await fetch("/api/pro/v1/onboarding", {headers: {"Authorization": "Bearer " + state.token}});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || ("HTTP " + res.status));
    return data;
  }
  function renderDiagnostic(payload={}) {
    const key = payload.key || {};
    const diag = payload.diagnostic || {};
    const endpoints = payload.endpoints || {};
    const checks = endpoints.checks || [];
    const security = payload.security || {};
    const truth = payload.truth_boundary || {};
    $("onboardingKpis").innerHTML = [
      ["Key", key.id || "-"],
      ["Scopes", (key.scopes || []).length],
      ["Per minute", key.rate_per_min],
      ["Workspace", diag.workspace_enabled ? "enabled" : "off"],
    ].map(([label, value]) => `<div class="onboarding-kpi"><b>${esc(number(value) === "-" ? value : number(value))}</b><span>${esc(label)}</span></div>`).join("");
    $("onboardingChecks").innerHTML = checks.length ? checks.map((item) => `<article class="onboarding-row">
      <h3>${esc(item.id)}</h3><p><span class="pill">${esc(item.method)}</span><span class="pill">${esc(item.available ? "available" : "missing scope")}</span><span class="pill">${esc(item.required_scope)}</span></p><p class="onboarding-mini">${esc(item.path)}</p>
    </article>`).join("") : '<p class="onboarding-empty">No endpoint checks.</p>';
    $("onboardingActions").innerHTML = (payload.next_actions || []).length ? (payload.next_actions || []).map((item) => `<article class="onboarding-row"><p>${esc(item)}</p></article>`).join("") : '<p class="onboarding-empty">No next action.</p>';
    $("onboardingQuick").innerHTML = (payload.quick_checks || []).length ? (payload.quick_checks || []).map((item) => `<article class="onboarding-row"><p><code>${esc(item)}</code></p></article>`).join("") : '<p class="onboarding-empty">No quick check.</p>';
    $("onboardingSecurity").innerHTML = `<article class="onboarding-row"><h3>Credential policy</h3><p><span class="pill">token_echoed:${esc(String(diag.token_echoed))}</span><span class="pill">url_token:${esc(String(security.token_in_url_allowed))}</span></p><p>${esc(security.browser_storage || "")}</p></article>
      <article class="onboarding-row"><h3>Truth boundary</h3><p>${(truth.not_claimed || []).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p><p>${esc(security.audit || "")}</p></article>`;
    setStatus(`Connected: key ${key.id || "-"} · scopes ${(key.scopes || []).join(", ")} · generated ${(payload.meta || {}).generated_at || "-"}`);
  }
  async function refresh() {
    setStatus("Loading onboarding diagnostic...");
    renderDiagnostic(await onboardingApi());
  }
  $("onboardingToken").value = state.token;
  $("onboardingConnect").addEventListener("click", async () => {
    state.token = $("onboardingToken").value.trim();
    sessionStorage.setItem(TOKEN_KEY, state.token);
    try { await refresh(); } catch (e) { setStatus(e.message, true); }
  });
  $("onboardingForget").addEventListener("click", () => {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    $("onboardingToken").value = "";
    renderDiagnostic({});
    setStatus("Disconnected");
  });
  if (state.token) refresh().catch(() => setStatus("Stored tab key could not authenticate.", true));
})();
"""
        return _html_response("Pro Onboarding", body, script=script)

    @app.get("/pro/workspace")
    def static_pro_workspace():
        body = """
<style>
.workspace-app{display:grid;gap:14px}
.workspace-bar{display:grid;grid-template-columns:minmax(220px,1fr) auto auto;gap:8px;align-items:end;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}
.workspace-bar label,.workspace-form label{display:grid;gap:5px;color:var(--faint);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.workspace-bar input,.workspace-form input,.workspace-form select,.workspace-form textarea{width:100%;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font:inherit;padding:10px 11px;letter-spacing:0}
.workspace-form textarea{min-height:72px;resize:vertical}
.workspace-button{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font-weight:800;padding:10px 12px;min-height:42px;cursor:pointer}
.workspace-button.primary{background:var(--accent);border-color:var(--accent);color:#06140f}
.workspace-button.warn{border-color:rgba(239,106,82,.45);color:#ffd4cc}
.workspace-button:disabled{opacity:.55;cursor:not-allowed}
.workspace-status{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);color:var(--muted);padding:10px 12px;font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.workspace-grid{display:grid;grid-template-columns:minmax(260px,.82fr) minmax(0,1.18fr);gap:12px;align-items:start}
.workspace-stack{display:grid;gap:12px}
.workspace-panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;min-width:0}
.workspace-panel h2,.workspace-panel h3{font-size:18px;margin:0 0 10px}
.workspace-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.workspace-kpi{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;min-width:0}
.workspace-kpi b{display:block;font-family:var(--mono);font-size:18px;line-height:1.1}
.workspace-kpi span{display:block;color:var(--faint);font-size:11px;margin-top:4px}
.workspace-list{display:grid;gap:8px}
.workspace-row{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;display:grid;gap:7px}
.workspace-row.active{border-color:var(--accent)}
.workspace-row-top{display:flex;align-items:center;justify-content:space-between;gap:10px}
.workspace-row h3{font-size:15px;margin:0;overflow-wrap:anywhere}
.workspace-row p{margin:0;color:var(--muted);font-size:13px}
.workspace-actions,.workspace-toolbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.workspace-toolbar{justify-content:space-between;margin-bottom:10px}
.workspace-toolbar select,.workspace-toolbar input{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);font:inherit;padding:10px 11px}
.workspace-toolbar input{width:120px}
.workspace-mini{font-family:var(--mono);font-size:11px;color:var(--faint)}
.workspace-table{display:block;overflow:auto;border-radius:8px}
.workspace-table table{min-width:760px}
.workspace-empty{color:var(--faint);font-size:13px;margin:0}
.workspace-form{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.workspace-form .wide{grid-column:1/-1}
.workspace-form .actions{grid-column:1/-1;margin:0}
.workspace-form-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.workspace-form-head h2{margin:0}
.workspace-toggle{display:flex!important;align-items:center;gap:8px;text-transform:none!important;letter-spacing:0!important;font-family:var(--sans)!important;font-size:13px!important;color:var(--muted)!important}
.workspace-toggle input{width:auto!important}
.workspace-detail-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}
.workspace-detail-grid div{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;min-width:0}
.workspace-detail-grid b{display:block;font-family:var(--mono);font-size:16px;line-height:1.1}
.workspace-detail-grid span{display:block;color:var(--faint);font-size:11px;margin-top:4px}
@media(max-width:980px){.workspace-grid,.workspace-bar{grid-template-columns:1fr}.workspace-kpis{grid-template-columns:1fr 1fr}.workspace-form{grid-template-columns:1fr}}
@media(max-width:700px){.workspace-kpis,.workspace-detail-grid{grid-template-columns:1fr}}
</style>
<section class="doc-hero"><div class="doc-copy"><div class="kicker">Pro workspace</div>
<h1>Workspace Cockpit</h1>
<p class="doc-lede">Saved watchlists, ticker-flow alerts, signal snapshots and recent activity for a scoped Pro API key.</p></div>
<aside class="doc-panel"><h3>Access boundary</h3><p>Browser data loads only after a valid Pro key is entered. The key is kept in tab session storage and is never sent in the URL.</p>
<p class="meta">Required scopes: funds:read workspace:write</p></aside></section>
<main class="workspace-app" data-pro-workspace-app>
  <section class="workspace-bar" aria-label="Pro workspace access">
    <label>API key <input id="workspaceToken" type="password" autocomplete="off" spellcheck="false" placeholder="13flow_live_..."></label>
    <button id="workspaceConnect" class="workspace-button primary" type="button">Connect</button>
    <button id="workspaceForget" class="workspace-button" type="button">Forget</button>
  </section>
  <div id="workspaceStatus" class="workspace-status">Disconnected</div>
  <section class="workspace-kpis" id="workspaceKpis">
    <div class="workspace-kpi"><b>-</b><span>Watchlists</span></div>
    <div class="workspace-kpi"><b>-</b><span>Open alerts</span></div>
    <div class="workspace-kpi"><b>-</b><span>Snapshots</span></div>
    <div class="workspace-kpi"><b>-</b><span>Activity events</span></div>
  </section>
  <section class="workspace-grid">
    <div class="workspace-stack">
      <section class="workspace-panel">
        <h2>Watchlists</h2>
        <div id="workspaceWatchlists" class="workspace-list"><p class="workspace-empty">No data loaded.</p></div>
      </section>
      <section class="workspace-panel">
        <div class="workspace-form-head"><h2 id="workspaceFormTitle">Create Watchlist</h2>
          <button id="workspaceCancelEdit" class="workspace-button" type="button" hidden>New</button>
        </div>
        <form id="workspaceCreate" class="workspace-form">
          <label>Name <input name="name" maxlength="80" required placeholder="Core tech monitor"></label>
          <label>Tickers <input name="tickers" required placeholder="AAPL, MSFT, NVDA"></label>
          <label>Action <select name="action"><option value="">Any</option><option value="alert">Alert</option><option value="watch">Watch</option><option value="monitor">Monitor</option></select></label>
          <label>Min score <input name="min_score" inputmode="decimal" min="0" max="100" placeholder="30"></label>
          <label class="wide">Move filters <input name="move" placeholder="NEW, ADD"></label>
          <label class="workspace-toggle wide"><input name="alert_enabled" type="checkbox" value="1"> Scheduled alerts</label>
          <label class="wide">Alert frequency <select name="alert_frequency"><option value="manual">Manual</option><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
          <label class="wide">Notes <textarea name="notes" maxlength="1000"></textarea></label>
          <div class="actions"><button id="workspaceSave" class="workspace-button primary" type="submit">Create</button></div>
        </form>
      </section>
    </div>
    <div class="workspace-stack">
      <section class="workspace-panel">
        <div class="workspace-row-top"><h2 id="workspaceSelectedTitle">Signals</h2><div class="workspace-actions">
          <button id="workspaceSnapshot" class="workspace-button primary" type="button" disabled>Snapshot</button>
          <button id="workspaceReportRefresh" class="workspace-button" type="button">Report</button>
          <button id="workspaceExportJson" class="workspace-button" type="button">Export JSON</button>
          <button id="workspaceExportCsv" class="workspace-button" type="button">Export CSV</button>
          <button id="workspaceRefresh" class="workspace-button" type="button">Refresh</button>
        </div></div>
        <div id="workspaceSignals" class="workspace-table"><p class="workspace-empty">Select a watchlist.</p></div>
      </section>
      <section class="workspace-panel">
        <h2>Workspace Report</h2>
        <div id="workspaceReport" class="workspace-list"><p class="workspace-empty">No report loaded.</p></div>
      </section>
      <section class="workspace-panel">
        <div class="workspace-toolbar"><h2>Alerts</h2><div class="workspace-actions">
          <select id="workspaceAlertStatus"><option value="open">Open</option><option value="acknowledged">Ack</option><option value="dismissed">Dismissed</option><option value="all">All</option></select>
          <input id="workspaceAlertTicker" type="search" inputmode="search" maxlength="12" placeholder="Ticker" aria-label="Ticker filter">
          <input id="workspaceAlertMinSeverity" type="number" inputmode="numeric" min="0" max="100" placeholder="Priority" aria-label="Minimum priority">
          <input id="workspaceAlertMinScore" type="number" inputmode="decimal" min="0" max="100" placeholder="Score" aria-label="Minimum score">
          <select id="workspaceAlertSort" aria-label="Alert sort"><option value="severity">Priority</option><option value="score">Score</option><option value="seen">Seen</option><option value="ticker">Ticker</option></select>
          <button id="workspaceAckAll" class="workspace-button" type="button">Ack visible</button>
          <button id="workspaceDismissAll" class="workspace-button warn" type="button">Dismiss visible</button>
        </div></div>
        <div id="workspaceAlerts" class="workspace-table"><p class="workspace-empty">No alerts loaded.</p></div>
      </section>
      <section class="workspace-panel">
        <h2>Alert Details</h2>
        <div id="workspaceAlertDetail" class="workspace-list"><p class="workspace-empty">Select an alert.</p></div>
      </section>
      <section class="workspace-panel">
        <h2>History</h2>
        <div id="workspaceHistory" class="workspace-table"><p class="workspace-empty">No history loaded.</p></div>
      </section>
      <section class="workspace-panel">
        <h2>Activity</h2>
        <div id="workspaceActivity" class="workspace-list"><p class="workspace-empty">No activity loaded.</p></div>
      </section>
    </div>
  </section>
</main>
"""
        script = r"""
(() => {
  const TOKEN_KEY = "13flow.pro.workspace.token";
  const $ = (id) => document.getElementById(id);
  const app = document.querySelector("[data-pro-workspace-app]");
  if (!app) return;
  const state = {token: sessionStorage.getItem(TOKEN_KEY) || "", selectedId: "", editingId: "", selectedAlertId: "", watchlists: [], alerts: [], allAlerts: [], alertSummary: {}, alertStatus: "open", alertTicker: "", alertMinSeverity: "", alertMinScore: "", alertSort: "severity"};
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const setStatus = (msg, bad=false) => {
    const node = $("workspaceStatus");
    node.textContent = msg;
    node.style.borderColor = bad ? "rgba(239,106,82,.55)" : "var(--line-soft)";
  };
  const number = (v) => Number.isFinite(Number(v)) ? String(Number(v)) : "-";
  async function api(path, options={}) {
    if (!state.token) throw new Error("API key required");
    const headers = Object.assign({"Authorization": "Bearer " + state.token}, options.headers || {});
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const res = await fetch("/api/pro/v1" + path, Object.assign({}, options, {headers}));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || ("HTTP " + res.status));
    return data;
  }
  function renderKpis(summary={}) {
    const alerts = (summary.alerts || {}).by_status || {};
    $("workspaceKpis").innerHTML = [
      ["Watchlists", summary.watchlists],
      ["Open alerts", alerts.open],
      ["Snapshots", summary.signal_snapshots],
      ["Activity events", summary.activity_events],
    ].map(([label, value]) => `<div class="workspace-kpi"><b>${esc(number(value))}</b><span>${esc(label)}</span></div>`).join("");
  }
  function renderWatchlists(items=[]) {
    state.watchlists = items;
    if (!items.length) {
      state.selectedId = "";
      $("workspaceWatchlists").innerHTML = '<p class="workspace-empty">No saved watchlist.</p>';
      $("workspaceSnapshot").disabled = true;
      if (state.editingId) resetForm();
      return;
    }
    if (!items.some((w) => w.id === state.selectedId)) state.selectedId = items[0].id;
    if (state.editingId && !items.some((w) => w.id === state.editingId)) resetForm();
    $("workspaceSnapshot").disabled = !state.selectedId;
    $("workspaceWatchlists").innerHTML = items.map((w) => {
      const active = w.id === state.selectedId ? " active" : "";
      const filters = w.filters || {};
      const chips = []
        .concat((filters.action || []).map((x) => "action:" + x))
        .concat((filters.move || []).map((x) => "move:" + x));
      if (filters.min_score !== null && filters.min_score !== undefined) chips.push("score>=" + filters.min_score);
      const policy = w.alert_policy || {};
      chips.push(policy.enabled ? ("alerts:" + (policy.frequency || "daily")) : "alerts:manual");
      return `<article class="workspace-row${active}" data-watchlist-id="${esc(w.id)}">
        <div class="workspace-row-top"><h3>${esc(w.name)}</h3><span class="workspace-mini">${esc(w.tickers.length)} tickers</span></div>
        <p>${esc(w.tickers.join(", "))}</p>
        <p>${chips.length ? chips.map((x) => `<span class="pill">${esc(x)}</span>`).join("") : '<span class="pill">no filters</span>'}</p>
        <div class="workspace-actions">
          <button class="workspace-button" type="button" data-action="select" data-id="${esc(w.id)}">Open</button>
          <button class="workspace-button" type="button" data-action="edit" data-id="${esc(w.id)}">Edit</button>
          <button class="workspace-button primary" type="button" data-action="snapshot" data-id="${esc(w.id)}">Snapshot</button>
          <button class="workspace-button warn" type="button" data-action="delete" data-id="${esc(w.id)}">Delete</button>
        </div>
      </article>`;
    }).join("");
  }
  function renderSignals(payload) {
    const signals = payload || {};
    const items = signals.items || [];
    const meta = signals.metadata || {};
    const selected = state.watchlists.find((w) => w.id === state.selectedId);
    $("workspaceSelectedTitle").textContent = selected ? "Signals · " + selected.name : "Signals";
    if (!items.length) {
      $("workspaceSignals").innerHTML = '<p class="workspace-empty">No matching signal.</p>';
      return;
    }
    $("workspaceSignals").innerHTML = `<table><thead><tr><th>Ticker</th><th>Action</th><th>Score</th><th>Move</th><th>Triggers</th></tr></thead><tbody>${items.map((item) => {
      const triggers = (item.triggers || []).slice(0, 3).map((t) => t.code || t.detail).join(", ");
      const moves = (item.movement_codes || []).join(", ");
      const score = ((item.score || {}).score ?? "-");
      return `<tr><td><a href="/stocks/${esc(item.ticker)}">${esc(item.ticker)}</a></td><td><span class="pill">${esc(item.action)}</span></td><td class="num">${esc(score)}</td><td>${esc(moves)}</td><td>${esc(triggers)}</td></tr>`;
    }).join("")}</tbody></table><p class="workspace-mini">returned=${esc(meta.returned_count || items.length)} filtered=${esc(meta.filtered_count || items.length)}</p>`;
  }
  function alertScore(alert) {
    const score = (alert.reason || {}).score;
    return Number.isFinite(Number(score)) ? Number(score) : -1;
  }
  function alertScoreLabel(alert) {
    const score = (alert.reason || {}).score;
    return Number.isFinite(Number(score)) ? String(Number(score)) : "-";
  }
  function alertSeverity(alert) {
    return Number.isFinite(Number(alert.severity)) ? Number(alert.severity) : -1;
  }
  function visibleAlerts(items=[]) {
    const ticker = state.alertTicker.trim().toUpperCase();
    const minSeverity = state.alertMinSeverity === "" ? null : Number(state.alertMinSeverity);
    const minScore = state.alertMinScore === "" ? null : Number(state.alertMinScore);
    const filtered = items.filter((alert) => {
      if (ticker && !String(alert.ticker || "").toUpperCase().includes(ticker)) return false;
      if (minSeverity !== null && alertSeverity(alert) < minSeverity) return false;
      if (minScore !== null && alertScore(alert) < minScore) return false;
      return true;
    });
    return filtered.sort((a, b) => {
      if (state.alertSort === "score") return alertScore(b) - alertScore(a) || alertSeverity(b) - alertSeverity(a);
      if (state.alertSort === "seen") return String(b.last_seen_at || "").localeCompare(String(a.last_seen_at || ""));
      if (state.alertSort === "ticker") return String(a.ticker || "").localeCompare(String(b.ticker || ""));
      return alertSeverity(b) - alertSeverity(a) || alertScore(b) - alertScore(a);
    });
  }
  function renderAlerts(items=[], summary={}) {
    state.allAlerts = items;
    state.alertSummary = summary || {};
    const visible = visibleAlerts(items);
    state.alerts = visible;
    if (!visible.some((a) => a.id === state.selectedAlertId)) state.selectedAlertId = visible[0]?.id || "";
    const byStatus = summary.by_status || {};
    if (!visible.length) {
      $("workspaceAlerts").innerHTML = `<p class="workspace-empty">No ${esc(state.alertStatus)} alert matches the current filters.</p>`;
      renderAlertDetail(null);
      return;
    }
    $("workspaceAlerts").innerHTML = `<p class="workspace-mini">showing=${esc(number(visible.length))}/${esc(number(items.length))} open=${esc(number(byStatus.open))} ack=${esc(number(byStatus.acknowledged))} dismissed=${esc(number(byStatus.dismissed))}</p>
      <table><thead><tr><th>Ticker</th><th>Priority</th><th>Score</th><th>Status</th><th>Action</th><th>Seen</th><th></th></tr></thead><tbody>${visible.map((a) => `<tr>
      <td><a href="/stocks/${esc(a.ticker)}">${esc(a.ticker)}</a></td><td class="num">${esc(a.severity)}</td><td class="num">${esc(alertScoreLabel(a))}</td><td><span class="pill">${esc(a.status)}</span></td><td>${esc(a.action)}</td><td class="workspace-mini">${esc(a.last_seen_at)}</td>
      <td><div class="workspace-actions">
        <button class="workspace-button primary" type="button" data-alert-detail="${esc(a.id)}">Details</button>
        <button class="workspace-button" type="button" data-alert="${esc(a.id)}" data-status="acknowledged">Ack</button>
        <button class="workspace-button warn" type="button" data-alert="${esc(a.id)}" data-status="dismissed">Dismiss</button>
        <button class="workspace-button" type="button" data-alert="${esc(a.id)}" data-status="open">Reopen</button>
      </div></td>
    </tr>`).join("")}</tbody></table>`;
    renderAlertDetail(state.alerts.find((a) => a.id === state.selectedAlertId) || visible[0]);
  }
  function renderAlertDetail(alert) {
    if (!alert) {
      $("workspaceAlertDetail").innerHTML = '<p class="workspace-empty">Select an alert.</p>';
      return;
    }
    const reason = alert.reason || {};
    const summary = reason.movement_summary || {};
    const triggers = reason.triggers || [];
    const moves = reason.movement_codes || [];
    $("workspaceAlertDetail").innerHTML = `<div class="workspace-detail-grid">
      <div><b>${esc(alert.ticker)}</b><span>Ticker</span></div>
      <div><b>${esc(reason.score ?? "-")}</b><span>Score</span></div>
      <div><b>${esc(alert.severity)}</b><span>Priority</span></div>
      <div><b>${esc(reason.confidence || "-")}</b><span>Confidence</span></div>
    </div>
    <p>${moves.map((x) => `<span class="pill">${esc(x)}</span>`).join("") || '<span class="pill">no move code</span>'}</p>
    <p class="workspace-mini">holders=${esc(number(summary.holder_count))} buyers=${esc(number(summary.buyers_count))} sellers=${esc(number(summary.sellers_count))} new=${esc(number(summary.new_positions))} exits=${esc(number(summary.exits))}</p>
    <div class="workspace-list">${triggers.length ? triggers.map((t) => `<article class="workspace-row"><h3>${esc(t.code || t.severity || "trigger")}</h3><p>${esc(t.detail || "")}</p><p><span class="pill">${esc(t.severity || "-")}</span></p></article>`).join("") : '<p class="workspace-empty">No trigger detail.</p>'}</div>
    <p class="workspace-mini">watchlist=${esc(alert.watchlist_id)} snapshot=${esc(alert.snapshot_id)} latest_13f=${esc(reason.latest_13f_quarter || "-")}</p>`;
  }
  function renderActivity(items=[]) {
    $("workspaceActivity").innerHTML = items.length ? items.map((e) => `<article class="workspace-row">
      <div class="workspace-row-top"><h3>${esc(e.title)}</h3><span class="workspace-mini">${esc(e.created_at)}</span></div>
      <p><span class="pill">${esc(e.event_type)}</span> ${esc(e.entity_type)} ${esc(e.entity_id)}</p>
    </article>`).join("") : '<p class="workspace-empty">No recent activity.</p>';
  }
  function renderHistory(items=[]) {
    if (!items.length) {
      $("workspaceHistory").innerHTML = '<p class="workspace-empty">No snapshot history.</p>';
      return;
    }
    $("workspaceHistory").innerHTML = `<table><thead><tr><th>Created</th><th>Tickers</th><th>Alerts</th><th>Watch</th></tr></thead><tbody>${items.map((s) => {
      const summary = s.summary || {};
      return `<tr><td class="workspace-mini">${esc(s.created_at)}</td><td>${esc((s.tickers || []).join(", "))}</td><td class="num">${esc(number(summary.alerts))}</td><td class="num">${esc(number(summary.watch))}</td></tr>`;
    }).join("")}</tbody></table>`;
  }
  function renderWorkspaceReport(payload={}) {
    const reports = payload.watchlists || [];
    if (!reports.length) {
      $("workspaceReport").innerHTML = '<p class="workspace-empty">No report available.</p>';
      return;
    }
    const summary = (payload.executive_summary || []).map((line) => `<p>${esc(line)}</p>`).join("");
    const sections = reports.slice(0, 5).map((entry) => {
      const watchlist = entry.watchlist || {};
      const delta = entry.delta || {};
      const lines = (entry.summary_lines || []).map((line) => `<p>${esc(line)}</p>`).join("");
      const alerts = (entry.top_alerts || []).slice(0, 3).map((a) => `<span class="pill">${esc(a.ticker)} ${esc(a.action)} p${esc(a.severity)}</span>`).join("") || '<span class="pill">no alert</span>';
      const signals = (entry.top_signals || []).slice(0, 5).map((s) => `<span class="pill">${esc(s.ticker)} ${esc(s.action)} ${esc(s.score ?? "-")}</span>`).join("") || '<span class="pill">no signal</span>';
      return `<article class="workspace-row">
        <div class="workspace-row-top"><h3>${esc(watchlist.name || "Watchlist")}</h3><span class="workspace-mini">${esc((watchlist.tickers || []).length)} tickers</span></div>
        ${lines}
        <p><span class="pill">added:${esc((delta.added_tickers || []).length)}</span><span class="pill">removed:${esc((delta.removed_tickers || []).length)}</span><span class="pill">action changes:${esc((delta.changed_actions || []).length)}</span></p>
        <p>${alerts}</p>
        <p>${signals}</p>
      </article>`;
    }).join("");
    $("workspaceReport").innerHTML = `<article class="workspace-row"><h3>Executive Summary</h3>${summary}</article>${sections}`;
  }
  async function loadWorkspaceReport() {
    const path = state.selectedId ? `/workspace/report?watchlist_id=${encodeURIComponent(state.selectedId)}` : "/workspace/report";
    const report = await api(path);
    renderWorkspaceReport(report);
  }
  async function loadSelected() {
    if (!state.selectedId) {
      renderSignals({items: []});
      renderHistory([]);
      await loadWorkspaceReport();
      return;
    }
    const signals = await api(`/workspace/watchlists/${state.selectedId}/signals`);
    renderSignals(signals.signals);
    const history = await api(`/workspace/watchlists/${state.selectedId}/signals/history?limit=10`);
    renderHistory(history.history || []);
    await loadWorkspaceReport();
  }
  async function refreshAll() {
    setStatus("Loading workspace...");
    const status = await api("/status");
    const overview = await api("/workspace/overview");
    const alerts = await api(`/workspace/alerts?status=${encodeURIComponent(state.alertStatus)}&limit=50`);
    renderKpis(overview.summary || {});
    renderWatchlists(overview.watchlists || []);
    renderAlerts(alerts.alerts || [], alerts.summary || {});
    renderActivity(overview.recent_activity || []);
    await loadSelected();
    const limits = status.workspace_limits || {};
    setStatus(`Connected: key ${status.key.id} · ${status.key.tier} · watchlists ${overview.summary.watchlists || 0}/${limits.max_watchlists_per_key || "-"}`);
  }
  async function snapshot(id) {
    state.selectedId = id || state.selectedId;
    if (!state.selectedId) return;
    setStatus("Creating snapshot...");
    await api(`/workspace/watchlists/${state.selectedId}/signals/snapshot`, {method: "POST"});
    await refreshAll();
  }
  async function downloadWorkspaceExport(format) {
    if (!state.token) throw new Error("API key required");
    const safeFormat = format === "csv" ? "csv" : "json";
    const res = await fetch(`/api/pro/v1/workspace/export?format=${safeFormat}`, {
      headers: {"Authorization": "Bearer " + state.token},
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || data.detail || ("HTTP " + res.status));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `13flow-workspace-export.${safeFormat}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus(`Workspace export downloaded: ${safeFormat.toUpperCase()}`);
  }
  async function deleteWatchlist(id) {
    const item = state.watchlists.find((w) => w.id === id);
    if (item && !window.confirm(`Delete watchlist "${item.name}"?`)) return;
    await api(`/workspace/watchlists/${id}/delete`, {method: "POST"});
    if (state.selectedId === id) state.selectedId = "";
    if (state.editingId === id) resetForm();
    await refreshAll();
  }
  function resetForm() {
    state.editingId = "";
    $("workspaceCreate").reset();
    $("workspaceFormTitle").textContent = "Create Watchlist";
    $("workspaceSave").textContent = "Create";
    $("workspaceCancelEdit").hidden = true;
  }
  function splitValues(raw) {
    return String(raw || "").split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
  }
  function field(form, name) {
    return form.elements.namedItem(name);
  }
  function watchlistPayloadFromForm(form) {
    const data = new FormData(form);
    const filters = {};
    const action = String(data.get("action") || "").trim();
    const minScoreRaw = String(data.get("min_score") || "").trim();
    const move = splitValues(data.get("move"));
    if (action) filters.action = [action];
    if (minScoreRaw) {
      const minScore = Number(minScoreRaw);
      if (!Number.isFinite(minScore) || minScore < 0 || minScore > 100) {
        throw new Error("Min score must be between 0 and 100");
      }
      filters.min_score = minScore;
    }
    if (move.length) filters.move = move;
    const alertEnabled = Boolean(data.get("alert_enabled"));
    const alertFrequency = String(data.get("alert_frequency") || "manual").trim().toLowerCase();
    if (alertEnabled && !["daily", "weekly"].includes(alertFrequency)) {
      throw new Error("Scheduled alerts require daily or weekly frequency");
    }
    return {
      name: String(data.get("name") || "").trim(),
      tickers: splitValues(data.get("tickers")),
      filters,
      alert_policy: {enabled: alertEnabled, frequency: alertEnabled ? alertFrequency : "manual"},
      notes: String(data.get("notes") || "").trim(),
    };
  }
  function editWatchlist(id) {
    const item = state.watchlists.find((w) => w.id === id);
    if (!item) return;
    const form = $("workspaceCreate");
    const filters = item.filters || {};
    const policy = item.alert_policy || {};
    state.editingId = id;
    field(form, "name").value = item.name || "";
    field(form, "tickers").value = (item.tickers || []).join(", ");
    field(form, "action").value = (filters.action || [])[0] || "";
    field(form, "min_score").value = filters.min_score ?? "";
    field(form, "move").value = (filters.move || []).join(", ");
    field(form, "alert_enabled").checked = Boolean(policy.enabled);
    field(form, "alert_frequency").value = policy.frequency || "manual";
    field(form, "notes").value = item.notes || "";
    $("workspaceFormTitle").textContent = "Edit Watchlist";
    $("workspaceSave").textContent = "Save changes";
    $("workspaceCancelEdit").hidden = false;
    setStatus(`Editing: ${item.name}`);
  }
  $("workspaceToken").value = state.token;
  $("workspaceConnect").addEventListener("click", async () => {
    state.token = $("workspaceToken").value.trim();
    sessionStorage.setItem(TOKEN_KEY, state.token);
    try { await refreshAll(); } catch (e) { setStatus(e.message, true); }
  });
  $("workspaceForget").addEventListener("click", () => {
    state.token = "";
    state.selectedId = "";
    sessionStorage.removeItem(TOKEN_KEY);
    $("workspaceToken").value = "";
    resetForm();
    renderKpis({});
    renderWatchlists([]);
    renderSignals({items: []});
    renderAlerts([]);
    renderAlertDetail(null);
    renderWorkspaceReport({});
    renderActivity([]);
    renderHistory([]);
    setStatus("Disconnected");
  });
  $("workspaceRefresh").addEventListener("click", () => refreshAll().catch((e) => setStatus(e.message, true)));
  $("workspaceSnapshot").addEventListener("click", () => snapshot().catch((e) => setStatus(e.message, true)));
  $("workspaceReportRefresh").addEventListener("click", () => loadWorkspaceReport().catch((e) => setStatus(e.message, true)));
  $("workspaceExportJson").addEventListener("click", () => downloadWorkspaceExport("json").catch((e) => setStatus(e.message, true)));
  $("workspaceExportCsv").addEventListener("click", () => downloadWorkspaceExport("csv").catch((e) => setStatus(e.message, true)));
  $("workspaceCancelEdit").addEventListener("click", resetForm);
  $("workspaceAlertStatus").addEventListener("change", (event) => {
    state.alertStatus = event.target.value;
    refreshAll().catch((e) => setStatus(e.message, true));
  });
  function refreshAlertFilters() {
    state.alertTicker = $("workspaceAlertTicker").value.trim();
    state.alertMinSeverity = $("workspaceAlertMinSeverity").value.trim();
    state.alertMinScore = $("workspaceAlertMinScore").value.trim();
    state.alertSort = $("workspaceAlertSort").value;
    renderAlerts(state.allAlerts, state.alertSummary);
  }
  $("workspaceAlertTicker").addEventListener("input", refreshAlertFilters);
  $("workspaceAlertMinSeverity").addEventListener("input", refreshAlertFilters);
  $("workspaceAlertMinScore").addEventListener("input", refreshAlertFilters);
  $("workspaceAlertSort").addEventListener("change", refreshAlertFilters);
  async function updateVisibleAlerts(status) {
    const ids = state.alerts.map((a) => a.id).filter(Boolean).slice(0, 50);
    if (!ids.length) return;
    setStatus(`Updating ${ids.length} alert(s)...`);
    for (const id of ids) {
      await api(`/workspace/alerts/${id}`, {method: "PATCH", body: JSON.stringify({status})});
    }
    await refreshAll();
  }
  $("workspaceAckAll").addEventListener("click", () => updateVisibleAlerts("acknowledged").catch((e) => setStatus(e.message, true)));
  $("workspaceDismissAll").addEventListener("click", () => updateVisibleAlerts("dismissed").catch((e) => setStatus(e.message, true)));
  $("workspaceCreate").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = watchlistPayloadFromForm(event.currentTarget);
      if (state.editingId) {
        const updated = await api(`/workspace/watchlists/${state.editingId}`, {method: "PUT", body: JSON.stringify(payload)});
        state.selectedId = updated.watchlist.id;
      } else {
        const created = await api("/workspace/watchlists", {method: "POST", body: JSON.stringify(payload)});
        state.selectedId = created.watchlist.id;
      }
      resetForm();
      await refreshAll();
    } catch (e) { setStatus(e.message, true); }
  });
  app.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (button.dataset.action === "select") {
        state.selectedId = button.dataset.id;
        renderWatchlists(state.watchlists);
        await loadSelected();
      }
      if (button.dataset.action === "edit") editWatchlist(button.dataset.id);
      if (button.dataset.action === "snapshot") await snapshot(button.dataset.id);
      if (button.dataset.action === "delete") await deleteWatchlist(button.dataset.id);
      if (button.dataset.alertDetail) {
        state.selectedAlertId = button.dataset.alertDetail;
        renderAlertDetail(state.alerts.find((a) => a.id === state.selectedAlertId));
      }
      if (button.dataset.alert) {
        await api(`/workspace/alerts/${button.dataset.alert}`, {method: "PATCH", body: JSON.stringify({status: button.dataset.status})});
        await refreshAll();
      }
    } catch (e) { setStatus(e.message, true); }
  });
  if (state.token) refreshAll().catch(() => setStatus("Stored tab key could not authenticate.", true));
})();
"""
        return _html_response("Pro Workspace", body, script=script)

    def _admin_login_response(error: str = "") -> Response:
        csrf = _admin_csrf_token()
        user = _admin_auth_user()
        totp = _admin_totp_required()
        error_html = (
            f'<div class="admin-login-error">{html_escape(error)}</div>'
            if error else ""
        )
        totp_field = (
            '<label>Authenticator code <input name="totp" inputmode="numeric" '
            'autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>'
            if totp else ""
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login · 13FLOW</title>
<style>
body{{margin:0;background:#06110c;color:#e9f7ef;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;display:grid;place-items:center;padding:20px}}
.admin-login{{width:min(420px,100%);border:1px solid rgba(116,156,132,.28);border-radius:8px;background:#102219;padding:18px;box-shadow:0 18px 60px rgba(0,0,0,.32)}}
h1{{font-size:24px;margin:0 0 6px}}p{{margin:0 0 14px;color:#a9bdb1;font-size:14px;line-height:1.45}}
form{{display:grid;gap:12px}}label{{display:grid;gap:5px;font-size:13px;color:#b8cfc1}}
input{{box-sizing:border-box;width:100%;border:1px solid rgba(116,156,132,.35);border-radius:8px;background:#08140f;color:#effbf3;padding:11px;font:inherit}}
button{{border:0;border-radius:8px;background:#20c48d;color:#04120c;padding:11px 14px;font-weight:750;cursor:pointer}}
.admin-login-error{{border:1px solid rgba(239,106,82,.55);border-radius:8px;background:rgba(239,106,82,.08);color:#ffd6cc;padding:10px;margin-bottom:12px;font-size:13px}}
.meta{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#7f9a89;font-size:11px;margin-top:12px}}
</style></head><body>
<main class="admin-login">
<h1>13FLOW Admin</h1>
<p>Sign in with the server-side admin account. API mutations still require a scoped admin API key inside the panel.</p>
{error_html}
<form method="post" action="/pro/admin/login" autocomplete="off">
<input type="hidden" name="csrf" value="{html_escape(csrf, quote=True)}">
<label>Email <input name="username" type="email" autocomplete="username" value="{html_escape(user, quote=True)}" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
{totp_field}
<button type="submit">Sign in</button>
</form>
<p class="meta">session={_admin_session_seconds()}s · totp_required={str(totp).lower()}</p>
</main></body></html>"""
        resp = Response(html, mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return resp

    @app.get("/pro/admin/login")
    def static_pro_admin_login():
        if not _admin_auth_configured():
            abort(404)
        if _admin_session_active():
            return redirect(url_for("static_pro_admin"))
        return _admin_login_response()

    @app.post("/pro/admin/login")
    def static_pro_admin_login_post():
        if not _admin_auth_configured():
            abort(404)
        ip = client_ip()
        if _admin_login_blocked(ip):
            app.logger.warning("admin_login_blocked ip=%s", ip)
            return _admin_login_response("Too many failed attempts. Try again later."), 429
        csrf = request.form.get("csrf", "")
        if not csrf or not hmac.compare_digest(csrf, session.get("admin_csrf", "")):
            _admin_record_login_failure(ip)
            app.logger.warning("admin_login_failed reason=csrf ip=%s", ip)
            return _admin_login_response("Session expired. Try again."), 400
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        totp = request.form.get("totp", "").strip()
        if (
            not hmac.compare_digest(username, _admin_auth_user())
            or not _admin_verify_password(password)
            or not _admin_verify_totp(totp)
        ):
            _admin_record_login_failure(ip)
            app.logger.warning("admin_login_failed user=%s ip=%s", username or "-", ip)
            return _admin_login_response("Invalid admin credentials."), 401
        session.clear()
        session["admin_user"] = _admin_auth_user()
        session["admin_login_at"] = int(time.time())
        session["admin_last_seen"] = int(time.time())
        session["admin_csrf"] = secrets.token_urlsafe(32)
        _admin_clear_login_failures(ip)
        app.logger.info("admin_login_ok user=%s ip=%s", _admin_auth_user(), ip)
        return redirect(url_for("static_pro_admin"))

    @app.post("/pro/admin/logout")
    def static_pro_admin_logout():
        user = session.get("admin_user", "-")
        session.clear()
        app.logger.info("admin_logout user=%s ip=%s", user, client_ip())
        return redirect(url_for("static_pro_admin_login"))

    @app.get("/pro/admin")
    @admin_panel_required
    def static_pro_admin():
        body = """
<style>
.admin-app{display:grid;gap:10px}
.admin-bar{display:grid;grid-template-columns:minmax(260px,1fr) auto auto auto;gap:8px;align-items:end;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px}
.admin-panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px;min-width:0}
.admin-panel h2,.admin-panel h3{font-size:15px;margin:0 0 8px}
.admin-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.admin-kpi{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:10px;min-width:0}
.admin-kpi b{display:block;font-family:var(--mono);font-size:18px;line-height:1.1}
.admin-kpi span{display:block;color:var(--faint);font-size:11px;margin-top:4px}
.admin-grid{display:grid;grid-template-columns:1.05fr 1fr;gap:10px}
.admin-list{display:grid;gap:8px}
.admin-row{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:8px;display:grid;gap:6px}
.admin-row-top{display:flex;align-items:center;justify-content:space-between;gap:10px}
.admin-row h3{font-size:13px;margin:0;overflow-wrap:anywhere}
.admin-row p{margin:0;color:var(--muted);font-size:12px}
.admin-status{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel);padding:10px;color:var(--muted);font-size:13px}
.admin-button{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--text);padding:8px 10px;font:inherit;cursor:pointer}
.admin-button.primary{background:var(--accent);color:#05120d;border-color:transparent}
.admin-button.danger{border-color:rgba(239,106,82,.45);color:#ffb09f}
.admin-form{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.admin-form label{display:grid;gap:4px;font-size:12px;color:var(--muted)}
.admin-form input,.admin-form select{width:100%;box-sizing:border-box}
.admin-token-once{border-color:rgba(255,197,92,.55);background:rgba(255,197,92,.08);color:var(--text);overflow-wrap:anywhere}
.admin-mini{font-family:var(--mono);font-size:11px;color:var(--faint)}
.admin-empty{color:var(--faint);font-size:13px;margin:0}
.admin-secondary{display:none}
@media(max-width:900px){.admin-bar,.admin-grid{grid-template-columns:1fr}.admin-kpis{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.admin-kpis{grid-template-columns:1fr}}
</style>
<section class="doc-hero"><div class="doc-copy"><div class="kicker">Pro admin</div>
<h1>Admin Console</h1>
<p class="doc-lede">Compact control plane for status, actionable errors and Pro API key lifecycle.</p></div>
<aside class="doc-panel"><h3>Locked surface</h3><p>Server-side admin session protects this page. Mutations require an API key with admin:write.</p></aside></section>
<main class="admin-app" data-pro-admin-app>
  <section class="admin-bar" aria-label="Pro admin access">
    <label>Admin API key <input id="adminToken" type="password" autocomplete="off" spellcheck="false" placeholder="13flow_live_... with admin:read or admin:write"></label>
    <button id="adminConnect" class="admin-button primary" type="button">Connect</button>
    <button id="adminForget" class="admin-button" type="button">Forget</button>
    <button id="adminLogout" class="admin-button" type="button">Logout</button>
  </section>
  <div id="adminStatus" class="admin-status">Disconnected</div>
  <section class="admin-kpis" id="adminKpis">
    <div class="admin-kpi"><b>-</b><span>Active keys</span></div>
    <div class="admin-kpi"><b>-</b><span>Rotation due</span></div>
    <div class="admin-kpi"><b>-</b><span>Open alerts</span></div>
    <div class="admin-kpi"><b>-</b><span>Server errors</span></div>
  </section>
  <section class="admin-grid">
    <section class="admin-panel"><h2>Priority</h2><div id="adminOps" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel"><h2>Errors</h2><div id="adminAudit" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel"><h2>API Keys</h2><div id="adminKeys" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel"><h2>Create API key</h2>
      <form id="adminCreateKey" class="admin-form">
        <label>Label <input name="label" required maxlength="120" placeholder="Acme pilot"></label>
        <label>Email <input name="contact_email" type="email" required maxlength="200" placeholder="ops@example.com"></label>
        <label>Scopes <select name="scopes"><option value="funds:read,quality:read,workspace:write">funds + quality + workspace</option><option value="funds:read,quality:read">funds + quality</option><option value="funds:read">funds only</option></select></label>
        <label>Expires days <input name="expires_days" type="number" min="1" max="365" value="30"></label>
        <label>Rotation days <input name="rotation_days" type="number" min="1" max="365" value="21"></label>
        <label>Rate/day <input name="rate_per_day" type="number" min="1" max="1000000" value="10000"></label>
        <button class="admin-button primary" type="submit">Create key</button>
      </form>
      <div id="adminCreatedToken" class="admin-status admin-token-once" hidden></div>
    </section>
    <section class="admin-panel admin-secondary"><h2>Release Readiness</h2><div id="adminRelease" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Workspace</h2><div id="adminWorkspace" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>External Checks</h2><div id="adminExternal" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Pilot Fulfillment</h2><div id="adminPilot" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Buyer Handoff</h2><div id="adminHandoff" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Pilot Closeout</h2><div id="adminCloseout" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Pilot Renewal</h2><div id="adminRenewal" class="admin-list"><p class="admin-empty">No data loaded.</p></div></section>
    <section class="admin-panel admin-secondary"><h2>Pilot Request Assist</h2><div id="adminRequestAssist" class="admin-list"><p class="admin-empty">No data loaded.</p></div><textarea id="adminRequestNote" class="admin-status" spellcheck="false" rows="9"></textarea><p><button id="adminReviewRequest" class="admin-button primary" type="button">Review request</button></p></section>
  </section>
</main>
"""
        script = r"""
(() => {
  const TOKEN_KEY = "13flow.pro.admin.token";
  const $ = (id) => document.getElementById(id);
  const app = document.querySelector("[data-pro-admin-app]");
  if (!app) return;
  const ADMIN_PATHS = [
    "/admin/health",
    "/admin/ops",
    "/admin/pilot-fulfillment",
    "/admin/buyer-handoff",
    "/admin/release-readiness",
    "/admin/pilot-closeout",
    "/admin/pilot-renewal",
    "/admin/pilot-request-assist",
  ];
  void ADMIN_PATHS;
  const state = {token: sessionStorage.getItem(TOKEN_KEY) || ""};
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (v) => Number.isFinite(Number(v)) ? String(Number(v)) : "-";
  const setStatus = (msg, bad=false) => {
    const node = $("adminStatus");
    node.textContent = msg;
    node.style.borderColor = bad ? "rgba(239,106,82,.55)" : "var(--line-soft)";
  };
  async function adminApi(path, init={}) {
    if (!state.token) throw new Error("Admin API key required");
    const headers = Object.assign({"Authorization": "Bearer " + state.token}, init.headers || {});
    if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const res = await fetch("/api/pro/v1" + path, Object.assign({}, init, {headers}));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || ("HTTP " + res.status));
    return data;
  }
  function renderRelease(payload={}) {
    const release = payload.release_readiness || {};
    const decision = release.decision || {};
    const boundary = release.release_boundary || {};
    const smokes = release.required_smokes || {};
    const statuses = release.source_statuses || {};
    const privacy = release.privacy || {};
    const blockers = decision.blockers || [];
    const notices = decision.notices || [];
    const actions = release.operator_next_actions || [];
    $("adminRelease").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((release.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(release.generated_at || "-")}</span></div>
      <p><span class="pill">go:${esc(String(decision.go))}</span><span class="pill">pilot_key:${esc(String(decision.can_issue_pilot_key))}</span><span class="pill">handoff:${esc(String(decision.can_send_buyer_handoff))}</span><span class="pill">renewal:${esc(decision.renewal_decision || "-")}</span></p>
      <p class="admin-mini">ops=${esc(statuses.ops || "-")} fulfillment=${esc(statuses.pilot_fulfillment || "-")} handoff=${esc(statuses.buyer_handoff || "-")} closeout=${esc(statuses.pilot_closeout || "-")}</p>
    </article>
    <article class="admin-row"><h3>Boundary</h3><p><span class="pill">auth_self_serve:${esc(String(boundary.browser_auth_self_serve))}</span><span class="pill">payment_self_serve:${esc(String(boundary.self_serve_payment))}</span><span class="pill">operator_keys:${esc(String(boundary.operator_issued_keys))}</span><span class="pill">investment_advice:${esc(String(!boundary.not_investment_advice))}</span></p></article>
    <article class="admin-row"><h3>Required smokes</h3><p>${Object.entries(smokes).map(([name, cmd]) => `<span class="pill">${esc(name)}</span><code>${esc(cmd || "-")}</code>`).join(" ")}</p></article>
    <article class="admin-row"><h3>Blockers</h3><p>${blockers.length ? blockers.map((x) => `<span class="pill">${esc(x)}</span>`).join("") : '<span class="pill">none</span>'}</p></article>
    <article class="admin-row"><h3>Notices</h3><p>${notices.length ? notices.slice(0, 6).map((x) => `<span class="pill">${esc(x)}</span>`).join("") : '<span class="pill">none</span>'}</p></article>
    <article class="admin-row"><h3>Next actions</h3><p>${actions.slice(0, 5).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p><p class="admin-mini">tokens=${esc(String(privacy.tokens_included))} secrets=${esc(String(privacy.secrets_included))} payloads_logged=${esc(String(privacy.payloads_logged))}</p></article>`;
  }
  function renderOps(payload={}) {
    const ops = payload.ops || {};
    const verdict = ops.verdict || {};
    const data = ops.public_data || {};
    const quality = data.quality_summary || {};
    const automation = ops.workspace_automation || {};
    const actions = verdict.operator_actions || [];
    $("adminOps").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((verdict.status || ops.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(ops.generated_at || "-")}</span></div>
      <p><span class="pill">state:${esc(data.public_state || "-")}</span><span class="pill">latest_13f:${esc(data.latest_13f_quarter || "-")}</span><span class="pill">trusted:${esc(number(quality.trusted_funds))}</span><span class="pill">due:${esc(number(automation.due_count))}</span></p>
      <p class="admin-mini">backup_verify=${esc((ops.backup || {}).operator_verify_command || "-")} · shell_checks=${esc(String((ops.service_contracts || {}).read_only_web_worker_shell_checks))}</p>
    </article>` +
      ((verdict.critical || []).map((x) => `<article class="admin-row"><h3>Critical</h3><p>${esc(x)}</p></article>`).join("")) +
      ((verdict.warnings || []).map((x) => `<article class="admin-row"><h3>Warning</h3><p>${esc(x)}</p></article>`).join("")) +
      ((verdict.notices || []).map((x) => `<article class="admin-row"><h3>Notice</h3><p>${esc(x)}</p></article>`).join("")) +
      (actions.length ? actions.slice(0, 8).map((x) => `<article class="admin-row"><h3>Action</h3><p>${esc(x)}</p></article>`).join("") : '<p class="admin-empty">No operator action.</p>');
  }
  function renderHealth(payload={}) {
    const health = payload.health || {};
    const keys = health.keys || {};
    const audit = health.audit || {};
    const workspace = health.workspace || {};
    $("adminKpis").innerHTML = [
      ["Active keys", keys.active],
      ["Rotation due", keys.rotation_due],
      ["Open alerts", workspace.open_alerts],
      ["Server errors", audit.server_errors],
    ].map(([label, value]) => `<div class="admin-kpi"><b>${esc(number(value))}</b><span>${esc(label)}</span></div>`).join("");
    const rows = keys.recent || [];
    $("adminKeys").innerHTML = rows.length ? rows.map((k) => `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc(k.label || k.id)}</h3><span class="admin-mini">${esc(k.id)}</span></div>
      <p><span class="pill">${esc(k.revoked ? "revoked" : (k.expired ? "expired" : "active"))}</span><span class="pill">rotation:${esc(k.rotation_due ? "due" : "scheduled")}</span><span class="pill">scopes:${esc((k.scopes || []).join(","))}</span></p>
      <p class="admin-mini">email=${esc(k.contact_email || "-")}</p>
      <p class="admin-mini">expires=${esc(k.expires_at || "-")} rotation_due=${esc(k.rotation_due_at || "-")} last_used=${esc(k.last_used_at || "-")}</p>
      <p>${k.revoked ? "" : `<button class="admin-button danger" type="button" data-revoke-key="${esc(k.id)}">Revoke</button>`}</p>
    </article>`).join("") : '<p class="admin-empty">No key usage bucket yet.</p>';
    $("adminAudit").innerHTML = `<article class="admin-row"><h3>Status</h3><p><span class="pill">${esc(health.status || "unknown")}</span><span class="pill">5xx:${esc(number(audit.server_errors))}</span><span class="pill">401:${esc(number(audit.unauthorized))}</span><span class="pill">403:${esc(number(audit.forbidden))}</span><span class="pill">429:${esc(number(audit.rate_limited))}</span></p><p class="admin-mini">latest=${esc(audit.latest_at || "-")}</p></article>` +
      ((audit.recent_errors || []).map((e) => `<article class="admin-row"><div class="admin-row-top"><h3>${esc(e.status)} ${esc(e.method)} ${esc(e.route)}</h3><span class="admin-mini">${esc(e.at)}</span></div><p><span class="pill">key:${esc(e.key_id || "-")}</span></p></article>`).join("") || '<p class="admin-empty">No recent error.</p>');
    $("adminWorkspace").innerHTML = `<article class="admin-row"><h3>Workspace totals</h3><p><span class="pill">watchlists:${esc(number(workspace.watchlists))}</span><span class="pill">snapshots:${esc(number(workspace.signal_snapshots))}</span><span class="pill">alerts:${esc(number(workspace.alerts))}</span><span class="pill">activity:${esc(number(workspace.activity_events))}</span></p><p class="admin-mini">latest_snapshot=${esc(workspace.latest_snapshot_at || "-")}</p></article>`;
    const external = health.external_checks || {};
    $("adminExternal").innerHTML = `<article class="admin-row"><h3>Out-of-process checks</h3><p>${esc(external.reason || "")}</p><p>${(external.expected_units || []).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p><p>${(external.expected_smokes || []).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>`;
    setStatus(`Connected: admin key ${payload.meta?.admin_key_id || "-"} · ${health.status || "unknown"} · generated ${health.generated_at || "-"}`);
  }
  function renderPilot(payload={}) {
    const pack = payload.pilot_fulfillment || {};
    const limits = pack.default_limits || {};
    const policy = pack.least_privilege_policy || {};
    const checklist = pack.checklist || {};
    const commands = pack.operator_commands || {};
    const events = pack.operator_events || {};
    const before = checklist.before_issue || [];
    const after = checklist.after_issue || [];
    const recent = events.recent || [];
    $("adminPilot").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((pack.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(pack.generated_at || "-")}</span></div>
      <p><span class="pill">read_only:${esc(String(pack.read_only))}</span><span class="pill">web_creates_tokens:${esc(String(pack.web_worker_creates_tokens))}</span><span class="pill">tokens_exposed:${esc(String(pack.tokens_exposed))}</span></p>
      <p><span class="pill">scopes:${esc((policy.default_customer_scopes || []).join(","))}</span><span class="pill">forbidden:${esc((policy.customer_forbidden_scopes || []).join(","))}</span></p>
      <p class="admin-mini">limits=${esc(number(limits.rate_per_min))}/min ${esc(number(limits.rate_per_day))}/day expiry=${esc(number(limits.expires_days))}d rotation=${esc(number(limits.rotation_days))}d operator_events=${esc(number(events.total))}</p>
    </article>
    <article class="admin-row"><h3>Create key command</h3><p><code>${esc(commands.create_bounded_pilot_key || "-")}</code></p></article>
    <article class="admin-row"><h3>Verify command</h3><p><code>${esc(commands.verify_issued_key_status || "-")}</code></p></article>
    <article class="admin-row"><h3>Recent operator events</h3><p>${recent.length ? recent.slice(0, 6).map((e) => `<span class="pill">${esc(e.event_type)}:${esc(e.key_id || "-")}</span>`).join("") : '<span class="pill">none</span>'}</p><p class="admin-mini">tokens_stored=${esc(String((events.privacy || {}).tokens_stored))} hashes_exposed=${esc(String((events.privacy || {}).token_hashes_exposed))}</p></article>
    <article class="admin-row"><h3>Before issue</h3><p>${before.slice(0, 6).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>
    <article class="admin-row"><h3>After issue</h3><p>${after.slice(0, 4).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>`;
  }
  function renderHandoff(payload={}) {
    const handoff = payload.buyer_handoff || {};
    const pack = handoff.customer_pack || {};
    const summary = handoff.issued_key_summary_template || {};
    const commands = handoff.customer_commands || {};
    const delivery = handoff.token_delivery || {};
    const privacy = handoff.privacy || {};
    const checklist = handoff.operator_checklist || [];
    $("adminHandoff").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((handoff.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(handoff.generated_at || "-")}</span></div>
      <p><span class="pill">tokens_included:${esc(String(handoff.tokens_included))}</span><span class="pill">web_delivery:${esc(String(delivery.web_worker_delivers_token))}</span><span class="pill">operator_delivery:${esc(String(delivery.operator_delivery_required))}</span></p>
      <p class="admin-mini">${esc(pack.positioning || "")}</p>
    </article>
    <article class="admin-row"><h3>Issued key summary template</h3><p><span class="pill">scopes:${esc((summary.scopes || []).join(","))}</span><span class="pill">limits:${esc(number(summary.rate_per_min))}/min ${esc(number(summary.rate_per_day))}/day</span><span class="pill">watchlists:${esc(number(summary.max_watchlists_per_key))}</span></p><p class="admin-mini">key_id=${esc(summary.key_id || "-")} expires=${esc(summary.expires_at || "-")} rotation=${esc(summary.rotation_due_at || "-")}</p></article>
    <article class="admin-row"><h3>Customer commands</h3><p>${Object.entries(commands).map(([name, cmd]) => `<span class="pill">${esc(name)}</span><code>${esc(cmd)}</code>`).join(" ")}</p></article>
    <article class="admin-row"><h3>Operator checklist</h3><p>${checklist.slice(0, 6).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>
    <article class="admin-row"><h3>Privacy</h3><p><span class="pill">tokens_echoed:${esc(String(privacy.tokens_echoed))}</span><span class="pill">hashes_exposed:${esc(String(privacy.token_hashes_exposed))}</span><span class="pill">payloads_logged:${esc(String(privacy.payloads_logged))}</span></p></article>`;
  }
  function renderCloseout(payload={}) {
    const report = payload.pilot_closeout || {};
    const summary = report.summary || {};
    const verdict = report.verdict || {};
    const privacy = report.privacy || {};
    const keys = report.keys || [];
    const actions = report.operator_next_actions || [];
    $("adminCloseout").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((verdict.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc((report.window || {}).since || "-")} -> ${esc((report.window || {}).until || "-")}</span></div>
      <p><span class="pill">keys:${esc(number(summary.keys))}</span><span class="pill">requests:${esc(number(summary.requests))}</span><span class="pill">ok:${esc(number(summary.ok_requests))}</span><span class="pill">errors:${esc(number(summary.server_errors))}</span><span class="pill">snapshots:${esc(number(summary.snapshots))}</span><span class="pill">alerts:${esc(number(summary.alerts))}</span></p>
      <p class="admin-mini">${(verdict.reasons || []).map((x) => esc(x)).join(" · ")}</p>
    </article>
    <article class="admin-row"><h3>Key summaries</h3><p>${keys.slice(0, 5).map((k) => `<span class="pill">${esc((k.key || {}).label || (k.key || {}).id)}:${esc(number((k.usage || {}).requests))} req:${esc(number((k.workspace || {}).watchlists))} wl</span>`).join("") || '<span class="pill">none</span>'}</p></article>
    <article class="admin-row"><h3>Closeout actions</h3><p>${actions.slice(0, 4).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>
    <article class="admin-row"><h3>Closeout privacy</h3><p><span class="pill">tokens_echoed:${esc(String(privacy.tokens_echoed))}</span><span class="pill">hashes_exposed:${esc(String(privacy.token_hashes_exposed))}</span><span class="pill">payloads_logged:${esc(String(privacy.payloads_logged))}</span></p></article>`;
  }
  function renderRenewal(payload={}) {
    const renewal = payload.pilot_renewal || {};
    const terms = renewal.recommended_terms || {};
    const message = renewal.customer_message || {};
    const privacy = renewal.privacy || {};
    const boundary = renewal.commercial_boundary || {};
    $("adminRenewal").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((renewal.decision || renewal.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(renewal.generated_at || "-")}</span></div>
      <p><span class="pill">status:${esc(renewal.status || "-")}</span><span class="pill">tokens_included:${esc(String(renewal.tokens_included))}</span><span class="pill">operator_review:${esc(String(boundary.operator_review_required))}</span></p>
      <p>${esc(renewal.rationale || "")}</p>
    </article>
    <article class="admin-row"><h3>Recommended terms</h3><p><span class="pill">scopes:${esc((terms.scopes || []).join(","))}</span><span class="pill">${esc(number(terms.rate_per_min))}/min</span><span class="pill">${esc(number(terms.rate_per_day))}/day</span><span class="pill">expiry:${esc(number(terms.expires_days))}d</span><span class="pill">rotation:${esc(number(terms.rotation_days))}d</span></p></article>
    <article class="admin-row"><h3>Customer message</h3><p><b>${esc(message.subject || "-")}</b></p><p>${(message.body_lines || []).slice(0, 5).map((x) => esc(x)).join(" ")}</p></article>
    <article class="admin-row"><h3>Renewal privacy</h3><p><span class="pill">tokens_echoed:${esc(String(privacy.tokens_echoed))}</span><span class="pill">hashes_exposed:${esc(String(privacy.token_hashes_exposed))}</span><span class="pill">payloads_logged:${esc(String(privacy.payloads_logged))}</span></p></article>`;
  }
  function renderRequestAssist(payload={}) {
    const assist = payload.pilot_request_assist || {};
    const privacy = assist.privacy || {};
    const sample = assist.sample_request || {};
    const missing = assist.missing_fields || [];
    const risks = assist.risk_flags || [];
    $("adminRequestAssist").innerHTML = `<article class="admin-row">
      <div class="admin-row-top"><h3>${esc((assist.status || "unknown").toUpperCase())}</h3><span class="admin-mini">${esc(assist.generated_at || "-")}</span></div>
      <p><span class="pill">stored:${esc(String(assist.request_persisted))}</span><span class="pill">tokens:${esc(String(assist.tokens_collected))}</span><span class="pill">missing:${esc(number(missing.length))}</span><span class="pill">risks:${esc(number(risks.length))}</span></p>
      <p class="admin-mini">server_side_pii_storage=${esc(String(privacy.server_side_pii_storage))} payloads_logged=${esc(String(privacy.payloads_logged))}</p>
    </article>
    <article class="admin-row"><h3>Operator checklist</h3><p>${(assist.operator_checklist || []).slice(0, 6).map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</p></article>
    <article class="admin-row"><h3>Risk flags</h3><p>${risks.length ? risks.map((x) => `<span class="pill">${esc(x)}</span>`).join("") : '<span class="pill">none</span>'}</p></article>`;
    if (!$("adminRequestNote").value.trim()) $("adminRequestNote").value = JSON.stringify(sample, null, 2);
  }
  async function refresh() {
    setStatus("Loading admin surface...");
    const surface = await adminApi("/admin/release-readiness?days=7&include=surface");
    renderRelease(surface);
    renderOps(surface);
    renderHealth({meta: surface.meta || {}, health: (surface.ops || {}).pro_control_plane || {}});
    renderPilot(surface);
    renderHandoff(surface);
    renderCloseout(surface);
    renderRenewal(surface);
    renderRequestAssist(surface);
  }
  $("adminToken").value = state.token;
  $("adminConnect").addEventListener("click", async () => {
    state.token = $("adminToken").value.trim();
    sessionStorage.setItem(TOKEN_KEY, state.token);
    try { await refresh(); } catch (e) { setStatus(e.message, true); }
  });
  $("adminForget").addEventListener("click", () => {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    $("adminToken").value = "";
    renderHealth({});
    renderRelease({});
    renderPilot({});
    renderHandoff({});
    renderCloseout({});
    renderRenewal({});
    renderRequestAssist({});
    setStatus("Disconnected");
  });
  $("adminLogout").addEventListener("click", async () => {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    await fetch("/pro/admin/logout", {method: "POST", credentials: "same-origin"});
    window.location.href = "/pro/admin/login";
  });
  $("adminKeys").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-revoke-key]");
    if (!button) return;
    const keyId = button.getAttribute("data-revoke-key");
    if (!keyId || !window.confirm(`Revoke API key ${keyId}?`)) return;
    try {
      await adminApi(`/admin/keys/${encodeURIComponent(keyId)}/revoke`, {method: "POST"});
      setStatus(`Revoked API key ${keyId}.`);
      await refresh();
    } catch (e) { setStatus(e.message, true); }
  });
  $("adminCreateKey").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      label: form.get("label"),
      contact_email: form.get("contact_email"),
      scopes: String(form.get("scopes") || "").split(","),
      expires_days: Number(form.get("expires_days") || 30),
      rotation_days: Number(form.get("rotation_days") || 21),
      rate_per_day: Number(form.get("rate_per_day") || 10000),
      rate_per_min: 120,
    };
    try {
      const result = await adminApi("/admin/keys", {method: "POST", body: JSON.stringify(payload)});
      const created = result.created_key || {};
      const tokenBox = $("adminCreatedToken");
      tokenBox.hidden = false;
      tokenBox.textContent = `Token shown once for ${created.label || created.id}: ${created.token || "-"}`;
      event.currentTarget.reset();
      setStatus(`Created API key ${created.id || "-"}. Copy the token now.`);
      await refresh();
    } catch (e) { setStatus(e.message, true); }
  });
  $("adminReviewRequest").addEventListener("click", async () => {
    try {
      const raw = $("adminRequestNote").value.trim();
      const payload = raw ? JSON.parse(raw) : {};
      const result = await adminApi("/admin/pilot-request-assist", {method: "POST", body: JSON.stringify(payload)});
      renderRequestAssist(result);
      setStatus("Pilot request transformed without storage.");
    } catch (e) { setStatus(e.message, true); }
  });
  if (state.token) refresh().catch(() => setStatus("Stored tab key could not authenticate.", true));
})();
"""
        return _html_response("Admin Console", body, script=script)

    @app.get("/pro")
    def static_pro_offer():
        offer = pro_offer_payload()
        contact = offer["offer"]["contact"]
        contact_link = html_escape(contact["mailto"], quote=True)
        included = "".join(
            "<div class=\"card\">"
            f"<h3>{html_escape(item['capability'])}</h3>"
            "<ul>" + "".join(f"<li>{html_escape(detail)}</li>" for detail in item["details"]) + "</ul>"
            "</div>"
            for item in offer["included"]
        )
        plans = "".join(
            "<div class=\"card\">"
            f"<h3>{html_escape(plan['name'])}</h3>"
            f"<p>{html_escape(plan['fit'])}</p>"
            f"<p><span class=\"pill\">{html_escape(plan['commercial_model'])}</span></p>"
            "<h4>Includes</h4><ul>"
            + "".join(f"<li>{html_escape(item)}</li>" for item in plan["includes"])
            + "</ul><h4>Pilot pass criteria</h4><ul>"
            + "".join(f"<li>{html_escape(item)}</li>" for item in plan["success_criteria"])
            + "</ul></div>"
            for plan in offer["plans"]
        )
        checklist = "".join(
            f"<li>{html_escape(item)}</li>" for item in offer["buyer_checklist"]
        )
        sales = offer["sales_packet"]
        questions = "".join(
            f"<li>{html_escape(item)}</li>" for item in sales["qualification_questions"]
        )
        handoff = "".join(
            f"<li>{html_escape(item)}</li>" for item in sales["pilot_handoff"]
        )
        commercial = offer["commercial_model"]
        commercial_cards = "".join(
            "<div class=\"card\">"
            f"<h3>{html_escape(pkg['name'])}</h3>"
            f"<p class=\"num\">Pricing: {html_escape(str(pkg['price_eur_per_month']))}</p>"
            f"<p>{html_escape(pkg['term'])}</p>"
            f"<p>{html_escape(pkg['support'])}</p>"
            f"<p class=\"meta\">Sell when: {html_escape(pkg['sell_when'])}</p>"
            "</div>"
            for pkg in commercial["recommended_packages"]
        )
        icp_cards = "".join(
            "<div class=\"card\">"
            f"<h3>{html_escape(item['name'])}</h3>"
            f"<p>{html_escape(item['pain'])}</p>"
            f"<p class=\"meta\">Buyer: {html_escape(item['buyer'])}</p>"
            "</div>"
            for item in commercial["ideal_customer_profiles"]
        )
        def market_card(item):
            source = (
                f"<p class=\"meta\"><a href=\"{html_escape(item['source_url'], quote=True)}\">source</a></p>"
                if item.get("source_url") else ""
            )
            return (
                "<div class=\"card\">"
                f"<h3>{html_escape(item['category'].replace('_', ' '))}</h3>"
                f"<p>{html_escape(item['observed_offer'])}</p>"
                f"<p class=\"meta\">Risk: {html_escape(item['risk_if_competing_directly'])}</p>"
                f"<p>{html_escape(item['thirteenflow_response'])}</p>"
                f"{source}</div>"
            )

        market_cards = "".join(market_card(item) for item in commercial["market_context"])
        compete_on = "".join(
            f"<li>{html_escape(item)}</li>" for item in commercial["pricing_policy"]["compete_on"]
        )
        good_fit = "".join(
            f"<li>{html_escape(item)}</li>" for item in commercial["qualification_filter"]["good_fit"]
        )
        bad_fit = "".join(
            f"<li>{html_escape(item)}</li>" for item in commercial["qualification_filter"]["bad_fit"]
        )
        evidence_pack = "".join(
            f"<li><a href=\"{html_escape(item, quote=True)}\">{html_escape(item)}</a></li>"
            for item in commercial["evidence_pack"]
        )
        not_yet = "".join(
            f"<li>{html_escape(item)}</li>" for item in offer["not_included_yet"]
        )
        onboarding = "".join(
            f"<li>{html_escape(step)}</li>" for step in offer["onboarding"]
        )
        limits = offer["default_limits"]
        body = (
            "<section class=\"purchase-hero\"><div class=\"purchase-copy\">"
            "<div class=\"home-eyebrow\"><span>Controlled pilot</span><span>Scoped API keys</span><span>No public checkout</span></div>"
            "<h1>13FLOW Pro API</h1>"
            "<p class=\"lede\">Source-linked 13F data, quality warnings and agent-ready read-only access for bounded technical evaluation. This is an operator-reviewed, limited-capacity service, not a self-serve SaaS checkout.</p>"
            "<div class=\"purchase-actions\"><a href=\"" + contact_link + "\">Request access</a>"
            "<a href=\"/buyer-pack\">Buyer pack</a><a href=\"/developers\">API docs</a>"
            "<a href=\"/pro/onboarding\">Onboarding diagnostic</a>"
            "<a href=\"/api/live-status\">Live status</a></div>"
            f"<p class=\"meta\">{html_escape(contact['expected_response'])}</p></div>"
            "<aside class=\"purchase-panel\"><h3>Access gate</h3>"
            "<div class=\"proof-line\"><b>01 · Review</b><span>Operator checks the use case, expected volume and redistribution boundary before issuing a key.</span></div>"
            "<div class=\"proof-line\"><b>02 · Scope</b><span>Keys are scoped, audited, rate-limited and can be revoked without deleting history.</span></div>"
            "<div class=\"proof-line\"><b>03 · Verify</b><span>Buyers can inspect status, validation, OpenAPI and methodology before integration.</span></div>"
            "<div class=\"proof-line\"><b>04 · Limit</b>"
            f"<span>{limits['rate_per_min']} / min · {limits['rate_per_day']} / day; "
            f"{limits['max_positions_per_fund_detail']} positions and "
            f"{limits['max_moves_per_fund_detail']} moves per bounded fund-detail call.</span></div>"
            "</aside></section>"
            "<div class=\"buyer-strip\">"
            "<div><b>Live product</b><span><a href=\"/api/product-status\">/api/product-status</a></span></div>"
            "<div><b>Validation</b><span><a href=\"/validation\">Evidence boundary</a></span></div>"
            "<div><b>Contract</b><span><a href=\"/api/pro/v1/openapi.json\">Pro OpenAPI</a></span></div>"
            "<div><b>Workspace</b><span><a href=\"/pro/workspace\">Workspace cockpit</a></span></div>"
            "</div>"
            "<section class=\"section-band\"><h2>What you get</h2>"
            f"<p class=\"lede\">{html_escape(offer['offer']['positioning'])}</p>"
            "<p class=\"meta\">No public checkout is enabled on the open build; access is operator issued.</p>"
            "<div class=\"decision-grid\">" + included + "</div></section>"
            "<section class=\"section-head\"><div><div class=\"kicker\">Evaluation tracks</div><h2>Plans for controlled pilot review</h2></div>"
            "<p>Choose the smallest review path that proves integration quality, operational fit and support boundaries before any quote.</p></section>"
            "<div class=\"grid\">" + plans + "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Access request checklist</h2>"
            "<p class=\"lede\">Send these details first so the operator can issue the right scoped key.</p>"
            f"<ul>{checklist}</ul>"
            "<p><a class=\"pill\" href=\"" + contact_link + "\">Email access request</a></p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Who buys this</h2>"
            "<div class=\"grid\">" + icp_cards + "</div></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Access boundary</h2>"
            f"<p class=\"lede\">{html_escape(commercial['principle'])}</p>"
            f"<p class=\"meta\">Strategy: {html_escape(commercial['pricing_policy']['strategy'])}. "
            f"{html_escape(commercial['pricing_policy']['discount_rule'])}</p>"
            "<div class=\"grid\">" + commercial_cards + "</div>"
            "<h3>Compete on</h3>"
            f"<ul>{compete_on}</ul>"
            f"<p class=\"meta\">{html_escape(commercial['do_not_discount_below']['reason'])}</p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Source-position boundary</h2>"
            "<p class=\"lede\">13FLOW should not race raw SEC API vendors to the bottom. "
            "It should sell verified workflow, method boundaries and buyer-specific evidence.</p>"
            "<div class=\"grid\">" + market_cards + "</div></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Qualification filter</h2>"
            "<div class=\"grid\"><div class=\"card\"><h3>Good fit</h3>"
            f"<ul>{good_fit}</ul></div>"
            "<div class=\"card\"><h3>Bad fit</h3>"
            f"<ul>{bad_fit}</ul></div>"
            "<div class=\"card\"><h3>Evidence pack</h3>"
            f"<ul>{evidence_pack}</ul></div></div></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Operator lead kit</h2>"
            "<div class=\"grid\"><div class=\"card\"><h3>Qualification questions</h3>"
            f"<ul>{questions}</ul></div>"
            "<div class=\"card\"><h3>Pilot handoff</h3>"
            f"<ul>{handoff}</ul></div></div>"
            "<p class=\"meta\">Machine-readable template: /api/pro-offer sales_packet.lead_reply_template</p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Not claimed yet</h2>"
            "<p class=\"lede\">These claims require additional evidence or configuration before use in sales material.</p>"
            f"<ul>{not_yet}</ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Onboarding flow</h2>"
            f"<ol>{onboarding}</ol></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Validation boundary</h2>"
            f"<p>{html_escape(offer['truth_boundary']['blocked_by'])}</p>"
            f"<p class=\"meta\">Current validation artifact hash: "
            f"{html_escape(offer['truth_boundary']['current_artifact']['features_sha256'])}</p>"
            f"<p class=\"meta\">Current validation price hash: "
            f"{html_escape(offer['truth_boundary']['current_artifact']['prices_sha256'])}</p></div>"
        )
        return _html_response("Pro API", body)

    _FONT_DIR = os.path.join(os.path.dirname(dash), "assets", "fonts")

    @app.get("/assets/fonts/<path:filename>")
    def font_asset(filename):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename or ""):
            abort(404)
        if not filename.endswith((".css", ".ttf", ".woff2")):
            abort(404)
        resp = send_from_directory(_FONT_DIR, filename)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.get("/app")
    def app_dashboard():
        return _serve_html(dash)

    @app.get("/confluence")
    def confluence_app_alias():
        return redirect("/app#confluence", code=302)

    @app.get("/")
    def index():
        live = live_status_payload()
        product = product_status_payload()
        validation = product["validation"]
        artifact = validation["current_artifact"]
        counts = live["counts"]
        status_label = "LIVE EDGAR" if live["public_state"] == "LIVE" and not live["uses_synthetic_data"] else live["public_state"]
        coverage = live.get("coverage") or {}
        coverage_value = coverage.get("overall_value_share")
        coverage_label = "-" if coverage_value is None else f"{float(coverage_value) * 100:.1f}%"
        quality = live.get("quality_summary") or {}
        data_as_of = live.get("data_as_of") or "unknown"
        latest_q = live.get("latest_13f_quarter") or "unknown"
        sha_short = live["git_sha"][:12]
        body = (
            "<section class=\"home-hero\"><div class=\"home-copy\">"
            "<div class=\"home-eyebrow\">"
            f"<span id=\"srcText\" class=\"pill\">{html_escape(status_label.replace('LIVE EDGAR', 'LIVE · EDGAR'))}</span>"
            "<span>13F x Form 4</span><span>API-ready</span><span>Operator reviewed</span></div>"
            "<h1>13FLOW</h1>"
            "<p class=\"home-lede\">A professional SEC-filings intelligence desk for analysts who need source-linked 13F ownership, bounded Form 4 overlap, quality warnings and API contracts before they trust a workflow with money.</p>"
            "<div class=\"home-proof\">"
            f"<div class=\"proof-item\"><b>{html_escape(str(counts.get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(str(counts.get('filings') or 0))}</b><span>SEC filings</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(coverage_label)}</b><span>value coverage</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(latest_q)}</b><span>latest 13F quarter</span></div>"
            "</div><div class=\"home-actions\">"
            "<a class=\"button\" href=\"/app\">Open research app</a>"
            "<a class=\"button secondary\" href=\"/signals\">Open Signals</a>"
            "<a class=\"button secondary\" href=\"/pro\">Evaluate Pro API</a>"
            "<a class=\"button secondary\" href=\"/buyer-pack\">Buyer pack</a></div></div>"
            "<aside class=\"cockpit-shot\" aria-label=\"13FLOW cockpit preview\">"
            "<div class=\"shot-top\"><div><div class=\"shot-title\">Research queue</div>"
            "<div class=\"meta\">Evidence first, claims bounded, API-ready</div></div>"
            f"<span class=\"shot-live\">{html_escape(status_label.replace('LIVE EDGAR', 'LIVE · EDGAR'))}</span></div>"
            "<div class=\"shot-grid\"><div class=\"quadrant\">"
            "<span class=\"axis y\">Form 4 overlap</span><div class=\"axis x\"><span>Low fund pressure</span><span>High fund pressure</span></div>"
            "<span class=\"bubble b1\">SIG</span><span class=\"bubble b2\">13F</span><span class=\"bubble b3\">F4</span>"
            "</div><div class=\"watchlist\">"
            "<div class=\"watch-row\"><b>QUEUE</b><span>Fresh institutional accumulation</span><i>ranked</i></div>"
            "<div class=\"watch-row\"><b>SOURCE</b><span>Accession, filing date, issuer context</span><i>linked</i></div>"
            "<div class=\"watch-row\"><b>FORM 4</b><span>Open-market insider overlap</span><i>bounded</i></div>"
            "<div class=\"watch-row\"><b>DQ</b><span>AUM and unit-scale warnings</span><i>visible</i></div>"
            "</div></div></aside></section>"
            "<section class=\"trust-band\">"
            f"<div><b>Live data status: {html_escape(status_label)}.</b><span>uses_synthetic_data={str(live['uses_synthetic_data']).lower()} · data_as_of={html_escape(data_as_of)}</span></div>"
            f"<div><b>/api/funds serves {html_escape(str(counts.get('funds') or 0))} funds</b><span>filings={html_escape(str(counts.get('filings') or 0))}; latest_rows={html_escape(str(counts.get('latest_filings') or 0))}</span></div>"
            f"<div><b>latest 13F quarter {html_escape(latest_q)}</b><span>SHA {html_escape(sha_short)}</span></div>"
            f"<div><b>{html_escape(str(quality.get('aum_jump_warnings') or 0))} quality warnings</b><span>{html_escape(str(quality.get('unit_scale_candidates') or 0))} unit-scale candidates</span></div>"
            "</section>"
            "<section class=\"section-head\"><div><div class=\"kicker\">Professional workflow</div><h2>Built for a buyer who asks for evidence first</h2></div>"
            "<p>13FLOW should feel like an operator-reviewed data product: compact, verifiable, and clear about what is live before any Pro key is issued.</p></section>"
            "<div class=\"journey\">"
            "<a class=\"step\" href=\"/app#confluence\"><div class=\"n\">01 · Triage</div><h3>Signal cockpit</h3><p>Prioritize names where 13F pressure and Form 4 activity overlap, then drill into source evidence.</p></a>"
            "<a class=\"step\" href=\"/funds\"><div class=\"n\">02 · Validate</div><h3>Fund and issuer context</h3><p>Check who moved, what changed, reported values, accessions and quality flags before a model update.</p></a>"
            "<a class=\"step\" href=\"/developers\"><div class=\"n\">03 · Integrate</div><h3>API and agent surfaces</h3><p>Use read-only endpoints, OpenAPI, MCP methodology and explicit commercial boundaries for downstream workflows.</p></a>"
            "</div>"
            "<section class=\"buyer-strip\">"
            "<div><b>Human desk</b><span>Source-linked triage for analysts and fundamental review.</span></div>"
            "<div><b>API team</b><span>Read-only contracts, status endpoints and no-store Pro boundaries.</span></div>"
            "<div><b>Compliance</b><span>Methodology, limitations and legal terms are visible before access.</span></div>"
            "<div><b>Operator gate</b><span>No self-serve checkout; scoped Pro keys are issued after review.</span></div>"
            "</section>"
            "<section class=\"boundary\">"
            "<div class=\"panel\"><h3>What is live</h3><ul>"
            "<li>Read-only public 13F data from SEC EDGAR-derived local storage.</li>"
            "<li>25-ticker mature 13F + Form 4 joined evidence pack ready for human review.</li>"
            "<li>Public API, MCP public tools, methodology contracts and status pages.</li></ul>"
            "<p><a class=\"pill\" href=\"/funds\">Funds</a> <a class=\"pill\" href=\"/stocks\">Stocks</a> "
            "<a class=\"pill\" href=\"/signals\">Signals</a> <a class=\"pill\" href=\"/api/openapi.json\">OpenAPI</a></p></div>"
            "<div class=\"panel\"><h3>What is not claimed</h3><ul>"
            "<li>No validated alpha claim.</li><li>No probability or expected-return model.</li>"
            "<li>No complete view of shorts, non-US books, full derivatives or intra-quarter fund trading.</li>"
            "<li>No exhaustive insider-only or distribution universe yet.</li>"
            "<li>No public self-serve checkout.</li><li>No production x402 payment flow.</li>"
            "<li>No SLA or redistribution right without written agreement.</li></ul>"
            f"<p><span class=\"pill\">{html_escape(validation['status'])}</span> "
            f"<span class=\"pill\">rows={html_escape(str(artifact['row_count']))}; tickers={html_escape(str(artifact['ticker_count']))}; row_errors={html_escape(str(artifact['row_error_count']))}</span></p>"
            "<p><a class=\"pill\" href=\"/validation\">Validation evidence</a> "
            "<a class=\"pill\" href=\"/methodology\">Methodology</a> "
            "<a class=\"pill\" href=\"/status\">Status</a> "
            "<a class=\"pill\" href=\"/faq\">FAQ</a></p></div></section>"
        )
        return _html_response("Home", body)

    @app.get("/dashboard.html")
    def dashboard_alias():
        return redirect("/app", code=301)

    _FAQ = os.path.join(os.path.dirname(dash), "faq.html")

    @app.get("/faq")
    def faq():
        return _serve_html(_FAQ)

    @app.get("/faq.html")
    def faq_legacy_alias():
        return redirect("/faq", code=301)

    @app.get("/about")
    def about_page():
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\">"
            "<div class=\"kicker\">About 13FLOW</div>"
            "<h1>Filing intelligence, built in the l0g lab</h1>"
            "<p class=\"doc-lede\">13FLOW is operated by l0g: an independent research and data lab "
            "focused on macro risk, markets, public data and machine-readable financial intelligence.</p>"
            "<div class=\"actions\"><a class=\"pill cta\" href=\"https://l0g.fr/\" rel=\"noopener\">Visit l0g.fr</a> "
            "<a class=\"pill\" href=\"/methodology\">Methodology</a> "
            "<a class=\"pill\" href=\"/validation\">Validation</a> "
            "<a class=\"pill\" href=\"/developers\">API</a></div></div>"
            "<aside class=\"doc-panel\"><h3>What 13FLOW does</h3>"
            "<div class=\"mini-list\">"
            "<div><b>13F pressure.</b> Institutional holdings are structured from SEC EDGAR filings, with source accessions and quality flags.</div>"
            "<div><b>Form 4 context.</b> Insider activity is joined where it can help a human analyst triage a research queue.</div>"
            "<div><b>API and MCP surfaces.</b> Public read-only endpoints expose the same boundaries to dashboards, notebooks and agent workflows.</div>"
            "</div></aside></section>"
            "<section class=\"doc-metrics\">"
            "<div class=\"doc-metric\"><b>SEC EDGAR</b><span>Primary public source for filings</span></div>"
            "<div class=\"doc-metric\"><b>Read-only</b><span>Open public build, no browser account</span></div>"
            "<div class=\"doc-metric\"><b>Auditable</b><span>Status, methodology and API contracts</span></div>"
            "<div class=\"doc-metric\"><b>l0g</b><span>Research lab behind the product</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>What is l0g?</h2>"
            "<p><a href=\"https://l0g.fr/\" rel=\"noopener\">l0g</a> is an independent open research project "
            "that turns public macro, market and regulatory data into dashboards, methodology notes, "
            "machine-readable endpoints and source-linked analysis. Its work is deliberately source-first: "
            "primary data is preferred, methods are documented, and uncertainty is kept visible instead of being hidden behind a glossy score.</p>"
            "<p>13FLOW is one of those tools. Where l0g.fr tracks macro regimes, debt risk and public datasets, "
            "13FLOW focuses on SEC filing intelligence: 13F institutional ownership, Form 4 insider disclosures, "
            "data-quality warnings and integration surfaces for analysts who want evidence before narrative.</p></section>"
            "<section class=\"doc-section\"><h2>Why this exists</h2>"
            "<div class=\"split\"><div class=\"doc-card\"><h3>For analysts</h3>"
            "<p>Reduce SEC filing noise into a reviewable queue: who moved, which issuer changed, what source filing supports it, and where the data can be wrong.</p></div>"
            "<div class=\"doc-card\"><h3>For builders</h3>"
            "<p>Expose stable read-only contracts for dashboards, notebooks and agent workflows without pretending the signal is validated alpha.</p></div></div>"
            "<div class=\"callout\" style=\"margin-top:12px\"><strong>The product stance is conservative.</strong> "
            "13FLOW sells workflow, structure and auditability. It does not sell a magic trading signal, a probability model or a performance promise.</div></section>"
            "<section class=\"doc-section\"><h2>Operating principles</h2>"
            "<div class=\"runbook\">"
            "<div class=\"runstep\"><b>01</b><span>Use public regulatory sources first, especially SEC EDGAR for the filing layer.</span></div>"
            "<div class=\"runstep\"><b>02</b><span>Show the boundary: delayed 13F snapshots, incomplete short exposure, mapping gaps and Form 4 limits.</span></div>"
            "<div class=\"runstep\"><b>03</b><span>Keep human-readable pages and machine-readable contracts aligned.</span></div>"
            "<div class=\"runstep\"><b>04</b><span>Separate evidence from claims: validation pages say what is proven and what is still only a hypothesis.</span></div>"
            "</div></section>"
            "<section class=\"doc-section\"><h2>Useful links</h2>"
            "<div class=\"doc-grid\">"
            "<a class=\"doc-card\" href=\"https://l0g.fr/\" rel=\"noopener\"><h3>l0g.fr</h3><p>The research lab and editorial ecosystem behind 13FLOW.</p></a>"
            "<a class=\"doc-card\" href=\"/methodology\"><h3>Methodology</h3><p>How the application and MCP layers should be interpreted.</p></a>"
            "<a class=\"doc-card\" href=\"/legal\"><h3>Legal and GDPR</h3><p>Publisher, privacy, RGPD rights, source and liability boundary.</p></a>"
            "</div></section>"
        )
        return _html_response("About", body)

    @app.get("/legal")
    def legal():
        body = (
            "<section class=\"doc-hero\"><div class=\"doc-copy\">"
            "<div class=\"kicker\">Legal, privacy and RGPD</div>"
            "<h1>Clear terms for a public filing research tool</h1>"
            "<p class=\"doc-lede\">13FLOW is an independent read-only research interface over public SEC EDGAR filings. "
            "This page covers publisher identity, GDPR / RGPD rights, logs, cookies, source boundaries and liability.</p>"
            "<div class=\"actions\"><a class=\"pill cta\" href=\"mailto:admin@toonux.com\">Contact publisher</a> "
            "<a class=\"pill\" href=\"/legal/pro-api\">Pro API terms</a> "
            "<a class=\"pill\" href=\"/api/live-status\">Live status</a></div></div>"
            "<aside class=\"doc-panel\"><h3>Short version</h3><div class=\"mini-list\">"
            "<div><b>No public account.</b> The open site does not require registration, browser login or self-serve checkout.</div>"
            "<div><b>No ad tracking.</b> Public pages do not rely on advertising pixels, social embeds or third-party analytics cookies.</div>"
            "<div><b>Public sources.</b> 13FLOW structures SEC EDGAR filings and exposes source-linked research surfaces.</div>"
            "</div></aside></section>"
            "<section class=\"doc-metrics\">"
            "<div class=\"doc-metric\"><b>Publisher</b><span>l0g / bluetouff</span></div>"
            "<div class=\"doc-metric\"><b>Contact</b><span>admin@toonux.com</span></div>"
            "<div class=\"doc-metric\"><b>Source</b><span>SEC EDGAR public filings</span></div>"
            "<div class=\"doc-metric\"><b>Updated</b><span>2026-07-03</span></div>"
            "</section>"
            "<section class=\"doc-section\"><h2>Publisher and entity behind 13FLOW</h2>"
            "<p>13FLOW is operated and published by <a href=\"https://l0g.fr/\" rel=\"noopener\">l0g</a>, "
            "an independent research and data project focused on macroeconomics, markets, public sources, "
            "systemic risk and machine-readable financial intelligence. The project is run by bluetouff.</p>"
            "<div class=\"mini-list\">"
            "<div><b>Site:</b> https://13flow.eu</div>"
            "<div><b>Contact and rights requests:</b> <a href=\"mailto:admin@toonux.com\">admin@toonux.com</a></div>"
            "<div><b>Relationship to third parties:</b> 13FLOW is not affiliated with the SEC, tracked funds, issuers or third-party data providers mentioned on the site.</div>"
            "</div></section>"
            "<section class=\"doc-section\"><h2>Personal data and GDPR / RGPD</h2>"
            "<p>The public 13FLOW site is designed as a low-data read-only surface. It does not require a public user account, "
            "does not ask visitors to create a profile and does not set advertising or behavioral analytics cookies.</p>"
            "<div class=\"split\"><div class=\"doc-card\"><h3>Data that may be processed</h3><ul>"
            "<li>Technical server logs: IP address, timestamp, requested URL, status code and user-agent.</li>"
            "<li>Security and abuse signals needed to operate the service, rate-limit abuse and investigate incidents.</li>"
            "<li>Messages you send voluntarily by email, including your email address and the content of the request.</li>"
            "<li>For operator-reviewed Pro access only: organization, billing contact, key id, endpoint, scope, status and audit metadata needed to administer the API.</li>"
            "</ul></div><div class=\"doc-card\"><h3>Why it is processed</h3><ul>"
            "<li>Operate the public site and APIs.</li><li>Secure the service and prevent abuse.</li>"
            "<li>Respond to legal, privacy, support or commercial requests.</li>"
            "<li>Administer scoped Pro API access when a pilot or agreement exists.</li></ul></div></div>"
            "<p class=\"callout\" style=\"margin-top:12px\"><strong>Legal basis.</strong> Processing is based on legitimate interest for security and service operation, "
            "pre-contractual or contractual necessity for Pro discussions and pilots, consent where you voluntarily contact the publisher, and legal obligation where applicable.</p></section>"
            "<section class=\"doc-section\"><h2>Your rights</h2>"
            "<p>Under the GDPR / RGPD, you can request access, rectification, erasure, restriction, objection and portability where applicable. "
            "Send requests to <a href=\"mailto:admin@toonux.com\">admin@toonux.com</a>. The request may require reasonable identity verification before disclosure or deletion.</p>"
            "<p>If you believe your request was not handled correctly, you can contact the French data protection authority: "
            "<a href=\"https://www.cnil.fr/\" rel=\"noopener\">CNIL</a>.</p></section>"
            "<section class=\"doc-section\"><h2>Cookies, third parties and retention</h2>"
            "<div class=\"split\"><div class=\"doc-card\"><h3>Cookies and trackers</h3>"
            "<p>The public research site does not use advertising cookies, social tracking pixels or third-party analytics tags. Fonts are self-hosted from the same domain.</p></div>"
            "<div class=\"doc-card\"><h3>Retention</h3>"
            "<p>Technical logs and audit records are kept only as long as needed for security, reliability, abuse prevention, legal evidence or active Pro administration.</p></div></div>"
            "<p class=\"meta\">External links, including l0g.fr, SEC.gov, GitHub or CNIL, are contacted only when you choose to open them.</p></section>"
            "<section class=\"doc-section\"><h2>Data and sources</h2>"
            "<p>13FLOW aggregates, formats and links public SEC EDGAR filings, including 13F-HR institutional holdings and Form 4 ownership reports. "
            "Those filings are public regulatory records. 13FLOW does not own the underlying SEC filings and does not sell raw SEC access as proprietary data.</p>"
            "<p>Filing delays, amendments, issuer mistakes, missing tickers, CUSIP mapping gaps and source inconsistencies can occur. "
            "Use accessions, methodology pages and validation endpoints to verify the evidence before relying on it.</p></section>"
            "<section class=\"doc-section\"><h2>No investment advice</h2>"
            "<p>13FLOW is a screening and research tool. Scores, rankings, Confluence views and API responses are not personalized recommendations, "
            "investment advice, solicitation, execution guidance, probability estimates or performance promises. You remain responsible for your own diligence, risk controls and regulatory framework.</p></section>"
            "<section class=\"doc-section\"><h2>Pro API, payments and commercial terms</h2>"
            "<p>The public site has no self-serve checkout and no public browser account creation. Pro access, when issued, is operator-reviewed and covered by separate terms, scopes, quotas and audit rules.</p>"
            "<p><a class=\"pill\" href=\"/legal/pro-api\">Read Pro API terms</a> "
            "<a class=\"pill\" href=\"/pro\">Evaluate Pro access</a></p></section>"
            "<section class=\"doc-section\"><h2>Intellectual property and liability</h2>"
            "<p>The 13FLOW interface, editorial text, name, product structure and logo are protected. Underlying SEC records remain reusable from their public source. "
            "13FLOW works to keep the site available and accurate, but cannot guarantee uninterrupted access, complete data, error-free filings, uninterrupted APIs or suitability for any investment process.</p></section>"
        )
        return _html_response("Legal, privacy and data terms", body)

    @app.get("/mentions-legales")
    @app.get("/mentions-legales.html")
    def mentions_legales():
        return redirect("/legal", code=301)

    return app


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="smartmoney.db")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost; this API has NO auth — do not "
                         "expose it directly, put it behind an authenticated reverse proxy)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--value", action="store_true", help="enable live valuation (stooq)")
    ap.add_argument("--open", dest="open_mode", action="store_true",
                    help="open build: no auth, no billing, no alerts — public read-only screens only")
    ap.add_argument("--readonly", action="store_true",
                    help="open the database read-only (the web process cannot write it)")
    args = ap.parse_args()
    if args.readonly:
        os.environ["SMARTMONEY_DB_READONLY"] = "1"
    prov = None
    if args.value:
        from .prices import StooqProvider
        prov = StooqProvider()
    if args.host not in ("127.0.0.1", "localhost") and not args.open_mode:
        print("WARNING: binding to a non-local address. This API is unauthenticated; "
              "front it with auth + TLS before exposing it.", flush=True)
    # debug=False: never enable the Werkzeug debugger on a network-facing app (RCE).
    create_app(args.db, provider=prov, open_mode=args.open_mode).run(
        host=args.host, port=args.port, debug=False)
