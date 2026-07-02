"""
Read-only JSON API over the Store, plus it serves the dashboard.

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
  GET /pro/v1/openapi.json            -> Pro API OpenAPI document
  GET /subscriptions
  GET /alerts/preview/<cik>
GET /  -> dashboard.html

Core endpoints work fully offline (reported, quarter-end figures). Valuation (value=1)
needs a price provider and hits the network at request time, so it's opt-in.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Optional

import functools
import secrets
from flask import Flask, Response, abort, jsonify, make_response, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .analytics import consensus_moves
from .alerts import build_alert, AlertEngine
from .accounts import AccountStore
from .auth import init_auth, login_required, current_user
from .billing import init_billing
from .hibp import default_breach_checker
from .channels import ConsoleChannel
from .pwhash import PasswordHasher
from .registry import Fund
from .tracker import Tier, EntitlementError
from .db import Store
from .diff import Move, diff_portfolios
from .portfolio import Portfolio
from .pro import APIKeyError, APIRateLimited, ProAPIStore
from .quality import data_quality_report
from .valuation import value_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
DASHBOARD = os.path.join(APP_ROOT, "dashboard.html")

MAX_SUBSCRIPTIONS_PER_USER = 50
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
            ciks = [r["cik"] for r in s.conn.execute("SELECT cik FROM funds")]
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
    dash = dashboard_path or DASHBOARD
    _truthy = lambda v: str(v or "").strip().lower() in ("1", "true", "yes", "on")
    demo_mode = _truthy(os.environ.get("SMARTMONEY_DEMO"))
    open_mode = open_mode or demo_mode or _truthy(os.environ.get("SMARTMONEY_OPEN"))
    read_only = demo_mode or _truthy(os.environ.get("SMARTMONEY_DB_READONLY"))
    pro_enabled = _truthy(os.environ.get("SMARTMONEY_PRO_API"))
    pro_db_path = os.environ.get("SMARTMONEY_PRO_DB") or os.path.join(APP_ROOT, "13flow-pro.db")

    # Auth + billing exist only in the full build. The open build skips them entirely:
    # no accounts, no Stripe, no sessions/CSRF machinery is even registered.
    if not open_mode:
        # One shared password hasher (stateless/thread-safe); a fresh AccountStore per request.
        _hasher = PasswordHasher()
        _breach_checker = default_breach_checker()   # HIBP k-anonymity (env-tunable)
        def accounts_factory() -> AccountStore:
            return AccountStore(db_path, hasher=_hasher, breach_checker=_breach_checker)
        init_auth(app, accounts_factory, secure_cookies=secure_cookies)
        init_billing(app, accounts_factory, secure_cookies=secure_cookies)

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
                "/api/pro/v1/openapi.json": {
                    "get": {"summary": "OpenAPI document for the Pro API",
                            "responses": {"200": {"description": "OpenAPI JSON"}}}
                },
            },
        }

    def client_ip() -> str:
        xff = request.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"

    def bearer_token() -> str:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return request.headers.get("X-13FLOW-Key", "").strip()

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
            rows = [dict(r) for r in s.conn.execute("SELECT cik,label,manager FROM funds ORDER BY label")]
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
            return jsonify({"date": date, "rows": s.consensus_holdings(date, min_funds)})
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
            ciks = [row["cik"] for row in s.conn.execute("SELECT cik FROM funds")]
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
    if not open_mode:
        # ---- subscriptions & alert preview ----------------------------------
        # ---- subscriptions (authenticated; tier enforced server-side) -------
        @app.get("/api/subscriptions")
        @login_required
        def subscriptions():
            user = current_user()
            s = store()
            try:
                subs = [dict(r) for r in s.conn.execute(
                    "SELECT * FROM subscriptions WHERE active=1 AND user_id=?", (user.id,))]
                for sub in subs:
                    fr = s.fund_row(sub["cik"])
                    sub["fund_label"] = fr["label"] if fr else sub["cik"]
                return jsonify(subs)
            finally:
                s.close()

        @app.post("/api/subscriptions")
        @login_required
        def create_subscription():
            user = current_user()
            if not getattr(user, "verified", True):           # defense in depth (login already gates)
                return jsonify({"error": "verify your email first"}), 403
            d = request.get_json(silent=True) or {}
            try:
                cik = clean_cik(d.get("cik", ""))
            except Exception:
                from werkzeug.exceptions import BadRequest
                raise BadRequest("invalid CIK")
            channel = d.get("channel", "console")
            target = d.get("target", "")
            if channel not in ("console", "webhook", "email"):
                return jsonify({"error": "invalid channel"}), 400
            s = store()
            try:
                count = s.conn.execute(
                    "SELECT COUNT(*) c FROM subscriptions WHERE active=1 AND user_id=?",
                    (user.id,)).fetchone()["c"]
                if count >= MAX_SUBSCRIPTIONS_PER_USER:
                    return jsonify({"error": "subscription limit reached"}), 409
                fr = s.fund_row(cik)
                fund = Fund(label=(fr["label"] if fr else cik),
                            manager=(fr["manager"] if fr else None), cik=cik, search_name="")
                # Tier comes from the authenticated user record — never from the client.
                tier = Tier(user.tier, [])
                engine = AlertEngine(s, channels={"console": ConsoleChannel()})
                try:
                    sub_id = engine.subscribe(tier, user.id, fund, channel, target=target, prime=True)
                except EntitlementError as e:
                    return jsonify({"error": str(e)}), 402         # payment required
                except (ValueError,) as e:
                    return jsonify({"error": str(e)}), 400         # bad target (SSRF/email guard)
                return jsonify({"id": sub_id, "cik": cik, "channel": channel}), 201
            finally:
                s.close()

        @app.delete("/api/subscriptions/<int:sub_id>")
        @login_required
        def delete_subscription(sub_id):
            user = current_user()
            s = store()
            try:
                row = s.conn.execute("SELECT user_id FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
                if row is None or row["user_id"] != user.id:
                    return jsonify({"error": "not found"}), 404   # don't leak others' ids
                s.deactivate_subscription(sub_id)
                return jsonify({"ok": True})
            finally:
                s.close()

        @app.get("/api/alerts/preview/<cik>")
        def alert_preview(cik):
            cik = clean_cik(cik)
            s = store()
            try:
                latest = s.latest_filing_row(cik)
                if not latest:
                    return jsonify({"error": "no filings"}), 404
                alert = build_alert(s, cik, latest["accession"])
                return jsonify(alert.to_dict() if alert else {"error": "could not build"})
            finally:
                s.close()

    @app.get("/api/coverage")
    def coverage_ep():
        date = request.args.get("date") or None
        s = store()
        try:
            return jsonify({"coverage": s.coverage(date),
                            "tail": s.unresolved_holdings(date)[:25]})
        finally:
            s.close()

    @app.get("/api/data-quality")
    def data_quality_ep():
        threshold = clean_float(request.args.get("threshold"), 100.0, 2.0, 10000.0)
        limit = clean_int(request.args.get("limit"), 100, 1, 500)
        s = store()
        try:
            return jsonify(data_quality_report(s, aum_jump_threshold=threshold, limit=limit))
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
                "description": "Get latest holders for one ticker.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"ticker": {"type": "string"}},
                    "required": ["ticker"],
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

    def _stock_payload(ticker: str) -> dict:
        t = (ticker or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", t):
            from werkzeug.exceptions import BadRequest
            raise BadRequest("invalid ticker")
        s = store()
        try:
            latest = s.conn.execute("SELECT MAX(report_date) d FROM latest_filings").fetchone()["d"]
            rows = [dict(r) for r in s.conn.execute(
                """SELECT fn.label, lf.cik, lf.report_date, f.accession, f.filing_date,
                          h.cusip, h.ticker, h.issuer, h.title_of_class, h.value_usd,
                          h.shares, h.weight
                   FROM latest_filings lf
                   JOIN filings f ON f.accession=lf.accession
                   JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                   JOIN funds fn ON fn.cik=lf.cik
                   WHERE lf.report_date=? AND UPPER(h.ticker)=?
                   ORDER BY h.value_usd DESC""",
                (latest, t),
            )]
        finally:
            s.close()
        return {
            "ticker": t,
            "latest_13f_quarter": latest,
            "holders": rows,
            "holder_count": len(rows),
            "total_value_usd": sum(r["value_usd"] or 0 for r in rows),
            "sec_company_search": f"https://www.sec.gov/edgar/search/#/q={t}",
        }

    def _mcp_call_tool(name: str, args: dict) -> dict:
        if name == "funds.list":
            s = store()
            try:
                rows = [dict(r) for r in s.conn.execute(
                    "SELECT cik,label,manager FROM funds ORDER BY label")]
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
                return data_quality_report(s, aum_jump_threshold=threshold, limit=limit)
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
                },
            })

        @app.get("/api/pro/v1/funds")
        @pro_required("funds:read")
        def pro_funds_ep():
            s = store()
            try:
                rows = [dict(r) for r in s.conn.execute(
                    "SELECT cik,label,manager FROM funds ORDER BY label")]
                quality = data_quality_report(s, limit=500)
                warnings_by_cik = {}
                for w in quality["warnings"]:
                    warnings_by_cik.setdefault(w["fund"]["cik"], []).append(w)
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
                quality = data_quality_report(s, limit=500)
                fund_warnings = [
                    w for w in quality["warnings"] if w["fund"]["cik"] == cik
                ]
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
                return jsonify({
                    "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                    "report": data_quality_report(s, aum_jump_threshold=threshold, limit=limit),
                })
            finally:
                s.close()

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
        generated_at = _now_iso()
        s = store()
        try:
            funds = s.conn.execute("SELECT COUNT(*) c FROM funds").fetchone()["c"] or 0
            filings = s.conn.execute("SELECT COUNT(*) c FROM filings").fetchone()["c"] or 0
            latest_rows = s.conn.execute("SELECT COUNT(*) c FROM latest_filings").fetchone()["c"] or 0
            latest = s.conn.execute("SELECT MAX(report_date) d FROM latest_filings").fetchone()["d"]
            earliest = s.conn.execute("SELECT MIN(report_date) d FROM latest_filings").fetchone()["d"]
            latest_filing_date = s.conn.execute("SELECT MAX(filing_date) d FROM filings").fetchone()["d"]
            accession_rows = s.conn.execute(
                """SELECT f.accession, f.cik, fn.label, f.form, f.report_date, f.filing_date
                   FROM latest_filings lf
                   JOIN filings f ON f.accession=lf.accession
                   LEFT JOIN funds fn ON fn.cik=f.cik
                   ORDER BY f.report_date DESC, f.filing_date DESC, f.accession DESC
                   LIMIT 12"""
            ).fetchall()
            coverage = s.coverage(latest) if latest else {
                "overall_value_share": None,
                "value_unresolved": None,
                "per_fund": [],
            }
            quality = data_quality_report(s, limit=1)["summary"]
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
                "unit_scale_candidates": quality["unit_scale_candidates"],
            },
            "public_endpoints": ["/api/live-status", "/api/version", "/api/funds", "/api/data-quality"],
        }

    @app.get("/api/live-status")
    def live_status_ep():
        return jsonify(live_status_payload())

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

    def _html_response(title: str, body: str) -> Response:
        nonce = secrets.token_urlsafe(16)
        nav = (
            '<nav><a class="brand" href="/">13<span>FL</span><b>OW</b></a>'
            '<a href="/funds">Funds</a><a href="/stocks">Stocks</a>'
            '<a href="/signals">Signals</a><a href="/faq">FAQ</a>'
            '<a href="/legal">Legal</a></nav>'
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(title)} · 13FLOW</title><link href="/assets/fonts/13flow-fonts.css" rel="stylesheet">
<style>
:root{{--bg:#0c1611;--panel:#13241c;--line:#1f3329;--text:#eaf5ef;--muted:#a9c4b7;--faint:#6f897d;--accent:#19c187;--amber:#e0a534;--sans:'Hanken Grotesk',system-ui,sans-serif;--display:'Bricolage Grotesque',system-ui,sans-serif;--mono:'Geist Mono',ui-monospace,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55;background-image:radial-gradient(46rem 30rem at 88% -8%,rgba(25,193,135,.14),transparent 60%)}}
.wrap{{max-width:1120px;margin:0 auto;padding:24px 24px 72px}}a{{color:var(--accent);text-decoration:none}}nav{{display:flex;gap:14px;align-items:center;margin-bottom:34px;flex-wrap:wrap}}nav a{{color:var(--muted);font-weight:650}}.brand{{font-family:var(--display);font-size:24px;font-weight:800;color:var(--text);margin-right:auto}}.brand span{{color:var(--accent)}}.brand b{{color:var(--amber)}}h1{{font-family:var(--display);font-size:40px;line-height:1.05;margin:0 0 8px}}.lede{{color:var(--muted);max-width:760px;margin:0 0 26px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}}.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px}}.card h2,.card h3{{font-family:var(--display);margin:0 0 6px}}.meta,.num{{font-family:var(--mono)}}.meta{{font-size:12px;color:var(--faint)}}.num{{font-size:13px}}table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}th:first-child,td:first-child{{text-align:left}}th{{font-family:var(--mono);font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}}td{{font-size:14px}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-family:var(--mono);font-size:11px;color:var(--muted)}}.sec{{font-family:var(--mono);font-size:11px}}footer{{margin-top:34px;color:var(--faint);font-size:12px}}
</style></head><body><div class="wrap">{nav}{body}<footer>13FLOW · SEC EDGAR public-domain data · screen, not investment advice.</footer></div><script nonce="{nonce}"></script></body></html>"""
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
            rows = [dict(r) for r in s.conn.execute(
                "SELECT cik,label,manager FROM funds ORDER BY label")]
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
            latest = s.conn.execute("SELECT MAX(report_date) d FROM latest_filings").fetchone()["d"]
            rows = [dict(r) for r in s.conn.execute(
                """SELECT UPPER(h.ticker) ticker, MAX(h.issuer) issuer,
                          COUNT(DISTINCT lf.cik) holders, SUM(h.value_usd) value_usd
                   FROM latest_filings lf
                   JOIN holdings h ON h.accession=lf.accession AND h.put_call=''
                   WHERE lf.report_date=? AND h.ticker IS NOT NULL AND h.ticker<>''
                   GROUP BY UPPER(h.ticker)
                   ORDER BY value_usd DESC LIMIT 300""",
                (latest,),
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
        rows = "".join(
            f"<tr><td><a href=\"/funds/{html_escape(r['cik'])}\">{html_escape(r['label'])}</a></td>"
            f"<td class=\"num\">{r['weight'] * 100:.2f}%</td><td class=\"num\">{_fmt_usd_html(r['value_usd'])}</td>"
            f"<td><a class=\"sec\" href=\"{html_escape(sec_accession_url(r['cik'], r['accession']))}\" rel=\"noopener\" target=\"_blank\">{html_escape(r['accession'])}</a></td></tr>"
            for r in payload["holders"]
        )
        body = (
            f"<h1>{html_escape(payload['ticker'])}</h1>"
            f"<p class=\"lede\">{payload['holder_count']} tracked funds held {html_escape(payload['ticker'])} "
            f"at {html_escape(payload['latest_13f_quarter'] or '-')}; aggregate reported value {_fmt_usd_html(payload['total_value_usd'])}. "
            f"<a href=\"{html_escape(payload['sec_company_search'])}\" rel=\"noopener\" target=\"_blank\">SEC company search</a></p>"
            f"<table><thead><tr><th>Fund</th><th>Weight</th><th>Value</th><th>SEC filing</th></tr></thead><tbody>{rows}</tbody></table>"
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

    @app.get("/")
    def index():
        return _serve_html(dash)

    @app.get("/dashboard.html")
    def dashboard_alias():
        return _serve_html(dash)

    _FAQ = os.path.join(os.path.dirname(dash), "faq.html")

    @app.get("/faq")
    @app.get("/faq.html")
    def faq():
        return _serve_html(_FAQ)

    _LEGAL = os.path.join(os.path.dirname(dash), "mentions-legales.html")

    @app.get("/legal")
    @app.get("/mentions-legales")
    @app.get("/mentions-legales.html")
    def mentions_legales():
        return _serve_html(_LEGAL)

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
