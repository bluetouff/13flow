"""
Thin EDGAR client.

SEC rules we MUST respect (or get 403'd / IP-banned):
  - Every request carries a descriptive User-Agent with a contact email.
  - Current ceiling is 10 requests/second. We default much lower and let operators tune
    SMARTMONEY_EDGAR_RATE_PER_SEC for repairs/backfills.
  - No "unclassified bots". A real UA string is the price of admission.

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

DATA_HOST = "https://data.sec.gov"
WWW_HOST = "https://www.sec.gov"


class RateLimiter:
    """Token-ish limiter: never exceed `rate` calls/sec, process-wide."""

    def __init__(self, rate_per_sec: float = 8.0):
        self._min_interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._min_interval - (now - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


@dataclass
class Filing:
    cik: str
    accession: str          # e.g. 0000950123-24-000123
    form: str               # 13F-HR, 13F-HR/A, ...
    filing_date: str        # YYYY-MM-DD (date filed)
    report_date: str        # YYYY-MM-DD (period of report = quarter end)
    primary_doc: str        # primary_doc.xml (cover page), not the holdings table

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


class EdgarClient:
    def __init__(self, user_agent: str, rate_per_sec: float | None = None, timeout: int = 30):
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "EDGAR requires a User-Agent containing a contact email, "
                "e.g. 'SmartMoney/1.0 you@example.com'. Requests without it return 403."
            )
        self._ua = user_agent
        if rate_per_sec is None:
            rate_per_sec = float(os.environ.get("SMARTMONEY_EDGAR_RATE_PER_SEC", "2.0"))
        self._limiter = RateLimiter(rate_per_sec)
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        )

    # --- low level ---------------------------------------------------------
    def _get(self, url: str) -> requests.Response:
        self._limiter.wait()
        # data.sec.gov wants a Host header; requests sets it automatically.
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp

    def get_json(self, url: str) -> dict:
        return self._get(url).json()

    def get_text(self, url: str) -> str:
        return self._get(url).text

    # --- CIK resolution ----------------------------------------------------
    def resolve_cik(self, name_or_cik: str) -> Optional[str]:
        """
        Resolve a fund/company to a 10-digit zero-padded CIK.

        If you pass digits, they're just normalized. Otherwise we hit the
        EDGAR company browse endpoint (ATOM) and take the first 13F filer match.
        For superinvestors, prefer seeding a known-good CIK in registry.py —
        name search can be ambiguous (multiple "Capital Management" entities).
        """
        s = name_or_cik.strip()
        if s.isdigit():
            return s.zfill(10)

        url = (
            f"{WWW_HOST}/cgi-bin/browse-edgar?action=getcompany"
            f"&company={requests.utils.quote(s)}&type=13F&dateb=&owner=include"
            f"&count=10&output=atom"
        )
        try:
            atom = self.get_text(url)
        except requests.HTTPError:
            return None
        # Cheap ATOM scrape: CIK appears as <cik>NNN</cik> or in the company-info block.
        import re

        m = re.search(r"<cik>\s*(\d+)\s*</cik>", atom, re.IGNORECASE)
        if m:
            return m.group(1).zfill(10)
        m = re.search(r"CIK=(\d+)", atom)
        return m.group(1).zfill(10) if m else None

    def entity_name(self, cik: str) -> str:
        """Return the registered entity name for a CIK (use to sanity-check a CIK)."""
        cik = cik.zfill(10)
        data = self.get_json(f"{DATA_HOST}/submissions/CIK{cik}.json")
        return data.get("name", "")

    # --- filings -----------------------------------------------------------
    def list_13f_filings(self, cik: str, include_amendments: bool = True) -> list[Filing]:
        """
        Return 13F-HR (and optionally /A amendments) filings, newest first.

        Note: the submissions feed returns the ~1000 most recent filings inline.
        Deeper history lives in data['filings']['files'][*]['name'] shards; we
        page into those only if needed. For active 13F filers the recent block
        almost always covers years of quarters.
        """
        cik = cik.zfill(10)
        data = self.get_json(f"{DATA_HOST}/submissions/CIK{cik}.json")
        out: list[Filing] = []

        def absorb(block: dict) -> None:
            forms = block.get("form", [])
            accns = block.get("accessionNumber", [])
            fdates = block.get("filingDate", [])
            rdates = block.get("reportDate", [])
            pdocs = block.get("primaryDocument", [])
            for i, form in enumerate(forms):
                if not form.startswith("13F-HR"):
                    continue
                if form.endswith("/A") and not include_amendments:
                    continue
                out.append(
                    Filing(
                        cik=cik,
                        accession=accns[i],
                        form=form,
                        filing_date=fdates[i] if i < len(fdates) else "",
                        report_date=rdates[i] if i < len(rdates) else "",
                        primary_doc=pdocs[i] if i < len(pdocs) else "",
                    )
                )

        absorb(data.get("filings", {}).get("recent", {}))

        # Page into older shards only when the recent block looks exhausted.
        for shard in data.get("filings", {}).get("files", []):
            name = shard.get("name")
            if not name:
                continue
            absorb(self.get_json(f"{DATA_HOST}/submissions/{name}"))

        out.sort(key=lambda f: f.filing_date, reverse=True)
        return out

    def fetch_info_table_xml(self, filing: Filing) -> str:
        """
        Find and download the information-table XML for a 13F filing.

        The cover page is primary_doc.xml; the holdings live in a *separate* XML
        whose name varies by filing agent (form13fInfoTable.xml, infotable.xml,
        <random>.xml ...). We read the filing's index.json, then pick the XML that
        actually contains an <infoTable> / <informationTable> root.
        """
        base = f"{WWW_HOST}/Archives/edgar/data/{int(filing.cik)}/{filing.accession_nodash}"
        index = self.get_json(f"{base}/index.json")
        items = index.get("directory", {}).get("item", [])
        xml_files = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]

        # De-prioritize the cover page; it's never the holdings table.
        candidates = [f for f in xml_files if "primary_doc" not in f.lower()] or xml_files
        for fname in candidates:
            text = self.get_text(f"{base}/{fname}")
            if "infoTable" in text or "informationTable" in text:
                return text
        raise FileNotFoundError(
            f"No information table found in {filing.accession} (form {filing.form}). "
            "Some 13F-NT / confidential-treatment filings legitimately omit it."
        )
