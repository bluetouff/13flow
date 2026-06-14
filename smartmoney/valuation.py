"""
Revalue a stored (quarter-end) portfolio at current prices.

What this unlocks:
  - CURRENT weights (the reported weights are frozen at quarter-end prices).
  - Implied P&L since the filing's report date: shares held x price move. This is
    'paper' P&L — it assumes the fund still holds what it last reported, which is the
    whole premise of 13F following, and is wrong the moment they trade. Label it as such.
  - A free DATA-QUALITY check: reported_value should ~= shares x quarter-end close.
    If it doesn't, the CUSIP->ticker mapping is probably wrong. We surface that ratio.

Out of scope (carried at reported value, flagged): option lines (putCall set) and any
position we can't price (no ticker, or the provider returns nothing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .portfolio import Portfolio
from .prices import PriceProvider


@dataclass
class ValuedPosition:
    cusip: str
    ticker: Optional[str]
    issuer: str
    put_call: str
    shares: float
    reported_value: float
    status: str                       # priced | option | no_ticker | no_price
    basis_date: Optional[date] = None
    px_basis: Optional[float] = None  # close at/just before report date
    px_now: Optional[float] = None
    current_value: float = 0.0
    current_weight: float = 0.0
    pnl_abs: Optional[float] = None
    pnl_pct: Optional[float] = None
    reconcile_ratio: Optional[float] = None   # reported / (shares*px_basis); ~1.0 is healthy
    market_cap: Optional[float] = None
    pct_of_company: Optional[float] = None


@dataclass
class ValuedPortfolio:
    fund_label: str
    report_date: str
    basis_date: str
    positions: list[ValuedPosition] = field(default_factory=list)
    reported_total: float = 0.0
    current_total: float = 0.0
    priced_basis_total: float = 0.0   # basis value of only the priced sleeve
    priced_current_total: float = 0.0

    @property
    def pnl_abs(self) -> float:
        return self.priced_current_total - self.priced_basis_total

    @property
    def pnl_pct(self) -> Optional[float]:
        return self.pnl_abs / self.priced_basis_total if self.priced_basis_total else None

    @property
    def unpriced_value(self) -> float:
        return self.current_total - self.priced_current_total

    def top(self, n: int = 20) -> list[ValuedPosition]:
        return sorted(self.positions, key=lambda p: p.current_value, reverse=True)[:n]


def value_portfolio(
    pf: Portfolio,
    provider: PriceProvider,
    basis_date: Optional[str] = None,
    with_fundamentals: bool = False,
) -> ValuedPortfolio:
    basis_iso = basis_date or pf.report_date
    basis = date.fromisoformat(basis_iso)
    vp = ValuedPortfolio(fund_label=pf.fund_label, report_date=pf.report_date, basis_date=basis_iso)

    for p in pf.positions.values():
        vpos = ValuedPosition(
            cusip=p.cusip, ticker=p.ticker, issuer=p.issuer, put_call=p.put_call,
            shares=p.shares, reported_value=p.value_usd, status="priced",
        )
        vp.reported_total += p.value_usd

        if p.put_call:
            vpos.status = "option"
            vpos.current_value = p.value_usd          # carry at reported
        elif not p.ticker:
            vpos.status = "no_ticker"
            vpos.current_value = p.value_usd
        else:
            now = provider.latest_close(p.ticker)
            then = provider.close_on_or_before(p.ticker, basis)
            if now is None or then is None:
                vpos.status = "no_price"
                vpos.current_value = p.value_usd
            else:
                vpos.basis_date, vpos.px_basis = then
                _, vpos.px_now = now
                vpos.current_value = p.shares * vpos.px_now
                basis_value = p.shares * vpos.px_basis
                vpos.pnl_abs = vpos.current_value - basis_value
                vpos.pnl_pct = (vpos.px_now / vpos.px_basis - 1.0) if vpos.px_basis else None
                vpos.reconcile_ratio = (p.value_usd / basis_value) if basis_value else None
                vp.priced_basis_total += basis_value
                vp.priced_current_total += vpos.current_value
                if with_fundamentals:
                    f = provider.fundamentals(p.ticker)
                    if f:
                        vpos.market_cap = f.market_cap
                        if f.shares_outstanding:
                            vpos.pct_of_company = p.shares / f.shares_outstanding

        vp.current_total += vpos.current_value
        vp.positions.append(vpos)

    if vp.current_total > 0:
        for vpos in vp.positions:
            vpos.current_weight = vpos.current_value / vp.current_total
    return vp
