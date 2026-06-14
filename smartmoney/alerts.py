"""
Real alert delivery.

Pipeline when a new 13F lands:
  detect new accession -> sync into store -> diff vs prior quarter -> build Alert
  -> fan out to each active subscription that hasn't been delivered this accession
  -> record delivery (idempotency).

Design choices that matter:
  - The value is the DIFF, not "a filing appeared". The Alert carries new/exit/add/trim.
  - Idempotency lives in the DB (deliveries table), keyed (subscription, accession), so a
    crash/restart never re-sends and never drops. In-memory state would lose both.
  - New subscribers are PRIMED by default: the current latest filing is marked delivered
    without sending, so they only get genuinely new filings (no instant backfill spam).
  - Alerts are a paid feature: subscribing is gated on Tier.alerts_enabled.
  - Fetching (network) and dispatch (store + channels) are separate methods so dispatch
    is fully testable offline and you can drive run_once from cron instead of a loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .channels import Channel, ConsoleChannel
from .db import Store
from .diff import Move, diff_portfolios
from .portfolio import Portfolio
from .registry import Fund
from .tracker import Tier, Tracker


# ---------------------------------------------------------------------------
@dataclass
class AlertMove:
    move: str
    ticker: Optional[str]
    issuer: str
    detail: str        # human-friendly ("NEW $1.2B", "+45% shares", ...)


@dataclass
class Alert:
    fund_label: str
    manager: Optional[str]
    cik: str
    accession: str
    form: str
    filing_date: str
    report_date: str
    prev_report_date: Optional[str]
    url: str
    counts: dict[str, int]                       # {"NEW": n, "EXIT": n, ...}
    moves: list[AlertMove] = field(default_factory=list)
    target: str = ""                             # set per-subscription at send time

    # -- rendering --
    def subject(self) -> str:
        amd = " (amendment)" if self.form.endswith("/A") else ""
        c = self.counts
        bits = []
        if c.get("NEW"):
            bits.append(f"{c['NEW']} new")
        if c.get("EXIT"):
            bits.append(f"{c['EXIT']} exits")
        head = ", ".join(bits) if bits else "filing update"
        return f"[SmartMoney] {self.fund_label} 13F{amd} ({self.report_date}): {head}"

    def to_text(self) -> str:
        lines = [self.subject(), ""]
        if self.manager:
            lines.append(f"{self.fund_label} — {self.manager}")
        prev = f" vs {self.prev_report_date}" if self.prev_report_date else " (first filing on record)"
        lines.append(f"Quarter {self.report_date}{prev} | filed {self.filing_date} | {self.form}")
        lines.append("")
        for label, key in (("New", "NEW"), ("Exited", "EXIT"),
                           ("Added", "ADD"), ("Trimmed", "TRIM")):
            rows = [m for m in self.moves if m.move == key]
            if not rows:
                continue
            lines.append(f"{label}:")
            for m in rows:
                tkr = f"{m.ticker} " if m.ticker else ""
                lines.append(f"  - {tkr}{m.issuer}: {m.detail}")
            lines.append("")
        lines.append(self.url)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


def _fmt_usd(x: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x/div:,.1f}{unit}"
    return f"${x:,.0f}"


def _edgar_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def build_alert(store: Store, cik: str, accession: str, top_n: int = 15) -> Optional[Alert]:
    """Build an Alert for a stored filing by diffing it against the prior stored quarter."""
    filing = store.get_filing(accession)
    if filing is None:
        return None
    fund = store.fund_row(cik) or {}

    report_date = filing["report_date"]
    curr = store.load_portfolio(cik, report_date)
    if curr is None:
        return None
    prev_q = store.previous_quarter(cik, report_date)
    prev = store.load_portfolio(cik, prev_q) if prev_q else Portfolio(
        cik=cik, fund_label=curr.fund_label, report_date="", form="")

    report = diff_portfolios(prev, curr)
    counts: dict[str, int] = {}
    moves: list[AlertMove] = []
    for mv in (Move.NEW, Move.EXIT, Move.ADD, Move.TRIM):
        rows = report.by_move(mv)
        counts[mv.value] = len(rows)
        for c in rows[:top_n]:
            if mv in (Move.NEW, Move.EXIT):
                v = c.curr_value if mv == Move.NEW else c.prev_value
                detail = f"{mv.value} {_fmt_usd(v)}"
            else:
                pct = c.share_change_pct
                detail = f"{(pct*100):+.0f}% shares" if pct is not None else mv.value
            if c.put_call:
                detail += f" [{c.put_call}]"
            moves.append(AlertMove(move=mv.value, ticker=c.ticker, issuer=c.issuer, detail=detail))

    return Alert(
        fund_label=curr.fund_label, manager=fund.get("manager"), cik=cik,
        accession=accession, form=filing["form"], filing_date=filing["filing_date"],
        report_date=report_date, prev_report_date=(prev_q or None),
        url=_edgar_url(cik, accession), counts=counts, moves=moves,
    )


# ---------------------------------------------------------------------------
class AlertEngine:
    """
    Ties Store + Tracker + channels together.

    `channels` maps a channel name ("console"/"webhook"/"email") to a Channel, OR a
    factory Callable[[subscription_dict], Channel] when the channel is per-target
    (a webhook URL / email address differs per subscription).
    """

    def __init__(self, store: Store, tracker: Optional[Tracker] = None,
                 channels: Optional[dict] = None):
        self.store = store
        self.tracker = tracker
        self.channels = channels or {"console": ConsoleChannel()}

    # -- subscription management (freemium gate here) ----------------------
    def subscribe(self, tier: Tier, user_id: str, fund: Fund, channel: str,
                  target: str = "", prime: bool = True) -> int:
        if not tier.alerts_enabled:
            from .tracker import EntitlementError
            raise EntitlementError("Alerts are a paid feature. Upgrade to subscribe.")
        cik = (fund.cik or "").zfill(10)
        if not cik:
            raise ValueError(f"No CIK for {fund.label}; resolve it before subscribing.")
        # Validate the untrusted target before it is ever persisted/used.
        if channel == "webhook":
            from .netsec import validate_public_url
            validate_public_url(target, resolve_dns=False)   # syntactic guard at entry
        elif channel == "email":
            from .netsec import validate_email_recipient
            validate_email_recipient(target)
        sub_id = self.store.add_subscription(user_id, cik, channel, target)
        if prime:
            latest = self.store.latest_filing_row(cik)
            if latest:
                self.store.record_delivery(sub_id, latest["accession"], "primed")
        return sub_id

    # -- dispatch (pure store + channels; no network) ----------------------
    def _resolve_channel(self, sub: dict) -> Optional[Channel]:
        ch = self.channels.get(sub["channel"])
        if ch is None:
            return None
        if isinstance(ch, Channel):
            return ch
        return ch(sub) if callable(ch) else None      # per-target factory

    def dispatch_for_fund(self, cik: str, top_n: int = 15) -> list[dict]:
        """
        Deliver the newest filing to every active subscription that hasn't received it.
        Returns a list of {subscription_id, accession, status} describing what happened.
        """
        cik = cik.zfill(10)
        latest = self.store.latest_filing_row(cik)
        if latest is None:
            return []
        accession = latest["accession"]

        subs = self.store.active_subscriptions(cik)
        pending = [s for s in subs if not self.store.was_delivered(s["id"], accession)]
        if not pending:
            return []

        alert = build_alert(self.store, cik, accession, top_n=top_n)
        results = []
        for sub in pending:
            channel = self._resolve_channel(sub)
            if channel is None:
                self.store.record_delivery(sub["id"], accession, "failed",
                                           f"no channel '{sub['channel']}'")
                results.append({"subscription_id": sub["id"], "accession": accession,
                                "status": "failed"})
                continue
            try:
                alert.target = sub["target"]
                channel.send(alert)
                self.store.record_delivery(sub["id"], accession, "sent")
                results.append({"subscription_id": sub["id"], "accession": accession,
                                "status": "sent"})
            except Exception as e:  # noqa: BLE001 — log + allow retry next run
                self.store.record_delivery(sub["id"], accession, "failed", str(e))
                results.append({"subscription_id": sub["id"], "accession": accession,
                                "status": "failed", "error": str(e)})
        return results

    # -- full cycle (network: sync, then dispatch) -------------------------
    def run_once(self, funds: list[Fund], top_n: int = 15) -> list[dict]:
        if self.tracker is None:
            raise RuntimeError("run_once needs a Tracker (set one) to fetch filings.")
        out: list[dict] = []
        for fund in funds:
            self.tracker.sync_fund(self.store, fund)   # only fetches new filings
            cik = (fund.cik or self.tracker.cik_for(fund)).zfill(10)
            out.extend(self.dispatch_for_fund(cik, top_n=top_n))
        return out

    def poll(self, funds: list[Fund], interval_sec: int = 3600, top_n: int = 15) -> None:
        """Blocking loop. In production prefer cron + run_once over this."""
        while True:
            self.run_once(funds, top_n=top_n)
            time.sleep(interval_sec)
