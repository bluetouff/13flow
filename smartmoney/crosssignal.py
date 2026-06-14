"""
Confluence — where smart institutions and corporate insiders agree (13FLOW).

The thesis in one module: a 13F says the big funds are accumulating a name (slow,
45-day-lagged, heavyweight). A cluster of Form 4 open-market buys says the people who
run the company are committing personal cash (fast, 2-day-lagged, high conviction).
When BOTH point the same way on the same ticker, you get a rare, hard-to-fake signal.

The scoring is split in two on purpose:
  - FEATURE EXTRACTION (`FeatureParams`): turns raw filings into conviction features —
    recency decay, buy-size vs prior holdings, cluster timing, seniority. These shape
    *what the signal measures* and are tuned by judgement / domain knowledge.
  - COMBINATION (`Weights`): folds the features into a 0-100 score. These are tuned
    empirically by the backtest harness (backtest.py) to maximise rank-IC vs forward returns.

This separation is what lets you optimise the weights without re-deriving the features,
and re-derive features without invalidating a fitted weight set.

Nothing here is investment advice; it is a *screen* — a transparent way to rank a universe
by how strongly two independent, public, high-quality signals coincide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from .forms4 import Form4, Form4Transaction


# ===========================================================================
# Feature-extraction parameters (the "what we measure" knobs)
# ===========================================================================
@dataclass(frozen=True)
class FeatureParams:
    # recency: an open-market buy decays in signal value with age.
    recency_halflife_days: float = 30.0      # 30d -> a 1-month-old buy counts half
    recent_window_days: int = 14             # buys inside this window count toward the "fresh cluster"
    # buy-size conviction: a purchase that materially grows the insider's stake means more
    # than a token buy. Mapped through a saturating curve on the % stake increase.
    sizing_floor: float = 0.70               # multiplier for negligible buys
    sizing_ceiling: float = 1.60             # multiplier for stake-doubling conviction buys
    sizing_midpoint_pct: float = 0.10        # 10% stake increase sits at the curve midpoint
    # seniority: whose buy it is.
    seniority_csuite: float = 1.60           # CEO / CFO / President / Chair
    seniority_officer: float = 1.20          # other named officers
    seniority_owner: float = 1.10            # 10% beneficial owners
    seniority_director: float = 1.00         # directors / baseline


DEFAULT_FEATURES = FeatureParams()


# ===========================================================================
# Combination weights (the "how we fold it together" knobs — tuned by backtest)
# ===========================================================================
@dataclass(frozen=True)
class Weights:
    # Per-pillar CAPS. They sum above 100 on purpose: each pillar saturates, so no
    # realistic name maxes all of them. Final score is clamped 0-100.
    institutional_breadth: float = 36.0   # fund breadth x conviction x 13F recency
    insider_cluster: float = 36.0         # recency/size/seniority-weighted insider conviction
    dollar_conviction: float = 18.0       # log-scaled, recency-weighted $ insiders committed
    agreement_bonus: float = 18.0         # confluence multiplier, scaled by freshness + cluster
    # penalties (subtracted)
    institutional_trim_penalty: float = 14.0
    insider_sell_penalty: float = 14.0
    # saturation shape (fixed by default; rarely tuned)
    breadth_half: float = 1.5
    cluster_half: float = 1.2
    dollar_ceiling: float = 25_000_000.0

    # --- the subset the optimiser is allowed to move, with sane bounds -------
    TUNABLE = ("institutional_breadth", "insider_cluster", "dollar_conviction",
               "agreement_bonus", "institutional_trim_penalty", "insider_sell_penalty")
    BOUNDS = {
        "institutional_breadth": (10.0, 50.0),
        "insider_cluster": (10.0, 50.0),
        "dollar_conviction": (0.0, 35.0),
        "agreement_bonus": (0.0, 35.0),
        "institutional_trim_penalty": (0.0, 30.0),
        "insider_sell_penalty": (0.0, 30.0),
    }

    def replace(self, **kw) -> "Weights":
        from dataclasses import replace as _r
        return _r(self, **kw)


DEFAULT_WEIGHTS = Weights()


# ===========================================================================
# Side 1: institutions (fed from the existing 13F diff/store layer)
# ===========================================================================
@dataclass(frozen=True)
class InstitutionalSignal:
    ticker: str
    funds_accumulating: int = 0      # tracked funds that opened/added last quarter
    funds_trimming: int = 0          # tracked funds that exited/trimmed
    total_value_usd: float = 0.0     # aggregate reported $ across accumulating funds
    fund_labels: tuple[str, ...] = ()
    # --- conviction enrichment (optional; default to neutral) ---
    conviction_funds: int = 0        # funds where it's a top holding OR a brand-new position
    avg_weight_pct: float = 0.0      # avg portfolio weight (%) across accumulating funds
    quarters_ago: int = 0            # 0 = latest 13F quarter; older data decays

    @property
    def net_funds(self) -> int:
        return self.funds_accumulating - self.funds_trimming


# ===========================================================================
# Side 2: insiders (aggregated from Form 4s, with conviction features)
# ===========================================================================
@dataclass(frozen=True)
class InsiderBuyer:
    name: str
    role: str
    is_c_suite: bool
    shares: float
    value_usd: float
    stake_increase_pct: float = 0.0   # how much this buyer grew their position
    days_since_last_buy: int = 0


@dataclass(frozen=True)
class InsiderActivity:
    ticker: str
    issuer_name: str = ""
    window_days: int = 90
    buyers: tuple[InsiderBuyer, ...] = ()
    sellers: tuple[InsiderBuyer, ...] = ()
    buy_value_usd: float = 0.0
    sell_value_usd: float = 0.0
    # --- conviction features (computed in aggregate_insider_activity) ---
    conviction_units: float = 0.0          # recency x size x seniority, summed over buyers
    recency_weighted_buy_usd: float = 0.0  # $ bought, decayed by age
    days_since_last_buy: Optional[int] = None
    recent_cluster_n: int = 0              # distinct buyers inside the recent window
    max_stake_increase_pct: float = 0.0

    @property
    def n_buyers(self) -> int:
        return len(self.buyers)

    @property
    def n_c_suite_buyers(self) -> int:
        return sum(1 for b in self.buyers if b.is_c_suite)

    @property
    def is_cluster(self) -> bool:
        return self.n_buyers >= 2

    @property
    def net_value_usd(self) -> float:
        return self.buy_value_usd - self.sell_value_usd


# ---- helpers ---------------------------------------------------------------
def _to_date(s) -> Optional[date]:
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _saturating(n: float, half: float) -> float:
    """Map [0, inf) -> [0, 1) with diminishing returns; `half` is the 0.5 point."""
    if n <= 0:
        return 0.0
    return n / (n + half)


def _recency(days_since: float, halflife: float) -> float:
    if days_since <= 0:
        return 1.0
    return 0.5 ** (days_since / max(halflife, 1e-6))


def _sizing_mult(stake_pct: Optional[float], p: FeatureParams) -> float:
    if stake_pct is None or stake_pct <= 0:
        return 1.0  # unknown prior holdings -> neutral, don't penalise
    frac = _saturating(stake_pct, p.sizing_midpoint_pct)
    return p.sizing_floor + (p.sizing_ceiling - p.sizing_floor) * frac


def _seniority(form: Form4, p: FeatureParams) -> float:
    if form.is_c_suite:
        return p.seniority_csuite
    if form.is_officer:
        return p.seniority_officer
    if form.is_ten_percent_owner:
        return p.seniority_owner
    return p.seniority_director


def _txn_stake_pct(t: Form4Transaction) -> Optional[float]:
    """% increase in the insider's position from this buy. None if prior unknown."""
    prior = t.shares_owned_after - t.shares
    if prior > 0:
        return min(t.shares / prior, 3.0)   # cap at +300% so one tiny-base buy can't dominate
    return None


