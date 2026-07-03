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

The offline gate is intentionally strict. A dataset is not publishable unless it carries
the frozen Confluence version fields, the parameter hash and all three forward-return
horizons:

```bash
python run.py --db /var/lib/13flow/13flow.db \
  --build-validation-dataset /var/lib/13flow/confluence_features.csv \
  --validation-prices /path/to/adjusted_prices.csv \
  --validation-form4 /path/to/normalized_form4_transactions.csv \
  --validation-code-commit "$SHA" \
  --validation-json
```

```bash
python run.py --validation-dataset /path/to/confluence_features.csv --validation-json
```

The first command builds a local point-in-time feature table from the 13F database,
an optional adjusted-price CSV and an optional normalized Form 4 CSV/JSONL artifact.
The second command gates an existing dataset. They emit:

- the dataset SHA256 hash;
- row count, ticker count, date range and train/validation/test split counts;
- missing required and recommended columns;
- version mismatches against `confluence_v1`, `confluence_features_v1` and
  `heuristic_default_v1`;
- rank IC, top-minus-bottom spread, hit rate, bootstrap confidence intervals and
  permutation p-values for the full score and available baselines.

`status=minimum_schema_valid_metrics_unreviewed` means the file passed the mechanical gate.
It does **not** mean the score is validated: the dataset builder, no-lookahead controls,
price source, delisting handling, costs, liquidity filters and neutralization still require
review before publication. `status=not_publishable` means no public performance claim is
allowed from that artifact.

Without `--validation-form4`, the builder exports the institutional 13F side
(`feature_scope=13f_only_no_form4`). With `--validation-form4`, it exports
`feature_scope=13f_form4_joined`, hashes the joined Form 4 accessions and fills insider
features such as open-market buyer count, buy value and insider score. The Form 4 join is
strictly point-in-time: a Form 4 is eligible only if its filing date is on or before the row
`as_of` date, and only transactions inside the trailing Form 4 window enter the score. This
is enough to test the complete Confluence feature contract, but still not enough for a
public validation claim until the Form 4 artifact coverage, price source, delisting handling,
costs and no-lookahead controls are reviewed.

The normalized Form 4 artifact can be built from SEC EDGAR separately from the offline
dataset builder:

```bash
SEC_UA='13FLOW/1.0 contact@example.com' python run.py \
  --build-validation-form4 \
  --validation-tickers /var/lib/13flow/validation_tickers_sample25.txt \
  --validation-form4-out /var/lib/13flow/validation_form4_sample25.csv \
  --validation-start 2024-07-03 \
  --validation-end 2026-07-02 \
  --validation-form4-sleep-sec 2 \
  --validation-form4-max-tickers 1 \
  --validation-json
```

This command touches SEC EDGAR. Use a descriptive `SEC_UA`, start with one ticker, preserve
the checkpointed CSV, and increase the ticker cap only after the smoke output has no repeated
429/5xx or parsing errors.

By default, the builder excludes non-priceable/common-equity suspects from validation rows:
convertible notes, preferreds, warrants, currency-suffixed FIGI artefacts, tickers with
spaces/digits and similar rows. Use `--validation-include-non-priceable` only for audit
exports; those rows carry `data_quality_flags` and must not enter headline metrics.

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
