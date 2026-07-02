# SmartMoney — 13F superinvestor tracker

Reconstructs hedge-fund / superinvestor portfolios straight from **SEC EDGAR**
13F-HR filings, and diffs quarter-over-quarter to surface new positions, exits,
adds and trims. Data is U.S.-government public domain — free to use and redistribute.

**Guides:** [`TEST_LOCAL.md`](TEST_LOCAL.md) to run it on your machine in minutes ·
[`INSTALL_SERVER.md`](INSTALL_SERVER.md) for production deployment · [`SECURITY.md`](SECURITY.md)
for the threat model and audit.

## Setup
```bash
pip install -r requirements.txt   # or: pip install requests defusedxml flask
export SEC_UA="SmartMoney/1.0 you@example.com"   # SEC requires a contact email or 403s you
export OPENFIGI_APIKEY="..."                       # optional, free; lifts FIGI rate limits
python run.py --list                 # tracked funds
python run.py --verify               # sanity-check seed CIKs against EDGAR
python run.py --fund "Berkshire Hathaway" --top 15 --enrich   # --enrich adds tickers
```
Offline tests (no network):
```bash
pip install pytest
python -m pytest tests/ -q     # all suites: parsing, figi, db, valuation, alerts, resolver, security, accounts
```

## CUSIP resolution & the long tail
13F rows carry only CUSIPs. `resolver.py` runs a confidence-ranked chain so the tail that
OpenFIGI misses still gets resolved where possible, with provenance recorded:
```
manual override (1.0) → OpenFIGI (0.95) → CUSIP issuer-prefix (0.65–0.85)
→ SEC name match (0.60) → unresolved (0.0, issuer name kept)
```
The CUSIP-prefix step reuses a confident sibling's ticker for a different share class/unit of
the same issuer; the SEC step matches `nameOfIssuer` against SEC `company_tickers.json`. Misses
are cached with a timestamp and re-tried after a TTL (the tail shrinks as data improves), not
forever. `--sync --enrich` uses the full chain.
```bash
python run.py --coverage                 # % of 13F value resolved, per fund + the worst tail
python run.py --resolve-sweep            # re-run the chain over unresolved CUSIPs, back-fill
```
Provenance + confidence are stored per holding, so the dashboard's reconcile dot and the
valuation reconcile ratio can flag weak mappings. Optional `cusip_overrides.json` (`{"<cusip>":"TKR"}`)
hard-maps stubborn names at top confidence.

## Dashboard (web UI)
A read-only Flask API exposes the store as JSON, and a single-file dashboard consumes it.
```bash
pip install flask
python -m smartmoney.api --db smartmoney.db            # serves http://localhost:5000
python -m smartmoney.api --db smartmoney.db --value     # also enable live valuation (stooq)
```
Open `http://localhost:5000`. The UI is branded **13FLOW** (dark editorial theme — emerald =
institutions, amber = insiders, converging like the Confluence score) with five screens:
**Consensus** (who's buying / most owned), **Funds** (holdings, current weights, implied P&L,
conviction sparklines), **Compare** (overlap matrix across funds), **Alerts** (subscriptions +
the diff feed), and **Confluence** (13F accumulation × insider buying). A served **FAQ** page
(`/faq`) explains the product, linked from the sidebar. `dashboard.html` also renders standalone
with built-in sample data across every screen when no API is reachable, so you can preview the
whole UI before syncing any data. API endpoints: `/api/funds`, `/api/fund/<cik>`,
`/api/consensus/{buys,holdings}`, `/api/compare`, `/api/alerts/preview/<cik>`,
`/api/signals/confluence`, `/api/data-quality` (all public, read-only), plus authenticated
`/api/auth/*` and user-scoped `/api/subscriptions`.