def aggregate_insider_activity(
    ticker: str,
    forms: Iterable[Form4],
    *,
    window_days: int = 90,
    issuer_name: str = "",
    as_of: Optional[date] = None,
    params: FeatureParams = DEFAULT_FEATURES,
) -> InsiderActivity:
    """
    Collapse Form 4s for one issuer into an InsiderActivity with conviction features.
    Only open-market intent (codes P / S) counts — grants / option exercises / tax
    withholding are excluded. Each buyer's purchases are summed; per-transaction we
    fold in recency (age decay), sizing (stake-increase) and the buyer's seniority.
    """
    today = as_of or date.today()
    name = issuer_name

    buys: dict[str, dict] = {}
    sells: dict[str, dict] = {}
    conviction_units = 0.0
    rweighted_usd = 0.0
    recent_buyers: set[str] = set()
    overall_last_days: Optional[int] = None
    max_stake = 0.0

    for f in forms:
        name = name or f.issuer_name
        senior = _seniority(f, params)
        key = f.owner_cik or f.owner_name

        for t in f.open_market_buys:
            d = _to_date(t.txn_date)
            days = max((today - d).days, 0) if d else window_days
            rec = _recency(days, params.recency_halflife_days)
            stake = _txn_stake_pct(t)
            size = _sizing_mult(stake, params)
            contrib = rec * size * senior

            slot = buys.setdefault(key, {
                "name": f.owner_name, "role": f.role_label, "c": f.is_c_suite,
                "sh": 0.0, "v": 0.0, "conv": 0.0, "rv": 0.0,
                "stake": 0.0, "last": days})
            slot["sh"] += t.shares
            slot["v"] += t.value_usd
            slot["conv"] += contrib
            slot["rv"] += t.value_usd * rec
            slot["stake"] = max(slot["stake"], stake or 0.0)
            slot["last"] = min(slot["last"], days)

            conviction_units += contrib
            rweighted_usd += t.value_usd * rec
            max_stake = max(max_stake, stake or 0.0)
            overall_last_days = days if overall_last_days is None else min(overall_last_days, days)
            if days <= params.recent_window_days:
                recent_buyers.add(key)

        for t in f.open_market_sells:
            slot = sells.setdefault(key, {
                "name": f.owner_name, "role": f.role_label, "c": f.is_c_suite,
                "sh": 0.0, "v": 0.0, "conv": 0.0, "rv": 0.0, "stake": 0.0, "last": 0})
            slot["sh"] += t.shares
            slot["v"] += t.value_usd

    def _mk(d: dict) -> tuple[InsiderBuyer, ...]:
        return tuple(
            InsiderBuyer(name=v["name"], role=v["role"], is_c_suite=v["c"],
                         shares=v["sh"], value_usd=v["v"],
                         stake_increase_pct=round(v.get("stake", 0.0), 4),
                         days_since_last_buy=int(v.get("last", 0)))
            for v in sorted(d.values(), key=lambda x: -x["v"])
        )

    return InsiderActivity(
        ticker=ticker.upper(),
        issuer_name=name,
        window_days=window_days,
        buyers=_mk(buys),
        sellers=_mk(sells),
        buy_value_usd=sum(v["v"] for v in buys.values()),
        sell_value_usd=sum(v["v"] for v in sells.values()),
        conviction_units=round(conviction_units, 4),
        recency_weighted_buy_usd=round(rweighted_usd, 2),
        days_since_last_buy=overall_last_days,
        recent_cluster_n=len(recent_buyers),
        max_stake_increase_pct=round(max_stake, 4),
    )


