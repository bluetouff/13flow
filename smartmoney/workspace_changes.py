"""Deterministic explanations shared by manual and scheduled watchlist snapshots."""

import math
import re


def score(item: dict) -> float | None:
    value = (item.get("score") or {}).get("score")
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def observation_metadata(items: list[dict], tickers: list[str]) -> dict:
    """Keep pre-filter coverage so disappearing from a filter is not a sale."""
    return {
        "scope_tickers": list(tickers),
        "observed_states": {
            item["ticker"]: {
                "action": item.get("action"),
                "confidence": (item.get("confidence") or {}).get("status"),
            } for item in items
        },
    }


def filing_sources(item: dict) -> list[dict]:
    sources = {}
    for move in item.get("top_movements") or []:
        filing = move.get("filing") or {}
        cik = str(move.get("cik") or "")
        if not re.fullmatch(r"[0-9]{1,10}", cik):
            continue
        for accession in filing.get("source_accessions") or [filing.get("accession")]:
            if not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
                continue
            sources[(cik, accession)] = {
                "cik": cik, "accession": accession,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/",
            }
    return [sources[key] for key in sorted(sources)]


def signal_state(item: dict | None, metadata: dict, ticker: str) -> tuple[str, str]:
    observation = (metadata.get("observed_states") or {}).get(ticker) or {}
    action = item.get("action") if item else observation.get("action")
    confidence = ((item.get("confidence") or {}).get("status") if item
                  else observation.get("confidence"))
    if action == "blocked" or confidence == "review":
        return "invalidated", "Data-quality checks no longer allow this signal."
    if item and action in {"alert", "watch"}:
        return "active", "The signal still meets the watchlist conditions."
    if item and action == "monitor":
        return "resolved", "The alert conditions are no longer met."
    scope = metadata.get("scope_tickers")
    if isinstance(scope, list) and ticker not in scope:
        return "resolved", "Ticker removed from this watchlist."
    if observation:
        return "resolved", "Ticker no longer matches the watchlist filters."
    return "unavailable", "No comparable observation; this is not evidence of an exit."


def signal_delta(current: dict, previous_snapshot: dict | None) -> dict:
    previous = (previous_snapshot or {}).get("signals") or {}
    before = {i["ticker"]: i for i in previous.get("items") or []}
    after = {i["ticker"]: i for i in current.get("items") or []}
    meta = current.get("metadata") or {}
    previous_meta = previous.get("metadata") or {}
    actions, scores, events = [], [], []
    for ticker in sorted(set(before) | set(after)):
        old, new = before.get(ticker), after.get(ticker)
        reasons = []
        state = "updated"
        old_score, new_score = score(old or {}), score(new or {})
        if old is None:
            state = "baseline" if previous_snapshot is None else "added"
            reasons.append("First saved observation." if previous_snapshot is None
                           else "Ticker entered the filtered watchlist; this alone does not establish a new purchase.")
        elif new is None:
            state, detail = signal_state(None, meta, ticker)
            reasons.append(detail)
        else:
            if old.get("action") != new.get("action"):
                actions.append({"ticker": ticker, "from": old.get("action"), "to": new.get("action")})
                reasons.append(f"Alert level: {old.get('action')} to {new.get('action')}.")
            if old_score != new_score and (
                old_score is None or new_score is None or abs(new_score - old_score) >= .1 - 1e-9
            ):
                scores.append({"ticker": ticker, "from": old_score, "to": new_score})
                reasons.append("Score availability changed." if old_score is None or new_score is None
                               else f"Screening score: {old_score:g} to {new_score:g}.")
                if old_score is not None and new_score is not None:
                    state = "strengthened" if new_score > old_score else "weakened"
            if old.get("latest_13f_quarter") != new.get("latest_13f_quarter"):
                reasons.append("The reporting quarter changed.")
            if filing_sources(old) != filing_sources(new):
                reasons.append("The SEC filings referenced by the displayed movements changed.")
                if any((m.get("filing") or {}).get("form") == "13F-HR/A" for m in new.get("top_movements") or []):
                    reasons.append("A referenced 13F amendment revises the reporting basis; its filing date is not a trade date.")
            if (old.get("confidence") or {}).get("status") != (new.get("confidence") or {}).get("status"):
                reasons.append("Data-quality status changed.")
            if (old.get("movement_summary") or {}) != (new.get("movement_summary") or {}):
                reasons.append("Reported institutional positions changed.")
            lifecycle, detail = signal_state(new, meta, ticker)
            if lifecycle in {"invalidated", "resolved"} and reasons:
                state = lifecycle
                reasons.append(detail)
            elif lifecycle == "active" and signal_state(old, previous_meta, ticker)[0] in {"invalidated", "resolved"}:
                state = "reactivated"
                reasons.append("The signal meets the watchlist conditions again.")
        if old is not None and reasons and meta.get("filters") != previous_meta.get("filters"):
            reasons.append("Watchlist filters changed between these snapshots.")
        if reasons:
            events.append({
                "ticker": ticker, "state": state, "reasons": reasons,
                "before": None if old is None else {"action": old.get("action"), "score": old_score,
                                                     "quarter": old.get("latest_13f_quarter")},
                "after": None if new is None else {"action": new.get("action"), "score": new_score,
                                                   "quarter": new.get("latest_13f_quarter")},
                "sources_before": filing_sources(old or {}),
                "sources_after": filing_sources(new or {}),
            })
    return {
        "baseline_snapshot_id": (previous_snapshot or {}).get("id"),
        "baseline_created_at": (previous_snapshot or {}).get("created_at"),
        "previous_count": len(before), "current_count": len(after),
        "added_tickers": sorted(set(after) - set(before)),
        "removed_tickers": sorted(set(before) - set(after)),
        "changed_actions": actions, "changed_scores": scores,
        "events": events,
        "evidence_scope": "SEC filings referenced by the saved top movements; not a complete filing history.",
    }
