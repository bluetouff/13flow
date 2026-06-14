"""
Form 4 — insider transactions, the *fast* half of the smart-money signal.

Why this matters next to 13F:
  - 13F has a 45-day reporting lag and only shows institutions. Form 4 is filed
    within **2 business days** of the trade, by the people who run the company.
  - The differentiating signal is the *confluence* (see crosssignal.py): a name where
    multiple tracked funds are accumulating AND insiders are buying open-market.

This module does two jobs and nothing else:
  1. DISCOVER Form 4 filings for a given *issuer* CIK (the company, not the insider).
  2. PARSE the ownership XML into a typed `Form4` with its transactions.

Design notes / conventions (mirrors edgar.py):
  - Same SEC etiquette: descriptive User-Agent w/ contact email, self-limited to ~8 req/s.
  - XML is parsed with defusedxml (billion-laughs / XXE hardening), namespace-agnostic.
  - Built to run standalone (own session + limiter) OR to ride an existing `EdgarClient`:
    pass `client=` and it reuses that client's session, limiter and headers. One-line wire-in.

EDGAR docs: https://www.sec.gov/info/edgar/ownershipxmlspec-v1-r1.doc
Discovery:  browse-edgar getcompany?type=4 returns Form 4s indexed under the issuer CIK.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import requests

try:  # hardened XML; falls back loudly rather than parsing untrusted XML unsafely
    from defusedxml.ElementTree import fromstring as _xml_fromstring
    _HAVE_DEFUSED = True
except Exception:  # pragma: no cover - defusedxml is a declared dependency
    from xml.etree.ElementTree import fromstring as _xml_fromstring  # noqa: S405
    _HAVE_DEFUSED = False

# Form 4 ownership docs are tiny; cap input up front to stop a hostile/oversized payload.
_MAX_XML_BYTES = 8 * 1024 * 1024


def _safe_xml(data):
    """Parse untrusted EDGAR XML. Uses defusedxml when available; otherwise enforces a
    size cap and refuses any DTD/ENTITY declaration (billion-laughs / XXE) before parsing."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if raw is None or len(raw) == 0:
        raise ValueError("empty XML")
    if len(raw) > _MAX_XML_BYTES:
        raise ValueError("XML exceeds size limit")
    if not _HAVE_DEFUSED:
        head = raw[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
            raise ValueError("XML DTD/ENTITY declarations are not allowed")
    return _xml_fromstring(raw)

WWW_HOST = "https://www.sec.gov"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Transaction codes we care about. Full table in the ownership spec; these are the
# ones with signal. P/S are *open-market* and carry the most information; A/M/F/G are
# compensation / mechanical and are deliberately down-weighted in crosssignal.py.
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"
GRANT_CODES = frozenset({"A", "M", "C", "G", "F", "D", "I", "X"})  # not open-market intent


class _RateLimiter:
    """Process-wide ceiling on request rate. Identical contract to edgar.RateLimiter."""

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


@dataclass(frozen=True)
class Form4Transaction:
    security_title: str
    txn_date: str            # YYYY-MM-DD
    code: str                # P, S, A, M, ...
    acquired_disposed: str   # "A" acquired / "D" disposed
    shares: float
    price_per_share: float
    direct: bool             # D = direct, I = indirect ownership
    shares_owned_after: float

    @property
    def is_open_market_buy(self) -> bool:
        return self.code == OPEN_MARKET_BUY and self.acquired_disposed == "A"

    @property
    def is_open_market_sell(self) -> bool:
        return self.code == OPEN_MARKET_SELL and self.acquired_disposed == "D"

    @property
    def value_usd(self) -> float:
        # transactionTotalValue is often omitted; reconstruct from shares * price.
        return round(self.shares * self.price_per_share, 2)


@dataclass(frozen=True)
class Form4:
    accession: str
    filing_date: str         # YYYY-MM-DD (when it hit EDGAR)
    period_of_report: str    # YYYY-MM-DD (earliest transaction date on the form)
    issuer_cik: str
    issuer_name: str
    issuer_ticker: str
    owner_cik: str
    owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str
    transactions: tuple[Form4Transaction, ...] = ()

    # --- role helpers used by the confluence scorer -----------------------------
    @property
    def is_c_suite(self) -> bool:
        """CEO / CFO / President / Chair — the highest-signal buyers."""
        t = (self.officer_title or "").lower()
        return self.is_officer and any(
            k in t for k in ("chief executive", "ceo", "chief financial", "cfo",
                             "president", "chair")
        )

    @property
    def role_label(self) -> str:
        if self.is_c_suite:
            # surface the actual title, trimmed
            return self.officer_title or "C-Suite"
        if self.is_officer:
            return self.officer_title or "Officer"
        if self.is_director:
            return "Director"
        if self.is_ten_percent_owner:
            return "10% Owner"
        return "Insider"

    @property
    def open_market_buys(self) -> list[Form4Transaction]:
        return [t for t in self.transactions if t.is_open_market_buy]

    @property
    def open_market_sells(self) -> list[Form4Transaction]:
        return [t for t in self.transactions if t.is_open_market_sell]


def _txt(node, tag: str, default: str = "") -> str:
    """Find a descendant tag (namespace-agnostic) and return stripped text."""
    if node is None:
        return default
    for el in node.iter():
        # strip any namespace prefix: '{ns}tag' -> 'tag'
        local = el.tag.rsplit("}", 1)[-1]
        if local == tag and el.text is not None:
            return el.text.strip()
    return default


def _value_of(node, container_tag: str) -> str:
    """Ownership XML wraps most fields as <container><value>X</value></container>."""
    if node is None:
        return ""
    for el in node.iter():
        if el.tag.rsplit("}", 1)[-1] == container_tag:
            return _txt(el, "value", "")
    return ""


def _to_float(s: str) -> float:
    try:
        return float((s or "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def parse_form4(xml_text: str, *, accession: str = "", filing_date: str = "") -> Form4:
    """
    Parse an ownership XML document (Form 4) into a typed `Form4`.

    Namespace-agnostic and defensive: missing optional fields degrade to ""/0.0
    rather than raising, because EDGAR filings vary in completeness.
    """
    root = _safe_xml(xml_text)

    # --- issuer ---------------------------------------------------------------
    issuer_cik = _txt(root, "issuerCik")
    issuer_name = _txt(root, "issuerName")
    issuer_ticker = _txt(root, "issuerTradingSymbol").upper()

    # --- reporting owner + relationship --------------------------------------
    owner_cik = _txt(root, "rptOwnerCik")
    owner_name = _txt(root, "rptOwnerName")

    def _flag(tag: str) -> bool:
        v = _txt(root, tag).strip().lower()
        return v in ("1", "true")

    is_director = _flag("isDirector")
    is_officer = _flag("isOfficer")
    is_ten_pct = _flag("isTenPercentOwner")
    officer_title = _txt(root, "officerTitle")

    period = _txt(root, "periodOfReport")

    # --- non-derivative transactions (Table I) -------------------------------
    txns: list[Form4Transaction] = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "nonDerivativeTransaction":
            continue
        security_title = _value_of(el, "securityTitle")
        txn_date = _value_of(el, "transactionDate")
        code = _value_of(el, "transactionCoding") or _txt(el, "transactionCode")
        # transactionCoding wraps several values; pull the code explicitly
        code = _coding_code(el) or code
        shares = _to_float(_amount(el, "transactionShares"))
        price = _to_float(_amount(el, "transactionPricePerShare"))
        ad = _amount(el, "transactionAcquiredDisposedCode").upper()
        owned_after = _to_float(_value_of(el, "postTransactionAmounts"))
        direct = (_value_of(el, "ownershipNature") or "D").upper().startswith("D")
        txns.append(Form4Transaction(
            security_title=security_title,
            txn_date=txn_date,
            code=(code or "").upper(),
            acquired_disposed=ad or "A",
            shares=shares,
            price_per_share=price,
            direct=direct,
            shares_owned_after=owned_after,
        ))

    return Form4(
        accession=accession,
        filing_date=filing_date or period,
        period_of_report=period,
        issuer_cik=issuer_cik.zfill(10) if issuer_cik else "",
        issuer_name=issuer_name,
        issuer_ticker=issuer_ticker,
        owner_cik=owner_cik,
        owner_name=owner_name,
        is_director=is_director,
        is_officer=is_officer,
        is_ten_percent_owner=is_ten_pct,
        officer_title=officer_title,
        transactions=tuple(txns),
    )


def _coding_code(txn_el) -> str:
    """transactionCoding contains transactionFormType + transactionCode; want the latter."""
    for el in txn_el.iter():
        if el.tag.rsplit("}", 1)[-1] == "transactionCode" and el.text:
            return el.text.strip()
    return ""


def _amount(txn_el, container_tag: str) -> str:
    """transactionAmounts wraps shares/price/AD-code each as <tag><value>..</value></tag>."""
    for el in txn_el.iter():
        if el.tag.rsplit("}", 1)[-1] == container_tag:
            return _txt(el, "value", "")
    return ""


class Form4Client:
    """
    Discovers and downloads Form 4 filings for an *issuer* CIK.

    Standalone:   Form4Client(user_agent="13FLOW/1.0 you@example.com")
    Drop-in:      Form4Client(client=my_edgar_client)   # reuses its session + limiter
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        *,
        client=None,
        rate_per_sec: float = 8.0,
        timeout: int = 30,
    ):
        self._timeout = timeout
        if client is not None:
            # Ride the existing EdgarClient: reuse its session/limiter if exposed.
            self._session = getattr(client, "_session", None) or getattr(client, "session", None) or requests.Session()
            self._limiter = getattr(client, "_limiter", None) or _RateLimiter(rate_per_sec)
        else:
            if not user_agent or "@" not in user_agent:
                raise ValueError(
                    "EDGAR requires a User-Agent containing a contact email, e.g. "
                    "'13FLOW/1.0 you@example.com'. Requests without it return 403."
                )
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": user_agent,
                                          "Accept-Encoding": "gzip, deflate"})
            self._limiter = _RateLimiter(rate_per_sec)

    # -- HTTP ------------------------------------------------------------------
    def _get(self, url: str) -> requests.Response:
        self._limiter.wait()
        r = self._session.get(url, timeout=self._timeout)
        r.raise_for_status()
        return r

    # -- discovery -------------------------------------------------------------
    def list_form4_accessions(
        self,
        issuer_cik: str,
        *,
        since: Optional[date] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return recent Form 4 filings for an issuer as
        [{'accession': '0001...-24-000123', 'filing_date': 'YYYY-MM-DD', 'href': ...}].

        Uses browse-edgar's Atom feed, which indexes ownership forms under the issuer CIK.
        `since` filters client-side by filing date; `limit` caps the feed size.
        """
        cik = str(issuer_cik).lstrip("0") or "0"
        url = (
            f"{WWW_HOST}/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            f"&type=4&dateb=&owner=include&count={min(limit, 100)}&output=atom"
        )
        feed = self._get(url).text
        root = _safe_xml(feed)
        out: list[dict] = []
        for entry in root.iter(f"{ATOM_NS}entry"):
            content = entry.find(f"{ATOM_NS}content")
            acc = _txt(content, "accession-number") or _txt(content, "accession-nunber")
            fdate = _txt(content, "filing-date")
            link = entry.find(f"{ATOM_NS}link")
            href = link.get("href") if link is not None else ""
            if not acc:
                continue
            if since and fdate and _parse_date(fdate) < since:
                continue
            out.append({"accession": acc, "filing_date": fdate, "href": href})
        return out

    def _index_json(self, accession: str, owner_or_issuer_cik: str) -> dict:
        nodash = accession.replace("-", "")
        cik = str(owner_or_issuer_cik).lstrip("0")
        url = f"{WWW_HOST}/Archives/edgar/data/{cik}/{nodash}/index.json"
        return self._get(url).json()

    def fetch_ownership_xml(self, accession: str, cik: str) -> str:
        """
        Locate the ownership XML inside a filing package and return its text.
        Prefers the document whose `type` is '4'; falls back to the first '.xml'
        that is not a rendering stylesheet.
        """
        idx = self._index_json(accession, cik)
        items = idx.get("directory", {}).get("item", [])
        nodash = accession.replace("-", "")
        cik_clean = str(cik).lstrip("0")
        base = f"{WWW_HOST}/Archives/edgar/data/{cik_clean}/{nodash}"

        def _is_ownership(name: str) -> bool:
            n = name.lower()
            return n.endswith(".xml") and not n.endswith((".xsl", "-index.xml"))

        # 1) document explicitly typed "4"
        for it in items:
            if it.get("type") == "4" and _is_ownership(it.get("name", "")):
                return self._get(f"{base}/{it['name']}").text
        # 2) any plausible ownership .xml
        for it in items:
            if _is_ownership(it.get("name", "")):
                return self._get(f"{base}/{it['name']}").text
        raise LookupError(f"No ownership XML found in {accession}")

    def insider_filings(
        self,
        issuer_cik: str,
        *,
        window_days: int = 90,
        max_filings: int = 60,
    ) -> list[Form4]:
        """
        High-level: every Form 4 for an issuer within the trailing `window_days`,
        parsed. This is the unit the confluence engine consumes.
        """
        since = date.today() - timedelta(days=window_days)
        metas = self.list_form4_accessions(issuer_cik, since=since, limit=max_filings)
        out: list[Form4] = []
        for m in metas:
            try:
                xml = self.fetch_ownership_xml(m["accession"], issuer_cik)
                f = parse_form4(xml, accession=m["accession"], filing_date=m["filing_date"])
                out.append(f)
            except Exception:
                # one malformed filing must never sink the batch
                continue
        return out


def _parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _today() -> date:  # indirection so tests can monkeypatch
    return date.today()
