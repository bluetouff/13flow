"""13F revisions: preserve raw filings and compose only explicit SEC amendments.

SEC Form 13F FAQ 58b/58c: RESTATEMENT replaces; NEW HOLDINGS supplements.
The component map is rebuilt for one fund/quarter inside the ingestion transaction.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS filing_revisions (
    accession TEXT PRIMARY KEY REFERENCES filings(accession) ON DELETE CASCADE,
    amendment_type TEXT,
    amendment_number INTEGER,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_components (
    accession TEXT NOT NULL REFERENCES filings(accession) ON DELETE CASCADE,
    source_accession TEXT NOT NULL REFERENCES filings(accession) ON DELETE CASCADE,
    PRIMARY KEY (accession, source_accession)
);

DROP VIEW IF EXISTS latest_filings;
CREATE VIEW latest_filings AS
SELECT cik, report_date, accession FROM (
    SELECT cik, report_date, accession,
           ROW_NUMBER() OVER (
               PARTITION BY cik, report_date
               ORDER BY filing_date DESC, accession DESC
           ) AS rn
    FROM filings
) WHERE rn = 1;

DROP VIEW IF EXISTS composed_holdings;
CREATE VIEW composed_holdings AS
SELECT c.accession, h.cusip, h.put_call, MAX(h.issuer) AS issuer, MAX(h.title_of_class) AS title_of_class,
       CASE WHEN COUNT(DISTINCT h.ticker)=1 THEN MAX(h.ticker) END AS ticker,
       MAX(h.figi_name) AS figi_name,
       CASE WHEN COUNT(DISTINCT h.ticker_source)=1 THEN MAX(h.ticker_source) END AS ticker_source,
       MIN(h.ticker_confidence) AS ticker_confidence, SUM(h.value_usd) AS value_usd, SUM(h.shares) AS shares,
       COALESCE(SUM(h.value_usd) / NULLIF(SUM(SUM(h.value_usd)) OVER (
           PARTITION BY c.accession), 0), 0) AS weight
FROM filing_components c JOIN holdings h ON h.accession=c.source_accession
GROUP BY c.accession, h.cusip, h.put_call;

DROP VIEW IF EXISTS portfolio_holdings;
CREATE VIEW portfolio_holdings AS
SELECT h.accession, h.cusip, h.put_call, h.issuer, h.title_of_class, h.ticker,
       h.figi_name, h.ticker_source, h.ticker_confidence, h.value_usd, h.shares, h.weight
FROM holdings h
WHERE NOT EXISTS (SELECT 1 FROM filing_components c WHERE c.accession=h.accession)
UNION ALL SELECT * FROM composed_holdings;

DROP VIEW IF EXISTS portfolio_filings;
CREATE VIEW portfolio_filings AS
SELECT f.accession, f.cik, f.form, f.filing_date, f.report_date,
       COALESCE(p.total_value, f.total_value) AS total_value,
       COALESCE(p.n_positions, f.n_positions) AS n_positions, f.fetched_at,
       r.amendment_type, r.amendment_number,
       COALESCE(r.status, CASE WHEN f.form='13F-HR/A' THEN 'unknown_amendment'
                             ELSE 'complete' END) AS composition_status,
       COALESCE((SELECT GROUP_CONCAT(source_accession) FROM (
           SELECT source_accession FROM filing_components WHERE accession=f.accession
           ORDER BY source_accession)), f.accession) AS source_accessions
FROM filings f LEFT JOIN filing_revisions r ON r.accession=f.accession
LEFT JOIN (
    SELECT accession, SUM(value_usd) AS total_value, COUNT(*) AS n_positions FROM (
        SELECT c.accession, h.cusip, h.put_call, SUM(h.value_usd) AS value_usd
        FROM filing_components c JOIN holdings h ON h.accession=c.source_accession
        GROUP BY c.accession, h.cusip, h.put_call
    ) GROUP BY accession
) p ON p.accession=f.accession;
"""


def rebuild_quarter(conn, cik: str, report_date: str) -> None:
    """Rebuild derived links only; raw holdings and reported totals never change."""
    rows = conn.execute(
        """SELECT f.accession, f.form, r.amendment_type, r.amendment_number
           FROM filings f LEFT JOIN filing_revisions r ON r.accession=f.accession
           WHERE f.cik=? AND f.report_date=? ORDER BY f.filing_date, f.accession""",
        (cik, report_date),
    ).fetchall()
    sources = []
    status = "missing_base"
    previous_number = 0
    for row in rows:
        accession = row["accession"]
        kind = row["amendment_type"]
        number = row["amendment_number"]
        if row["form"] == "13F-HR":
            sources, status, previous_number = [accession], "complete", 0
        elif kind == "RESTATEMENT" and number is not None:
            sources, status, previous_number = [accession], "complete", number
        elif kind == "NEW HOLDINGS" and number is not None:
            if not sources:
                status = "missing_base"
            elif number != previous_number + 1:
                status = "missing_amendment"
            sources = [*sources, accession]
            previous_number = number
        else:
            status = "unknown_amendment"
            sources = [*sources, accession]
        conn.execute(
            "UPDATE filing_revisions SET status=? WHERE accession=?",
            (status, accession),
        )
        conn.execute("DELETE FROM filing_components WHERE accession=?", (accession,))
        # Incomplete chains stay available as raw audit records, never as merged facts.
        if status == "complete" and len(sources) > 1:
            conn.executemany(
                "INSERT INTO filing_components(accession,source_accession) VALUES (?,?)",
                [(accession, source) for source in sources],
            )


def install_legacy_read_views(conn) -> None:
    """Serve an older read-only snapshot without migrating or writing its file."""
    conn.executescript("""
        CREATE TEMP VIEW latest_filings AS
        SELECT cik, report_date, accession FROM (
            SELECT cik, report_date, accession, ROW_NUMBER() OVER (
                PARTITION BY cik, report_date ORDER BY filing_date DESC, accession DESC
            ) AS rn FROM main.filings
        ) WHERE rn=1;
        CREATE TEMP VIEW portfolio_holdings AS SELECT * FROM main.holdings;
        CREATE TEMP VIEW portfolio_filings AS
        SELECT f.*, NULL AS amendment_type, NULL AS amendment_number,
               CASE WHEN f.form='13F-HR/A' THEN 'unknown_amendment'
                    ELSE 'complete' END AS composition_status,
               f.accession AS source_accessions
        FROM main.filings f;
    """)
