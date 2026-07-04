# Security audit — SmartMoney

Scope: the whole codebase (EDGAR ingestion → resolver → store → valuation → alerts → API/UI).
This is a working audit, not a checklist: each finding below was verified against the actual
code, and the fixed ones have regression tests in `tests/test_security_offline.py`.

## Trust boundaries
- **Untrusted, network-facing:** the Flask API (`api.py`) and the dashboard it serves.
- **Semi-trusted external data:** EDGAR filings (XML), OpenFIGI/SEC/price responses. Fetched
  over TLS from known hosts, but their *content* (issuer names, etc.) is attacker-influenceable.
- **Operator-controlled:** env secrets (`SEC_UA`, `OPENFIGI_APIKEY`, `MASSIVE_API_KEY`, SMTP),
  the DB file, the registry, CLI invocation.
- **User-supplied destinations:** webhook URLs and email addresses on subscriptions — these
  leave the server, so they are the highest-risk inputs.

## Findings

| # | Severity | Issue | Status |
|---|----------|-------|--------|
| 1 | High | Flask `debug=True` on the runner — Werkzeug debugger is RCE if exposed | **Fixed** — `debug=False`, binds `127.0.0.1` by default, warns on non-local bind |
| 2 | High | SSRF: webhook delivery POSTed to any user-supplied URL (cloud metadata, internal services) | **Fixed** — `netsec.validate_public_url` blocks loopback/private/link-local/reserved IPs + `localhost`/`.internal`/`.local`; checked at subscribe time and at send; redirects disabled |
| 3 | High | XSS: dashboard rendered API/filing strings into `innerHTML` unescaped | **Fixed** — `esc()` HTML-escapes every dynamic field (issuer, label, manager, ticker, cusip, target, …) |
| 4 | Med | XML parsing via stdlib ElementTree — billion-laughs / XXE exposure | **Fixed** — prefers `defusedxml`; hardened fallback refuses DTD/ENTITY + size cap |
| 5 | Med | Email header injection via recipient/subject (CRLF) | **Fixed** — `validate_email_recipient` rejects CRLF/control chars; `EmailMessage` + subject CRLF-stripped; verified TLS context |
| 6 | Med | Massive API key sent in URL query string (leaks to logs/proxies/Referer) | **Fixed** — moved to `Authorization: Bearer` header |
| 7 | Med | No authn/authz on the API; `Tier` was caller-asserted, not server-enforced | **Fixed** — full accounts/sessions system; tier read from the DB user row; paid features gated server-side (see below) |
| 8 | Low | `/api/compare` unbounded `ciks` fan-out (mild DoS) | **Fixed** — capped to 12, each CIK validated |
| 9 | Low | Non-numeric CIK reached queries (500s) | **Fixed** — `clean_cik` validates → 400 |
| 10| Low | Missing response hardening headers / body-size cap | **Fixed** — nosniff/DENY/no-referrer, `MAX_CONTENT_LENGTH` |

### Verified-clean (no action needed)
- **SQL injection:** all queries use `?` placeholders; the only f-string SQL interpolates
  fixed internal constants (`ALTER TABLE … {col}`, a constant `WHERE … = ?` fragment) — no
  user data in SQL text.
- **No** `eval`/`exec`/`pickle`/`os.system`/`subprocess`/`shell=True`/`yaml.load`.
- **No** hardcoded secrets — all credentials come from env.
- **TLS** verification left at default (never `verify=False`); request **timeouts** set on all
  outbound calls; EDGAR rate-limited.
- `send_file` serves a constant path (no traversal).

## Browser accounts and checkout
Core V1 deliberately does not ship browser accounts, public signup, password
storage, email verification, Stripe checkout or browser subscription management.
Those legacy modules were removed to keep the controlled pilot smaller and
easier to operate.

Paid access is handled through the Pro API control plane: operator-issued API
keys, scopes, persistent quotas, request audit, expiry and rotation.

## Pro API control plane
The Pro API is intentionally separate from browser sessions. It is disabled unless
`SMARTMONEY_PRO_API=1` is set.

- **API-key format** — the plaintext token is high-entropy and shown only once at creation
  time. In production, the database stores only an HMAC-SHA256 hash derived with the
  server-only `SMARTMONEY_PRO_KEY_PEPPER` plus a non-secret key id used for lookup. A token
  generated from a GitHub clone or another 13FLOW instance cannot authenticate against
  `13flow.eu` without the production pepper and a row in the production Pro DB. When
  `SMARTMONEY_PRO_REQUIRE_KEY_PEPPER=1`, legacy SHA-256 rows are rejected unless
  `SMARTMONEY_PRO_ACCEPT_LEGACY_SHA256_KEYS=1` is temporarily configured for rotation.
- **Scopes** — every Pro route declares a required scope. Current scopes are `funds:read`
  and `quality:read`; missing scope returns **403**.
- **Rate limits** — per-key minute and day buckets are persisted in `SMARTMONEY_PRO_DB`, so
  limits survive worker restarts. Exceeding a bucket returns **429** with `Retry-After`.
