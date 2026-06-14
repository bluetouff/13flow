"""
Read-only JSON endpoint for the Confluence feature. Drop-in blueprint matching the
existing api.py conventions (read-only, JSON, no mutation).

Wire-in (one line in your create_app):

    from .api_signals import make_signals_blueprint
    app.register_blueprint(make_signals_blueprint(provider))

`provider` is anything implementing `confluence(window_days) -> list[ConfluenceSignal]`.
A reference `StoreConfluenceProvider` is sketched below: it reads institutional
accumulation from your existing store and pulls Form 4s via forms4.Form4Client.

For demo/preview without a DB or network, `SampleConfluenceProvider` returns the same
shape the dashboard expects, so the UI lights up immediately.
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


def confluence_payload(signals, window: int, min_score: float = 0.0) -> dict:
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
    return {"kpis": kpis, "signals": payload}


def make_signals_blueprint(provider: ConfluenceProvider, cache_dir=None) -> Blueprint:
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
                    return jsonify(json.load(fh))
            except (FileNotFoundError, OSError, ValueError):
                pass  # no/invalid cache -> fall back to the provider

        signals = provider.confluence(window)
        return jsonify(confluence_payload(signals, window, min_score))

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
    def confluence(self, window_days: int) -> list[ConfluenceSignal]:
        from .sample_confluence import sample_signals
        return sample_signals(window_days)
