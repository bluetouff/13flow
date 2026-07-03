"""
Read-only JSON endpoint for the Confluence feature. Drop-in blueprint matching the
existing api.py conventions (read-only, JSON, no mutation).

Wire-in (one line in your create_app):

    from .api_signals import make_signals_blueprint
    app.register_blueprint(make_signals_blueprint(provider))

`provider` is anything implementing `confluence(window_days) -> list[ConfluenceSignal]`.
A reference `StoreConfluenceProvider` is sketched below: it reads institutional
accumulation from your existing store and pulls Form 4s via forms4.Form4Client.

For explicit demo/preview mode, `SampleConfluenceProvider` returns the same shape the
dashboard expects. Production should use a live provider or a precomputed cache file; it
must not silently fall back to sample data.
"""

from __future__ import annotations

from typing import Protocol

from flask import Blueprint, jsonify, request

from .crosssignal import (
    ConfluenceSignal, InsiderActivity, InsiderBuyer, InstitutionalSignal,
    aggregate_insider_activity, build_confluence,
)


class ConfluenceProvider(Protocol):
    def confluence(self, window_days: int) -> list[ConfluenceSignal]:
        ...


class ConfluenceUnavailable(RuntimeError):
    """Raised when no live/cache Confluence data can be served."""


class UnconfiguredConfluenceProvider:
    def confluence_metadata(self) -> dict:
        return {
            "provider": "unconfigured",
            "demo_mode": False,
            "sample_data": False,
        }

    def confluence(self, window_days: int) -> list[ConfluenceSignal]:
        raise ConfluenceUnavailable(
            "Confluence live data is not configured and no precomputed cache is available. "
            "Enable SMARTMONEY_CONFLUENCE_LIVE=1 with SEC_UA, publish confluence-<window>.json "
            "with run.py --confluence, or set SMARTMONEY_CONFLUENCE_DEMO=1 only for explicit demos."
        )


def default_methodology_metadata() -> dict:
    return {
        "score_interpretation": (
            "Ordinal exploratory ranking from 0 to 100; not a probability, not a calibrated "
            "historical frequency, and not an expected return estimate."
        ),
        "calibration_status": "not_calibrated_on_live_history",
        "validation_status": "hypothesis_not_live_validated",
        "backtest_status": (
            "Backtest harness available; no published point-in-time live-history "
            "out-of-sample result is attached to this score version."
        ),
        "weight_policy": (
            "Default weights are heuristic judgement parameters. Optimized weights must be "
            "reported separately with their train/validation/test split, feature version, "
            "and out-of-sample evidence."
        ),
        "effective_universe": {
            "institutional": (
                "Tracked 13F managers stored in the market database; latest_filings selects "
                "one complete-enough filing per manager and report date."
            ),
            "insider": (
                "Production Confluence scans Form 4 only for tickers with meaningful "
                "tracked-fund accumulation, normally SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS=3. "
                "This is an SEC-rate-limit control, not a complete insider-universe crawl."
            ),
        },
        "filing_scope_boundary": {
            "form_13f": (
                "Form 13F exposes delayed long US reportable securities. It does not expose "
                "shorts, most international holdings, bonds, full derivatives exposure, "
                "intra-quarter trading, or positions temporarily omitted through confidential "
                "treatment."
            ),
            "form_4": (
                "Confluence uses normalized Table I Form 4 transactions and treats open-market "
                "P/S activity as the usable rail. Table II derivative rows, 10b5-1 plan flags, "
                "multi-owner attribution nuances and weighted-average price footnotes are not "
                "fully modeled in the live score yet."
            ),
        },
        "validation_protocol": {
            "train": "2014-01-01 through 2022-12-31",
            "validation": "2023-01-01 through 2024-12-31",
            "test": "2025-01-01 through 2026-12-31",
            "forward_horizons_days": [20, 60, 120],
            "required_controls": [
                "point-in-time feature availability",
                "tradable universe and liquidity rules",
                "adjusted prices including delistings where available",
                "transaction costs, execution lag, and rebalance frequency",
                "sector, size, and beta neutralization",
                "confidence intervals, permutation tests, and walk-forward stability",
                "baselines: insider-only, fund-count-only, confluence-without-score, equal-weight",
            ],
        },
        "quantitative_evidence_boundary": (
            "Current production score is a transparent hypothesis and ranking heuristic. "
            "It must not be described as validated until a frozen version passes the "
            "published out-of-sample protocol."
        ),
        "known_limitations": [
            "Feature half-lives, seniority multipliers, dollar caps, saturation curves, and "
            "agreement bonus are judgement parameters until sensitivity tables and live "
            "historical validation are published.",
            "The agreement bonus can overlap with information already present in the "
            "institutional and insider pillars.",
            "Fund-count and dollar variables depend on the tracked-fund universe size.",
            "The effective Form 4 universe is partial in production: insider filings are "
            "looked up only for tickers that pass the institutional accumulation scan "
            "threshold, so insider-only, distribution, and divergent categories are not "
            "exhaustive.",
            "Form 4 parsing currently focuses on Table I transaction rows; derivative Table II "
            "activity, 10b5-1 plan flags, multi-owner attribution and price footnotes remain "
            "methodology limits until explicitly modeled.",
            "The score currently excludes valuation, liquidity, market cap, sector, market "
            "regime, and base-rate returns.",
            "Quadrants describe direction; the numeric score describes heuristic intensity, "
            "so quadrant and rank can diverge.",
        ],
    }


