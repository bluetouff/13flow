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

## Authentication & accounts (finding #7 — implemented)
`accounts.py` + `auth.py` + `pwhash.py` provide the accounts layer. Design choices, all
chosen for security:
- **Password hashing** — Argon2id (`argon2-cffi`) when available, else stdlib `scrypt`
  (~64 MiB, memory-hard); per-password salt; optional server-side **pepper**
  (`SMARTMONEY_PW_PEPPER`) HMAC-mixed before hashing so a DB leak alone is not crackable;
  constant-time verify; transparent rehash-on-login for upgrades.
- **Sessions** — opaque 256-bit random tokens; only their SHA-256 is stored (DB read yields
  no usable token); absolute (30d) + idle (7d) expiry; revocable (logout, logout-all, and
  automatically on password change). Chosen over JWT for revocability and to avoid
  alg-confusion / key-management pitfalls.
- **Cookies** — `sm_session` is HttpOnly + Secure + SameSite=Strict; `sm_csrf` is the
  readable half of a **double-submit CSRF** check enforced (constant-time) on all mutating
  `/api` requests that carry a session.
- **Brute force** — per-account failed-attempt counter with temporary lockout, plus per-IP
  rate limiting on `/api/auth/*`.
- **Enumeration resistance** — login returns one generic error and runs a dummy KDF verify
  for unknown accounts (timing parity); password reset always responds 200.
- **Password reset** — single-use, 1-hour, hashed tokens; delivered out of band.
- **Email verification** — registration creates an *unverified* account and emails a
  single-use 24h hashed token; login is refused until verified, but the check runs only
  *after* a correct password, so it never reveals which emails are registered. The
  resend/verify endpoints always respond 200. Invariant: a valid session ⇒ a verified email
  (enforced at the login chokepoint, plus defense-in-depth checks on checkout/subscribe).
- **Breached-password check** — new/changed/reset passwords are checked against HaveIBeenPwned
  via the **k-anonymity range API**: only the SHA-1's first 5 hex chars are sent (never the
  password, never the full hash), with `Add-Padding: true` to blunt traffic analysis.
  Fail-open by default (an HIBP outage can't block all signups); the local deny-list is the
  fast first pass. SHA-1 here is only the corpus index — password *storage* is Argon2id/scrypt.
- **Server-side tier** — `Tier` is built from the authenticated user's DB row on every
  request; paid features (alert subscriptions) return **402** for free users. The client
  cannot assert its own tier. Public read-only market data stays open (it is public-domain).

Operational requirements (also in INSTALL_SERVER.md):
- Secure cookies require **HTTPS** (the nginx/TLS step). For local http testing only, set
  `SMARTMONEY_INSECURE_COOKIES=1`.
- Set `SMARTMONEY_PW_PEPPER` to a long random secret in production; store it outside the DB.
  (Rotating it invalidates existing hashes — plan a re-hash-on-login migration if you do.)
- Configure SMTP (`SMTP_HOST`/`PORT`/`USER`/`PASS`/`FROM`) so verification mail is actually
  sent; without it the link is only logged. Never set `SMARTMONEY_DEV_EMAIL_ECHO=1` in
  production (it returns the verification link in the API response — dev/local only).
- The auth rate limiter is in-process; behind multiple workers/hosts, back it with Redis.
- HIBP runs fail-open; set `SMARTMONEY_HIBP_FAIL_CLOSED=1` to reject on outage, or
  `SMARTMONEY_DISABLE_HIBP=1` to turn it off. It needs outbound HTTPS to api.pwnedpasswords.com.
- Bootstrap accounts with `run.py --create-user` / `--set-tier` (password via `getpass`,
  never on the command line).

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
  `default-src 'none'; script-src 'self' 'nonce-…'; style-src 'self' 'unsafe-inline'
  https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:;
  connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  The nonce is generated per request (`secrets.token_urlsafe`) and injected into the one
  inline `<script>`. JSON responses get `default-src 'none'`. Even if an escaping bug slipped
  through, injected `<script>` can't run (no matching nonce) and can't exfiltrate
  (`connect-src 'self'`).
  - *Accepted residual*: `style-src 'unsafe-inline'` remains because the UI uses inline
    `style="…"` attributes (which nonces can't cover). Style injection cannot execute JS;
    the practical risk is cosmetic. Removing it would require lifting all inline styles into
    classes — a future refinement, not a hole.
- **Safe redirects.** The (full-build) billing flow only follows a returned URL via
  `safeGo()`, which requires an `http(s)://` scheme — no `javascript:`/`data:` open-redirect.
- **Clickjacking / framing.** `X-Frame-Options: DENY` (app) plus `frame-ancestors 'none'`
  (CSP) plus `base-uri 'none'`.
- **Third-party surface.** The only external origins are Google Fonts (CSS + font files),
  pinned in the CSP. No third-party JS, analytics, or trackers. (Self-hosting the fonts would
  drop even that origin — a further-hardening option.)

## Open (public, read-only) build
Toggled by `SMARTMONEY_OPEN=1`; intended for an unauthenticated public deployment.

- **Whole classes of risk removed by construction.** Auth, billing, subscriptions, and alert
  routes are **not registered** — `/api/auth/*`, `/api/billing/*`, `/api/subscriptions`,
  `/api/alerts/*` return **404**. No sessions, cookies, CSRF, password, or payment code runs.
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
