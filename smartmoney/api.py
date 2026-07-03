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
from flask import (
    Flask, Response, abort, jsonify, make_response, redirect, request, send_from_directory,
)
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
                "/api/product-status": {"get": {"summary": "Go-to-market readiness and proof boundary",
                                                "responses": {"200": {"description": "Product status"}}}},
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
                        "summary": "Latest holders for one ticker",
                        "parameters": [{"name": "ticker", "in": "path", "required": True,
                                        "schema": {"type": "string", "pattern": "^[A-Z0-9.\\-]{1,12}$"}}],
                        "responses": {"200": {"description": "Ticker holder detail"},
                                      "400": {"description": "Invalid ticker"}},
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

    @app.get("/api/stocks/<ticker>")
    def stock_ep(ticker):
        return jsonify(_stock_payload(ticker))

    def _mcp_call_tool(name: str, args: dict) -> dict:
        if name == "product.status":
            return product_status_payload()
        if name == "pro.offer":
            return pro_offer_payload()
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

    def product_status_payload() -> dict:
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
                "status": "pipeline_smoke_validated_full_quant_blocked",
                "score_claim": "ordinal_heuristic_not_probability_not_expected_return",
                "current_artifact": {
                    "scope": "25-ticker price/sample validation smoke",
                    "features_sha256": "4ecceb420a466b138de6d4672844158705c0da4ed5425bc661e97df8ecfc8592",
                    "prices_sha256": "2e35a5713c3e0654134d8d05d6f50b7013729ce6634d31db4e5e2e534ba57c9e",
                    "publishable_as_full_validation": False,
                },
                "blocked_by": (
                    "No full 2013-2026 adjusted-price CSV or reviewed normalized Form 4 "
                    "transaction artifact is installed on production. Do not relaunch "
                    "external historical-price or Form 4 fan-out from zen; import vetted "
                    "local files, then validate them offline."
                ),
                "required_next_artifact": (
                    "/var/lib/13flow/validation_prices_full.csv plus "
                    "/var/lib/13flow/validation_form4_full.csv"
                ),
            },
            "offer_boundary": {
                "sell_now": [
                    "verifiable SEC EDGAR-derived 13F data",
                    "read-only public API",
                    "scoped Pro API keys with audit and rate limits",
                    "MCP read-only integration with Pro tools failing closed",
                    "data-quality warnings and methodology contracts",
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
        body = (
            "<h1>Status</h1>"
            "<p class=\"lede\">Human-readable evidence page for the currently served 13FLOW build. "
            "Use this page to distinguish deployed production state from stale local audits or screenshots.</p>"
            "<div class=\"grid\">"
            f"<div class=\"card\"><h3>Evidence status</h3><p><span class=\"{status_class}\">"
            f"{html_escape(live['public_state'])}</span></p>"
            f"<p class=\"meta\">uses_synthetic_data={str(live['uses_synthetic_data']).lower()}</p></div>"
            "<div class=\"card\"><h3>Deployed commit</h3>"
            f"<p><code>{html_escape(live['git_sha'])}</code></p>"
            f"<p class=\"meta\">generated {html_escape(live['generated_at'])}</p></div>"
            "<div class=\"card\"><h3>Validation boundary</h3>"
            f"<p>{html_escape(validation['score_claim'])}</p>"
            f"<p class=\"meta\">status={html_escape(validation['status'])}</p></div>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Runtime proof</h2>"
            f"<table><thead><tr><th>Field</th><th>Current value</th></tr></thead><tbody>{rows_html}</tbody></table>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Verification endpoints</h2>"
            f"<table><thead><tr><th>Endpoint</th><th>Use</th></tr></thead><tbody>{endpoint_rows}</tbody></table>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Validation artifact</h2>"
            f"<p class=\"meta\">scope={html_escape(artifact['scope'])}</p>"
            f"<p><code>features_sha256={html_escape(artifact['features_sha256'])}</code></p>"
            f"<p><code>prices_sha256={html_escape(artifact['prices_sha256'])}</code></p>"
            f"<p>Publishable as full validation: <code>{str(artifact['publishable_as_full_validation']).lower()}</code></p>"
            f"<p>{html_escape(validation['blocked_by'])}</p>"
            "</div>"
            "<div class=\"grid\" style=\"margin-top:18px\">"
            "<div class=\"card\"><h3>Sell now</h3><ul>" + sell_now + "</ul></div>"
            "<div class=\"card\"><h3>Do not claim yet</h3><ul>" + do_not_claim + "</ul></div>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Operator gate</h2>"
            f"<p>{html_escape(product['operator_policy']['deployment_gate'])}</p>"
            f"<p class=\"meta\">{html_escape(product['operator_policy']['external_api_safety'])}</p>"
            "</div>"
        )
        return _html_response("Status", body)

    def pro_offer_payload() -> dict:
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
                "runbook": "docs/PRO_API_ONBOARDING.md",
                "contact": {
                    "email": "admin@toonux.com",
                    "mailto": (
                        "mailto:admin@toonux.com?subject=13FLOW%20Pro%20API%20access"
                    ),
                    "expected_response": "operator review before any token is issued",
                },
            },
            "plans": [
                {
                    "name": "Pilot access",
                    "fit": "one research desk or analyst validating 13F workflows",
                    "commercial_model": "operator quoted",
                    "includes": [
                        "one scoped API key",
                        "default limits unless negotiated",
                        "fund list, fund detail and data-quality endpoints",
                        "bounded first probes with operator verification",
                    ],
                    "success_criteria": [
                        "status and funds probes pass",
                        "one bounded fund detail is ingested client-side",
                        "client accepts current validation boundary",
                    ],
                },
                {
                    "name": "Desk API",
                    "fit": "repeatable internal dashboards, notebooks or data pipelines",
                    "commercial_model": "operator quoted",
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
                    "name": "Agent / MCP workflow",
                    "fit": "automated agent access to 13F context and quality metadata",
                    "commercial_model": "operator quoted",
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
                "pricing_currency": "EUR",
                "principle": (
                    "Sell the audited 13F workflow, quality boundary, support and MCP readiness; "
                    "do not sell raw SEC data as if it were proprietary."
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
                        "name": "Paid pilot",
                        "price_eur_per_month": 490,
                        "term": "30 days, renewable once before conversion",
                        "included_keys": 1,
                        "included_limits": {"per_min": 120, "per_day": 10000},
                        "support": "best-effort email, one onboarding session",
                        "sell_when": "prospect wants to validate one workflow against real live data",
                    },
                    {
                        "name": "Desk API",
                        "price_eur_per_month": 1500,
                        "term": "annual preferred; monthly at operator discretion",
                        "included_keys": 2,
                        "included_limits": {"per_min": 240, "per_day": 50000},
                        "support": "email support, quarterly key review, methodology change notices",
                        "sell_when": "one desk or data team depends on the API repeatedly",
                    },
                    {
                        "name": "Agent / MCP",
                        "price_eur_per_month": 2500,
                        "term": "annual preferred",
                        "included_keys": 3,
                        "included_limits": {"per_min": 300, "per_day": 100000},
                        "support": "MCP integration handoff, fail-closed test pack, audit verification",
                        "sell_when": "client is wiring 13FLOW into automated research agents",
                    },
                    {
                        "name": "Enterprise / redistribution",
                        "price_eur_per_month": "from 6000",
                        "term": "custom contract",
                        "included_keys": "custom",
                        "included_limits": "custom",
                        "support": "custom SLA, legal/security review, redistribution terms",
                        "sell_when": "client needs redistribution, many keys, custom limits or procurement terms",
                    },
                ],
                "do_not_discount_below": {
                    "full_live_api_access_eur_per_month": 490,
                    "reason": "below that level the buyer gets curated live workflow, support and audit for less than the operator cost of serious onboarding",
                },
                "pricing_policy": {
                    "strategy": "better_not_cheaper",
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
                    "discount_rule": "reduce scope, term or request limits before reducing the full live API floor",
                },
                "market_context": [
                    {
                        "provider": "SEC.gov",
                        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
                        "observed_offer": "official EDGAR JSON APIs and nightly bulk files",
                        "risk_if_competing_directly": "free official source makes raw filing resale indefensible",
                        "thirteenflow_response": "sell normalized workflow, quality warnings, status evidence and support around the official data",
                    },
                    {
                        "provider": "SEC-API.io",
                        "source_url": "https://sec-api.io/pricing",
                        "observed_offer": "broad SEC API suite with self-serve personal/business plans and enterprise options",
                        "risk_if_competing_directly": "a generic 13F or Form 4 endpoint would be compared against a mature low-cost API vendor",
                        "thirteenflow_response": "position as a narrower research product: 13F, Form 4 validation, Confluence boundary, MCP and audit-ready onboarding",
                    },
                    {
                        "provider": "Quiver Quantitative",
                        "source_url": "https://api.quiverquant.com/",
                        "observed_offer": "alternative-data API and retail platform, including insider trades, hedge fund activity and MCP surface",
                        "risk_if_competing_directly": "broad alternative-data UX is hard to beat with a narrower raw-data catalogue",
                        "thirteenflow_response": "stay professional and evidence-first: fewer claims, stronger method boundary, scoped Pro API and verifiable MCP behavior",
                    },
                    {
                        "provider": "Dataroma",
                        "source_url": "https://www.dataroma.com/m/home.php",
                        "observed_offer": "free curated superinvestor portfolios and significant insider buys",
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
                    "package": "Pilot access | Desk API | Agent / MCP workflow",
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
                    "--api-key-rate-per-min 120 --api-key-rate-per-day 10000"
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
                "token_storage": "plaintext token shown once; SHA-256 hash stored at rest",
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

    def _html_response(title: str, body: str) -> Response:
        nonce = secrets.token_urlsafe(16)
        nav = (
            '<nav class="topnav"><a class="brand" href="/">13<span>FL</span><b>OW</b></a>'
            '<div class="navlinks"><a href="/funds">Funds</a><a href="/stocks">Stocks</a>'
            '<a href="/signals">Signals</a><a href="/status">Status</a><a href="/methodology">Methodology</a>'
            '<a href="/developers">Developers</a><a href="/pro">Pro API</a><a href="/faq">FAQ</a>'
            '<a href="/legal">Legal</a></div></nav>'
        )
        footer = (
            '<footer class="site-footer"><div class="foot-grid">'
            '<div><h4>13FLOW</h4><p>SEC EDGAR-derived 13F and Form 4 research surfaces '
            'for analysts, APIs and agent workflows.</p></div>'
            '<div><h4>Product</h4><a href="/funds">Funds</a><a href="/stocks">Stocks</a>'
            '<a href="/signals">Signals</a><a href="/pro">Pro API</a></div>'
            '<div><h4>Method</h4><a href="/methodology">Overview</a>'
            '<a href="/methodology/app">Application</a><a href="/methodology/mcp">MCP</a>'
            '<a href="/api/methodology/confluence-v1">Confluence v1</a></div>'
            '<div><h4>Trust</h4><a href="/status">Status</a><a href="/developers">Developers</a>'
            '<a href="/api/openapi.json">OpenAPI</a><a href="/api/live-status">Live status</a>'
            '<a href="/legal">Legal</a></div>'
            '</div><div class="fine"><span>Public filings research. Not investment advice.</span>'
            '<span>Source: SEC EDGAR · LIVE state exposed at /api/live-status</span></div></footer>'
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(title)} · 13FLOW</title><link href="/assets/fonts/13flow-fonts.css" rel="stylesheet">
<style>
:root{{--bg:#0c1611;--panel:#13241c;--panel-2:#16291f;--line:#1f3329;--line-soft:#182a20;--text:#eaf5ef;--muted:#a9c4b7;--faint:#6f897d;--accent:#19c187;--amber:#e0a534;--sans:'Hanken Grotesk',system-ui,sans-serif;--display:'Bricolage Grotesque',system-ui,sans-serif;--mono:'Geist Mono',ui-monospace,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55;letter-spacing:0;background-image:linear-gradient(180deg,rgba(255,255,255,.025),transparent 420px)}}
.wrap{{max-width:1120px;margin:0 auto;padding:24px 24px 0}}a{{color:var(--accent);text-decoration:none}}.topnav{{display:flex;gap:18px;align-items:center;margin-bottom:38px;flex-wrap:wrap}}.navlinks{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-left:auto}}.navlinks a{{color:var(--muted);font-weight:650;font-size:13px;padding:8px 11px;border-radius:999px}}.navlinks a:hover{{color:var(--text);background:var(--panel-2)}}.brand{{font-family:var(--display);font-size:24px;font-weight:800;color:var(--text);margin-right:auto}}.brand span{{color:var(--accent)}}.brand b{{color:var(--amber)}}h1{{font-family:var(--display);font-size:40px;line-height:1.05;margin:0 0 8px}}h2{{font-family:var(--display)}}.lede{{color:var(--muted);max-width:760px;margin:0 0 26px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}}.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px}}.card h2,.card h3{{font-family:var(--display);margin:0 0 6px}}.card p,.panel p,.panel li{{color:var(--muted)}}.meta,.num{{font-family:var(--mono)}}.meta{{font-size:12px;color:var(--faint)}}.num{{font-size:13px}}pre{{white-space:pre-wrap;background:var(--panel-2);border:1px solid var(--line);border-radius:14px;padding:14px;overflow:auto}}code{{font-family:var(--mono)}}table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}th:first-child,td:first-child{{text-align:left}}th{{font-family:var(--mono);font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}}td{{font-size:14px}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 9px;font-family:var(--mono);font-size:11px;color:var(--muted);margin:2px 5px 2px 0}}.sec{{font-family:var(--mono);font-size:11px}}.site-footer{{margin-top:46px;border-top:1px solid var(--line);padding:28px 0 34px;color:var(--muted)}}.foot-grid{{display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:26px}}.site-footer h4{{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin:0 0 10px}}.site-footer p{{margin:0;color:var(--muted);font-size:13px;line-height:1.55;max-width:38ch}}.site-footer a{{display:block;color:var(--text);font-weight:600;font-size:13px;margin:7px 0}}.site-footer a:hover{{color:var(--accent)}}.fine{{border-top:1px solid var(--line-soft);margin-top:24px;padding-top:16px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--faint)}}@media(max-width:760px){{.wrap{{padding:18px 16px 0}}.topnav{{align-items:flex-start}}.navlinks{{margin-left:0}}.foot-grid{{grid-template-columns:1fr}}h1{{font-size:34px}}}}
</style></head><body><div class="wrap">{nav}{body}{footer}</div><script nonce="{nonce}"></script></body></html>"""
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
            ],
            "not_verified_yet": [
                "Confluence v1 is not validated as alpha, probability or expected-return model",
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
                f"<p>Publishable as full validation: <code>{str(artifact.get('publishable_as_full_validation')).lower()}</code></p>"
                f"<p class=\"meta\">features_sha256={html_escape(str(artifact.get('features_sha256') or ''))}</p>"
                f"<p class=\"meta\">prices_sha256={html_escape(str(artifact.get('prices_sha256') or ''))}</p></div>"
            )
        body = (
            f"<h1>{html_escape(title)}</h1>"
            f"<p class=\"lede\">{html_escape(payload['scope'])}</p>"
            "<div class=\"grid\">"
            f"<div class=\"card\"><h3>API contract</h3><p><a href=\"{html_escape(api_path)}\">{html_escape(api_path)}</a></p>"
            f"<p class=\"meta\">SHA {html_escape(payload['git_sha'][:12])}</p></div>"
            f"<div class=\"card\"><h3>Current state</h3><p class=\"meta\">Generated {html_escape(payload['generated_at'])}</p></div>"
            "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Method</h2>"
            f"<ul>{bullets}</ul></div>"
            + proof_panels +
            artifact_panel +
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Sources and contracts</h2>"
            f"<ul>{sources}</ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Interpretation boundary</h2>"
            f"<ul>{caveats}</ul>"
            f"<p class=\"meta\">Validation status: {html_escape(str(boundary.get('status') or 'see API contract'))}</p></div>"
        )
        return _html_response(title, body)

    @app.get("/methodology")
    def methodology_hub():
        body = (
            "<h1>Methodology</h1>"
            "<p class=\"lede\">How 13FLOW turns public SEC filings into read-only research surfaces, "
            "and how the MCP layer exposes that context to agents.</p>"
            "<div class=\"grid\">"
            "<a class=\"card\" href=\"/methodology/app\"><h2>Application methodology</h2>"
            "<p>Data pipeline, 13F caveats, quality warnings and validation boundary.</p></a>"
            "<a class=\"card\" href=\"/methodology/mcp\"><h2>MCP methodology</h2>"
            "<p>Tool contract, Pro gating, fail-closed behavior and agent safety.</p></a>"
            "</div>"
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
            f"<p class=\"num\">{html_escape(str(pkg['price_eur_per_month']))} EUR / month</p>"
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
        market_cards = "".join(
            "<div class=\"card\">"
            f"<h3>{html_escape(item['provider'])}</h3>"
            f"<p>{html_escape(item['observed_offer'])}</p>"
            f"<p class=\"meta\">Risk: {html_escape(item['risk_if_competing_directly'])}</p>"
            f"<p>{html_escape(item['thirteenflow_response'])}</p>"
            f"<p class=\"meta\"><a href=\"{html_escape(item['source_url'], quote=True)}\">source</a></p>"
            "</div>"
            for item in commercial["market_context"]
        )
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
            "<h1>13FLOW Pro API</h1>"
            "<p class=\"lede\">Source-linked 13F data, quality warnings and agent-ready "
            "read-only access for research desks that want to stop hand-scraping SEC filings.</p>"
            "<p><a class=\"pill\" href=\"" + contact_link + "\">Request access</a> "
            f"<span class=\"meta\">{html_escape(contact['expected_response'])}</span></p>"
            "<div class=\"grid\">"
            "<div class=\"card\"><h3>What you get</h3>"
            f"<p>{html_escape(offer['offer']['positioning'])}</p>"
            "<p class=\"meta\">No public checkout is enabled on the open build; access is operator issued.</p></div>"
            "<div class=\"card\"><h3>Default limits</h3>"
            f"<p class=\"num\">{limits['rate_per_min']} / min · {limits['rate_per_day']} / day</p>"
            f"<p class=\"meta\">{limits['max_positions_per_fund_detail']} positions and "
            f"{limits['max_moves_per_fund_detail']} moves per bounded fund-detail call.</p></div>"
            "<div class=\"card\"><h3>Verification</h3>"
            "<p><a href=\"/api/pro-offer\">/api/pro-offer</a> · "
            "<a href=\"/api/product-status\">/api/product-status</a> · "
            "<a href=\"/status\">/status</a> · "
            "<a href=\"/api/pro/v1/openapi.json\">Pro OpenAPI</a></p></div>"
            "</div>"
            "<h2>Plans</h2><div class=\"grid\">" + plans + "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Access request checklist</h2>"
            "<p class=\"lede\">Send these details first so the operator can issue the right scoped key.</p>"
            f"<ul>{checklist}</ul>"
            "<p><a class=\"pill\" href=\"" + contact_link + "\">Email access request</a></p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Who buys this</h2>"
            "<div class=\"grid\">" + icp_cards + "</div></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Pricing guide</h2>"
            f"<p class=\"lede\">{html_escape(commercial['principle'])}</p>"
            f"<p class=\"meta\">Strategy: {html_escape(commercial['pricing_policy']['strategy'])}. "
            f"{html_escape(commercial['pricing_policy']['discount_rule'])}</p>"
            "<div class=\"grid\">" + commercial_cards + "</div>"
            "<h3>Compete on</h3>"
            f"<ul>{compete_on}</ul>"
            f"<p class=\"meta\">Do not sell full live API access below "
            f"{html_escape(str(commercial['do_not_discount_below']['full_live_api_access_eur_per_month']))} EUR / month. "
            f"{html_escape(commercial['do_not_discount_below']['reason'])}</p></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Competitive position</h2>"
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
            "<h2>Included</h2><div class=\"grid\">" + included + "</div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Not claimed yet</h2>"
            "<p class=\"lede\">These claims require additional evidence or configuration before use in sales material.</p>"
            f"<ul>{not_yet}</ul></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Onboarding flow</h2>"
            f"<ol>{onboarding}</ol></div>"
            "<div class=\"panel\" style=\"margin-top:18px\"><h2>Validation boundary</h2>"
            f"<p>{html_escape(offer['truth_boundary']['blocked_by'])}</p>"
            f"<p class=\"meta\">Current sample feature hash: "
            f"{html_escape(offer['truth_boundary']['current_artifact']['features_sha256'])}</p>"
            f"<p class=\"meta\">Current sample price hash: "
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

    @app.get("/")
    def index():
        return _serve_html(dash)

    @app.get("/dashboard.html")
    def dashboard_alias():
        return redirect("/", code=301)

    _FAQ = os.path.join(os.path.dirname(dash), "faq.html")

    @app.get("/faq")
    def faq():
        return _serve_html(_FAQ)

    @app.get("/faq.html")
    def faq_legacy_alias():
        return redirect("/faq", code=301)

    _LEGAL = os.path.join(os.path.dirname(dash), "mentions-legales.html")

    @app.get("/legal")
    def legal():
        return _serve_html(_LEGAL)

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
