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
  GET /pro/v1/data-quality            -> Pro data-quality report (API key)
  GET /subscriptions
  GET /alerts/preview/<cik>
GET /  -> dashboard.html

Core endpoints work fully offline (reported, quarter-end figures). Valuation (value=1)
needs a price provider and hits the network at request time, so it's opt-in.
"""

from __future__ import annotations

import os
from typing import Optional

import functools
import secrets
from flask import Flask, Response, jsonify, make_response, request
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


class _StoreConfluence:
    """Live Confluence provider: institutional side from the 13F store, insider side from
    Form 4s via EDGAR. Issuer ticker->CIK comes from SEC's company_tickers.json. Any failure
    degrades gracefully (logs + returns what it has) so the endpoint never 500s."""

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
                out[t] = InstitutionalSignal(
                    ticker=t,
                    funds_accumulating=m.n_funds,
                    funds_trimming=trim_by_ticker.get(t, 0),
                    fund_labels=tuple(m.funds),
                )
            return out
        finally:
            s.close()

    def confluence(self, window_days: int):
        from .crosssignal import aggregate_insider_activity, build_confluence
        from .forms4 import Form4Client
        try:
            inst = self._institutional()
        except Exception as e:
            __import__("logging").getLogger("smartmoney.api").warning("inst build failed: %s", e)
            return []
        if not inst:
            return []
        idx = self._issuer_index()
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


def create_app(db_path: str = "smartmoney.db", provider=None,
               dashboard_path: Optional[str] = None, secure_cookies: bool = True,
               open_mode: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024   # this API takes no large bodies
    dash = dashboard_path or DASHBOARD
    _truthy = lambda v: str(v or "").strip().lower() in ("1", "true", "yes", "on")
    open_mode = open_mode or _truthy(os.environ.get("SMARTMONEY_OPEN"))
    read_only = _truthy(os.environ.get("SMARTMONEY_DB_READONLY"))
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
    #   3) demo provider (live-shaped sample data) so the screen works with no DB/network
    from .api_signals import make_signals_blueprint, SampleConfluenceProvider
    if os.environ.get("SMARTMONEY_CONFLUENCE_LIVE", "").lower() in ("1", "true", "yes") \
            and os.environ.get("SEC_UA"):
        confluence_provider = _StoreConfluence(db_path, os.environ["SEC_UA"])
    else:
        confluence_provider = SampleConfluenceProvider()
    _cache_dir = os.environ.get("SMARTMONEY_CACHE_DIR") or "."
    app.register_blueprint(make_signals_blueprint(confluence_provider, cache_dir=_cache_dir))

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
                    return jsonify({"error": e.code}), e.status_code
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
                        "methodology": {
                            "source": "SEC EDGAR 13F-HR information tables",
                            "latest_filing_rule": "latest complete-enough accession per CIK/report_date",
                            "aum_jump_threshold": quality["parameters"]["aum_jump_threshold"],
                        },
                    },
                    "quality_summary": quality["summary"],
                    "funds": out,
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

    @app.get("/api/config")
    def config_ep():
        # Lets the single-page dashboard adapt to the build it is served by.
        return jsonify({"open": open_mode,
                        "features": {"auth": not open_mode,
                                     "alerts": not open_mode,
                                     "billing": not open_mode,
                                     "pro_api": pro_enabled}})

    @app.get("/api/version")
    @app.get("/healthz")
    def version_ep():
        return jsonify({"app": "13flow", "git_sha": _git_sha(), "open": open_mode})

    # ---- dashboard ------------------------------------------------------
    def _serve_html(path):
        # Serve a local HTML file with a strict, per-request nonce CSP. The page's single
        # inline <script> gets the nonce; inline event-handler attributes are NOT used, so
        # script-src needs no 'unsafe-inline'. (Inline style attributes remain, hence
        # style-src 'unsafe-inline' — a far weaker allowance than for scripts.)
        if not os.path.exists(path):
            return Response("not found", status=404)
        nonce = secrets.token_urlsafe(16)
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read().replace("<script>", f'<script nonce="{nonce}">')
        resp = Response(html, mimetype="text/html; charset=utf-8")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
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