# ===========================================================================
# The combined signal
# ===========================================================================
QUADRANTS = {
    "conviction": "Conviction",
    "institutional": "Institutional bid",
    "insider": "Insider conviction",
    "distribution": "Distribution",
    "divergent": "Divergent",
    "neutral": "Neutral",
}


@dataclass
class ConfluenceSignal:
    ticker: str
    issuer_name: str
    score: float
    quadrant: str
    verdict: str
    rationale: str
    institutional: InstitutionalSignal
    insider: InsiderActivity
    x: float = 0.0
    y: float = 0.0
    breakdown: dict = field(default_factory=dict)   # pillar-by-pillar contribution

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer_name": self.issuer_name,
            "score": round(self.score, 1),
            "quadrant": self.quadrant,
            "quadrant_label": QUADRANTS.get(self.quadrant, "—"),
            "verdict": self.verdict,
            "rationale": self.rationale,
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "breakdown": {k: round(v, 1) for k, v in self.breakdown.items()},
            "institutional": {
                "funds_accumulating": self.institutional.funds_accumulating,
                "funds_trimming": self.institutional.funds_trimming,
                "net_funds": self.institutional.net_funds,
                "conviction_funds": self.institutional.conviction_funds,
                "avg_weight_pct": round(self.institutional.avg_weight_pct, 2),
                "quarters_ago": self.institutional.quarters_ago,
                "total_value_usd": round(self.institutional.total_value_usd, 2),
                "fund_labels": list(self.institutional.fund_labels),
            },
            "insider": {
                "window_days": self.insider.window_days,
                "n_buyers": self.insider.n_buyers,
                "n_c_suite_buyers": self.insider.n_c_suite_buyers,
                "is_cluster": self.insider.is_cluster,
                "recent_cluster_n": self.insider.recent_cluster_n,
                "days_since_last_buy": self.insider.days_since_last_buy,
                "max_stake_increase_pct": round(self.insider.max_stake_increase_pct, 4),
                "conviction_units": round(self.insider.conviction_units, 3),
                "buy_value_usd": round(self.insider.buy_value_usd, 2),
                "recency_weighted_buy_usd": round(self.insider.recency_weighted_buy_usd, 2),
                "sell_value_usd": round(self.insider.sell_value_usd, 2),
                "net_value_usd": round(self.insider.net_value_usd, 2),
                "buyers": [
                    {"name": b.name, "role": b.role, "is_c_suite": b.is_c_suite,
                     "value_usd": round(b.value_usd, 2),
                     "stake_increase_pct": b.stake_increase_pct,
                     "days_since_last_buy": b.days_since_last_buy}
                    for b in self.insider.buyers
                ],
            },
        }


