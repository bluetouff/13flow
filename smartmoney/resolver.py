"""
Long-tail CUSIP -> ticker resolution.

OpenFIGI resolves the great majority of 13(f) securities, but a tail comes back empty:
units, recently-listed names, odd share classes, some foreign issuers. Leaving those as
permanent misses throws away two free signals we already have:

  1. Every 13F row carries `nameOfIssuer` — so an unresolved CUSIP still has a NAME.
  2. The CUSIP issuer prefix (first 6 chars) is shared by ALL securities of one issuer,
     so a miss whose prefix matches a CUSIP we already resolved is almost certainly the
     same company (a different share class / unit).

So instead of a single resolver we run a CHAIN, each step lower-confidence than the last,
and we record provenance + confidence so the UI can flag weak mappings (and the valuation
reconcile check can catch wrong ones):

  manual override (1.0) -> OpenFIGI (0.95) -> CUSIP prefix (0.65-0.85)
  -> SEC name match (0.6) -> unresolved (0.0, but we keep the issuer name)

Misses are cached with a timestamp and re-tried after a TTL (the tail shrinks over time as
FIGI/SEC data improves), rather than cached forever.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from .figi import OpenFigiClient

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Corporate noise stripped before name matching (applied to BOTH sides).
_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
    "LTD", "LIMITED", "LLC", "LP", "PLC", "NV", "SA", "AG", "SE", "THE",
    "COM", "CL", "CLASS", "A", "B", "C", "HLDG", "HLDGS", "HOLDING", "HOLDINGS",
    "GROUP", "GRP", "TR", "TRUST", "FUND", "ADR", "ADS", "SPONSORED", "NEW",
}


def normalize_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    toks = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(toks)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Resolution:
    cusip: str
    ticker: Optional[str]
    name: Optional[str]
    figi: Optional[str]
    source: str          # manual | openfigi | cusip_prefix | sec_name | none
    confidence: float    # 0..1
    resolved_at: str


# ---------------------------------------------------------------------------
class ResolutionCache:
    """JSON cache that keeps provenance and re-tries weak/negative entries after a TTL."""

    def __init__(self, path: str | os.PathLike | None = None,
                 negative_ttl_days: int = 30, min_confidence: float = 0.5):
        # Cache lives in SMARTMONEY_CACHE_DIR if set (else cwd) — so an ingest user who has no
        # write access to the install dir (/opt/...) can still persist the cache next to the DB.
        if path is None:
            path = Path(os.environ.get("SMARTMONEY_CACHE_DIR") or ".") / ".smartmoney_resolution_cache.json"
        self.path = Path(path)
        self.neg_ttl = timedelta(days=negative_ttl_days)
        self.min_confidence = min_confidence
        self._data: dict[str, dict] = {}
        self._dirty = False
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, cusip: str) -> Optional[Resolution]:
        rec = self._data.get(cusip.upper())
        return Resolution(**rec) if rec else None

    def put(self, res: Resolution) -> None:
        self._data[res.cusip.upper()] = asdict(res)
        self._dirty = True

    def needs_query(self, cusip: str, now: Optional[datetime] = None) -> bool:
        rec = self._data.get(cusip.upper())
        if rec is None:
            return True
        res = Resolution(**rec)
        if res.ticker and res.confidence >= self.min_confidence:
            return False                       # solid hit — keep
        now = now or datetime.now(timezone.utc)
        try:
            age = now - datetime.fromisoformat(res.resolved_at)
        except (ValueError, TypeError):
            return True
        return age >= self.neg_ttl             # weak/negative: retry once stale

    def confident_resolutions(self, min_conf: float = 0.9) -> list[Resolution]:
        out = []
        for rec in self._data.values():
            r = Resolution(**rec)
            if r.ticker and r.confidence >= min_conf:
                out.append(r)
        return out

    def save(self) -> None:
        if self._dirty:
            self.path.write_text(json.dumps(self._data, indent=0))
            self._dirty = False


# ---------------------------------------------------------------------------
def load_sec_ticker_index(user_agent: str,
                          session: Optional[requests.Session] = None) -> dict[str, tuple[str, str]]:
    """
    Fetch SEC company_tickers.json and build {normalized_title: (ticker, title)}.
    Free, official, and a great fallback because 13F issuer names usually match SEC titles.
    """
    sess = session or requests.Session()
    resp = sess.get(SEC_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    return build_sec_index(resp.json())


def build_sec_index(raw: dict) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for row in raw.values():
        title = row.get("title", "")
        ticker = row.get("ticker", "")
        norm = normalize_name(title)
        if len(norm) >= 3 and norm not in index:    # first (usually primary class) wins
            index[norm] = (ticker, title)
    return index


# ---------------------------------------------------------------------------
class CusipResolver:
    """Chain of resolvers. `resolve` takes (cusip, issuer_name) pairs."""

    def __init__(self, openfigi: Optional[OpenFigiClient] = None,
                 sec_index: Optional[dict] = None,
                 overrides: Optional[dict] = None,
                 cache: Optional[ResolutionCache] = None):
        self.openfigi = openfigi
        self.sec_index = sec_index or {}
        self.overrides = {k.upper(): v for k, v in (overrides or {}).items()}
        self.cache = cache

    # -- helpers ----------------------------------------------------------
    def _prefix_index(self, fresh: dict[str, Resolution]) -> dict[str, Resolution]:
        idx: dict[str, Resolution] = {}
        pool = list(fresh.values())
        if self.cache:
            pool += self.cache.confident_resolutions(0.9)
        for r in pool:
            if r.ticker and r.confidence >= 0.9:
                idx.setdefault(r.cusip[:6], r)
        return idx

    def _sec_match(self, issuer: str) -> Optional[tuple[str, str]]:
        norm = normalize_name(issuer)
        return self.sec_index.get(norm) if len(norm) >= 3 else None

    # -- main -------------------------------------------------------------
    def resolve(self, items: list[tuple[str, str]]) -> dict[str, Resolution]:
        now = datetime.now(timezone.utc)
        issuer = {c.upper(): (n or "") for c, n in items if c}
        out: dict[str, Resolution] = {}
        to_query: list[str] = []

        for c in issuer:
            cached = self.cache.get(c) if self.cache else None
            if cached and not (self.cache and self.cache.needs_query(c, now)):
                out[c] = cached
            else:
                to_query.append(c)

        # 1) manual overrides
        remaining: list[str] = []
        for c in to_query:
            ov = self.overrides.get(c)
            if ov:
                tkr = ov if isinstance(ov, str) else ov.get("ticker")
                nm = None if isinstance(ov, str) else ov.get("name")
                out[c] = Resolution(c, tkr, nm, None, "manual", 1.0, _now_iso())
            else:
                remaining.append(c)

        # 2) OpenFIGI (batch)
        if self.openfigi and remaining:
            fmap = self.openfigi.map_cusips(remaining)
            still = []
            for c in remaining:
                m = fmap.get(c)
                if m and m.ticker:
                    out[c] = Resolution(c, m.ticker, m.name, m.figi, "openfigi", 0.95, _now_iso())
                else:
                    still.append(c)
            remaining = still

        # 3) CUSIP issuer-prefix (reuse a confident sibling's ticker)
        index = self._prefix_index(out)
        still = []
        for c in remaining:
            hit = index.get(c[:6])
            if hit:
                same = normalize_name(issuer[c]) == normalize_name(hit.name or "")
                out[c] = Resolution(c, hit.ticker, hit.name, None, "cusip_prefix",
                                    0.85 if same else 0.65, _now_iso())
            else:
                still.append(c)
        remaining = still

        # 4) SEC name match
        still = []
        for c in remaining:
            hit = self._sec_match(issuer[c])
            if hit:
                out[c] = Resolution(c, hit[0], hit[1], None, "sec_name", 0.6, _now_iso())
            else:
                still.append(c)
        remaining = still

        # 5) unresolved — keep the issuer name, cache as a TTL'd miss
        for c in remaining:
            out[c] = Resolution(c, None, issuer[c] or None, None, "none", 0.0, _now_iso())

        if self.cache:
            for c in to_query:
                self.cache.put(out[c])
            self.cache.save()
        return out


# ---------------------------------------------------------------------------
def resolve_portfolio(pf, resolver: CusipResolver) -> None:
    """Attach ticker + provenance to a Portfolio's long-stock positions, in place."""
    items = [(p.cusip, p.issuer) for p in pf.positions.values() if not p.put_call]
    res = resolver.resolve(items)
    for p in pf.positions.values():
        r = res.get(p.cusip.upper())
        if r and r.ticker:
            p.ticker = r.ticker
            p.figi_name = r.name or p.figi_name
            p.ticker_source = r.source
            p.ticker_confidence = r.confidence


def coverage(pf) -> dict:
    """Resolution coverage for one portfolio: counts, value share, by-source, worst tail."""
    rows = [p for p in pf.positions.values() if not p.put_call]
    total_val = sum(p.value_usd for p in rows) or 1.0
    resolved = [p for p in rows if p.ticker]
    by_source: dict[str, float] = {}
    for p in resolved:
        by_source[p.ticker_source or "?"] = by_source.get(p.ticker_source or "?", 0.0) + p.value_usd
    tail = sorted((p for p in rows if not p.ticker), key=lambda p: p.value_usd, reverse=True)
    return {
        "n_total": len(rows), "n_resolved": len(resolved),
        "value_total": sum(p.value_usd for p in rows),
        "value_resolved": sum(p.value_usd for p in resolved),
        "value_share": sum(p.value_usd for p in resolved) / total_val,
        "by_source_value": by_source,
        "tail": [{"cusip": p.cusip, "issuer": p.issuer, "value": p.value_usd} for p in tail[:25]],
    }
