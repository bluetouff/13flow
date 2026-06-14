"""
Diff two portfolios (prev -> curr) into the moves a 'smart money' tracker cares about.

Classification is by SHARE COUNT, not value — value moves with price, shares move
with conviction. A position whose value rose only because the stock rallied isn't a
'buy'; we want to surface what the manager actually did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .portfolio import Portfolio, Position


class Move(str, Enum):
    NEW = "NEW"            # opened a position
    EXIT = "EXIT"          # closed entirely
    ADD = "ADD"            # increased share count
    TRIM = "TRIM"          # decreased share count (still holding)
    HOLD = "HOLD"          # share count unchanged


@dataclass
class Change:
    move: Move
    cusip: str
    issuer: str
    put_call: str
    prev_shares: float
    curr_shares: float
    prev_value: float
    curr_value: float
    curr_weight: float      # weight in the current portfolio (0 for exits)
    ticker: str | None = None

    @property
    def share_change_pct(self) -> float | None:
        if self.prev_shares == 0:
            return None  # new position; percent change is undefined
        return (self.curr_shares - self.prev_shares) / self.prev_shares


@dataclass
class DiffReport:
    fund_label: str
    prev_period: str
    curr_period: str
    changes: list[Change]

    def by_move(self, move: Move) -> list[Change]:
        sub = [c for c in self.changes if c.move == move]
        # Most interesting first: new/exit by size, add/trim by magnitude of change.
        if move in (Move.NEW, Move.EXIT, Move.HOLD):
            sub.sort(key=lambda c: max(c.curr_value, c.prev_value), reverse=True)
        else:
            sub.sort(key=lambda c: abs((c.share_change_pct or 0)), reverse=True)
        return sub


def diff_portfolios(prev: Portfolio, curr: Portfolio, hold_epsilon: float = 0.005) -> DiffReport:
    """
    hold_epsilon: relative share change below which we treat a position as HOLD,
    to swallow rounding noise (e.g. 0.5%).
    """
    keys = set(prev.positions) | set(curr.positions)
    changes: list[Change] = []

    for key in keys:
        p: Position | None = prev.positions.get(key)
        c: Position | None = curr.positions.get(key)

        prev_sh = p.shares if p else 0.0
        curr_sh = c.shares if c else 0.0
        prev_val = p.value_usd if p else 0.0
        curr_val = c.value_usd if c else 0.0
        ref = c or p  # at least one exists
        weight = c.weight if c else 0.0

        if p is None and c is not None:
            move = Move.NEW
        elif c is None and p is not None:
            move = Move.EXIT
        else:
            rel = (curr_sh - prev_sh) / prev_sh if prev_sh else 0.0
            if abs(rel) <= hold_epsilon:
                move = Move.HOLD
            elif rel > 0:
                move = Move.ADD
            else:
                move = Move.TRIM

        changes.append(
            Change(
                move=move,
                cusip=ref.cusip,
                issuer=ref.issuer,
                put_call=ref.put_call,
                prev_shares=prev_sh,
                curr_shares=curr_sh,
                prev_value=prev_val,
                curr_value=curr_val,
                curr_weight=weight,
                ticker=getattr(ref, "ticker", None),
            )
        )
    return DiffReport(
        fund_label=curr.fund_label,
        prev_period=prev.report_date,
        curr_period=curr.report_date,
        changes=changes,
    )