def score_confluence(
    inst: InstitutionalSignal,
    insider: InsiderActivity,
    *,
    weights: Weights = DEFAULT_WEIGHTS,
) -> ConfluenceSignal:
    w = weights

    # --- pillar 1: institutional = breadth x conviction x 13F-recency ---
    conv_mult = (1.0
                 + 0.5 * _saturating(inst.conviction_funds, 2.0)   # high-conviction funds
                 + 0.5 * min(1.0, inst.avg_weight_pct / 5.0))       # avg 5% weight -> max kick
    q_decay = 0.6 ** max(inst.quarters_ago, 0)
    inst_pos = _saturating(inst.funds_accumulating * conv_mult, w.breadth_half) * q_decay
    s_inst = w.institutional_breadth * inst_pos

    # --- pillar 2: insider conviction (recency/size/seniority already baked in) ---
    insider_pos = _saturating(insider.conviction_units, w.cluster_half)
    s_insider = w.insider_cluster * insider_pos

    # --- pillar 3: recency-weighted dollars (log scale) ---
    dollars = max(insider.recency_weighted_buy_usd, 0.0)
    s_dollar = (w.dollar_conviction * math.log10(dollars + 1) / math.log10(w.dollar_ceiling + 1)
                if dollars > 0 else 0.0)
    s_dollar = min(s_dollar, w.dollar_conviction)

    # --- pillar 4: agreement, scaled by freshness + cluster ---
    both_buying = inst.funds_accumulating > 0 and insider.n_buyers > 0
    if both_buying:
        days = insider.days_since_last_buy if insider.days_since_last_buy is not None else 999
        fresh = _recency(days, DEFAULT_FEATURES.recency_halflife_days)
        clust = _saturating(insider.recent_cluster_n, 1.0)
        s_agree = w.agreement_bonus * (0.5 + 0.5 * max(fresh, clust))
    else:
        s_agree = 0.0

    # --- penalties ---
    penalty = 0.0
    if inst.funds_trimming > inst.funds_accumulating:
        penalty += w.institutional_trim_penalty * _saturating(inst.funds_trimming, 3.0)
    if insider.sell_value_usd > insider.buy_value_usd and insider.sell_value_usd > 0:
        penalty += w.insider_sell_penalty * _saturating(len(insider.sellers), 2.0)

    raw = s_inst + s_insider + s_dollar + s_agree - penalty
    score = max(0.0, min(100.0, raw))

    breakdown = {
        "institutional": s_inst, "insider": s_insider, "dollars": s_dollar,
        "agreement": s_agree, "penalty": -penalty,
    }

    x = _signed_saturating(inst.net_funds, 3.0)
    y = _signed_saturating(_net_buyers(insider), 2.0)
    quadrant, verdict, rationale = _classify(inst, insider)

    return ConfluenceSignal(
        ticker=insider.ticker or inst.ticker, issuer_name=insider.issuer_name,
        score=score, quadrant=quadrant, verdict=verdict, rationale=rationale,
        institutional=inst, insider=insider, x=x, y=y, breakdown=breakdown,
    )


