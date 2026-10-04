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
  - Same SEC etiquette: descriptive User-Agent w/ contact email, self-limited well below
    the SEC ceiling by default.
  - XML is parsed with defusedxml (billion-laughs / XXE hardening), namespace-agnostic.
  - Built to run standalone (own session + limiter) OR to ride an existing `EdgarClient`:
    pass `client=` and it reuses that client's session, limiter and headers. One-line wire-in.

EDGAR docs: https://www.sec.gov/info/edgar/ownershipxmlspec-v1-r1.doc
Discovery:  browse-edgar getcompany?type=4 returns Form 4s indexed under the issuer CIK.
"""

from __future__ import annotations

import threading
import time
import os
import re
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime
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
DATA_HOST = "https://data.sec.gov"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Transaction codes we care about. Full table in the ownership spec; these are the
# ones with signal. P/S are *open-market* and carry the most information; A/M/F/G are
# compensation / mechanical and are deliberately down-weighted in crosssignal.py.
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"
GRANT_CODES = frozenset({"A", "M", "C", "G", "F", "D", "I", "X"})  # not open-market intent


class _RateLimiter:
    """Process-wide ceiling on request rate. Identical contract to edgar.RateLimiter."""

    def __init__(self, rate_per_sec: float = 2.0):
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
    reporting_owner_ciks: tuple[str, ...] = ()

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
    if (root.tag.rsplit("}", 1)[-1] != "ownershipDocument"
            or _txt(root, "documentType") not in {"4", "4/A"}):
        raise ValueError("Expected a Form 4 ownership document")

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
        reporting_owner_ciks=tuple(
            _txt(owner, "rptOwnerCik").zfill(10)
            for owner in root
            if owner.tag.rsplit("}", 1)[-1] == "reportingOwner"
            and re.fullmatch(r"[0-9]{1,10}", _txt(owner, "rptOwnerCik"))
        ),
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
        rate_per_sec: float = 2.0,
        timeout: int = 30,
        cache_dir=None,
        progress=None,
    ):
        self._timeout = timeout
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._progress = progress or (lambda message: None)
        self.filings_downloaded = 0
        self.filings_cached = 0
        self.non_issuer_filings = []
        if self._cache_dir is not None:
            if self._cache_dir.is_symlink():
                raise ValueError("Form 4 cache directory must not be a symlink")
            self._cache_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
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
            env_rate = os.environ.get("SMARTMONEY_EDGAR_RATE_PER_SEC")
            self._limiter = _RateLimiter(float(env_rate) if env_rate else rate_per_sec)

    # -- HTTP ------------------------------------------------------------------
    def _get(self, url: str, _max_tries: int = 4) -> requests.Response:
        last_exc = None
        for attempt in range(_max_tries):
            self._limiter.wait()
            r = self._session.get(url, timeout=self._timeout)
            if r.status_code in (429, 503):
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else 2.0 * (2 ** attempt)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError):
                        raise requests.HTTPError("Invalid SEC Retry-After; refresh stopped", response=r)
                last_exc = requests.HTTPError(f"{r.status_code} for {url}", response=r)
                if not 0 <= delay <= 30:
                    # Stop resumably instead of retrying before a long server delay.
                    raise last_exc
                if attempt + 1 < _max_tries:
                    self._progress(f"  SEC HTTP {r.status_code}: retry {attempt + 1}/{_max_tries}, waiting {delay:g}s.")
                    time.sleep(delay)
                continue
            r.raise_for_status()
            return r
        if last_exc:
            raise last_exc
        raise requests.HTTPError(f"request failed after {_max_tries} tries: {url}")

    # -- discovery -------------------------------------------------------------
    def list_form4_accessions(
        self,
        issuer_cik: str,
        *,
        since: Optional[date] = None,
        limit: int = 100,
        strict: bool = False,
    ) -> list[dict]:
        """
        Return recent Form 4 filings for an issuer as
        [{'accession': '0001...-24-000123', 'filing_date': 'YYYY-MM-DD', 'href': ...}].

        Uses the SEC submissions API. `since` filters by filing date; `limit` caps
        the result. Strict mode requires a complete, valid recent-filings block.
        """
        # Modern data.sec.gov submissions API (JSON), not the legacy browse-edgar
        # Atom feed. A company's list can also contain filings in which it is a
        # reporting owner of another issuer. Classify that role from the XML.
        cik10 = str(issuer_cik).lstrip("0").zfill(10)
        data = self._get(f"{DATA_HOST}/submissions/CIK{cik10}.json").json()
        if strict:
            rec = data["filings"]["recent"]
            columns = [rec[key] for key in ("form", "accessionNumber", "filingDate")]
            if (not all(isinstance(column, list) for column in columns)
                    or len({len(column) for column in columns}) != 1):
                raise ValueError("Invalid SEC recent-filings columns")
            if "primaryDocument" in rec and (not isinstance(rec["primaryDocument"], list)
                    or len(rec["primaryDocument"]) != len(rec["form"])):
                raise ValueError("Invalid SEC primary-document column")
        else:
            rec = data.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        accns = rec.get("accessionNumber", [])
        fdates = rec.get("filingDate", [])
        out: list[dict] = []
        for i, form in enumerate(forms):
            if form not in ("4", "4/A"):
                continue
            acc = accns[i] if i < len(accns) else ""
            fdate = fdates[i] if i < len(fdates) else ""
            if strict:
                if not isinstance(acc, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", acc):
                    raise ValueError("Invalid SEC Form 4 accession")
                date.fromisoformat(fdate)
            if not acc:
                continue
            if since and fdate and _parse_date(fdate) < since:
                continue
            meta = {"accession": acc, "filing_date": fdate, "href": ""}
            if "primaryDocument" in rec:
                meta["primary_document"] = rec["primaryDocument"][i]
                if strict:
                    self._ownership_filename(meta["primary_document"])
            out.append(meta)
            if len(out) >= limit:
                break
        return out

    def _index_json(self, accession: str, owner_or_issuer_cik: str) -> dict:
        nodash = accession.replace("-", "")
        cik = str(owner_or_issuer_cik).lstrip("0")
        url = f"{WWW_HOST}/Archives/edgar/data/{cik}/{nodash}/index.json"
        return self._get(url).json()

    @staticmethod
    def _ownership_filename(document: str) -> str:
        # SEC primaryDocument may include its presentation stylesheet directory.
        # Fetch the raw XML, accepting no URLs, traversal, escapes or other paths.
        if not isinstance(document, str) or not re.fullmatch(
                r"(?:xslF345X[0-9]+/)?[A-Za-z0-9_][A-Za-z0-9_.-]*\.xml", document):
            raise ValueError("Invalid SEC ownership document name")
        return document.rsplit("/", 1)[-1]

    def fetch_ownership_xml(self, accession: str, cik: str, *, primary_document=None) -> str:
        """
        Locate the ownership XML inside a filing package and return its text.
        Use the SEC submissions document name when present, avoiding a directory
        request. Older callers use the directory, rejecting ambiguous XML choices.
        """
        if (not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
                or not re.fullmatch(r"[0-9]{1,10}", str(cik)) or int(cik) == 0):
            raise ValueError("Invalid SEC filing identity")
        nodash = accession.replace("-", "")
        cik_clean = str(cik).lstrip("0")
        base = f"{WWW_HOST}/Archives/edgar/data/{cik_clean}/{nodash}"
        if primary_document is not None:
            name = self._ownership_filename(primary_document)
        else:
            items = self._index_json(accession, cik).get("directory", {}).get("item", [])
            candidates = [it for it in items if isinstance(it.get("name"), str)
                          and it["name"].endswith(".xml") and not it["name"].endswith("-index.xml")]
            typed = [it for it in candidates if it.get("type") in {"4", "4/A"}]
            candidates = typed or candidates
            if len(candidates) != 1:
                raise LookupError(f"Expected one ownership XML in {accession}")
            name = self._ownership_filename(candidates[0]["name"])
        return self._get(f"{base}/{name}").text

    @staticmethod
    def _is_issuer_filing(filing: Form4, cik: str) -> bool:
        expected = str(cik).zfill(10)
        if not re.fullmatch(r"[0-9]{10}", expected) or int(expected) == 0:
            raise ValueError("Invalid requested SEC issuer CIK")
        if re.fullmatch(r"[0-9]{10}", filing.issuer_cik) and int(filing.issuer_cik) > 0:
            if filing.issuer_cik == expected:
                return True
            if expected in filing.reporting_owner_ciks:
                return False
        # Only validated public identifiers appear in diagnostics, never XML text.
        actual = filing.issuer_cik if re.fullmatch(r"[0-9]{10}", filing.issuer_cik) else "invalid"
        accession = filing.accession if re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", filing.accession) else "invalid"
        raise ValueError(f"Form 4 {accession}: XML issuer {actual} does not match "
                         f"requested issuer {expected}; reporting-owner role not established")

    def _filing_cache_path(self, issuer_cik: str, accession: str):
        if self._cache_dir is None:
            return None
        cik = str(issuer_cik).zfill(10)
        if (not re.fullmatch(r"[0-9]{10}", cik)
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)):
            raise ValueError("Invalid Form 4 cache identity")
        path = self._cache_dir / f"{cik}-{accession}.json"
        if path.is_symlink():
            raise ValueError("Form 4 cache entry must not be a symlink")
        return path

    def _read_filing_cache(self, path):
        if path is None:
            return None
        try:
            # JSON escaping can expand the already bounded XML by up to six times.
            with path.open("rb") as source:
                raw = source.read(6 * _MAX_XML_BYTES + 4097)
            if len(raw) > 6 * _MAX_XML_BYTES + 4096:
                return None
            record = json.loads(raw)
            retrieved = datetime.fromisoformat(record["retrieved_at"])
            xml = record["xml"]
            if (record["version"] != 1 or record["identity"] != path.stem or retrieved.tzinfo is None
                    or retrieved > datetime.now(timezone.utc)
                    or record["sha256"] != hashlib.sha256(xml.encode("utf-8")).hexdigest()):
                return None
            return xml
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            return None

    def _write_filing_cache(self, path, xml):
        if path is None:
            return
        record = {"version": 1, "identity": path.stem, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "sha256": hashlib.sha256(xml.encode("utf-8")).hexdigest(), "xml": xml}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".form4-", delete=False) as output:
                temporary = output.name
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def insider_filings(
        self,
        issuer_cik: str,
        *,
        window_days: int = 90,
        max_filings: int = 60,
        strict: bool = False,
    ) -> list[Form4]:
        """
        Parse up to `max_filings` recent Form 4/4A filings within `window_days`.
        `strict=True` reports invalid submissions or a failed filing instead of
        silently omitting it. Verified reporting-owner-only filings are disclosed
        separately and never enter this issuer's transactions.
        """
        since = date.today() - timedelta(days=window_days)
        self._progress(f"  SEC issuer {issuer_cik}: checking current filing list.")
        metas = self.list_form4_accessions(issuer_cik, since=since, limit=max_filings, strict=strict)
        self._progress(f"  SEC issuer {issuer_cik}: {len(metas)} filings to read.")
        out: list[Form4] = []
        for number, m in enumerate(metas, 1):
            try:
                path = self._filing_cache_path(issuer_cik, m["accession"])
                xml = self._read_filing_cache(path)
                if xml is not None:
                    try:
                        cached = parse_form4(xml, accession=m["accession"], filing_date=m["filing_date"])
                        self._is_issuer_filing(cached, issuer_cik)
                    except Exception:
                        xml = None  # Corruption must be repaired from SEC, never scored.
                from_cache = xml is not None
                if xml is None:
                    kwargs = {"primary_document": m["primary_document"]} if "primary_document" in m else {}
                    xml = self.fetch_ownership_xml(m["accession"], issuer_cik, **kwargs)
                f = cached if from_cache else parse_form4(xml, accession=m["accession"], filing_date=m["filing_date"])
                is_issuer = self._is_issuer_filing(f, issuer_cik)
                if from_cache:
                    self.filings_cached += 1
                else:
                    # Save each validated filing immediately. Failed runs retain only
                    # reusable source documents, never a partially published score.
                    self._write_filing_cache(path, xml)
                    self.filings_downloaded += 1
                if is_issuer:
                    out.append(f)
                else:
                    self.non_issuer_filings.append({"accession": f.accession, "filing_date": f.filing_date,
                        "requested_cik": str(issuer_cik).zfill(10), "issuer_cik": f.issuer_cik,
                        "reason": "requested_company_is_reporting_owner_only"})
                    self._progress(f"  SEC filing {f.accession}: issuer {f.issuer_cik}; "
                                   f"{issuer_cik} is a reporting owner only, excluded from its score.")
                if number == 1 or number % 10 == 0 or number == len(metas):
                    self._progress(f"  SEC issuer {issuer_cik}: filing {number}/{len(metas)} "
                                   f"({'cache' if from_cache else 'downloaded'}).")
            except Exception:
                if strict:
                    raise
                # one malformed filing must never sink the batch
                continue
        return out


def _parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _today() -> date:  # indirection so tests can monkeypatch
    return date.today()
