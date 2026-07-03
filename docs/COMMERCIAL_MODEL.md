# 13FLOW commercial model

Source check date: 2026-07-03. Competitor pricing and packaging change over
time; re-check the linked pages before quoting a buyer.

## Principle

13FLOW should not sell "SEC data" as if the underlying filings were proprietary.
SEC EDGAR is the source of truth. The commercial product is the professional
workflow around it:

- normalized 13F surfaces;
- reviewed Form 4 validation boundary;
- Confluence methodology contract;
- source-linked quality warnings;
- operator-issued Pro API keys, rate limits and audit rows;
- MCP tools that fail closed without valid Pro access;
- a public evidence pack that a buyer can verify before onboarding.

The strategy is better, not cheaper. Do not compete on generic filing download
volume. Compete on evidence, workflow fit, reproducibility and buyer-specific
support.

## Market map

| Provider | Publicly visible offer | Risk for 13FLOW | 13FLOW response |
| --- | --- | --- | --- |
| SEC.gov | Official EDGAR JSON APIs, real-time updates and nightly bulk files. The APIs are free and do not require API keys. SEC notes it does not provide technical support for developing or debugging scripted downloads. | Raw filing access is free at the source. Reselling it as proprietary data is weak. | Sell normalized workflow, source links, quality boundaries, operator support and a buyer evidence pack. |
| SEC-API.io | Broad SEC API platform. Public page shows a free tier, personal/startup plans, business internal-use plans and custom enterprise. Products include Form 3/4/5 insider trading data and Form 13F holdings. | A generic 13F/Form 4 API will be compared to a mature low-cost SEC API vendor. | Position 13FLOW as narrower and deeper: 13F plus Form 4 confluence, validation gates, MCP readiness and auditability. |
| Quiver Quantitative | Alternative-data API starting from low self-serve pricing, with datasets such as insider trades, hedge fund activity, top shareholders and an MCP server. | Broad alternative-data UX can absorb users who want many datasets quickly. | Stay professional and evidence-first. Sell a bounded institutional workflow, not a retail alternative-data terminal. |
| Dataroma | Free curated superinvestor portfolios and significant insider buys. | Free curation satisfies casual retail curiosity. | Avoid retail checkout. Sell machine-readable proof, scoped access, audit and buyer-specific workflows. |

Sources:

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC developer resources: https://www.sec.gov/about/developer-resources
- SEC-API pricing: https://sec-api.io/pricing
- Quiver API: https://api.quiverquant.com/
- Dataroma: https://www.dataroma.com/m/home.php

## Ideal customers

### 1. Research desk

Buyer: family office, small asset manager, independent research desk.

Pain: manual 13F checks, spreadsheet reconciliation, source-link verification,
and uncertainty around which quality warnings matter.

Sell: bounded Pro API, quality metadata, status evidence, and a workflow that
keeps the analyst out of manual EDGAR repetition.

### 2. Data pipeline owner

Buyer: internal data team inside a fund, advisory shop or analytics vendor.

Pain: needs stable, bounded API access over institutional portfolios and quality
warnings without inventing a local parsing pipeline.

Sell: API contract, limits, audit trail, truncation counters, methodology
contract and rotation policy.

### 3. Agent workflow builder

Buyer: AI or automation team building internal research agents.

Pain: needs read-only institutional ownership context and Pro tools that do not
leak data or silently work without authorization.

Sell: MCP public tools, Pro tools, fail-closed behavior, product-status probes
and handoff tests.

## Packages

| Package | Price guide | Term | Fit |
| --- | ---: | --- | --- |
| Paid pilot | 490 EUR / month | 30 days, renewable once | One workflow against real live data. |
| Desk API | 1,500 EUR / month | Annual preferred | One desk or data team depends on the API repeatedly. |
| Agent / MCP | 2,500 EUR / month | Annual preferred | Client wires 13FLOW into automated research agents. |
| Enterprise / redistribution | from 6,000 EUR / month | Custom contract | Redistribution, many keys, custom limits, procurement or SLA. |

Pricing rule: do not discount full live API access below 490 EUR / month. If a
buyer pushes for less, reduce scope, duration, request limits or support before
reducing the floor.

## What is included

- API-key authentication through `Authorization: Bearer` or `X-13FLOW-Key`.
- Scopes: `funds:read` and `quality:read`.
- Persistent per-key rate limits.
- Request audit trail.
- Bounded fund-detail payloads with truncation counters.
- Data-quality warnings surfaced as first-class output.
- Public methodology and product-status contracts.
- MCP public tools and Pro tools with fail-closed behavior.

## What must not be claimed yet

Do not claim:

- validated alpha;
- expected returns;
- probability calibration;
- investment advice;
- complete insider-only coverage;
- production x402 paid access;
- full 2013-2026 quantitative validation.

Correct wording: 13FLOW is a source-linked research workflow over SEC
EDGAR-derived 13F and Form 4 surfaces. Confluence v1 is a heuristic feature
contract until the full validation dataset passes.

## Qualification filter

Good fit:

- professional buyer with a repeatable 13F research workflow;
- needs API or MCP access rather than screenshots;
- accepts the current no-alpha validation boundary;
- values audit trail, source links and methodology stability.

Bad fit:

- wants cheap raw SEC access only;
- requires a public self-serve checkout today;
- expects investment advice, price targets or validated alpha;
- needs redistribution without a custom contract.

## Evidence pack

Send or show these before quoting serious access:

```text
/validation
/status
/api/product-status
/api/live-status
/api/pro-offer
/api/openapi.json
/api/pro/v1/openapi.json
/api/methodology/confluence-v1
```

For a pilot, the buyer must also accept the current validation boundary and run
the status, funds and bounded fund-detail probes from `docs/PRO_API_ONBOARDING.md`.

## Objections

**"SEC data is free."**

Yes. 13FLOW does not charge for the existence of EDGAR filings. It charges for a
bounded workflow: normalization, method contracts, quality warnings, API/MCP
access, audit trail and operator support.

**"SEC-API is cheaper."**

For generic SEC access, it may be the right tool. 13FLOW is priced for buyers
who want a narrower 13F plus Form 4 research workflow with explicit validation
boundaries and MCP behavior.

**"Can we start for free?"**

No full live Pro API for free. Use public endpoints and `/status` first. If the
buyer has a serious workflow, quote the paid pilot.

**"Can we redistribute this data?"**

Only under the enterprise/redistribution package with explicit written terms.
