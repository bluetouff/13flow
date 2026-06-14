"""
Reconstruct a clean portfolio from raw 13F holdings.

A single issuer can appear on many rows (different internal managers, voting
discretion, share lots). We aggregate to one line per economic position, keyed
by (cusip, put_call) so that long stock, puts, and calls on the same name stay
distinct — they're very different bets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .parser import RawHolding

# The SEC switched 13F <value> reporting from thousands to whole dollars with the
# 2022 amendments, first effective for filings on/after this date.
DOLLARS_EFFECTIVE = date(2023, 1, 3)


@dataclass
class Position:
    cusip: str
    issuer: str
    title_of_class: str
    put_call: str           # '', 'Put', 'Call'
    value_usd: float        # normalized to actual USD
    shares: float
    weight: float = 0.0     # share of total portfolio value, 0..1
    ticker: str | None = None       # filled by enrichment (OpenFIGI / resolver chain)
    figi_name: str | None = None     # canonical instrument name from FIGI
    ticker_source: str | None = None # manual | openfigi | cusip_prefix | sec_name
    ticker_confidence: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.cusip, self.put_call)


@dataclass
class Portfolio:
    cik: str
    fund_label: str
    report_date: str         # quarter end, YYYY-MM-DD
    form: str
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        return sum(p.value_usd for p in self.positions.values())

    def top(self, n: int = 20) -> list[Position]:
        return sorted(self.positions.values(), key=lambda p: p.value_usd, reverse=True)[:n]


def _value_multiplier(report_date: str) -> float:
    try:
        d = date.fromisoformat(report_date)
    except (ValueError, TypeError):
        # Unknown period: assume modern (dollars). Better to under- than over-scale.
        return 1.0
    return 1.0 if d >= DOLLARS_EFFECTIVE else 1000.0


def build_portfolio(
    cik: str,
    fund_label: str,
    report_date: str,
    form: str,
    raw: list[RawHolding],
) -> Portfolio:
    mult = _value_multiplier(report_date)
    pf = Portfolio(cik=cik, fund_label=fund_label, report_date=report_date, form=form)

    for h in raw:
        if not h.cusip:
            continue
        key = (h.cusip, h.put_call)
        pos = pf.positions.get(key)
        if pos is None:
            pf.positions[key] = Position(
                cusip=h.cusip,
                issuer=h.name_of_issuer,
                title_of_class=h.title_of_class,
                put_call=h.put_call,
                value_usd=h.value * mult,
                shares=h.shares,
            )
        else:
            pos.value_usd += h.value * mult
            pos.shares += h.shares

    total = pf.total_value
    if total > 0:
        for p in pf.positions.values():
            p.weight = p.value_usd / total
    return pf