- **Audit** — every Pro request writes an audit event with key id when known, route, method,
  status, IP, user agent, and timestamp. Denied and rate-limited calls are audited too.
- **HTTP cache safety** — Pro responses set `Cache-Control: private, no-store, max-age=0`,
  `Pragma: no-cache`, `Expires: 0`, and `Vary: Authorization, X-13FLOW-Key`.
- **Data-plane separation** — the Pro DB should be a small writable runtime DB
  (`/var/lib/13flow-pro/13flow-pro.db`) owned by the dedicated Pro service account. The
  13F data DB remains read-only for both public and Pro API services.
- **Revocation** — `run.py --revoke-api-key <key_id>` marks a key revoked immediately; no
  bearer token material is required or stored for revocation.
- **Retention** — `run.py --prune-pro-audit-days DAYS` deletes Pro audit rows older than
  the chosen retention window. Run this only after encrypted backups are confirmed.

Operational requirements:
- Put `SMARTMONEY_PRO_DB` outside the code tree, owned by the dedicated `flowpro` service
  user, mode `0640` or as restrictive as your backup/ops model allows.
- Set `SMARTMONEY_PRO_KEY_PEPPER` to a long random server-only secret in
  `/etc/13flow/13flow-pro.env`, set `SMARTMONEY_PRO_REQUIRE_KEY_PEPPER=1`, and never commit
  or print that pepper. Keep `SMARTMONEY_PRO_ACCEPT_LEGACY_SHA256_KEYS=0` after rotation so
  database-only SHA row insertion cannot mint accepted production keys.
- Keep the market-data DB read-only for gunicorn (`SMARTMONEY_DB_READONLY=1`). Route
  `/api/pro/` to `13flow-pro.service` and remove Pro API env/write access from the public
  `13flow.service`.
- Treat generated tokens like passwords: never put them in shell history, logs, URLs, or
  screenshots. Prefer a vault and rotate keys per institution.
- Back up `SMARTMONEY_PRO_DB` with encryption only. The bundled `deploy/backup-pro-db.sh`
  refuses plaintext backups and supports GPG public-key encryption or a symmetric
  passphrase file fallback. Verify at least one restore with
  `deploy/verify-pro-db-backup.sh` before pruning Pro audit rows; for public-key
  backups, perform that restore check only on a host with the matching private key.
- Define a retention window for Pro audit rows. A practical starting point is 180 days
  online plus encrypted backups retained according to the legal and operational policy.
- Keep edge rate limiting in front of the app as a second layer. The app-level limiter is a
  product/abuse control, not a DDoS shield.
- Protect `/pro/admin` with the server-side admin session: `SMARTMONEY_ADMIN_SESSION_SECRET`,
  `SMARTMONEY_ADMIN_PASSWORD_PBKDF2`, short `SMARTMONEY_ADMIN_SESSION_SECONDS`, and
  `SMARTMONEY_ADMIN_TOTP_REQUIRED=1` after initial enrollment. Admin page access and Pro API
  mutation power remain separate: the page login opens the panel, while an `admin:write`
  Pro key is still required for key lifecycle actions.

Operational requirements (also in INSTALL_SERVER.md):
- Secure cookies require **HTTPS** (the nginx/TLS step). For local http testing only, set
  `SMARTMONEY_INSECURE_COOKIES=1`.
- Public browser accounts, SMTP verification, HIBP and Stripe settings are not part of
  Core V1.

## Deployment hardening checklist
- [ ] `pip install defusedxml` (activates the strongest XML parse path).
- [ ] Run the API behind auth + TLS (reverse proxy); never `--host 0.0.0.0` without it.
- [ ] Keep `debug=False` (default). Never enable the Werkzeug debugger in production.
- [ ] Put outbound webhook/email traffic behind an **egress allowlist/proxy** — this closes
      the residual DNS-rebinding gap that `validate_public_url` alone cannot (a hostname can
      resolve differently between check and connect).
- [ ] Store secrets in a secrets manager, not shell history; rotate the OpenFIGI/Massive keys.
- [ ] Restrict DB file permissions (`chmod 600`); it holds subscription targets.
- [ ] Add rate limiting at the proxy (the app has none) and request logging.
- [ ] When auth lands: server-side identity → `Tier`; CSRF tokens on any mutating route.
- [ ] If Pro API is enabled, route it to `13flow-pro.service`; public `13flow.service` must
      have no `SMARTMONEY_PRO_API`, no `SMARTMONEY_PRO_DB`, and no writable
      `/var/lib/13flow-pro` path. Back up the Pro DB and monitor audit volume.