def _net_buyers(insider: InsiderActivity) -> float:
    return insider.n_buyers - len(insider.sellers)


def _signed_saturating(n: float, half: float) -> float:
    if n == 0:
        return 0.0
    sign = 1.0 if n > 0 else -1.0
    return sign * _saturating(abs(n), half)


def _classify(inst: InstitutionalSignal, insider: InsiderActivity) -> tuple[str, str, str]:
    inst_buy = inst.funds_accumulating > 0 and inst.net_funds >= 0
    inst_sell = inst.net_funds < 0
    ins_buy = insider.n_buyers > 0 and insider.net_value_usd >= 0
    ins_sell = insider.net_value_usd < 0 and insider.sell_value_usd > 0

    def usd(v: float) -> str:
        if v >= 1e6:
            return f"${v/1e6:.1f}M"
        if v >= 1e3:
            return f"${v/1e3:.0f}K"
        return f"${v:.0f}"

    fresh = ""
    if insider.days_since_last_buy is not None and insider.n_buyers:
        fresh = f" (freshest buy {insider.days_since_last_buy}d ago)"

    if inst_buy and ins_buy:
        csuite = f", incl. {insider.n_c_suite_buyers} C-suite" if insider.n_c_suite_buyers else ""
        return ("conviction", "Conviction",
                f"{inst.funds_accumulating} fund(s) accumulating while {insider.n_buyers} "
                f"insider(s) bought {usd(insider.buy_value_usd)} open-market{csuite}{fresh}.")
    if inst_sell and ins_sell:
        return ("distribution", "Distribution",
                f"Funds trimming and insiders selling {usd(insider.sell_value_usd)} — smart money exiting.")
    if (inst_buy and ins_sell) or (inst_sell and ins_buy):
        return ("divergent", "Divergent", "Institutions and insiders disagree — no confluence.")
    if inst_buy:
        return ("institutional", "Institutional bid",
                f"{inst.funds_accumulating} fund(s) accumulating; no insider buying yet.")
    if ins_buy:
        return ("insider", "Insider conviction",
                f"{insider.n_buyers} insider(s) bought {usd(insider.buy_value_usd)}{fresh}; funds not yet in.")
    return ("neutral", "Neutral", "No meaningful accumulation on either side.")


def build_confluence(
    institutional: dict[str, InstitutionalSignal],
    insider: dict[str, InsiderActivity],
    *,
    weights: Weights = DEFAULT_WEIGHTS,
    min_score: float = 0.0,
) -> list[ConfluenceSignal]:
    """Join the two sides on ticker; return signals ranked by score (desc)."""
    tickers = set(institutional) | set(insider)
    signals: list[ConfluenceSignal] = []
    for t in tickers:
        inst = institutional.get(t, InstitutionalSignal(ticker=t))
        ins = insider.get(t, InsiderActivity(ticker=t))
        sig = score_confluence(inst, ins, weights=weights)
        if sig.score >= min_score:
            signals.append(sig)
    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
