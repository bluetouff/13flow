# Confluence v1 Research Contract

Confluence v1 is frozen as a transparent screening hypothesis, not as a validated alpha
model. This document defines what is fixed today and what evidence must be published
before the product can use stronger language.

## Frozen scope

- Score version: `confluence_v1`
- Feature schema: `confluence_features_v1`
- Weight version: `heuristic_default_v1`
- Machine-readable spec: `docs/confluence_v1.json`
- Public API: `GET /api/methodology/confluence-v1`

The score is an ordinal 0-100 ranking. It is not a probability, not a calibrated historical
frequency, and not an expected-return estimate.

## Frozen universe

The institutional side is the tracked 13F manager universe stored in the market SQLite DB.
For each manager and quarter, the `latest_filings` view selects one complete-enough filing:
later amendments supersede originals only when they retain sufficient position coverage.

The insider side is Form 4 open-market activity when a live/precomputed Confluence provider
is explicitly configured. Production must not silently substitute sample data.

Every public validation report must disclose:

- fund universe and count;
- latest 13F quarter and filing-date availability;
- accession list or accession hash manifest;
- ticker-resolution coverage;
- Form 4 lookup universe and any operational trims;
- source commit and parameter hash.

## Point-in-time dataset

A valid historical dataset has one row per `(as_of, ticker, score_version, horizon)` and must
be generated using only records available at `as_of`.

Required columns:

- `as_of`, `ticker`, `issuer_name`;
- `score_version`, `feature_schema_version`, `weight_version`, `parameter_hash`;
- all institutional feature inputs;
- all insider feature inputs;
- final score, quadrant, and breakdown;
- 13F accession set/hash and Form 4 accession set/hash;
- price source, execution timestamp, adjusted entry price, adjusted exit price;
- forward returns at 20, 60 and 120 trading days;
- liquidity, market-cap, sector, beta and data-quality flags.

No row may use future filings, later ticker mappings, future prices, survivorship-only
constituents or post-test parameter changes.

## Baselines and validation

Publish all of the following for train, validation, test and walk-forward windows:

- insider-only rail;
- institutional breadth/fund-count-only rail;
- quadrant without numeric score;
- equal-weight top names;
- random/permutation baseline;
- default heuristic weights;
- any optimized weights, reported separately from defaults.

Metrics:

- rank IC at 20, 60 and 120 trading days;
- top-minus-bottom spread;
- hit rate;
- turnover and transaction-cost drag;
- coverage by year, sector and capitalization bucket;
- confidence intervals and permutation p-values;
- drawdown and volatility for portfolio variants.

## Append-only signal history

Production signal revisions are exposed as append-only JSONL:

- file: `confluence-history.jsonl` in `SMARTMONEY_CACHE_DIR`;
- public API: `GET /api/signals/confluence/history`;
- writer: `python run.py --append-signal-history`;
- live precompute: `python run.py --confluence` appends revisions after cache generation.

Each row includes:

- `recorded_at`, `generated_at`, `source`;
- `ticker`, `window_days`, `score`, `quadrant`;
- `score_version`, `feature_schema_version`, `weight_version`;
- `parameter_hash`, `code_commit`, `revision_hash`;
- full signal payload.

Rows are never edited in place. Corrections are new rows with a new `revision_hash`.

## Operator commands

Freeze/update the machine-readable spec:

```bash
python run.py --freeze-confluence-v1 docs/confluence_v1.json
```

Append existing cache snapshots to history:

```bash
SMARTMONEY_CACHE_DIR=/var/lib/13flow \
python run.py --append-signal-history --confluence-windows 30,90,180
```

Validate the minimum schema and metrics artifact for a point-in-time feature table:

```bash
python run.py \
  --build-validation-prices \
  --validation-tickers /var/lib/13flow/validation_tickers_sample25.txt \
  --validation-prices-out /var/lib/13flow/validation_prices_sample25.csv \
  --validation-price-provider massive \
  --validation-start 2013-01-01 \
  --validation-end 2026-07-02 \
  --validation-price-sleep-sec 15 \
  --validation-price-retry-attempts 8 \
  --validation-price-retry-base-sec 60 \
  --validation-price-retry-max-sec 900 \
  --validation-json

python run.py --db /var/lib/13flow/13flow.db \
  --build-validation-dataset /var/lib/13flow/confluence_features.csv \
  --validation-prices /var/lib/13flow/validation_prices_sample25.csv \
  --validation-tickers /var/lib/13flow/validation_tickers_sample25.txt \
  --validation-code-commit "$SHA" \
  --validation-json

python run.py --validation-dataset /path/to/confluence_features.csv --validation-json
```

The price exporter writes `ticker,date,adj_close` adjusted closes, resumes from existing
rows unless `--validation-price-force` is set, deduplicates repeated ticker/date rows, retries
`429`/`5xx` responses with exponential backoff and reports complete/partial history coverage.
`massive` is the preferred price source; `yahoo` is available only as a no-key research
fallback when the primary vendor account cannot serve enough history, and must be disclosed
as such in any validation artifact.
Reuse the same `--validation-tickers` file when building a priced sample dataset; otherwise
unpriced tickers from the full universe will remain in the feature table. This is a publication
gate, not a claim of validation. The output must be archived with the dataset hash,
price-source notes, costs,
liquidity rules and review notes before any public result is described as validated. The
first builder release exports
`feature_scope=13f_only_no_form4`; complete Confluence validation still requires Form 4
insider features to be joined point-in-time.

Precompute live Confluence and append revisions:

```bash
SEC_UA='13FLOW/1.0 contact@example.com' \
SMARTMONEY_CACHE_DIR=/var/lib/13flow \
python run.py --db /var/lib/13flow/13flow.db --confluence
```

The last command touches EDGAR/Form 4 and must stay behind the SEC-safe rate-limit policy.