def confluence_payload(signals, window: int, min_score: float = 0.0,
                       metadata: dict | None = None) -> dict:
    """Assemble the endpoint's JSON (KPIs + per-signal dicts) from a list of signals.
    Shared by the live endpoint and the precompute CLI so a cached file is identical in
    shape to a live response."""
    payload = [s.to_dict() for s in signals if s.score >= min_score]
    conviction = [s for s in signals if s.quadrant == "conviction"]
    kpis = {
        "n_signals": len(payload),
        "n_conviction": len(conviction),
        "n_csuite_clusters": sum(
            1 for s in signals
            if s.insider.n_c_suite_buyers and s.insider.is_cluster
        ),
        "top_ticker": signals[0].ticker if signals else None,
        "top_score": round(signals[0].score, 1) if signals else None,
        "insider_buy_usd": round(sum(s.insider.buy_value_usd for s in signals), 2),
        "window_days": window,
    }
    meta = default_methodology_metadata()
    if metadata:
        meta.update(metadata)
    return {"metadata": meta, "kpis": kpis, "signals": payload}


def merge_methodology_metadata(payload: dict, provider_metadata: dict | None = None) -> dict:
    """Attach current methodology metadata to live or cached Confluence payloads."""
    out = dict(payload)
    meta = default_methodology_metadata()
    existing = out.get("metadata")
    if isinstance(existing, dict):
        meta.update(existing)
    if provider_metadata:
        meta.update(provider_metadata)
    out["metadata"] = meta
    return out


def make_signals_blueprint(provider: ConfluenceProvider, cache_dir=None, cache_enricher=None) -> Blueprint:
    bp = Blueprint("signals", __name__)

    @bp.get("/api/signals/confluence")
    def confluence():
        try:
            window = max(7, min(365, int(request.args.get("window", 90))))
        except (TypeError, ValueError):
            window = 90
        try:
            min_score = max(0.0, min(100.0, float(request.args.get("min_score", 0))))
        except (TypeError, ValueError):
            min_score = 0.0

        # Fast path: serve a precomputed cache file if present (`run.py --confluence` writes it).
        # Keeps the public tier off EDGAR entirely and makes the page instant.
        if cache_dir and min_score == 0.0:
            import json, os
            cpath = os.path.join(cache_dir, f"confluence-{window}.json")
            try:
                with open(cpath, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                    if cache_enricher:
                        payload = cache_enricher(payload)
                    return jsonify(merge_methodology_metadata(
                        payload, {"served_from_cache": True}
                    ))
            except (FileNotFoundError, OSError, ValueError):
                pass  # no/invalid cache -> fall back to the provider

        try:
            signals = provider.confluence(window)
        except ConfluenceUnavailable as e:
            metadata = getattr(provider, "confluence_metadata", lambda: {})()
            return jsonify({
                "error": "confluence_unavailable",
                "message": str(e),
                "metadata": merge_methodology_metadata({}, metadata)["metadata"],
                "parameters": {"window_days": window, "min_score": min_score},
            }), 503
        metadata = getattr(provider, "confluence_metadata", lambda: {})()
        return jsonify(confluence_payload(signals, window, min_score, metadata=metadata))

    return bp


# ---------------------------------------------------------------------------
# Reference provider against the real store (sketch — wire to your store API)
# ---------------------------------------------------------------------------
class StoreConfluenceProvider:
    """
    Joins your existing 13F store with live Form 4s.

    `store` is expected to expose the institutional side per ticker. The exact method
    names depend on your store.py; adapt `_institutional()` to your schema. The two
    obvious sources you already persist:
      - consensus 'adds/opens' per ticker last quarter  -> funds_accumulating
      - consensus 'trims/exits' per ticker last quarter  -> funds_trimming
    """

    def __init__(self, store, form4_client):
        self._store = store
        self._f4 = form4_client

    def _institutional(self) -> dict[str, InstitutionalSignal]:
        # EXAMPLE adapter — replace bodies with your store's real queries.
        rows = self._store.consensus_accumulation()  # -> [{ticker, adds, trims, value, funds}]
        out: dict[str, InstitutionalSignal] = {}
        for r in rows:
            out[r["ticker"].upper()] = InstitutionalSignal(
                ticker=r["ticker"].upper(),
                funds_accumulating=int(r.get("adds", 0)),
                funds_trimming=int(r.get("trims", 0)),
                total_value_usd=float(r.get("value", 0.0)),
                fund_labels=tuple(r.get("funds", ())),
            )
        return out

    def _insider(self, tickers, window_days: int) -> dict[str, InsiderActivity]:
        out: dict[str, InsiderActivity] = {}
        for ticker, issuer_cik in tickers:  # store maps ticker -> issuer CIK
            forms = self._f4.insider_filings(issuer_cik, window_days=window_days)
            forms = [f for f in forms if f.issuer_ticker == ticker or not f.issuer_ticker]
            out[ticker.upper()] = aggregate_insider_activity(
                ticker, forms, window_days=window_days)
        return out

    def confluence(self, window_days: int) -> list[ConfluenceSignal]:
        inst = self._institutional()
        ticker_ciks = self._store.ticker_cik_map(list(inst))  # ticker -> issuer CIK
        ins = self._insider(ticker_ciks.items(), window_days)
        return build_confluence(inst, ins)


# ---------------------------------------------------------------------------
# Sample provider — zero network, zero DB. Lets the dashboard render live-shaped data.
# ---------------------------------------------------------------------------
class SampleConfluenceProvider:
    def confluence_metadata(self) -> dict:
        return {
            "provider": "sample_confluence",
            "demo_mode": True,
            "sample_data": True,
            "effective_universe": "Explicit demo dataset; not production data.",
        }

    def confluence(self, window_days: int) -> list[ConfluenceSignal]:
        from .sample_confluence import sample_signals
        return sample_signals(window_days)
