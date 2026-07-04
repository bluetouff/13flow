# Post-run Form 4 validation runbook

This runbook is for the operator moment after a long `--build-validation-form4`
export finishes. It is intentionally offline-first: do not start another SEC,
Massive, Stooq, Yahoo or EDGAR fan-out from production while reviewing the
artifact.

## Current 2026-07-03 queue

Production was last observed at:

```text
67f3b3370aa907c42ad35298634abfa7a8d62352
```

Do not deploy while the Form 4 export process is still writing. After the run is
finished, deploy the latest queued commit:

```text
a1a4ce04b6b87c9e3d68d4140353fd1c6aafb56c
```

This includes:

- `f3d8ac3` offline Form 4 CSV validation gate;
- `03e2f56` removal of legacy retail chrome from the dashboard source;
- `3048216` public `/status` evidence page;
- `5b6bfb1` clearer Confluence validation boundary;
- `a1a4ce0` `/status` surfaced in product navigation.

## 1. Confirm the export is no longer running

```bash
ps -eo pid,etime,pcpu,pmem,cmd | grep '[r]un.py.*build-validation-form4' || true
sudo stat -c '%y %s bytes %n' /var/lib/13flow/validation_form4_liquid25_v2.csv
sudo wc -l /var/lib/13flow/validation_form4_liquid25_v2.csv
sudo tail -n 5 /var/lib/13flow/validation_form4_liquid25_v2.csv
```

Proceed only if no exporter process is still running.

## 2. Back up the completed artifact

```bash
sudo cp -a /var/lib/13flow/validation_form4_liquid25_v2.csv \
  /var/lib/13flow/validation_form4_liquid25_v2.before-validation-$(date +%Y%m%d-%H%M%S).csv
```

## 3. Deploy the queued code

Deploy only after the export has stopped. Use the latest queued SHA:

```bash
SHA=a1a4ce04b6b87c9e3d68d4140353fd1c6aafb56c
TMP=/tmp/13flow-deploy-$(date +%Y%m%d-%H%M%S)

git clone --depth 1 https://github.com/bluetouff/13flow.git "$TMP"
cd "$TMP"
test "$(git rev-parse HEAD)" = "$SHA"

sudo tar -C /opt --exclude=13flow/.venv --exclude=13flow/mcp-server/node_modules \
  -czf /var/lib/13flow/13flow-code.bak-before-$SHA-$(date +%Y%m%d-%H%M%S).tgz 13flow

sudo SHA="$SHA" SRC="$TMP" /opt/13flow/deploy/deploy-code-safe.sh
EXPECTED_SHA="$SHA" sudo /opt/13flow/deploy/smoke-public.sh
```

Do not use a raw `rsync --delete` followed by recursive `chown` / `chmod` on
all of `/opt/13flow`. `.venv` and `mcp-server/node_modules` are host-built
runtime dependencies, not Git artifacts; clobbering either one takes production
down even if the application code itself is valid.

## 4. Validate the Form 4 CSV offline

Run the validator from the deployed code:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --validate-form4-csv /var/lib/13flow/validation_form4_liquid25_v2.csv \
  --validation-tickers /var/lib/13flow/validation_tickers_liquid25.txt \
  --validation-start 2024-07-03 \
  --validation-end 2026-07-02 \
  --validation-json
```

Expected gate:

- `status` should be `ready`, or `review` with a clearly understood non-blocking
  reason;
- `mixed_issuer_ticker_count` must be `0`;
- `unexpected_ticker_count` must be `0`;
- `invalid_row_count` must be `0`;
- `duplicate_row_count` must be `0`;
- `open_market_buy_rows` and `open_market_sell_rows` should be reviewed for
  plausibility, not optimized for size.

If `mixed_issuer_ticker_count > 0`, stop. That means the CSV still contains a
ticker joined to more than one issuer CIK, such as the earlier reporting-owner
contamination pattern.

## 5. Build the joined Confluence feature dataset

Only after the Form 4 CSV gate is acceptable:

```bash
SHA=$(curl -fsS https://13flow.eu/api/version | /opt/13flow/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["git_sha"])')

sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --db /var/lib/13flow/13flow.db \
  --build-validation-dataset /var/lib/13flow/confluence_features_liquid25_v2_mature.csv \
  --validation-prices /var/lib/13flow/validation_prices_liquid25_massive.csv \
  --validation-form4 /var/lib/13flow/validation_form4_liquid25_v2.csv \
  --validation-tickers /var/lib/13flow/validation_tickers_liquid25.txt \
  --validation-start 2024-07-03 \
  --validation-end 2025-09-30 \
  --validation-code-commit "$SHA" \
  --validation-json
```

The mature end date intentionally excludes the latest 13F quarter when the local
price file cannot yet provide complete 120-trading-day forward returns. Review
the gate output before using the file in any public methodology material.
Complete Confluence validation still requires broader coverage, price-source
review, delisting treatment, costs, no-lookahead checks and out-of-sample
metrics.

Expected mature-artifact milestone:

- `status=minimum_schema_valid_metrics_unreviewed`;
- `evidence_review.status=mechanical_evidence_ready_for_review`;
- `row_error_count=0`;
- forward-return coverage is 1.0 for 20d, 60d and 120d;
- the artifact is ready for human review, not a public alpha claim.

## 6. Archive evidence

Save the relevant command outputs with the date:

- export completion summary;
- `/status` page or `/api/product-status` JSON after deploy;
- public smoke output;
- `--validate-form4-csv` JSON;
- `--build-validation-dataset` JSON;
- hashes and sizes of the CSV artifacts.

Do not claim validated alpha, probability, expected return or a complete insider
universe from this 25-ticker artifact.