## Accounts & auth
Read-only market data is public (it's public-domain). The **paid** feature — filing-alert
subscriptions — sits behind a full accounts system, with the tier enforced **server-side**
from the user's database row (the client can never assert its own tier).
```bash
python run.py --create-user you@example.com --tier paid   # password prompted (never in argv)
python run.py --set-tier you@example.com --tier free       # change a tier later
python run.py --verify-user you@example.com                # mark an email verified (operator)
```
Security design (full rationale in [`SECURITY.md`](SECURITY.md)): Argon2id password hashing
(stdlib scrypt fallback) with an optional server-side pepper; opaque 256-bit sessions stored
only as hashes, with absolute + idle expiry and full revocation; HttpOnly + Secure +
SameSite=Strict session cookie plus a double-submit CSRF token on every mutation; per-account
lockout and per-IP rate limiting; enumeration-resistant login and password reset.

**Email verification**: registration creates an *unverified* account and emails a single-use
24h link; login is refused until the address is verified — but only *after* a correct password,
so it never leaks which emails exist. Invariant: holding a session implies a verified email.

**Breached-password check (HaveIBeenPwned)**: new/changed passwords are checked against the
HIBP Pwned Passwords corpus via the **k-anonymity range API** — only the first 5 chars of the
password's SHA-1 ever leave the process (never the password, never the full hash), with
`Add-Padding` against traffic analysis. Fail-open by default so an HIBP outage can't block
signups; the local deny-list remains the fast first pass.

Relevant env vars: `SMARTMONEY_PW_PEPPER` (long random secret, set in production),
`SMARTMONEY_INSECURE_COOKIES=1` (local http testing only — Secure cookies otherwise need HTTPS),
`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM` (to actually send verification mail;
without them the link is logged), `SMARTMONEY_DEV_EMAIL_ECHO=1` (dev only: return the verify
link in the API response for local testing), `SMARTMONEY_HIBP_FAIL_CLOSED=1` /
`SMARTMONEY_DISABLE_HIBP=1` (tune the breach check).
Sign-in / register / sign-out and subscription management are built into the dashboard.

## Billing (Stripe) — with a local mock for testing
Upgrading from Free to **Pro** runs through Stripe Checkout; the tier flips to `paid` **only**
on a signature-verified webhook (never from the browser or the success redirect), handled
idempotently. The whole flow is in `billing.py`.

Two modes, chosen automatically:
- **Stripe mode** (when `STRIPE_SECRET_KEY` is set): real Stripe Checkout + Customer Portal +
  webhook. Needs `STRIPE_PRICE_ID` and `STRIPE_WEBHOOK_SECRET` too. See `INSTALL_SERVER.md`.
- **Mock mode** (no Stripe key): a local, styled fake-checkout page lets you click "Pay" and
  watch your account flip to Pro — no Stripe account, no network. This is the graphical flow
  to test locally; the mock endpoints are **absent in production** (disabled once a Stripe key
  is present). See `TEST_LOCAL.md`.

Endpoints: `POST /api/billing/checkout`, `POST /api/billing/portal`,
`POST /api/billing/webhook` (signature-verified, CSRF-exempt), and — mock only —
`GET /billing/mock-checkout` + `POST /api/billing/mock-complete`.

## Alerts — real delivery
Subscribe to a fund and get the **diff** (not just "a filing appeared") delivered when a
new 13F lands. Channels: console (default), webhook, email.
```bash
python run.py --subscribe "Berkshire Hathaway"                       # console, primed
python run.py --subscribe "Scion Asset Mgmt" --channel webhook --target https://hooks.you/x
python run.py --list-subs
python run.py --alerts-run          # sync subscribed funds from EDGAR, then deliver new ones
python run.py --alerts-dispatch     # deliver pending from already-stored filings (offline)
```
Email needs `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` in the env.
Key properties: idempotent (a crash/restart never re-sends or drops — the deliveries table is
the boundary), new subscribers are **primed** so they only get future filings, failed sends are
recorded and retried next run, and subscribing is gated to the paid tier. Drive `--alerts-run`
from cron rather than the in-process `poll()` loop.

## Confluence (13FLOW) — where 13F accumulation meets insider buying
A separate screen ranks tickers where **superinvestor 13F accumulation** and **open-market
Form 4 insider buying** coincide — a rare, hard-to-fake overlap. It reuses the existing EDGAR
etiquette (UA + rate limit), the `defusedxml` hardening, and the dashboard theme.
```
GET /api/signals/confluence?window=90&min_score=0
→ { "kpis": {...}, "signals": [ {ticker, score, quadrant, breakdown{...}, institutional{...}, insider{...}}, ... ] }
```
The 0–100 score is split in two layers on purpose: `FeatureParams` controls *what the signal
measures* (recency half-life, buy-size curve, cluster window, seniority), tuned by judgement;
`Weights` controls *how the four pillars combine* (institutional breadth, insider conviction,
recency-weighted dollars, agreement bonus, minus trim/sell penalties), tuned empirically with
the backtest harness. Each signal exposes its per-pillar `breakdown` so the score is fully
transparent, and a quadrant label (Conviction / Institutional bid / Insider conviction /
Distribution / Divergent / Neutral). It's a **screen, not advice** — a ranked view of two
public, high-quality signals.

The screen lives as a fifth dashboard tab and runs out-of-the-box on **sample data** (no DB or
network). The live provider needs EDGAR access for Form 4s:
```bash
SEC_UA="you@example.com" SMARTMONEY_CONFLUENCE_LIVE=1 python -m smartmoney.api --db demo.db
```
With the flag off (default), the endpoint serves the same-shaped sample so the UI lights up
immediately. Calibrate the weights against forward returns with `python -m smartmoney.backtest`
(synthetic demo) — see `FORMS4_INTEGRATION.md` for the full feature write-up.

## Valuation — current weights & implied P&L
A 13F reports value at *quarter-end*. To see what the book is worth *now* and the paper
P&L since the filing, revalue stored holdings at live prices:
```bash
python run.py --value "Berkshire Hathaway"                       # stooq (free, default)
python run.py --value "Berkshire Hathaway" --provider massive --fundamentals
python run.py --value "Berkshire Hathaway" --basis 2024-09-30    # value a specific quarter
```
Prices are pluggable: **stooq** (free, no key) by default; **massive** (Massive Market Data,
Polygon-shaped) with `MASSIVE_API_KEY` set, which also yields market cap and % of company owned.
Each priced line shows a **reconcile ratio** (reported ÷ shares×quarter-end-close): ~1.00×
means the CUSIP→ticker map is right; far from 1 flags a bad mapping. P&L is *paper* — it
assumes holdings are unchanged since the filing, which the 45-day lag guarantees they're not.

## Persistence & cross-fund screens
Snapshots are stored in SQLite so diffs and multi-fund questions become queries.
```bash
python run.py --sync "Berkshire Hathaway" --enrich      # backfill one fund
python run.py --sync-all --max-quarters 12 --enrich     # backfill everything
python run.py --buys 2024-12-31 --min-funds 3           # who's BUYING (diff-based)
python run.py --consensus 2024-12-31 --min-funds 3      # who's HOLDING (pure SQL)
python run.py --quality --db smartmoney.db              # DB-only data-quality warnings
python run.py --timeline "Berkshire Hathaway" --cusip 037833100   # conviction over time
```
`--sync` only fetches filings not already stored, so re-runs are cheap and pick up just
the newest quarter. The `--buys`/`--consensus`/`--timeline` screens are pure DB reads —
no SEC_UA needed once data is synced.

## Open build (public, read-only — no auth, no Stripe, no alerts)
There is a first-class **open mode** for a public deployment that exposes only the read-only
screens (Consensus / Funds / Compare / Confluence) with no accounts, no payment, and no
alerts. Turn it on with a single env var (or `--open`):
```bash
SMARTMONEY_OPEN=1 SMARTMONEY_DB_READONLY=1 python -m smartmoney.api --db demo.db
# or: python -m smartmoney.api --db demo.db --open --readonly
```
In open mode the app **does not even register** the auth, billing, subscription, or alert
routes (`/api/auth/*`, `/api/billing/*`, `/api/subscriptions`, `/api/alerts/*` all return
404, not 401), `SMARTMONEY_DB_READONLY=1` opens SQLite read-only so the web process can't
write the database, and the dashboard auto-detects the build via `/api/config` — hiding the
Sign in button and the Alerts tab. The same codebase runs the full build when the flag is
absent. A complete **Debian + Apache** deployment kit (gunicorn systemd unit with a sandbox,
Apache TLS reverse-proxy vhost with a GET-only method allow-list + HSTS/CSP, an ingest user
separated from the web user, and a scheduled refresh) lives in [`deploy/`](deploy/) — see
[`deploy/INSTALL_DEBIAN_APACHE.md`](deploy/INSTALL_DEBIAN_APACHE.md).

## Architecture
- `edgar.py` — rate-limited client (8 req/s, under SEC's 10/s ceiling), CIK resolution,
  submissions feed, locates + downloads the holdings XML.
- `parser.py` — namespace-agnostic info-table parser → raw holdings.
- `portfolio.py` — aggregates rows to one line per (CUSIP, put/call), normalizes value units, weights.
- `figi.py` — **CUSIP → ticker** via OpenFIGI v3: batched, rate-limited, 429-aware,
  no-exchCode fallback, persistent disk cache.
- `resolver.py` — long-tail resolver chain (OpenFIGI → CUSIP-prefix → SEC name → manual),
  confidence + provenance, retryable cache, coverage reporting.
- `diff.py` — classifies moves by **share count**: NEW / EXIT / ADD / TRIM / HOLD.
- `db.py` — **SQLite store**: save/load portfolios, a `latest_filings` view so amendments
  supersede, and SQL screens (consensus holdings, conviction timeline, holders, AUM timeline).
- `analytics.py` — **consensus buys/sells** across funds (diff-based, the sharper screen).
- `prices.py` — pluggable price/fundamentals providers: `StooqProvider` (free) + `MassiveProvider`.
- `valuation.py` — revalue a stored portfolio at current prices: current weights, implied
  P&L since quarter-end, reconcile check, % of company owned.
- `registry.py` — superinvestor seed list (CIK is the stable key).
- `tracker.py` — wires it together: `sync_fund` ingestion + freemium gating (free = 3 funds).
- `channels.py` — delivery channels: console / webhook / email (+ callable for tests).
- `alerts.py` — `AlertEngine`: diff-carrying alerts, persistent dedup, priming, paid-tier gate.
- `api.py` — read-only Flask JSON API over the store; serves the dashboard; wires in auth.
- `netsec.py` — egress safety: SSRF guard for webhook URLs + email-recipient validation.
- `pwhash.py` — password hashing (Argon2id, scrypt fallback, optional pepper, rehash).
- `accounts.py` — users, opaque revocable sessions, lockout, reset tokens, email verification, server-side tier.
- `auth.py` — Flask glue: secure cookies, double-submit CSRF, rate limiting, `/api/auth/*`.
- `hibp.py` — HaveIBeenPwned k-anonymity breached-password check (privacy-preserving).
- `notify.py` — transactional email (verification links) over hardened SMTP, with a dev fallback.
- `billing.py` — Stripe subscriptions (signature-verified, idempotent webhook) + local mock.
- `forms4.py` — Form 4 discovery by issuer CIK + ownership-XML parser (open-market P/S), XXE-hardened.
- `crosssignal.py` — Confluence engine: 13F accumulation × insider buying → scored, classified signal.
- `backtest.py` — rank-IC / quantile-spread evaluation + coordinate-ascent weight optimiser.
- `api_signals.py` — read-only `GET /api/signals/confluence` blueprint (live + sample providers).
- `dashboard.html` — single-file web UI (consensus / funds / compare / alerts / confluence + auth + upgrade).
- `faq.html` — branded FAQ / explainer page, served at `/faq`, sharing the dashboard's theme.

See **`SECURITY.md`** for the threat model, the audit findings, and deployment hardening.

## Gotchas this code already handles (and the ones it doesn't)
**Handled:**
- Mandatory `User-Agent` + 10 req/s limit on EDGAR.
- **Value units changed in 2023**: pre-2023-01-03 `<value>` is in *thousands*, after it's
  *whole dollars*. Normalized by report date in `portfolio.py`.
- Multiple rows per issuer aggregated; puts/calls kept distinct from long stock.
- Amendments (13F-HR/A) skipped for the headline diff (they restate, not re-trade).
- **CUSIP → ticker** via OpenFIGI: batch 100 jobs/req with key (5 without), v3 `warning`
  = no-match handled, 429 backoff, results cached to disk so steady-state cost ≈ only new CUSIPs.

**Not handled yet (the real work ahead):**
- **No-match CUSIPs.** OpenFIGI resolves the vast majority of 13(f) securities, but expect a
  long tail (some bonds, units, recently-issued names) to come back empty — they're cached as
  misses and worth a periodic re-sweep.
- **The 45-day lag.** 13F is filed up to 45 days after quarter-end, so no alert is ever
  trade-fresh — the edge is being first to the *filing event* and to a clean diff, not to price.
- Confidential-treatment requests can delay/omit positions; backfill when the amendment lands.
- Pre-2013 filings are plain-text tables, not XML — this parser targets the XML era.

## Roadmap toward the product
1. ✅ CUSIP→ticker enrichment (OpenFIGI) — `figi.py`. Next: join price/market-cap.
2. ✅ Persistence (SQLite) + cross-fund screens — `db.py` / `analytics.py`.
3. ✅ Price / market-cap join — `prices.py` / `valuation.py` (current weights, implied P&L).
4. ✅ Real alert delivery — `alerts.py` / `channels.py` (diff payload, dedup, channels, paywall).
5. Freemium server-side: gating logic lives in `tracker.Tier` (fund limit + alerts flag).
6. ✅ UX layer — `api.py` + `dashboard.html` (consensus, fund pages, compare, alert feed).

When you outgrow SQLite, the swap to Postgres is mechanical: the schema uses standard
window functions + one view, and the repository is plain SQL (no ORM lock-in).

## License

13FLOW is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0-or-later).
If you run a modified version on a network server, you must make the complete source
available to its users. See [LICENSE](LICENSE).

Data: SEC EDGAR (US public domain). 13FLOW is an analysis screen, not investment advice.
