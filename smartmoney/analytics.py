"""
Cross-fund analytics that need the diff engine (not just a GROUP BY).

`consensus_holdings` in db.py answers "who HOLDS X" with pure SQL. This module
answers the sharper question — "who BOUGHT X this quarter" — which requires each
fund's quarter-over-quarter diff, then aggregating moves across funds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import Store
from .diff import Move, diff_portfolios


@dataclass
class ConsensusMove:
    cusip: str
    ticker: str | None
    issuer: str
    n_funds: int
    funds: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)  # parallel to funds


def consensus_moves(
    store: Store,
    ciks: list[str],
    report_date: str,
    kinds: tuple[Move, ...] = (Move.NEW, Move.ADD),
    min_funds: int = 3,
) -> list[ConsensusMove]:
    """
    For each fund, diff `report_date` against that fund's own previous quarter,
    keep positions whose move is in `kinds`, then aggregate by CUSIP across funds.

    Default (NEW, ADD) = 'smart money is buying this'. Pass (EXIT, TRIM) to flip it
    into a 'smart money is dumping this' screen.
    """
    agg: dict[str, ConsensusMove] = {}

    for cik in ciks:
        cik = cik.zfill(10)
        curr = store.load_portfolio(cik, report_date)
        if curr is None:
            continue
        prev_q = store.previous_quarter(cik, report_date)
        prev = store.load_portfolio(cik, prev_q) if prev_q else None
        if prev is None:
            # No prior quarter to diff against: treat every holding as effectively NEW.
            if Move.NEW not in kinds:
                continue
            for p in curr.positions.values():
                if p.put_call:
                    continue
                _bump(agg, p.cusip, p.ticker, p.issuer, curr.fund_label, Move.NEW)
            continue

        report = diff_portfolios(prev, curr)
        for c in report.changes:
            if c.put_call or c.move not in kinds:
                continue
            _bump(agg, c.cusip, c.ticker, c.issuer, curr.fund_label, c.move)

    out = [m for m in agg.values() if m.n_funds >= min_funds]
    out.sort(key=lambda m: m.n_funds, reverse=True)
    return out


def _bump(agg, cusip, ticker, issuer, fund_label, move) -> None:
    m = agg.get(cusip)
    if m is None:
        m = ConsensusMove(cusip=cusip, ticker=ticker, issuer=issuer, n_funds=0)
        agg[cusip] = m
    if fund_label not in m.funds:        # one vote per fund
        m.funds.append(fund_label)
        m.moves.append(move.value)
        m.n_funds += 1
        if ticker and not m.ticker:
            m.ticker = ticker
