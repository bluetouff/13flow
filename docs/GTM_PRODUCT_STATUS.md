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

Machine-readable product status:

```bash
curl -fsS https://13flow.eu/api/product-status | python3 -m json.tool
```

## Claims not allowed yet

Do not claim:

- validated alpha;
- a calibrated probability;
- an expected-return model;
- a complete insider-only or distribution universe;
- production x402 paid access;
- full 2013-2026 quantitative validation.

The current Confluence score is an ordinal heuristic rank. The correct wording is
`backtest harness available; default weights are heuristic`.

## Quantitative validation status

Current milestone:

- price pipeline: validated on a 25-ticker sample;
- feature pipeline: validated on a 25-ticker sample;
- sample price artifact SHA256:
  `2e35a5713c3e0654134d8d05d6f50b7013729ce6634d31db4e5e2e534ba57c9e`;
- sample feature artifact SHA256:
  `4ecceb420a466b138de6d4672844158705c0da4ed5425bc661e97df8ecfc8592`;
- Form 4 join: supported in the offline validation builder through a reviewed
  normalized CSV/JSONL artifact;
- full validation: blocked until vetted adjusted-price and Form 4 artifacts
  covering the full required universe and history are imported.

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
be rebuilt and evaluated.

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
`pipeline_smoke_validated_full_quant_blocked`, sales and documentation must not
describe Confluence as fully validated.
