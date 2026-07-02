# Confluence — Form 4 × 13F (13FLOW)

Surfaces the names where **superinvestor 13F accumulation** and **open-market insider
buying** coincide, scores the overlap with transparent heuristic defaults, and ranks it.
Reuses the existing EDGAR etiquette (UA + 8 req/s), `defusedxml` hardening, the dashboard
theme and `esc()`.

## Files added
| File | Role |
|---|---|
| `smartmoney/forms4.py` | Form 4 discovery (by issuer CIK) + ownership-XML parser → typed `Form4` / `Form4Transaction`. |
| `smartmoney/crosssignal.py` | Confluence engine. `FeatureParams` (extraction) + `Weights` (combination) → scored, classified `ConfluenceSignal` with a per-pillar `breakdown`. |
| `smartmoney/backtest.py` | Rank-IC / quantile-spread / hit-rate evaluation + coordinate-ascent weight optimiser. Synthetic demo exercises the harness; it is not live-history validation. |
| `smartmoney/api_signals.py` | Read-only Flask blueprint `GET /api/signals/confluence`; `StoreConfluenceProvider` (real) + `SampleConfluenceProvider` (demo). |
| `smartmoney/sample_confluence.py` | Synthetic data routed through the real pipeline so the UI/endpoint run with no DB or network. |
| `tests/test_forms4_offline.py` | Offline tests: parsing, recency/sizing features, ranking, and the optimiser. |
| `dashboard_confluence.html` | Standalone "Confluence" screen with the score-weighting decomposition; live API + embedded fallback. |

## How the signal is built
1. **Institutions** — from your 13F diff layer: funds opening/adding (`funds_accumulating`)
   vs trimming/exiting (`funds_trimming`), plus optional conviction enrichment
   (`conviction_funds` = funds where it's a top/new position, `avg_weight_pct`, `quarters_ago`).
2. **Insiders** — `forms4.insider_filings(issuer_cik, window_days)` pulls Form 4s; only
   **open-market P/S** count. `aggregate_insider_activity()` folds in three conviction features:
   - **Recency** — each buy decays with age (30-day half-life by default), so a 2-day-old buy
     dominates an 80-day-old one. Drives `conviction_units`, `recency_weighted_buy_usd`, `days_since_last_buy`.
   - **Buy size** — % stake increase from `sharesOwnedFollowingTransaction`; a purchase that
     materially grows the insider's position outweighs a token buy.
   - **Cluster timing** — distinct buyers inside a 14-day window (`recent_cluster_n`).
   - **Seniority** — C-suite > officer > 10% owner > director.
3. **Score (0–100)** — `score_confluence()` combines four saturating pillars and exposes the
   contribution of each in `breakdown`: institutional (breadth × conviction × 13F-recency),
   insider conviction, recency-weighted dollars, and an agreement bonus scaled by freshness +
   cluster. Penalties apply for net-trimming funds or net-selling insiders. Quadrant:
   *Conviction / Institutional bid / Insider conviction / Distribution / Divergent / Neutral*.

## Two-layer scoring: features vs weights
`FeatureParams` controls **what the signal measures** (half-life, sizing curve, seniority
multipliers) — judgement parameters until sensitivity tables are published. `Weights`
controls **how the pillars combine**. The shipped defaults are heuristic, not calibrated
on published live history. The split means you can fit alternative weights without
re-deriving features, and vice versa, but fitted weights must carry their train/validation/
test split and out-of-sample evidence.

## Validation and weight research (backtest.py)
Feed point-in-time historical observations to evaluate the hypothesis or fit research
weights against forward returns:
```python
from smartmoney.backtest import Observation, evaluate, optimize_weights
# build from your store: features snapshotted at a past as-of date, joined to the
# realised forward return over your horizon (next quarter), via Massive Market Data / stooq.
obs = [Observation(inst=..., insider=..., fwd_return=0.12), ...]
print(evaluate(obs))                       # baseline rank-IC / quantile spread / hit-rate
best_weights, report = optimize_weights(obs)   # coordinate ascent within Weights.BOUNDS
```
`evaluate` reports the **Spearman rank-IC** (does a higher score mean a higher forward
return?), the **top-minus-bottom quantile spread**, and the top-quantile **hit rate**.
`optimize_weights` does derivative-free coordinate ascent over the six tunable weights.
Run `python -m smartmoney.backtest` for a synthetic demo that recovers a planted relationship
(IC ≈ 0.89 → 0.91, with the optimiser correctly pushing the insider weight to its ceiling).
The shape/feature params stay fixed during weight optimisation by design.

> The synthetic IC is high because the demo's returns are mostly signal; on real data expect a
> modest IC (single-digit % to ~0.1 is already useful in cross-sectional equity screens). The
> point of the harness is the *workflow* and the *relative* before/after comparison, not the level.

Current production wording is therefore: **backtest harness available; default weights are
heuristic**. Do not describe the live Confluence score as validated, calibrated, or
backtested until a frozen score version has passed `VALIDATION_PROTOCOL.md` with published
train, validation, and out-of-sample results.

## Wire-in (two steps)

**1. Serve the endpoint.** In your `api.create_app`:
```python
from .api_signals import make_signals_blueprint, StoreConfluenceProvider
from .forms4 import Form4Client

f4 = Form4Client(client=edgar_client)          # reuses your EdgarClient session + limiter
provider = StoreConfluenceProvider(store, f4)
app.register_blueprint(make_signals_blueprint(provider))
```
Then adapt two small methods in `StoreConfluenceProvider` to your `store.py` schema:
`consensus_accumulation()` (you already compute adds/trims per ticker for the Consensus
screen) and `ticker_cik_map(tickers)` (ticker → issuer CIK; you have CIKs from the 13F side
and tickers from OpenFIGI enrichment, so this is a join you already have the pieces for).

To preview immediately with no wiring, use `SampleConfluenceProvider()` instead.

**2. Add the screen.** Serve `dashboard_confluence.html` (or fold it in as a 5th nav pill
next to Consensus / Funds / Compare / Alerts). It calls `GET /api/signals/confluence?window=N`
and falls back to embedded sample data when the API isn't reachable, exactly like your main
dashboard. All injected text goes through `esc()`.

## Endpoint
```
GET /api/signals/confluence?window=90&min_score=0
→ { "kpis": {...}, "signals": [ ConfluenceSignal.to_dict(), ... ] }
```
`window` clamps to 7–365 days; `min_score` to 0–100.

## Gotchas already handled
- **Open-market only**: P/S count; A/M/F/G/C are parsed but excluded from the signal.
- **C-suite detection** from `officerTitle` (CEO/CFO/President/Chair) drives the seniority weight.
- **One bad filing won't sink the batch** — `insider_filings()` skips unparseable docs.
- **Namespace-agnostic, XXE-hardened** XML parsing (`defusedxml`), same as the 13F parser.
- **Document discovery**: prefers the package doc typed `4`, falls back to the first ownership `.xml`.

## Worth knowing (not yet handled)
- **Form 4 indexing by issuer** uses browse-edgar's Atom feed. For very high-volume issuers,
  paginate (`count`/`start`) or switch to the quarterly *Insider Transactions* flat files for backfill.
- **Joint/multi-owner filings** are attributed to the first reporting owner; rare, low impact.
- **Tickers vs CIKs**: the join relies on your existing CUSIP→ticker→CIK mapping; no-match names
  simply won't get an insider rail (they degrade to single-rail institutional signals).
- **Validation boundary**: this is a transparent **screen**, not investment advice and not a
  validated expected-return model until the protocol evidence is published.