## Residual risks (known, documented)
- **DNS rebinding** on webhooks (mitigated, not eliminated — see egress proxy above).
- **Untrusted filing content**: a hostile EDGAR filer could embed markup in an issuer name;
  the dashboard escaping (#3) neutralizes it for the web UI, and the email path is text-only.
- **Resolver confidence**: low-confidence CUSIP→ticker guesses are flagged, not trusted — the
  valuation reconcile ratio is the cross-check; do not treat sub-0.9 mappings as authoritative.

## Confluence / Form 4 (13FLOW)
- **Same EDGAR trust boundary as 13F.** Form 4 ownership documents are semi-trusted external
  XML: parsed with the same `defusedxml` hardening (no DTD/ENTITY, size cap) as the 13F parser
  (finding #4), namespace-agnostic, and one unparseable filing is skipped rather than sinking
  the batch. Issuer names and insider roles reach the UI only through the dashboard's `esc()`
  (finding #3).
- **Read-only, public data.** The endpoint (`GET /api/signals/confluence`) is read-only and
  serves only public-domain SEC data; `window`/`min_score` are clamped server-side (7–365 / 0–100).
- **Live provider rate-limits EDGAR** (UA + ≤8 req/s) and is gated behind `SMARTMONEY_CONFLUENCE_LIVE=1`
  + `SEC_UA`; off by default it serves sample data, so no outbound calls happen unless opted in.
- **Not advice.** It's a transparent *screen* — the per-pillar `breakdown` is exposed precisely
  so a score can't masquerade as a recommendation.

## Frontend / UI hardening
The dashboard and FAQ are static single-file pages; their risk is DOM-XSS, so the defences
are about output encoding and a strict CSP.

- **Output encoding everywhere.** Every dynamic value rendered into `innerHTML` goes through
  `esc()` (escapes `& < > " '`) — tickers, issuer names, fund labels, managers, channels,
  targets, report dates, move codes, insider roles, quadrant classes. User-typed values
  (e.g. the login email) are written with `.textContent`, never `innerHTML`. Audited: no
  unescaped server/user string reaches the DOM as markup.
- **No inline event handlers.** All behaviour is wired with `addEventListener`/`.onclick` in
  the page's single (nonce'd) script — there are zero `on*=` attributes and zero
  `javascript:` URLs in the markup, so the script CSP needs no `'unsafe-inline'`.
- **Per-request nonce CSP.** HTML responses carry
  `default-src 'none'; script-src 'self' 'nonce-…'; style-src 'self' 'unsafe-inline';
  font-src 'self'; img-src 'self' data:;
  connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  The nonce is generated per request (`secrets.token_urlsafe`) and injected into the one
  inline `<script>`. JSON responses get `default-src 'none'`. Even if an escaping bug slipped
  through, injected `<script>` can't run (no matching nonce) and can't exfiltrate
  (`connect-src 'self'`).
  - *Accepted residual*: `style-src 'unsafe-inline'` remains because the UI uses inline
    `style="…"` attributes (which nonces can't cover). Style injection cannot execute JS;
    the practical risk is cosmetic. Removing it would require lifting all inline styles into
    classes — a future refinement, not a hole.
- **Clickjacking / framing.** `X-Frame-Options: DENY` (app) plus `frame-ancestors 'none'`
  (CSP) plus `base-uri 'none'`.
- **Third-party surface.** Public UI fonts are self-hosted from `/assets/fonts/`; the CSP no
  longer allows Google Fonts origins. No third-party JS, analytics, or trackers are loaded by
  the public dashboard, FAQ, legal notice, evidence pages or Pro cockpit pages.

## Open (public, read-only) build
Core V1 always registers the public read-only surface plus the separate Pro API
surface. `SMARTMONEY_OPEN=1` is retained for compatibility.

- **Whole classes of risk removed by construction.** Auth, billing, subscriptions, and browser
  alert routes are not implemented — `/api/auth/*`, `/api/billing/*`,
  `/api/subscriptions`, `/api/alerts/*` return **404**. No sessions, cookies, CSRF,
  password, or payment code runs.
- **Pro API is explicit.** `/api/pro/v1/*` is registered only when `SMARTMONEY_PRO_API=1`
  and is protected by API keys, scopes, rate limits, and audit in the separate Pro DB.
- **GET-only, enforced twice.** Only read endpoints exist; Apache also denies anything but
  `GET/HEAD/OPTIONS` at the edge (`<LimitExcept>`).
- **The web process cannot write the DB.** `SMARTMONEY_DB_READONLY=1` opens SQLite
  `mode=ro`; filesystem ownership (ingest user writes, web user reads) enforces it again. The
  ingest job runs separately and checkpoints the WAL so the served file is self-contained.
- **Hardened error surface.** Bad query params (e.g. `min_funds=abc`) return a uniform JSON
  **400**, not a 500; all `HTTPException`s render as `{"error": …}` and any uncaught
  exception is logged and returned as a generic JSON **500** — no stack traces, no HTML error
  pages, no framework/version leakage.
- **systemd sandbox + TLS reverse proxy.** See `deploy/` — non-root service,
  `ProtectSystem=strict`, no capabilities, `MemoryDenyWriteExecute`, syscall allow-list;
  bound to `127.0.0.1` behind Apache TLS with HSTS.
- **Residual / by-design.** Rate limiting is at the edge (mod_evasive / CDN), not in-app.
  Live Confluence (`SMARTMONEY_CONFLUENCE_LIVE=1`) makes the web tier do outbound EDGAR
  calls — leave it off (sample) or precompute if you want the web tier strictly offline.
