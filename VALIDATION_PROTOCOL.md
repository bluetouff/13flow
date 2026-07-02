# Confluence Validation Protocol

This document defines the evidence required before any Confluence score version can be
called calibrated, validated, or backtested on live history.

## Current status

- **Feature engine:** implemented and transparent.
- **Backtest harness:** available in `smartmoney/backtest.py`.
- **Frozen score contract:** `confluence_v1`, published in `docs/confluence_v1.json`
  and `GET /api/methodology/confluence-v1`.
- **Default weights:** heuristic judgement parameters, frozen as
  `heuristic_default_v1`.
- **Published live-history validation:** not yet available.
- **Signal revision history:** append-only JSONL contract available via
  `confluence-history.jsonl` and `GET /api/signals/confluence/history`.
- **Product wording allowed today:** "backtest harness available; default weights are
  heuristic".
- **Product wording reserved for later:** "validated", "calibrated", "backtested
  strategy", or any equivalent claim, until the evidence below is published for a frozen
  version.

## Evidence boundary

Every published score version must separate three layers:

1. **Hypothesis** - feature definitions, default weights, effective universe, and known
   limitations.
2. **Calibration** - any fitted parameter or weight, with its training window, objective,
   constraints, and feature version.
3. **Result** - out-of-sample metrics computed after calibration is frozen.

If a layer is missing, the API and documentation must say so.

## Point-in-time dataset

The validation dataset must be reproducible from scripts and hashes, or published as a
feature table when licensing allows. It must include:

- as-of timestamp for every 13F filing, amendment, Form 4 filing, issuer mapping, and
  price observation used by the feature builder;
- issuer universe and inclusion/exclusion rules;
- liquidity rules, minimum price, minimum dollar volume, and corporate-action handling;
- adjusted prices and delisting treatment where available;
- feature schema version, code commit, default/fitted weights version, and parameter file
  hash;
- source provenance for EDGAR filings, price data, and any mapping table.

The builder must reconstruct signals only from data available at the as-of date. Later
filings, later amendments, later mappings, survivorship-only universes, and future prices
must not leak into historical features.

The point-in-time row schema is defined in `docs/CONFLUENCE_V1.md`. The minimum identity
columns are `as_of`, `ticker`, `score_version`, `feature_schema_version`,
`weight_version`, `parameter_hash`, source accession hashes, and code commit.

## Frozen split

Use a calendar split first, then add walk-forward analysis:

| Period | Role | Rule |
|---|---|---|
| 2014-01-01 to 2022-12-31 | Train | Feature experiments and weight fitting allowed. |
| 2023-01-01 to 2024-12-31 | Validation | Model selection and sensitivity review allowed. |
| 2025-01-01 to 2026-12-31 | Test | Frozen once; no parameter changes after looking. |

If the live history is too sparse, report the coverage limitation instead of moving the
test boundary after the fact.

## Execution assumptions

Backtests must declare:

- signal availability lag from filing acceptance to tradable decision;
- rebalance frequency and holding windows;
- execution price convention;
- transaction costs, slippage, and turnover;
- handling of names without price data;
- long-only, long-short, sector-neutral, beta-neutral, or other portfolio construction
  constraints.

## Required metrics

Measure each horizon at 20, 60, and 120 trading days:

- rank IC and confidence interval;
- top-minus-bottom spread;
- hit rate;
- drawdown and volatility for any portfolio construction;
- turnover and capacity/liquidity diagnostics;
- coverage: number of names, sectors, market-cap buckets, and calendar periods;
- stability by year, sector, market-cap bucket, and market regime.

## Baselines

Compare the full score against simple alternatives:

- insider-only rail;
- institutional breadth or fund-count-only rail;
- Confluence quadrant without numeric score;
- equal-weight top names;
- random/permutation baseline;
- sector, size, beta, and liquidity neutralized variants where relevant.

Publish results for both default heuristic weights and optimized weights. Do not select the
reported weight set after seeing test results.

## Statistical controls

At minimum:

- bootstrap or block-bootstrap confidence intervals;
- permutation tests on returns or labels;
- walk-forward stability tests;
- sensitivity tables for feature half-life, seniority, dollar cap, saturation, agreement
  bonus, and penalty weights;
- multiple-testing disclosure when several horizons, universes, or objectives are tried.

## Version log

Each released score must record:

- feature schema version;
- parameter file hash;
- weight version and whether weights are default or optimized;
- training, validation, and test date ranges;
- code commit;
- dataset hash or feature-table hash;
- metrics artifact hash.

The term "validated" is reserved for a frozen version that passes this protocol on the
declared out-of-sample test.

## Publication surfaces

The public and Pro surfaces must make the validation boundary machine-readable:

- `GET /api/methodology/confluence-v1` exposes the frozen parameters, universe,
  train/validation/test split and parameter hash.
- `GET /api/signals/confluence` exposes current signal payloads and methodology metadata.
- `GET /api/signals/confluence/history` exposes append-only revisions generated from
  cached/live Confluence snapshots.
- `docs/CONFLUENCE_V1.md` describes the point-in-time row schema, baselines, metrics and
  operator commands.

If any of these surfaces is absent or stale, the product must not claim that Confluence is
validated or calibrated.
