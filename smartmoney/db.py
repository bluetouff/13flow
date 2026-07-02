"""
Persistence layer (SQLite).

Turns isolated 13F snapshots into a queryable time series. SQLite keeps us
dependency-free for now; the SQL is kept portable so a Postgres swap later is
mechanical (window functions + a view, both supported there too).

Design notes:
  - A stored FILING is a portfolio snapshot, keyed by its accession number.
  - Amendments (13F-HR/A) share a (cik, report_date) with the original. The
    `latest_filings` VIEW resolves each quarter to the latest complete-enough
    accession, so true restatements supersede originals while tiny partial
    amendments do not replace a full portfolio snapshot.
  - CUSIP is the reliable grouping key (that's its job); ticker is carried along as
    a display label via MAX(), so funds enriched at different times don't fragment.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .edgar import Filing
from .portfolio import Portfolio, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    cik      TEXT PRIMARY KEY,
    label    TEXT NOT NULL,
    manager  TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    accession    TEXT PRIMARY KEY,
    cik          TEXT NOT NULL REFERENCES funds(cik),
    form         TEXT NOT NULL,
    filing_date  TEXT,
    report_date  TEXT NOT NULL,
    total_value  REAL,
    n_positions  INTEGER,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_filings_cik_period ON filings(cik, report_date);

CREATE TABLE IF NOT EXISTS holdings (
    accession      TEXT NOT NULL REFERENCES filings(accession) ON DELETE CASCADE,
    cusip          TEXT NOT NULL,
    put_call       TEXT NOT NULL DEFAULT '',
    issuer         TEXT,
    title_of_class TEXT,
    ticker         TEXT,
    figi_name      TEXT,
    ticker_source  TEXT,
    ticker_confidence REAL,
    value_usd      REAL NOT NULL,
    shares         REAL NOT NULL,
    weight         REAL NOT NULL,
    PRIMARY KEY (accession, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS ix_holdings_cusip  ON holdings(cusip);
CREATE INDEX IF NOT EXISTS ix_holdings_ticker ON holdings(ticker);

-- Alert subscriptions: who watches which fund, on which channel.
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    cik         TEXT NOT NULL,
    channel     TEXT NOT NULL,             -- console | webhook | email
    target      TEXT NOT NULL DEFAULT '',  -- url / email address (empty for console)
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT,
    UNIQUE(user_id, cik, channel, target)
);
CREATE INDEX IF NOT EXISTS ix_subs_cik ON subscriptions(cik, active);

-- Delivery log = idempotency boundary. One row per (subscription, accession).
CREATE TABLE IF NOT EXISTS deliveries (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    accession       TEXT NOT NULL,
    status          TEXT NOT NULL,         -- sent | primed | failed
    error           TEXT,
    delivered_at    TEXT,
    PRIMARY KEY (subscription_id, accession)
);

-- One row per (cik, report_date) pointing at the latest complete-enough accession.
-- Some 13F-HR/A filings are partial corrections with only a handful of holdings;
-- prefer the newest filing whose position count is at least half of the largest
-- snapshot for that quarter, falling back to the filing itself when it is alone.
DROP VIEW IF EXISTS latest_filings;
CREATE VIEW latest_filings AS
SELECT cik, report_date, accession FROM (
    SELECT cik, report_date, accession,
           ROW_NUMBER() OVER (
               PARTITION BY cik, report_date
               ORDER BY
                   CASE WHEN COALESCE(n_positions, 0) >= max_positions * 0.5
                        THEN 1 ELSE 0 END DESC,
                   filing_date DESC,
                   accession DESC
           ) AS rn
    FROM (
        SELECT f.*,
               MAX(COALESCE(n_positions, 0)) OVER (
                   PARTITION BY cik, report_date
               ) AS max_positions
        FROM filings f
    )
) WHERE rn = 1;
"""


