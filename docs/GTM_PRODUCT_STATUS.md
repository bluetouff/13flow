# 13FLOW go-to-market product status

This document is the operator-facing boundary between what 13FLOW can sell now,
what is implemented but still gated, and what must not be claimed before the
evidence exists.

## Current sellable surface

13FLOW can be positioned as a professional, reproducible SEC EDGAR 13F data
product with:

- verifiable LIVE public state via `/api/live-status` and `/api/version`;
- read-only public JSON endpoints over tracked 13F funds, filings, holders,
  data-quality warnings and methodology contracts;
- static crawler-friendly pages for funds, stocks and signals;
- a scoped Pro API with API-key authentication, persistent rate limits and audit
  rows;
- a read-only MCP server whose public tools are free and whose Pro tools fail
  closed without a Pro key or configured x402 settlement;
- append-only Confluence signal history and a frozen Confluence v1 methodology
  contract;
- offline preflight and public smoke gates.

Pricing notes, prospect emails and marketing correspondence are intentionally
kept out of this repository. GitHub should only carry product boundaries,
operator runbooks, public truth surfaces and verification gates.

The maintainability boundary for what belongs in the controlled pilot is
`docs/CORE_V1_BOUNDARY.md`. Use it before adding any new public, Pro or admin
surface.

Machine-readable product status:

```bash
curl -fsS https://13flow.eu/api/product-status | python3 -m json.tool
```

Human-readable validation evidence page:

```bash
curl -fsS https://13flow.eu/validation
```

## Claims not allowed yet

Do not claim:

- validated alpha;
- a calibrated probability;
- an expected-return model;
- a complete insider-only or distribution universe;
- a complete fund portfolio view, including shorts, non-US books, bonds,
  intra-quarter trading, full derivative exposure or confidential-treatment
  omissions;
- fully modeled Form 4 derivative Table II exposure, 10b5-1 plan flags,
  multi-owner attribution or weighted-average price footnotes;
- production x402 paid access;
- full 2013-2026 quantitative validation.

The current Confluence score is an ordinal heuristic rank. The correct wording is
`backtest harness available; default weights are heuristic`.

## Data scope boundary

13FLOW can sell source-linked workflow and review evidence, not omniscient
ownership data.

- Form 13F is delayed, quarterly, long-US-reportable-securities disclosure. It
  is not a complete view of a fund's shorts, international book, bonds, full
  derivatives or intra-quarter trading.
- Production Confluence uses a bounded Form 4 issuer universe controlled by the
  tracked 13F activity threshold. Insider-only and distribution quadrants are
  useful labels inside that bounded universe, not exhaustive market scans.
- Current Form 4 processing is suitable for normalized Table I open-market
  activity review. Table II derivative rows, 10b5-1 plan flags, multi-owner
  attribution and weighted-average price footnotes remain explicit limitations
  until separately modeled and validated.

## Quantitative validation status

Current milestone:

- price pipeline: validated on a 25-ticker sample;
- Form 4 artifact: validated on a 25-ticker sample with one issuer CIK per
  ticker and zero duplicate/invalid transaction rows;
- mature joined feature artifact:
  `/var/lib/13flow/confluence_features_liquid25_v2_mature.csv`;
- mature joined feature artifact SHA256:
  `3ab0cebaf893520580d5dc9ae338dbcb5c8344efdb6aeb990dc4af7936f456b9`;
- artifact status: `minimum_schema_valid_metrics_unreviewed`;
- evidence review: `mechanical_evidence_ready_for_review`;
- artifact scope: 25 tickers, 125 rows, `13f_form4_joined`, 100% forward-return
  coverage for 20d/60d/120d, zero row errors;
- full validation: blocked until broader/full-universe adjusted-price and Form 4
  artifacts are reviewed for price source, delisting treatment, costs,
  liquidity and no-lookahead controls.

Do not relaunch external historical-price scraping or Form 4 fan-out loops from
production. Use bulk vendor exports or locally prepared files.

Expected full price file:

```csv
ticker,date,adj_close
AAPL,2013-01-02,16.687
AAPL,2013-01-03,16.475
```

Install and validate the imported file:

```bash
sudo install -o flowingest -g flowapp -m 640 \
  /tmp/validation_prices_full.csv \
  /var/lib/13flow/validation_prices_full.csv

sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --validate-price-csv /var/lib/13flow/validation_prices_full.csv \
  --validation-tickers /var/lib/13flow/validation_tickers_priceable.txt \
  --validation-start 2013-01-01 \
  --validation-end 2026-07-02 \
  --validation-json
```

Expected normalized Form 4 transaction file:

```csv
ticker,accession,filing_date,transaction_date,owner_cik,owner_name,officer_title,is_officer,transaction_code,acquired_disposed,shares,price_per_share,shares_owned_after
AAPL,0000320193-26-000004,2026-05-04,2026-05-02,0000000001,Example CEO,Chief Executive Officer,1,P,A,10000,180.00,50000
```

Only after that validation passes should the full point-in-time feature dataset
be rebuilt and evaluated. A 25-ticker mature artifact can be described as
mechanically schema-valid and ready for human review, but it is still not a
public validation or alpha claim.

For the post-run operator sequence after a long Form 4 export, use
`docs/POST_RUN_FORM4_VALIDATION.md` before building or publishing the joined
Confluence artifact.

## External API safety

Default operator policy:

- small samples first;
- explicit sleep and retry/backoff;
- honor `Retry-After`;
- resumable exports only;
- stop after repeated provider failures;
- never loop Yahoo, Stooq, Massive, SEC or EDGAR from production to force a
  missing historical dataset.

## Deployment gate

Every production deploy must end with:

```bash
curl -fsS https://13flow.eu/api/version
EXPECTED_SHA="$SHA" sudo /opt/13flow/deploy/smoke-public.sh
curl -fsS http://127.0.0.1:8849/healthz | python3 -m json.tool
curl -fsS https://13flow.eu/api/product-status | python3 -m json.tool
```

The product status endpoint is part of the commercial truth surface. If it says
`mechanical_evidence_ready_for_review_metrics_unreviewed`, sales and
documentation must not describe Confluence as fully validated. The correct
wording is: 25-ticker mature 13F + Form 4 joined validation artifact is
mechanically schema-valid and ready for human review; metrics remain unreviewed
and are not an alpha claim.
