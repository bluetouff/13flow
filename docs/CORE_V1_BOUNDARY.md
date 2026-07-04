# 13FLOW Core V1 boundary

This document is the product-change gate for the controlled pilot. Its purpose
is to keep 13FLOW useful, secure and maintainable while the first paid users are
qualified manually.

## Principle

Core V1 is not a broad SaaS platform. It is a source-linked SEC EDGAR research
workflow with explicit quality gates, bounded Pro access and operator-managed
pilot delivery.

Before adding a new endpoint, page, scheduled job or admin panel, the change
must answer all of these questions:

- Which paying workflow does it unblock?
- Which existing surface cannot reasonably carry it?
- What privacy or security boundary does it touch?
- Which smoke test or offline test proves it will not regress?
- What operator work does it remove, rather than create?

If the answers are weak, keep the idea in the parking lot.

## Core public surface

These public surfaces are part of Core V1:

- `/api/version` and `/api/live-status` for deployed-state proof;
- `/api/data-quality`, `/api/funds`, `/api/stocks/{ticker}` and trusted ticker
  flow endpoints for read-only research context;
- `/api/watchlist/discover` for automatic trusted-flow discovery;
- `/api/product-status`, `/api/commercial-readiness`,
  `/api/security-posture`, `/api/buyer-pack` and `/api/pro-offer` for buyer
  truth surfaces;
- `/api/openapi.json`, `/api/methodology/app`, `/api/methodology/mcp` and
  `/api/methodology/confluence-v1` for machine-readable contracts;
- `/status`, `/validation`, `/security`, `/readiness`, `/buyer-pack`, `/pilot`,
  `/pilot/request`, `/pro`, `/pro/onboarding`, `/pro/workspace` and
  `/developers` for human-readable evidence and onboarding.

The public open build remains read-only. It must not expose browser accounts,
self-serve checkout, public form submission, token collection or mutable customer
state.

## Core Pro surface

These Pro surfaces are part of Core V1:

- `/api/pro/v1/status`, `/api/pro/v1/usage` and
  `/api/pro/v1/onboarding` for customer-safe diagnostics;
- `/api/pro/v1/funds`, `/api/pro/v1/fund/{cik}`,
  `/api/pro/v1/data-quality`, `/api/pro/v1/watchlist` and
  `/api/pro/v1/watchlist/discover` for bounded read access;
- `/api/pro/v1/workspace/*` for saved watchlists, snapshots, alerts, reports
  and exports;
- `/api/pro/v1/admin/health`, `/api/pro/v1/admin/ops`,
  `/api/pro/v1/admin/pilot-fulfillment`,
  `/api/pro/v1/admin/buyer-handoff`,
  `/api/pro/v1/admin/pilot-closeout`,
  `/api/pro/v1/admin/pilot-renewal` and
  `/api/pro/v1/admin/pilot-request-assist` for operator control.

The Pro web worker must not create plaintext customer tokens, return token
hashes, store public pilot-request PII, or become the billing system. Operator
key creation, delivery, rotation and revocation stay explicit.

## Keep out of Core V1

Do not add these until the controlled pilot proves demand and capacity:

- public self-serve checkout or account signup;
- public submission endpoints that persist prospect PII;
- automated billing, invoices, CRM sync or support-ticket workflows;
- production x402 settlement;
- new research claims such as validated alpha, expected returns or probability
  calibration;
- broad market-data expansion beyond the current 13F, Form 4 and quality-gate
  boundary;
- more dashboards when an existing admin, buyer-pack, onboarding or workspace
  surface can carry the need;
- external data fan-out from production to satisfy long historical validation
  gaps.

## Maintenance rule

Prefer extending existing contracts over adding new surfaces:

- buyer-facing truth belongs in `/api/product-status`, `/api/buyer-pack`,
  `/api/pro-offer`, `/api/security-posture` or their existing pages;
- operator health belongs in `/api/pro/v1/admin/ops` or `/pro/admin`;
- customer workspace value belongs in `/api/pro/v1/workspace/*` or
  `/pro/workspace`;
- onboarding value belongs in `/api/pro/v1/onboarding`, `/pro/onboarding` or
  `docs/PRO_API_ONBOARDING.md`.

Any new surface must be documented in this file or deliberately rejected from
Core V1.

## Acceptance gate

A Core V1 change is not ready until all relevant checks pass:

```bash
python -m pytest tests/ -q
EXPECTED_SHA="$SHA" sudo /opt/13flow/deploy/smoke-public.sh
EXPECTED_SHA="$SHA" PRO_TOKEN="$PRO_TOKEN" sudo /opt/13flow/deploy/smoke-pro-workspace.sh
EXPECTED_SHA="$SHA" sudo /opt/13flow/deploy/smoke-pro-key-lifecycle.sh
```

For production work, also check encrypted Pro DB backup verification and the
admin ops verdict. Public data publication must remain automated fail-closed:
routine 13F publication must not require manual filing validation.
