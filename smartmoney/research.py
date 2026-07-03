"""
Research contracts for Confluence validation.

Everything here is DB/file only: no EDGAR, no prices, no network. The goal is to make
the production score version, parameters, universe and signal revisions auditable before
any performance claim is attached to them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable

from .crosssignal import DEFAULT_FEATURES, DEFAULT_WEIGHTS

CONFLUENCE_VERSION = "confluence_v1"
FEATURE_SCHEMA_VERSION = "confluence_features_v1"
WEIGHT_VERSION = "heuristic_default_v1"
HISTORY_FILENAME = "confluence-history.jsonl"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def current_git_sha(app_root: str | None = None) -> str:
    env_sha = os.environ.get("SMARTMONEY_GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    root = app_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    git_dir = os.path.join(root, ".git")
    try:
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            with open(os.path.join(git_dir, *ref.split("/")), "r", encoding="utf-8") as fh:
                return fh.read().strip()
        return head
    except OSError:
        return "unknown"


def confluence_v1_spec(code_commit: str = "unknown") -> dict[str, Any]:
    """Frozen machine-readable contract for the production Confluence v1 hypothesis."""
    features = asdict(DEFAULT_FEATURES)
    weights = asdict(DEFAULT_WEIGHTS)
    weights.pop("TUNABLE", None)
    weights.pop("BOUNDS", None)
    spec = {
        "version": CONFLUENCE_VERSION,
        "status": "frozen_hypothesis_not_validated",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "weight_version": WEIGHT_VERSION,
        "code_commit": code_commit,
        "score_interpretation": {
            "scale": "0_to_100_ordinal_ranking",
            "is_probability": False,
            "is_expected_return": False,
            "is_calibrated_frequency": False,
        },
        "parameters": {
            "feature_extraction": features,
            "combination_weights": weights,
            "tunable_weights": list(DEFAULT_WEIGHTS.TUNABLE),
            "bounds": DEFAULT_WEIGHTS.BOUNDS,
        },
        "effective_universe": {
            "institutional": (
                "Tracked 13F managers stored in the SQLite market database; latest_filings "
                "selects one complete-enough filing per manager and report date."
            ),
            "insider": (
                "Form 4 open-market P/S issuer filings when live/precomputed Confluence is "
                "enabled; no silent sample fallback in production. Production Confluence is "
                "not a complete insider-universe crawl and does not make insider-only or "
                "distribution quadrants exhaustive."
            ),
            "filing_scope_boundary": {
                "form_13f": (
                    "Delayed long US reportable securities only; no complete view of shorts, "
                    "most non-US holdings, bonds, full derivative books, intra-quarter trading "
                    "or confidential-treatment omissions."
                ),
                "form_4": (
                    "Table I open-market P/S activity is the current usable rail. Table II "
                    "derivatives, 10b5-1 plan flags, multi-owner attribution and weighted-"
                    "average price footnotes remain explicit limitations until modeled."
                ),
            },
            "coverage_fields_required": [
                "funds_scanned",
                "latest_13f_quarter",
                "accessions",
                "ticker_resolution_coverage",
            ],
        },
        "splits": {
            "train": {"from": "2014-01-01", "to": "2022-12-31"},
            "validation": {"from": "2023-01-01", "to": "2024-12-31"},
            "test": {"from": "2025-01-01", "to": "2026-12-31", "frozen": True},
        },
        "required_public_evidence": [
            "point_in_time_feature_dataset_or_hash_manifest",
            "baselines",
            "walk_forward",
            "out_of_sample_test",
            "transaction_costs",
            "confidence_intervals",
            "revision_history",
        ],
    }
    spec["parameter_hash"] = stable_json_hash(spec["parameters"])
    return spec


def signal_history_entry(payload: dict[str, Any], signal: dict[str, Any],
                         *, window_days: int, source: str,
                         code_commit: str = "unknown",
                         generated_at: str | None = None) -> dict[str, Any]:
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    signal_body = dict(signal)
    ticker = str(signal_body.get("ticker") or "").upper()
    entry = {
        "history_version": 1,
        "recorded_at": utc_now_iso(),
        "generated_at": generated_at or meta.get("generated_at") or payload.get("generated_at"),
        "source": source,
        "window_days": window_days,
        "ticker": ticker,
        "score_version": CONFLUENCE_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "weight_version": WEIGHT_VERSION,
        "parameter_hash": confluence_v1_spec(code_commit)["parameter_hash"],
        "code_commit": code_commit,
        "score": signal_body.get("score"),
        "quadrant": signal_body.get("quadrant"),
        "revision_hash": stable_json_hash({
            "version": CONFLUENCE_VERSION,
            "window_days": window_days,
            "ticker": ticker,
            "signal": signal_body,
            "metadata": meta,
        }),
        "signal": signal_body,
    }
    return entry


def append_signal_history(payloads: Iterable[tuple[int, str, dict[str, Any]]],
                          history_path: str,
                          *,
                          code_commit: str = "unknown") -> dict[str, Any]:
    """Append Confluence cache payloads to JSONL history; never rewrites old records."""
    os.makedirs(os.path.dirname(os.path.abspath(history_path)) or ".", exist_ok=True)
    n_payloads = 0
    n_signals = 0
    with open(history_path, "a", encoding="utf-8") as fh:
        for window_days, source, payload in payloads:
            n_payloads += 1
            for sig in payload.get("signals") or []:
                entry = signal_history_entry(
                    payload, sig, window_days=window_days, source=source,
                    code_commit=code_commit,
                )
                fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
                n_signals += 1
    return {
        "history_path": history_path,
        "payloads_appended": n_payloads,
        "signals_appended": n_signals,
        "score_version": CONFLUENCE_VERSION,
        "code_commit": code_commit,
    }


def read_signal_history(history_path: str, *, limit: int = 100,
                        ticker: str | None = None,
                        window_days: int | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(1000, int(limit)))
    want_ticker = ticker.upper() if ticker else None
    out: list[dict[str, Any]] = []
    try:
        with open(history_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if want_ticker and row.get("ticker") != want_ticker:
                    continue
                if window_days is not None and int(row.get("window_days") or 0) != window_days:
                    continue
                out.append(row)
    except OSError:
        return []
    return out[-limit:]
