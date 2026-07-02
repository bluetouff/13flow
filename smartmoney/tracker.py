"""
High-level tracker + freemium gating.

The freemium rule lives in one place so it's trivial to move server-side later:
free users watch up to FREE_TIER_FUND_LIMIT funds, no alerts; paid unlocks the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diff import DiffReport, diff_portfolios
from .db import Store
from .edgar import EdgarClient, Filing
from .figi import OpenFigiClient, TickerCache, enrich_portfolio
from .resolver import CusipResolver, resolve_portfolio
from .parser import parse_info_table
from .portfolio import Portfolio, build_portfolio
from .registry import Fund

FREE_TIER_FUND_LIMIT = 3


class EntitlementError(Exception):
    pass


@dataclass
class Tier:
    name: str               # "free" | "paid"
    watched: list[str]      # fund labels the user follows

    @property
    def alerts_enabled(self) -> bool:
        return self.name == "paid"

    def assert_can_watch(self, label: str) -> None:
        if self.name == "paid":
            return
        if label in self.watched:
            return
        if len(self.watched) >= FREE_TIER_FUND_LIMIT:
            raise EntitlementError(
                f"Free tier follows {FREE_TIER_FUND_LIMIT} funds. "
                f"Upgrade to add '{label}'."
            )


class Tracker:
    def __init__(
        self,
        client: EdgarClient,
        figi: OpenFigiClient | None = None,
        cache: TickerCache | None = None,
        resolver: CusipResolver | None = None,
    ):
        self.client = client
        self.figi = figi
        self.cache = cache
        self.resolver = resolver

    def cik_for(self, fund: Fund) -> str:
        cik = fund.cik
        if cik:
            return cik.zfill(10)
        resolved = self.client.resolve_cik(fund.search_name)
        if not resolved:
            raise LookupError(f"Could not resolve CIK for '{fund.label}'")
        return resolved

    def portfolio_for_filing(self, fund: Fund, filing: Filing) -> Portfolio:
        xml = self.client.fetch_info_table_xml(filing)
        raw = parse_info_table(xml)
        pf = build_portfolio(
            cik=filing.cik,
            fund_label=fund.label,
            report_date=filing.report_date,
            form=filing.form,
            raw=raw,
            filing_date=filing.filing_date,
        )
        if self.resolver is not None:
            resolve_portfolio(pf, self.resolver)
        elif self.figi is not None:
            enrich_portfolio(pf, self.figi, self.cache)
        return pf

    def latest_filings(self, fund: Fund, limit: int = 2) -> list[Filing]:
        cik = self.cik_for(fund)
        # Skip amendments for the headline diff; they restate, not re-trade.
        filings = self.client.list_13f_filings(cik, include_amendments=False)
        return filings[:limit]

    def latest_diff(self, fund: Fund) -> DiffReport | None:
        """Diff the two most recent 13F-HR quarters. None if <2 quarters exist."""
        filings = self.latest_filings(fund, limit=2)
        if len(filings) < 2:
            return None
        curr = self.portfolio_for_filing(fund, filings[0])
        prev = self.portfolio_for_filing(fund, filings[1])
        return diff_portfolios(prev, curr)

    def sync_fund(self, store: Store, fund: Fund, max_quarters: int | None = None) -> int:
        """
        Backfill a fund into the store. Only filings not already persisted are
        fetched + parsed (and enriched, if a FIGI client is attached), so re-runs
        are cheap and pick up only the newest quarter each time. Returns #saved.
        """
        cik = self.cik_for(fund)
        filings = self.client.list_13f_filings(cik, include_amendments=True)
        if max_quarters is not None:
            filings = filings[:max_quarters]
        already = store.stored_accessions(cik)
        saved = 0
        for filing in filings:
            if filing.accession in already:
                continue
            try:
                pf = self.portfolio_for_filing(fund, filing)
            except FileNotFoundError:
                continue  # 13F-NT / confidential filings with no info table
            store.save_portfolio(pf, filing, manager=fund.manager)
            saved += 1
        return saved
