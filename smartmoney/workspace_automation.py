"""Server-side Pro workspace automation.

This runner snapshots saved Pro watchlists whose alert policy is enabled. It is
designed for systemd timers: bounded, idempotent by cadence, and independent of
plaintext API tokens.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

from .api import create_app
from .pro import ProAPIStore


def _score(item: dict) -> float:
    return float(((item.get("score") or {}).get("score")) or 0.0)


def _matches_filters(item: dict, filters: dict) -> bool:
    summary = item.get("movement_summary") or {}
    actions = set(filters.get("action") or [])
    moves = set(filters.get("move") or [])
    if actions and item.get("action") not in actions:
        return False
    if moves and not (moves & set(item.get("movement_codes") or [])):
        return False
    if filters.get("min_score") is not None and _score(item) < float(filters["min_score"]):
        return False
    if filters.get("min_holders") is not None and int(summary.get("holder_count") or 0) < int(filters["min_holders"]):
        return False
    if filters.get("min_buyers") is not None and int(summary.get("buyers_count") or 0) < int(filters["min_buyers"]):
        return False
    if filters.get("max_13f_value_usd") is not None:
        value = float(summary.get("total_value_usd") or 0.0)
        if value > float(filters["max_13f_value_usd"]):
            return False
    return True


def _summary(items: list[dict]) -> dict:
    return {
        "alerts": len([i for i in items if i.get("action") == "alert"]),
        "watch": len([i for i in items if i.get("action") == "watch"]),
        "monitor": len([i for i in items if i.get("action") == "monitor"]),
        "blocked": len([i for i in items if i.get("action") == "blocked"]),
    }


def _signals_for_watchlist(client, item: dict) -> dict:
    response = client.get(
        "/api/watchlist/preview",
        query_string={"tickers": ",".join(item.get("tickers") or [])},
    )
    if response.status_code != 200:
        raise RuntimeError(f"preview failed with HTTP {response.status_code}")
    payload = response.get_json() or {}
    watchlist = payload.get("watchlist") or payload
    filters = item.get("filters") or {}
    source_items = list(watchlist.get("items") or [])
    filtered = [x for x in source_items if _matches_filters(x, filters)]
    metadata = dict(watchlist.get("metadata") or {})
    metadata.update({
        "version": "saved_watchlist_signals_v1",
        "source": "saved_workspace_watchlist",
        "saved_watchlist_id": item["id"],
        "saved_watchlist_name": item["name"],
        "automation": "server_scheduled_snapshot",
        "input_count": len(item.get("tickers") or []),
        "returned_count": len(filtered),
        "filtered_count": len(filtered),
        "filters": filters,
        "human_review_required_for_routine_publication": False,
    })
    return {
        "metadata": metadata,
        "summary": _summary(filtered),
        "items": filtered,
    }


def _delta_basis(payload: dict) -> dict[str, dict]:
    out = {}
    for item in (payload or {}).get("items") or []:
        ticker = str(item.get("ticker") or "").upper().strip()
        if ticker:
            out[ticker] = {
                "action": item.get("action"),
                "score": _score(item),
            }
    return out


def _signal_delta(current: dict, previous_snapshot: dict | None) -> dict:
    previous_signals = (previous_snapshot or {}).get("signals") or {}
    current_by_ticker = _delta_basis(current)
    previous_by_ticker = _delta_basis(previous_signals)
    current_tickers = set(current_by_ticker)
    previous_tickers = set(previous_by_ticker)
    shared = sorted(current_tickers & previous_tickers)
    changed_actions = []
    changed_scores = []
    for ticker in shared:
        prev = previous_by_ticker[ticker]
        curr = current_by_ticker[ticker]
        if prev.get("action") != curr.get("action"):
            changed_actions.append({"ticker": ticker, "from": prev.get("action"), "to": curr.get("action")})
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


def run_workspace_automation(
    db_path: str,
    pro_db_path: str,
    *,
    max_watchlists: int = 25,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    app = create_app(db_path, secure_cookies=False, open_mode=True)
    client = app.test_client()
    processed: list[dict] = []
    failures: list[dict] = []
    with ProAPIStore(pro_db_path) as pro:
        due = pro.list_due_automated_watchlists(max_items=max_watchlists, now=now)
        for item in due:
            try:
                signals = _signals_for_watchlist(client, item)
                result = {
                    "key_id": item["key_id"],
                    "watchlist_id": item["id"],
                    "name": item["name"],
                    "frequency": item["automation_frequency"],
                    "signals": len(signals.get("items") or []),
                    "dry_run": dry_run,
                }
                if not dry_run:
                    previous_rows = pro.list_signal_snapshots(
                        item["key_id"], item["id"], limit=1, include_signals=True,
                    )
                    previous = previous_rows[0] if previous_rows else None
                    snapshot = pro.create_signal_snapshot(
                        item["key_id"], item["id"], signals, max_snapshots=100,
                    )
                    alerts = pro.upsert_workspace_alerts(
                        item["key_id"], item["id"], snapshot["id"], signals,
                    )
                    delta = _signal_delta(signals, previous)
                    pro.record_workspace_activity(
                        item["key_id"],
                        "signals.snapshot.automated",
                        "watchlist",
                        item["id"],
                        f"Automated signal snapshot: {item['name']}",
                        detail={
                            "snapshot_id": snapshot["id"],
                            "signal_count": len(snapshot["tickers"]),
                            "frequency": item["automation_frequency"],
                            "alerts": alerts,
                            "delta": {
                                "added": len(delta["added_tickers"]),
                                "removed": len(delta["removed_tickers"]),
                                "changed_actions": len(delta["changed_actions"]),
                                "changed_scores": len(delta["changed_scores"]),
                            },
                        },
                    )
                    result.update({
                        "snapshot_id": snapshot["id"],
                        "alerts": alerts,
                        "delta": delta,
                    })
                processed.append(result)
            except Exception as exc:  # noqa: BLE001 - runner must report and continue.
                failures.append({
                    "key_id": item.get("key_id"),
                    "watchlist_id": item.get("id"),
                    "name": item.get("name"),
                    "error": str(exc),
                })
                if not dry_run:
                    pro.record_workspace_activity(
                        item["key_id"],
                        "signals.snapshot.automation_failed",
                        "watchlist",
                        item["id"],
                        f"Automated snapshot failed: {item['name']}",
                        detail={"error": str(exc)[:500]},
                    )
    return {
        "ok": not failures,
        "dry_run": dry_run,
        "due": len(processed) + len(failures),
        "processed": processed,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded Pro workspace alert snapshots")
    parser.add_argument("--db", default=os.environ.get("SMARTMONEY_DB", "/var/lib/13flow/13flow.db"))
    parser.add_argument("--pro-db", default=os.environ.get("SMARTMONEY_PRO_DB", "/var/lib/13flow-pro/13flow-pro.db"))
    parser.add_argument("--max-watchlists", type=int, default=int(os.environ.get("SMARTMONEY_WORKSPACE_AUTOMATION_MAX", "25")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = run_workspace_automation(
        args.db,
        args.pro_db,
        max_watchlists=args.max_watchlists,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
