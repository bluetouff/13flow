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
  GET /pro/v1/openapi.json            -> Pro API OpenAPI document
  GET /subscriptions
  GET /alerts/preview/<cik>
GET /      -> static public proof home
GET /app   -> dashboard.html research app

Core endpoints work fully offline (reported, quarter-end figures). Valuation (value=1)
needs a price provider and hits the network at request time, so it's opt-in.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from html import escape as html_escape
from types import SimpleNamespace
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
from .registry import Fund, active_ciks
from .tracker import Tier, EntitlementError
from .db import Store
from .diff import Move, diff_portfolios
from .portfolio import Portfolio
from .pro import APIKeyError, APIRateLimited, ProAPIStore
from .quality import data_quality_report, quality_gate_report
from .valuation import value_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)
DASHBOARD = os.path.join(APP_ROOT, "dashboard.html")

MAX_SUBSCRIPTIONS_PER_USER = 50
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

    def _clean_workspace_alert_status(raw, *, allow_all: bool = False) -> str | None:
        from werkzeug.exceptions import BadRequest
        value = str(raw or "open").strip().lower()
        allowed = {"open", "acknowledged", "dismissed"}
        if allow_all and value == "all":
            return None
        if value not in allowed:
            raise BadRequest("invalid alert status")
        return value

    def _pro_store_call(fn):
        ps = ProAPIStore(pro_db_path)
        try:
            return fn(ps)
        finally:
            ps.close()

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
                watchlists = ps.list_watchlists(key.key_id)
            return jsonify({
                "meta": {
                    "api": "13flow-pro",
                    "version": "v1",
                    "git_sha": _git_sha(),
                    "workspace_scope": "api_key",
                    "ui_exposed": False,
                    "automation": "manual_snapshot_only",
                },
                "summary": summary,
                "recent_alerts": recent_alerts,
                "watchlists": watchlists[:50],
            })

        @app.get("/api/pro/v1/workspace/alerts")
        @pro_required("workspace:write")
        def pro_workspace_alerts_ep():
            key = request.pro_api_key
            status = _clean_workspace_alert_status(request.args.get("status"), allow_all=True)
            limit = clean_int(request.args.get("limit"), 50, 1, 100)
            watchlist_id = (request.args.get("watchlist_id") or "").strip() or None
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
            payload = request.get_json(silent=True) or {}
            status = _clean_workspace_alert_status(payload.get("status"))
            with ProAPIStore(pro_db_path) as ps:
                alert = ps.update_workspace_alert_status(key.key_id, alert_id, status)
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
                },
                "watchlists": items,
            })

        @app.post("/api/pro/v1/workspace/watchlists")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_create_ep():
            key = request.pro_api_key
            payload = _clean_saved_watchlist_payload(request.get_json(silent=True) or {})
            item = _pro_store_call(lambda ps: ps.create_watchlist(
                key.key_id,
                payload["name"],
                payload["tickers"],
                filters=payload["filters"],
                alert_policy=payload["alert_policy"],
                notes=payload["notes"],
            ))
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": item,
            }), 201

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_get_ep(watchlist_id):
            key = request.pro_api_key
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
            payload = _clean_saved_watchlist_payload(request.get_json(silent=True) or {})
            item = _pro_store_call(lambda ps: ps.update_watchlist(
                key.key_id,
                watchlist_id,
                payload["name"],
                payload["tickers"],
                filters=payload["filters"],
                alert_policy=payload["alert_policy"],
                notes=payload["notes"],
            ))
            if item is None:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "watchlist": item,
            })

        @app.delete("/api/pro/v1/workspace/watchlists/<watchlist_id>")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_delete_ep(watchlist_id):
            key = request.pro_api_key
            deleted = _pro_store_call(lambda ps: ps.delete_watchlist(key.key_id, watchlist_id))
            if not deleted:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "meta": {"api": "13flow-pro", "version": "v1", "git_sha": _git_sha()},
                "deleted": True,
                "id": watchlist_id,
            })

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>/preview")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_preview_ep(watchlist_id):
            key = request.pro_api_key
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
                "delta": _saved_watchlist_signal_delta(signals, previous),
                "alerts": alerts,
            }), 201

        @app.get("/api/pro/v1/workspace/watchlists/<watchlist_id>/signals/history")
        @pro_required("workspace:write")
        def pro_workspace_watchlists_signals_history_ep(watchlist_id):
            key = request.pro_api_key
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
                "quarantined_funds": gate["summary"]["quarantined_funds"],
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
            '<div class="navlinks"><a class="primary" href="/app">Cockpit</a><a href="/confluence">Confluence</a>'
            '<a href="/funds">Funds</a><a href="/stocks">Stocks</a><a href="/signals">Signals</a>'
            '<a href="/status">Status</a><a href="/validation">Validation</a><a href="/methodology">Methodology</a>'
            '<a href="/developers">API</a><a href="/pro">Pro</a><a href="/about">About</a></div></nav>'
        )
        footer = (
            '<footer class="site-footer"><div class="foot-grid">'
            '<div><h4>13FLOW</h4><p>SEC EDGAR-derived 13F and Form 4 research surfaces '
            'for analysts, APIs and agent workflows.</p></div>'
            '<div><h4>Product</h4><a href="/confluence">Confluence</a><a href="/funds">Funds</a><a href="/stocks">Stocks</a>'
            '<a href="/signals">Signals</a><a href="/validation">Validation</a><a href="/pro">Pro API</a></div>'
            '<div><h4>Method</h4><a href="/methodology">Overview</a>'
            '<a href="/methodology/app">Application</a><a href="/methodology/mcp">MCP</a>'
            '<a href="/api/methodology/confluence-v1">Confluence v1</a></div>'
            '<div><h4>Trust</h4><a href="/status">Status</a><a href="/about">About</a><a href="/developers">Developers</a>'
            '<a href="/api/openapi.json">OpenAPI</a><a href="/api/live-status">Live status</a>'
            '<a href="/legal">Legal</a></div>'
            '</div><div class="fine"><span>Public filings research. Not investment advice.</span>'
            '<span>Built by <a href="https://l0g.fr/" rel="noopener">l0g</a> · Source: SEC EDGAR · LIVE state exposed at /api/live-status</span></div></footer>'
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape(title)} · 13FLOW</title><link href="/assets/fonts/13flow-fonts.css" rel="stylesheet">
<style>
:root{{--bg:#0c1611;--panel:#13241c;--panel-2:#16291f;--panel-3:#101f18;--line:#1f3329;--line-soft:#182a20;--text:#eaf5ef;--muted:#a9c4b7;--faint:#6f897d;--accent:#19c187;--amber:#e0a534;--danger:#ef6a52;--sans:'Hanken Grotesk',system-ui,sans-serif;--display:'Bricolage Grotesque',system-ui,sans-serif;--mono:'Geist Mono',ui-monospace,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.55;letter-spacing:0;background-image:linear-gradient(180deg,rgba(255,255,255,.025),transparent 420px)}}a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1180px;margin:0 auto;padding:22px 24px 0}}.topnav{{position:sticky;top:0;z-index:10;display:flex;gap:18px;align-items:center;margin:0 -24px 34px;padding:14px 24px;border-bottom:1px solid var(--line);background:rgba(12,22,17,.92);backdrop-filter:blur(14px)}}.navlinks{{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin-left:auto}}.navlinks a{{color:var(--muted);font-weight:650;font-size:13px;padding:7px 10px;border-radius:8px}}.navlinks a:hover{{color:var(--text);background:var(--panel-2)}}.navlinks a.primary{{color:#06140f;background:var(--accent)}}.brand{{font-family:var(--display);font-size:24px;font-weight:800;color:var(--text);margin-right:auto;letter-spacing:0}}.brand span{{color:var(--accent)}}.brand b{{color:var(--amber)}}h1{{font-family:var(--display);font-size:44px;line-height:1.02;margin:0 0 10px;letter-spacing:0}}h2,h3{{font-family:var(--display);letter-spacing:0}}.lede{{color:var(--muted);max-width:780px;margin:0 0 24px;font-size:16px}}.hero{{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);gap:18px;align-items:stretch;margin-bottom:18px}}.hero .panel{{min-height:100%}}.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}}.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px}}.card{{display:block;color:var(--text)}}.card:hover{{border-color:var(--accent);background:var(--panel-2)}}.card h2,.card h3{{margin:0 0 6px}}.card p,.panel p,.panel li{{color:var(--muted)}}.card a,.panel a,code{{overflow-wrap:anywhere}}.panel,.meta{{overflow-wrap:anywhere;word-break:break-word}}.meta,.num{{font-family:var(--mono)}}.meta{{font-size:12px;color:var(--faint)}}.num{{font-size:13px}}pre{{white-space:pre-wrap;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto}}code{{font-family:var(--mono)}}table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}th:first-child,td:first-child{{text-align:left}}th{{font-family:var(--mono);font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}}td{{font-size:14px}}.pill{{display:inline-block;max-width:100%;border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-family:var(--mono);font-size:11px;color:var(--muted);margin:2px 5px 2px 0;overflow-wrap:anywhere;word-break:break-word;white-space:normal}}a.pill,.pill.cta{{color:#06140f;background:var(--accent);border-color:var(--accent);font-weight:700}}.sec{{font-family:var(--mono);font-size:11px}}.status-strip{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:18px}}.status-strip div{{border:1px solid var(--line);border-radius:8px;background:var(--panel-2);padding:12px}}.home-hero{{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(360px,.95fr);gap:26px;align-items:center;margin:8px 0 20px;min-height:520px}}.home-copy{{padding:20px 0 28px}}.home-eyebrow{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}}.home-eyebrow span{{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);background:var(--panel-2);border-radius:8px;padding:6px 9px}}.home-copy h1,.home-title{{font-family:var(--display);font-size:72px;line-height:.92;margin:0 0 18px;letter-spacing:0;max-width:760px;overflow-wrap:anywhere}}.home-title .mark{{color:var(--accent)}}.home-lede{{font-size:20px;line-height:1.48;color:var(--muted);max-width:660px;margin:0}}.home-proof{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:26px;max-width:720px}}.proof-item{{border-top:1px solid var(--line);padding-top:11px}}.proof-item b{{display:block;font-family:var(--mono);font-size:22px;line-height:1.1;color:var(--text)}}.proof-item span{{display:block;font-size:12px;color:var(--faint);margin-top:4px}}.home-actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:28px}}.home-actions .button{{display:inline-flex;align-items:center;justify-content:center;min-height:40px;border-radius:8px;padding:10px 14px;font-weight:800;color:#06140f;background:var(--accent);border:1px solid var(--accent)}}.home-actions .button.secondary{{background:transparent;color:var(--text);border-color:var(--line)}}.home-actions .button.secondary:hover{{border-color:var(--accent);color:var(--accent)}}.cockpit-shot{{border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,var(--panel),var(--panel-3));box-shadow:0 28px 70px -34px rgba(0,0,0,.85);overflow:hidden}}.shot-top{{display:flex;justify-content:space-between;gap:12px;align-items:center;border-bottom:1px solid var(--line);padding:14px 16px}}.shot-title{{font-family:var(--display);font-weight:800;font-size:17px}}.shot-live{{font-family:var(--mono);font-size:10px;color:var(--accent);border:1px solid rgba(25,193,135,.32);border-radius:8px;padding:5px 8px;background:rgba(25,193,135,.08)}}.shot-grid{{display:grid;grid-template-columns:1.1fr .9fr;gap:1px;background:var(--line)}}.quadrant{{background:var(--panel);padding:18px;min-height:284px;position:relative}}.axis{{position:absolute;font-family:var(--mono);font-size:10px;color:var(--faint)}}.axis.x{{bottom:12px;left:18px;right:18px;display:flex;justify-content:space-between}}.axis.y{{top:18px;right:16px}}.bubble{{position:absolute;width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font-family:var(--mono);font-size:11px;font-weight:800;color:#06140f;background:var(--accent);box-shadow:0 0 0 8px rgba(25,193,135,.08)}}.bubble.b2{{width:42px;height:42px;left:54%;top:24%;background:var(--amber)}}.bubble.b1{{left:66%;top:44%}}.bubble.b3{{width:36px;height:36px;left:31%;top:54%;background:#7fb89d;color:#081310}}.watchlist{{background:var(--panel);padding:16px}}.watch-row{{display:grid;grid-template-columns:52px 1fr auto;gap:10px;align-items:center;border-bottom:1px solid var(--line-soft);padding:10px 0}}.watch-row:last-child{{border-bottom:0}}.watch-row b{{font-family:var(--mono);font-size:12px;color:var(--accent)}}.watch-row span{{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.watch-row i{{font-family:var(--mono);font-style:normal;font-size:11px;color:var(--faint)}}.trust-band{{display:grid;grid-template-columns:1.1fr repeat(3,.8fr);gap:10px;margin:18px 0 26px}}.trust-band div{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px}}.trust-band b{{display:block;font-family:var(--mono);font-size:13px;color:var(--text)}}.trust-band span{{display:block;color:var(--muted);font-size:12px;margin-top:5px}}.section-head{{display:flex;justify-content:space-between;gap:18px;align-items:end;margin:34px 0 14px}}.section-head h2{{font-size:28px;line-height:1.05;margin:0}}.section-head p{{margin:0;color:var(--muted);max-width:520px}}.journey{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.journey .step{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:18px;display:block;color:var(--text)}}.journey .step:hover{{border-color:var(--accent);background:var(--panel-2)}}.step .n{{font-family:var(--mono);font-size:11px;color:var(--accent);margin-bottom:10px}}.step h3{{font-size:19px;margin:0 0 8px}}.step p{{margin:0;color:var(--muted)}}.boundary{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}.boundary .panel h3{{margin-top:0}}.boundary ul{{padding-left:18px;margin:0}}.doc-hero{{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(300px,.9fr);gap:18px;align-items:stretch;margin:8px 0 18px}}.doc-hero>*,.doc-copy,.doc-panel,.doc-section,.doc-card,.runstep{{min-width:0}}.doc-copy{{padding:18px 0}}.doc-copy h1{{font-size:58px;line-height:.96;margin-bottom:14px;overflow-wrap:anywhere}}.doc-lede{{font-size:19px;line-height:1.5;color:var(--muted);max-width:720px;margin:0}}.doc-panel{{border:1px solid var(--line);border-radius:8px;background:linear-gradient(180deg,var(--panel),var(--panel-3));padding:18px;box-shadow:0 24px 58px -36px rgba(0,0,0,.75);overflow-wrap:anywhere}}.doc-panel h3{{margin:0 0 12px;font-size:18px}}.doc-metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:18px 0 24px}}.doc-metric{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;min-width:0}}.doc-metric b{{display:block;font-family:var(--mono);font-size:21px;color:var(--text);line-height:1.1;overflow-wrap:anywhere}}.doc-metric span{{display:block;color:var(--faint);font-size:12px;margin-top:6px}}.doc-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}}.doc-card{{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:18px;display:block;color:var(--text)}}.doc-card:hover{{border-color:var(--accent);background:var(--panel-2)}}.doc-card h3{{margin:0 0 8px;font-size:19px}}.doc-card p{{margin:0;color:var(--muted)}}.doc-section{{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:20px;overflow-wrap:anywhere}}.doc-section h2{{font-size:24px;margin:0 0 10px}}.doc-section p{{color:var(--muted)}}.doc-section ul{{margin:10px 0 0;padding-left:19px}}.doc-section li{{margin:7px 0}}.runbook{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}}.runstep{{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:14px}}.runstep b{{display:block;font-family:var(--mono);font-size:11px;color:var(--accent);margin-bottom:8px}}.runstep span{{display:block;color:var(--muted);font-size:13px}}.callout{{border-left:3px solid var(--accent);background:var(--panel-2);border-radius:8px;padding:14px 16px;color:var(--muted)}}.callout strong{{color:var(--text)}}.split{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}.mini-list{{display:grid;gap:8px;margin-top:12px}}.mini-list div{{border:1px solid var(--line-soft);border-radius:8px;background:var(--panel-2);padding:11px 12px;color:var(--muted)}}.mini-list b{{color:var(--text)}}.site-footer{{margin-top:46px;border-top:1px solid var(--line);padding:28px 0 34px;color:var(--muted)}}.foot-grid{{display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:26px}}.site-footer h4{{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin:0 0 10px}}.site-footer p{{margin:0;color:var(--muted);font-size:13px;line-height:1.55;max-width:38ch}}.site-footer a{{display:block;color:var(--text);font-weight:600;font-size:13px;margin:7px 0}}.site-footer a:hover{{color:var(--accent)}}.fine{{border-top:1px solid var(--line-soft);margin-top:24px;padding-top:16px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--faint)}}@media(max-width:980px){{.home-hero,.doc-hero{{grid-template-columns:minmax(0,1fr);min-height:0}}.trust-band,.doc-metrics{{grid-template-columns:1fr 1fr}}.journey,.doc-grid{{grid-template-columns:1fr}}.boundary,.split{{grid-template-columns:1fr}}.runbook{{grid-template-columns:1fr 1fr}}}}@media(max-width:860px){{.hero{{grid-template-columns:1fr}}.status-strip{{grid-template-columns:1fr}}.home-proof{{grid-template-columns:1fr 1fr}}.shot-grid{{grid-template-columns:1fr}}.quadrant{{min-height:240px}}}}@media(max-width:760px){{.wrap{{padding:0 16px}}.topnav{{position:relative;display:block;margin:0 -16px 22px;padding:12px 16px}}.brand{{display:block;margin:0 0 10px}}.navlinks{{margin-left:0;display:flex;flex-wrap:nowrap;overflow-x:auto;gap:6px;padding-bottom:4px}}.navlinks a{{white-space:nowrap;flex:0 0 auto}}.foot-grid{{grid-template-columns:1fr}}h1{{font-size:34px}}.home-copy{{padding:4px 0 20px}}.home-copy h1,.home-title,.doc-copy h1{{font-size:48px}}.home-lede,.doc-lede{{font-size:17px;line-height:1.42}}.home-proof{{grid-template-columns:1fr 1fr;margin-top:20px}}.trust-band,.doc-metrics{{grid-template-columns:1fr}}.home-actions{{margin-top:20px}}.home-actions .button{{flex:1 1 155px}}.cockpit-shot{{margin-top:4px}}.runbook{{grid-template-columns:1fr}}table{{display:block;overflow-x:auto}}}}
.fine a{{display:inline;margin:0;font-size:inherit;color:var(--muted)}}
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
        movement_rows = "".join(
            f"<tr><td><a href=\"/funds/{html_escape(m['cik'])}\">{html_escape(m['label'])}</a>"
            f"<div class=\"meta\">prev {html_escape(str(m.get('previous_quarter') or '-'))}</div></td>"
            f"<td><span class=\"pill\">{html_escape(m['move'])}</span></td>"
            f"<td class=\"num\">{_fmt_usd_html(m.get('prev_value_usd'))}</td>"
            f"<td class=\"num\">{_fmt_usd_html(m.get('curr_value_usd'))}</td>"
            f"<td class=\"num\">{float(m.get('curr_weight') or 0) * 100:.2f}%</td>"
            f"<td>{('<a class=\"sec\" href=\"' + html_escape(m['sec_filing_url']) + '\" rel=\"noopener\" target=\"_blank\">SEC</a>') if m.get('sec_filing_url') else '-'}</td></tr>"
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
            "read-only access for bounded technical evaluation. This is an operator-reviewed, "
            "limited-capacity service, not a self-serve SaaS checkout.</p>"
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
            "<a href=\"/validation\">/validation</a> · "
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
            f"<p class=\"meta\">{html_escape(commercial['do_not_discount_below']['reason'])}</p></div>"
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
            "<span>13F x Form 4</span><span>Read-only API</span><span>Methodology contracts</span></div>"
            "<h1>13FLOW</h1>"
            "<p class=\"home-lede\">SEC filing intelligence for fundamental analysts, macro investors and agent workflows. "
            "Track institutional ownership, bounded insider Form 4 activity, source links and data-quality boundaries from one operator-reviewed research cockpit.</p>"
            "<div class=\"home-proof\">"
            f"<div class=\"proof-item\"><b>{html_escape(str(counts.get('funds') or 0))}</b><span>tracked funds</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(str(counts.get('filings') or 0))}</b><span>SEC filings</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(coverage_label)}</b><span>value coverage</span></div>"
            f"<div class=\"proof-item\"><b>{html_escape(latest_q)}</b><span>latest 13F quarter</span></div>"
            "</div><div class=\"home-actions\">"
            "<a class=\"button\" href=\"/app\">Open research app</a>"
            "<a class=\"button secondary\" href=\"/confluence\">Open Confluence</a>"
            "<a class=\"button secondary\" href=\"/pro\">Evaluate Pro API</a></div></div>"
            "<aside class=\"cockpit-shot\" aria-label=\"13FLOW cockpit preview\">"
            "<div class=\"shot-top\"><div><div class=\"shot-title\">Confluence cockpit</div>"
            "<div class=\"meta\">13F accumulation x insider Form 4 evidence</div></div>"
            f"<span class=\"shot-live\">{html_escape(status_label.replace('LIVE EDGAR', 'LIVE · EDGAR'))}</span></div>"
            "<div class=\"shot-grid\"><div class=\"quadrant\">"
            "<span class=\"axis y\">Insider buying intensity</span><div class=\"axis x\"><span>Low fund pressure</span><span>High fund pressure</span></div>"
            "<span class=\"bubble b1\">SIG</span><span class=\"bubble b2\">13F</span><span class=\"bubble b3\">F4</span>"
            "</div><div class=\"watchlist\">"
            "<div class=\"watch-row\"><b>NEW</b><span>Fresh institutional accumulation</span><i>ranked</i></div>"
            "<div class=\"watch-row\"><b>ADD</b><span>Existing holders increasing weight</span><i>weighted</i></div>"
            "<div class=\"watch-row\"><b>F4</b><span>Open-market insider overlap</span><i>sourced</i></div>"
            "<div class=\"watch-row\"><b>DQ</b><span>AUM and unit-scale warnings</span><i>gated</i></div>"
            "</div></div></aside></section>"
            "<section class=\"trust-band\">"
            f"<div><b>Live data status: {html_escape(status_label)}.</b><span>uses_synthetic_data={str(live['uses_synthetic_data']).lower()} · data_as_of={html_escape(data_as_of)}</span></div>"
            f"<div><b>/api/funds serves {html_escape(str(counts.get('funds') or 0))} funds</b><span>filings={html_escape(str(counts.get('filings') or 0))}; latest_rows={html_escape(str(counts.get('latest_filings') or 0))}</span></div>"
            f"<div><b>latest 13F quarter {html_escape(latest_q)}</b><span>SHA {html_escape(sha_short)}</span></div>"
            f"<div><b>{html_escape(str(quality.get('aum_jump_warnings') or 0))} quality warnings</b><span>{html_escape(str(quality.get('unit_scale_candidates') or 0))} unit-scale candidates</span></div>"
            "</section>"
            "<section class=\"section-head\"><div><div class=\"kicker\">Commercial workflow</div><h2>From filing noise to an investable research queue</h2></div>"
            "<p>13FLOW is built for analysts who need structured evidence, not another screen full of unsourced momentum labels.</p></section>"
            "<div class=\"journey\">"
            "<a class=\"step\" href=\"/app#confluence\"><div class=\"n\">01 · Triage</div><h3>Signal cockpit</h3><p>Prioritize names where 13F pressure and Form 4 activity overlap, then drill into source evidence.</p></a>"
            "<a class=\"step\" href=\"/funds\"><div class=\"n\">02 · Attribute</div><h3>Fund and issuer context</h3><p>See who moved, what changed, reported values, accessions and quality flags before a model update.</p></a>"
            "<a class=\"step\" href=\"/developers\"><div class=\"n\">03 · Integrate</div><h3>API and agent surfaces</h3><p>Use read-only endpoints, OpenAPI, MCP methodology and explicit commercial boundaries for downstream workflows.</p></a>"
            "</div>"
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
