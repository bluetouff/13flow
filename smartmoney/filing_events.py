"""Observed 13F revisions, recorded by the existing ingest writer only.

The journal starts when installed. A baseline is never a backdated observation,
and a filing date is never presented as a trade date.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS filing_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    cik TEXT NOT NULL,
    report_date TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_filing_events_quarter
    ON filing_events(cik, report_date, sequence DESC);
"""
KINDS = ("baseline", "filing_observed", "amendment_observed", "data_revised")


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _snapshot(conn, cik, report_date):
    row = conn.execute(
        """SELECT p.*, f.total_value AS reported_value, fn.label
           FROM latest_filings lf JOIN portfolio_filings p ON p.accession=lf.accession
           JOIN filings f ON f.accession=p.accession JOIN funds fn ON fn.cik=p.cik
           WHERE lf.cik=? AND lf.report_date=?""", (cik, report_date),
    ).fetchone()
    if row is None:
        return None
    accessions = sorted(set(row["source_accessions"].split(",") + [row["accession"]]))
    # Never construct source links from arbitrary strings stored in the database.
    if not re.fullmatch(r"[0-9]{10}", cik) or int(cik) == 0 or any(
        not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", a) for a in accessions
    ):
        return None
    holdings = conn.execute(
        """SELECT cusip,put_call,ticker,shares,value_usd FROM portfolio_holdings
           WHERE accession=? ORDER BY cusip,put_call""", (row["accession"],),
    ).fetchall()
    complete = row["composition_status"] == "complete"
    state = {
        "accession": row["accession"], "form": row["form"],
        "published_on": row["filing_date"], "amendment_type": row["amendment_type"],
        "amendment_number": row["amendment_number"],
        "composition_status": row["composition_status"],
        "reported_value_usd": row["reported_value"],
        "portfolio_value_usd": row["total_value"] if complete else None,
        "positions": row["n_positions"] if complete else None,
        "holdings_hash": hashlib.sha256(_json([tuple(r) for r in holdings]).encode()).hexdigest(),
        "sources": [{"accession": a, "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{a.replace('-', '')}/"}
                    for a in accessions],
    }
    return {"label": row["label"], "state": state}


def capture(conn, cik, report_date, *, baseline=False):
    """Append a changed state inside the caller's database transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    current = _snapshot(conn, cik, report_date)
    if current is None:
        return
    state = current["state"]
    digest = hashlib.sha256(_json(state).encode()).hexdigest()
    previous = conn.execute(
        """SELECT state_hash,payload_json FROM filing_events WHERE cik=? AND report_date=?
           ORDER BY sequence DESC LIMIT 1""", (cik, report_date),
    ).fetchone()
    if previous and previous["state_hash"] == digest:
        return
    before = json.loads(previous["payload_json"])["after"] if previous else None
    kind = "data_revised"
    if before is None and baseline:
        kind = "baseline"
    elif before is None or before["accession"] != state["accession"]:
        kind = "amendment_observed" if state["form"] == "13F-HR/A" else "filing_observed"
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    event = {
        "id": uuid.uuid4().hex, "kind": kind, "recorded_at": recorded_at,
        "fund": {"cik": cik, "label": current["label"]}, "report_date": report_date,
        "before": before, "after": state,
        "changed_fields": sorted(k for k in state if before is not None and before.get(k) != state[k]),
        "interpretation": "initial_observation" if kind == "baseline" else "observed_revision",
    }
    conn.execute(
        """INSERT INTO filing_events(event_id,cik,report_date,state_hash,recorded_at,payload_json)
           VALUES (?,?,?,?,?,?)""", (event["id"], cik, report_date, digest, recorded_at, _json(event)),
    )


def capture_baselines(conn, ciks):
    """Observe each tracked fund's latest two quarters without refetching EDGAR."""
    with conn:
        for cik in sorted(ciks):
            quarters = conn.execute(
                "SELECT report_date FROM latest_filings WHERE cik=? ORDER BY report_date DESC LIMIT 2",
                (cik,),
            ).fetchall()
            for quarter in reversed(quarters):
                capture(conn, cik, quarter["report_date"], baseline=True)


def read(conn, *, after=0, limit=100, cik=None, active_ciks=None):
    """Bounded cursor reads; old production snapshots remain readable before migration."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        return _read(conn, after=after, limit=limit, cik=cik, active_ciks=active_ciks)
    finally:
        if owns_transaction:
            conn.rollback()  # End the consistent read snapshot without writing.


def _read(conn, *, after, limit, cik, active_ciks):
    empty = {"version": 1, "status": "not_initialized", "stream_id": None,
             "started_at": None, "head": 0, "next_cursor": 0, "has_more": False,
             "events": [], "scope": "observed_13f_revisions",
             "limitations": ["Initial states are baselines, not historical observations.",
                             "Publication dates have day precision and are not trade dates.",
                             "Composition completeness is not a fund quality or investment rating.",
                             "Ranking changes and private workspace activity are outside this feed."]}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='filing_events'").fetchone():
        return empty
    first = conn.execute("SELECT event_id,recorded_at FROM filing_events ORDER BY sequence LIMIT 1").fetchone()
    if first is None:
        return {**empty, "status": "empty"}
    head = conn.execute("SELECT MAX(sequence) FROM filing_events").fetchone()[0]
    clauses, params = ["sequence> ?"], [after]
    if active_ciks is not None:
        ciks = sorted(active_ciks)
        clauses.append("cik IN (" + ",".join("?" for _ in ciks) + ")" if ciks else "0=1")
        params.extend(ciks)
    if cik:
        clauses.append("cik=?")
        params.append(cik)
    rows = conn.execute(
        "SELECT sequence,payload_json FROM filing_events WHERE " + " AND ".join(clauses)
        + " ORDER BY sequence LIMIT ?", (*params, limit + 1),
    ).fetchall()
    events = [{**json.loads(r["payload_json"]), "sequence": r["sequence"]} for r in rows[:limit]]
    return {**empty, "status": "ok", "stream_id": first["event_id"],
            "started_at": first["recorded_at"], "head": head,
            "next_cursor": events[-1]["sequence"] if len(rows) > limit else head,
            "has_more": len(rows) > limit, "events": events}
