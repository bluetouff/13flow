"""
Backtest harness for the Confluence score (13FLOW).

The score combines pillars with weights (crosssignal.Weights). This module answers the
only question that matters once a point-in-time dataset exists: *do higher-scored names
actually outperform afterwards?* It can also fit alternative weights on a declared
training sample.

Metrics (all rank-based, so they don't care about score scale):
  - rank IC: Spearman correlation between score and forward return. The headline number.
  - quantile spread: mean forward return of the top quantile minus the bottom quantile.
  - hit rate: share of top-quantile names with positive forward return.

Optimiser: coordinate ascent over Weights.TUNABLE within Weights.BOUNDS, maximising a
blended objective (IC + a slice of quantile spread). Robust, derivative-free, and easy
to reason about for six parameters. Optimised weights are research output, not product
truth, until they survive a frozen validation and out-of-sample test.

DATA CONTRACT — you supply observations from your own store:
    Observation(inst=InstitutionalSignal, insider=InsiderActivity, fwd_return=float)
where fwd_return is the realised return over your chosen horizon AFTER the as-of date the
features were computed (e.g. the next quarter). Build these by snapshotting features at
historical filing dates and joining to forward prices (Massive Market Data / stooq).

There is no look-ahead help here and no network. Feed it real history to evaluate a
hypothesis; publish the feature version, split, baselines, costs, and out-of-sample
results before calling any weight set validated.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .crosssignal import (
    DEFAULT_WEIGHTS, InsiderActivity, InsiderBuyer, InstitutionalSignal,
    Weights, score_confluence,
)


@dataclass(frozen=True)
class Observation:
    inst: InstitutionalSignal
    insider: InsiderActivity
    fwd_return: float          # realised forward return, e.g. +0.12 for +12%


# ---------------------------------------------------------------------------
# rank statistics (no numpy / scipy dependency)
# ---------------------------------------------------------------------------
def _avg_ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0           # 1-based average rank over the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb)


def spearman_ic(scores: Sequence[float], returns: Sequence[float]) -> float:
    return _pearson(_avg_ranks(scores), _avg_ranks(returns))


def quantile_spread(scores, returns, q: int = 5) -> float:
    paired = sorted(zip(scores, returns), key=lambda p: p[0])
    n = len(paired)
    if n < q:
        return 0.0
    bucket = max(1, n // q)
    bottom = [r for _, r in paired[:bucket]]
    top = [r for _, r in paired[-bucket:]]
    return (sum(top) / len(top)) - (sum(bottom) / len(bottom))


def hit_rate(scores, returns, q: int = 5) -> float:
    paired = sorted(zip(scores, returns), key=lambda p: p[0])
    n = len(paired)
    if n < q:
        return 0.0
    bucket = max(1, n // q)
    top = [r for _, r in paired[-bucket:]]
    return sum(1 for r in top if r > 0) / len(top)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(obs: Sequence[Observation], weights: Weights = DEFAULT_WEIGHTS,
             q: int = 5) -> dict:
    scores = [score_confluence(o.inst, o.insider, weights=weights).score for o in obs]
    rets = [o.fwd_return for o in obs]
    return {
        "n": len(obs),
        "ic": round(spearman_ic(scores, rets), 4),
        "quantile_spread": round(quantile_spread(scores, rets, q), 4),
        "hit_rate": round(hit_rate(scores, rets, q), 4),
    }


def _objective(obs, weights, q, ic_w=1.0, spread_w=0.5) -> float:
    scores = [score_confluence(o.inst, o.insider, weights=weights).score for o in obs]
    rets = [o.fwd_return for o in obs]
    return ic_w * spearman_ic(scores, rets) + spread_w * quantile_spread(scores, rets, q)


def optimize_weights(
    obs: Sequence[Observation],
    base: Weights = DEFAULT_WEIGHTS,
    *,
    q: int = 5,
    passes: int = 6,
    verbose: bool = False,
) -> tuple[Weights, dict]:
    """
    Coordinate ascent over Weights.TUNABLE within Weights.BOUNDS. Multiplicative-ish
    steps that shrink each pass. Returns (best_weights, before/after metrics).
    """
    best = base
    best_obj = _objective(obs, best, q)
    start_metrics = evaluate(obs, base, q)
    steps = [8.0, 4.0, 2.0, 1.0]

    for p in range(passes):
        step = steps[min(p, len(steps) - 1)]
        improved = False
        for name in Weights.TUNABLE:
            lo, hi = Weights.BOUNDS[name]
            cur = getattr(best, name)
            for delta in (step, -step):
                cand_val = min(hi, max(lo, cur + delta))
                if cand_val == cur:
                    continue
                cand = best.replace(**{name: cand_val})
                obj = _objective(obs, cand, q)
                if obj > best_obj + 1e-9:
                    best, best_obj, improved = cand, obj, True
                    if verbose:
                        print(f"  pass {p} {name}: {cur:.1f}->{cand_val:.1f}  obj={obj:.4f}")
        if not improved:
            break

    end_metrics = evaluate(obs, best, q)
    return best, {"before": start_metrics, "after": end_metrics,
                  "weights": {k: round(getattr(best, k), 2) for k in Weights.TUNABLE}}


# ---------------------------------------------------------------------------
# synthetic data — proves the harness recovers a known relationship offline
# ---------------------------------------------------------------------------
def make_synthetic(n: int = 400, seed: int = 7,
                   true_w=(0.6, 1.4, 0.5), noise: float = 0.6) -> list[Observation]:
    """
    Build n observations whose forward return is a noisy function of latent drivers,
    with insider conviction (true_w[1]) deliberately mattering MORE than the default
    weights assume. A good optimiser should lift the insider weight and improve IC.
    """
    rng = random.Random(seed)
    obs: list[Observation] = []
    a, b, c = true_w
    for _ in range(n):
        funds = rng.choices([0, 1, 2, 3, 4, 5], weights=[3, 4, 4, 3, 2, 1])[0]
        n_buy = rng.choices([0, 1, 2, 3], weights=[5, 4, 2, 1])[0]
        n_cs = min(n_buy, rng.choices([0, 1, 2], weights=[5, 3, 1])[0])
        conv = round(n_buy * (1.0 + 0.4 * n_cs) * rng.uniform(0.4, 1.3), 3)
        dollars = (rng.uniform(0.05, 12) * 1e6) if n_buy else 0.0
        days = rng.randint(1, 88) if n_buy else None
        recent = 1 if (days is not None and days <= 14 and n_buy) else 0

        inst = InstitutionalSignal(
            ticker="SYN", funds_accumulating=funds,
            funds_trimming=rng.choices([0, 0, 0, 1, 2], weights=[6, 0, 0, 2, 1])[0],
            conviction_funds=min(funds, rng.randint(0, 2)),
            avg_weight_pct=round(rng.uniform(0, 6), 2) if funds else 0.0,
        )
        insider = InsiderActivity(
            ticker="SYN",
            buyers=tuple(InsiderBuyer("x", "Officer", k < n_cs, 0, dollars / max(n_buy, 1))
                         for k in range(n_buy)),
            buy_value_usd=dollars, conviction_units=conv,
            recency_weighted_buy_usd=dollars * (0.5 if not days else 0.5 ** (days / 30)),
            days_since_last_buy=days, recent_cluster_n=recent,
        )
        # latent "truth": insider conviction weighted heavily, plus breadth & dollars
        latent = (a * funds
                  + b * conv
                  + c * (math.log10(dollars + 1) / 7.0))
        fwd = latent * 0.03 + rng.gauss(0, noise) * 0.03
        obs.append(Observation(inst=inst, insider=insider, fwd_return=round(fwd, 4)))
    return obs


def demo() -> dict:
    obs = make_synthetic()
    base = evaluate(obs, DEFAULT_WEIGHTS)
    tuned, report = optimize_weights(obs)
    return {"baseline": base, "tuned": report["after"], "tuned_weights": report["weights"]}


if __name__ == "__main__":
    import json
    print(json.dumps(demo(), indent=2))