class Store:
    def __init__(self, path: str = "smartmoney.db", read_only: bool = False):
        self.read_only = read_only
        if read_only:
            # Open-mode web workers connect read-only: the process physically cannot
            # write the database. No schema creation/migration (the file is a snapshot
            # produced offline by the ingest CLI; checkpoint its WAL after ingest).
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(holdings)")}
        for col, decl in (("ticker_source", "TEXT"), ("ticker_confidence", "REAL")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE holdings ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- write -------------------------------------------------------------
    def upsert_fund(self, cik: str, label: str, manager: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO funds(cik, label, manager) VALUES (?,?,?)
               ON CONFLICT(cik) DO UPDATE SET label=excluded.label,
                                              manager=COALESCE(excluded.manager, funds.manager)""",
            (cik.zfill(10), label, manager),
        )
        self.conn.commit()

    def has_filing(self, accession: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM filings WHERE accession=?", (accession,))
        return cur.fetchone() is not None

    def stored_accessions(self, cik: str) -> set[str]:
        cur = self.conn.execute("SELECT accession FROM filings WHERE cik=?", (cik.zfill(10),))
        return {r["accession"] for r in cur.fetchall()}

    def save_portfolio(self, pf: Portfolio, filing: Filing, manager: Optional[str] = None) -> None:
        cik = filing.cik.zfill(10)
        self.upsert_fund(cik, pf.fund_label, manager)
        with self.conn:  # transaction
            self.conn.execute(
                """INSERT INTO filings(accession, cik, form, filing_date, report_date,
                                       total_value, n_positions, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(accession) DO UPDATE SET
                       form=excluded.form, filing_date=excluded.filing_date,
                       report_date=excluded.report_date, total_value=excluded.total_value,
                       n_positions=excluded.n_positions, fetched_at=excluded.fetched_at""",
                (
                    filing.accession, cik, filing.form, filing.filing_date,
                    filing.report_date, pf.total_value, len(pf.positions),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            # Replace holdings for this accession (idempotent re-save).
            self.conn.execute("DELETE FROM holdings WHERE accession=?", (filing.accession,))
            self.conn.executemany(
                """INSERT INTO holdings(accession, cusip, put_call, issuer, title_of_class,
                                        ticker, figi_name, ticker_source, ticker_confidence,
                                        value_usd, shares, weight)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (filing.accession, p.cusip, p.put_call, p.issuer, p.title_of_class,
                     p.ticker, p.figi_name, p.ticker_source, p.ticker_confidence,
                     p.value_usd, p.shares, p.weight)
                    for p in pf.positions.values()
                ],
            )

    # --- read --------------------------------------------------------------
    def _fund_label(self, cik: str) -> str:
        r = self.conn.execute("SELECT label FROM funds WHERE cik=?", (cik,)).fetchone()
        return r["label"] if r else cik

    def quarters(self, cik: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT report_date FROM filings WHERE cik=? ORDER BY report_date", (cik.zfill(10),)
        )
        return [r["report_date"] for r in cur.fetchall()]

    def previous_quarter(self, cik: str, report_date: str) -> Optional[str]:
        r = self.conn.execute(
            """SELECT MAX(report_date) AS prev FROM filings
               WHERE cik=? AND report_date < ?""",
            (cik.zfill(10), report_date),
        ).fetchone()
        return r["prev"] if r and r["prev"] else None

    def _accession_for(self, cik: str, report_date: Optional[str]) -> Optional[sqlite3.Row]:
        if report_date is None:
            return self.conn.execute(
                """SELECT lf.accession, lf.report_date FROM latest_filings lf
                   JOIN filings f ON f.accession=lf.accession
                   WHERE lf.cik=? ORDER BY lf.report_date DESC LIMIT 1""",
                (cik.zfill(10),),
            ).fetchone()
        return self.conn.execute(
            "SELECT accession, report_date FROM latest_filings WHERE cik=? AND report_date=?",
            (cik.zfill(10), report_date),
        ).fetchone()

    def load_portfolio(self, cik: str, report_date: Optional[str] = None) -> Optional[Portfolio]:
        cik = cik.zfill(10)
        row = self._accession_for(cik, report_date)
        if row is None:
            return None
        accession = row["accession"]
        finfo = self.conn.execute(
            "SELECT form, report_date FROM filings WHERE accession=?", (accession,)
        ).fetchone()
        pf = Portfolio(cik=cik, fund_label=self._fund_label(cik),
                       report_date=finfo["report_date"], form=finfo["form"])
        for h in self.conn.execute("SELECT * FROM holdings WHERE accession=?", (accession,)):
            pos = Position(
                cusip=h["cusip"], issuer=h["issuer"], title_of_class=h["title_of_class"],
                put_call=h["put_call"], value_usd=h["value_usd"], shares=h["shares"],
                weight=h["weight"], ticker=h["ticker"], figi_name=h["figi_name"],
                ticker_source=h["ticker_source"], ticker_confidence=h["ticker_confidence"],
            )
            pf.positions[pos.key] = pos
        return pf

    def latest_portfolios(self, cik: str, n: int = 2) -> list[Portfolio]:
        qs = self.quarters(cik)[-n:][::-1]  # newest first
        out = [self.load_portfolio(cik, q) for q in qs]
        return [p for p in out if p is not None]

    # --- SQL analytics -----------------------------------------------------
    def consensus_holdings(self, report_date: str, min_funds: int = 3) -> list[dict]:
        """Stocks held by >= min_funds funds at a given quarter-end (long stock only)."""
        cur = self.conn.execute(
            """
            SELECT h.cusip                         AS cusip,
                   MAX(h.ticker)                   AS ticker,
                   MAX(h.issuer)                   AS issuer,
                   COUNT(DISTINCT lf.cik)          AS n_funds,
                   SUM(h.value_usd)                AS total_value,
                   GROUP_CONCAT(DISTINCT fn.label) AS funds
            FROM latest_filings lf
            JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
            JOIN funds fn   ON fn.cik = lf.cik
            WHERE lf.report_date = ?
            GROUP BY h.cusip
            HAVING n_funds >= ?
            ORDER BY n_funds DESC, total_value DESC
            """,
            (report_date, min_funds),
        )
        return [dict(r) for r in cur.fetchall()]

    def conviction_timeline(self, cik: str, cusip: str) -> list[dict]:
        """One position's shares / value / weight across all stored quarters."""
        cur = self.conn.execute(
            """
            SELECT lf.report_date AS report_date, h.shares AS shares,
                   h.value_usd AS value_usd, h.weight AS weight
            FROM latest_filings lf
            JOIN holdings h ON h.accession = lf.accession
            WHERE lf.cik = ? AND h.cusip = ? AND h.put_call = ''
            ORDER BY lf.report_date
            """,
            (cik.zfill(10), cusip.upper()),
        )
        return [dict(r) for r in cur.fetchall()]

    def holders(self, cusip: str, report_date: str) -> list[dict]:
        """Which funds held a CUSIP at a quarter-end, and how much."""
        cur = self.conn.execute(
            """
            SELECT fn.label AS fund, h.value_usd AS value_usd,
                   h.shares AS shares, h.weight AS weight
            FROM latest_filings lf
            JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
            JOIN funds fn   ON fn.cik = lf.cik
            WHERE lf.report_date = ? AND h.cusip = ?
            ORDER BY h.value_usd DESC
            """,
            (report_date, cusip.upper()),
        )
        return [dict(r) for r in cur.fetchall()]

    # --- resolution coverage / long tail ----------------------------------
    def coverage(self, report_date: Optional[str] = None) -> dict:
        """Ticker-resolution coverage by fund + overall (long stock only)."""
        where = "WHERE lf.report_date = ?" if report_date else ""
        args = (report_date,) if report_date else ()
        cur = self.conn.execute(
            f"""
            SELECT fn.label AS fund, lf.report_date AS report_date,
                   COUNT(*) AS n,
                   SUM(CASE WHEN h.ticker IS NOT NULL AND h.ticker<>'' THEN 1 ELSE 0 END) AS n_res,
                   SUM(h.value_usd) AS val,
                   SUM(CASE WHEN h.ticker IS NOT NULL AND h.ticker<>'' THEN h.value_usd ELSE 0 END) AS val_res
            FROM latest_filings lf
            JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
            JOIN funds fn   ON fn.cik = lf.cik
            {where}
            GROUP BY lf.cik
            ORDER BY (val - val_res) DESC
            """,
            args,
        )
        per_fund = [dict(r) for r in cur.fetchall()]
        tot = sum(r["val"] or 0 for r in per_fund) or 1.0
        tot_res = sum(r["val_res"] or 0 for r in per_fund)
        for r in per_fund:
            r["value_share"] = (r["val_res"] / r["val"]) if r["val"] else None
        return {"overall_value_share": tot_res / tot, "value_unresolved": tot - tot_res,
                "per_fund": per_fund}

    def unresolved_holdings(self, report_date: Optional[str] = None) -> list[dict]:
        """The tail: unresolved CUSIPs aggregated across funds, biggest dollar first."""
        where = "AND lf.report_date = ?" if report_date else ""
        args = (report_date,) if report_date else ()
        cur = self.conn.execute(
            f"""
            SELECT h.cusip AS cusip, MAX(h.issuer) AS issuer,
                   SUM(h.value_usd) AS value, COUNT(DISTINCT lf.cik) AS n_funds
            FROM latest_filings lf
            JOIN holdings h ON h.accession = lf.accession AND h.put_call = ''
            WHERE (h.ticker IS NULL OR h.ticker = '') {where}
            GROUP BY h.cusip
            ORDER BY value DESC
            """,
            args,
        )
        return [dict(r) for r in cur.fetchall()]

    def apply_resolution(self, cusip: str, ticker: str, name: Optional[str],
                        source: str, confidence: float) -> int:
        """Back-fill a newly resolved CUSIP onto all currently-unresolved holdings rows."""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE holdings
                   SET ticker=?, figi_name=COALESCE(?, figi_name),
                       ticker_source=?, ticker_confidence=?
                   WHERE cusip=? AND (ticker IS NULL OR ticker='')""",
                (ticker, name, source, confidence, cusip.upper()),
            )
            return cur.rowcount

    def fund_value_timeline(self, cik: str) -> list[dict]:
        cur = self.conn.execute(
            """SELECT lf.report_date AS report_date, f.total_value AS total_value,
                      f.n_positions AS n_positions
               FROM latest_filings lf JOIN filings f ON f.accession = lf.accession
               WHERE lf.cik = ? ORDER BY lf.report_date""",
            (cik.zfill(10),),
        )
        return [dict(r) for r in cur.fetchall()]

    # --- filing lookups (for alerting) ------------------------------------
    def get_filing(self, accession: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM filings WHERE accession=?", (accession,)).fetchone()
        return dict(r) if r else None

    def latest_filing_row(self, cik: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT * FROM filings WHERE cik=? ORDER BY filing_date DESC, accession DESC LIMIT 1",
            (cik.zfill(10),),
        ).fetchone()
        return dict(r) if r else None

    def fund_row(self, cik: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM funds WHERE cik=?", (cik.zfill(10),)).fetchone()
        return dict(r) if r else None

    # --- subscriptions -----------------------------------------------------
    def add_subscription(self, user_id: str, cik: str, channel: str, target: str = "") -> int:
        cik = cik.zfill(10)
        with self.conn:
            self.conn.execute(
                """INSERT INTO subscriptions(user_id, cik, channel, target, active, created_at)
                   VALUES (?,?,?,?,1,?)
                   ON CONFLICT(user_id, cik, channel, target)
                   DO UPDATE SET active=1""",
                (user_id, cik, channel, target,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        r = self.conn.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND cik=? AND channel=? AND target=?",
            (user_id, cik, channel, target),
        ).fetchone()
        return r["id"]

    def deactivate_subscription(self, sub_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,))

    def active_subscriptions(self, cik: Optional[str] = None) -> list[dict]:
        if cik is None:
            cur = self.conn.execute("SELECT * FROM subscriptions WHERE active=1")
        else:
            cur = self.conn.execute(
                "SELECT * FROM subscriptions WHERE active=1 AND cik=?", (cik.zfill(10),)
            )
        return [dict(r) for r in cur.fetchall()]

    def subscribed_ciks(self) -> list[str]:
        cur = self.conn.execute("SELECT DISTINCT cik FROM subscriptions WHERE active=1")
        return [r["cik"] for r in cur.fetchall()]

    # --- delivery log (idempotency) ---------------------------------------
    def was_delivered(self, sub_id: int, accession: str) -> bool:
        r = self.conn.execute(
            "SELECT status FROM deliveries WHERE subscription_id=? AND accession=?",
            (sub_id, accession),
        ).fetchone()
        # 'failed' rows are allowed to retry; sent/primed are terminal.
        return r is not None and r["status"] in ("sent", "primed")

    def record_delivery(self, sub_id: int, accession: str, status: str,
                        error: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO deliveries(subscription_id, accession, status, error, delivered_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(subscription_id, accession)
                   DO UPDATE SET status=excluded.status, error=excluded.error,
                                 delivered_at=excluded.delivered_at""",
                (sub_id, accession, status, error,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
