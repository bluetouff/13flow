# TEST_LOCAL — run & test SmartMoney on your machine

A step-by-step path from zero to a running dashboard, with no database server and no
cloud anything. Everything except the optional "live EDGAR" step works fully offline.

## Prerequisites
- **Python 3.10+** — check with `python3 --version`
- A web browser
- (That's it — no Node, no DB server, no Docker required)

---

## 1. Get the code
Unzip the project and enter the folder:
```bash
unzip smartmoney.zip
cd smartmoney
```

## 2. Create a virtual environment and install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Run the test suite (offline)
```bash
python -m pytest tests/ -q
```
You should see all suites pass (parsing, OpenFIGI, persistence, valuation, alerts,
resolver, security). This confirms the install is healthy.

## 4. Preview the dashboard with zero setup
Just open the file in a browser — double-click `dashboard.html`, or:
```bash
open dashboard.html          # macOS   (Linux: xdg-open dashboard.html)
```
It can't reach an API this way, so it renders **built-in sample data** — the badge in the
sidebar reads `SAMPLE DATA`. This is enough to judge the look and the four screens
(Consensus / Funds / Compare / Alerts).

## 5. Run it against a real backend (sample DB, still offline)
Seed a demo database, then start the API (which also serves the dashboard):
```bash
python seed_demo.py --db demo.db
SMARTMONEY_INSECURE_COOKIES=1 SMARTMONEY_DEV_EMAIL_ECHO=1 python -m smartmoney.api --db demo.db
```
Open **http://127.0.0.1:5000** — the badge now reads `LIVE`, and every number comes from
the API reading the SQLite DB. Press `Ctrl-C` to stop the server.

> **Why these two env vars (local only)?**
> - `SMARTMONEY_INSECURE_COOKIES=1` — the session cookie is `Secure`, so over plain
>   `http://localhost` the browser would refuse to store it and sign-in would silently fail.
>   This drops `Secure` **for local testing only** (production uses HTTPS).
> - `SMARTMONEY_DEV_EMAIL_ECHO=1` — there's no SMTP locally, so this makes the API hand back
>   the email-verification link in its response; the dashboard then shows a **"Dev: verify
>   now →"** link so you can verify in one click. **Never set either in production.**

## 5b. Try the accounts / email-verification flow (graphical)
1. In the dashboard click **Sign in → Create one**, enter an email + a strong password
   (≥ 12 chars; common/breached passwords are rejected) and submit.
2. You'll see **"Check your email"**. Because dev-echo is on, a **"Dev: verify now →"** link
   appears — click it. The page bounces back with a *"Email verified"* toast.
3. Now **Sign in** with the same credentials. (Try signing in *before* verifying to see the
   "Email not verified — resend" path.)
4. Open the **Alerts** screen. A *free* account is refused with a `402` upgrade prompt
   (the paid gate is enforced server-side).

Prefer to skip email for an operator account? Create one pre-verified from the CLI:
```bash
python run.py --db demo.db --create-user me@example.com --tier paid   # password prompted, pre-verified
python run.py --db demo.db --verify-user someone@example.com           # verify an existing user
```

## 5c. Test the upgrade flow graphically (mock Stripe checkout)
With no `STRIPE_SECRET_KEY` set, billing runs in **mock mode** — a local fake checkout so you
can click through the whole upgrade end-to-end, no Stripe account or network needed:
1. Sign in as the **free** user above.
2. Click **★ Upgrade to Pro** in the sidebar (or the upgrade link on the Alerts screen).
3. The pricing card opens → **Continue to checkout** → you land on a styled checkout page
   (test card `4242 4242 4242 4242` is pre-filled) → click **Pay €12.00**.
4. You're redirected back; a toast says *"Welcome to Pro — alerts unlocked"*, the sidebar
   pill flips to **PAID**, and you can now add subscriptions on the Alerts screen.

Under the hood the "Pay" button calls the same code path a real Stripe webhook would: the
tier flips server-side, not from the browser. The mock endpoints exist only in mock mode.

> To rehearse the **real** Stripe flow locally instead, set `STRIPE_SECRET_KEY`,
> `STRIPE_PRICE_ID`, and `STRIPE_WEBHOOK_SECRET` (test-mode keys) and run `stripe listen
> --forward-to localhost:5000/api/billing/webhook` — see INSTALL_SERVER.md.

## 6. Try the command line against the demo DB (offline)
In a second terminal (with the venv activated):
```bash
python run.py --db demo.db --coverage
python run.py --db demo.db --buys 2024-06-30 --min-funds 2
python run.py --db demo.db --consensus 2024-06-30 --min-funds 2
python run.py --db demo.db --timeline "Berkshire Hathaway" --cusip 037833100
python run.py --list
```

## 7. (Optional) Pull REAL data from EDGAR
This step uses the internet. The SEC requires a contact email in your User-Agent or it
returns 403.
```bash
export SEC_UA="SmartMoney/1.0 you@example.com"     # use a real email
export OPENFIGI_APIKEY="..."                        # optional, free, raises rate limits
python run.py --sync "Berkshire Hathaway" --enrich --db live.db
python run.py --db live.db --coverage
python run.py --db live.db --value "Berkshire Hathaway"   # current weights + implied P&L (stooq)
python -m smartmoney.api --db live.db                     # dashboard on real data
```
Windows (PowerShell): use `setx` or `$env:SEC_UA="..."` instead of `export`.

## 8. (Optional) Try alerts (offline)
```bash
python run.py --db demo.db --subscribe "Berkshire Hathaway" --channel console --no-prime
python run.py --db demo.db --alerts-dispatch      # prints the diff that would be delivered
```

## 9. The Confluence tab (13F × Form 4)
Open the dashboard (step 5) and click **✦ Confluence** in the sidebar. It works **out of the
box on sample data** — KPI row, the quadrant map, and ranked cards with a per-pillar score
breakdown, switchable across 30/90/180-day windows. No DB rows or network needed; the badge
reads SAMPLE.

To run it **live** (pulls real Form 4s from EDGAR for the tickers your funds are accumulating):
```bash
SEC_UA="you@example.com" SMARTMONEY_CONFLUENCE_LIVE=1 \
  SMARTMONEY_INSECURE_COOKIES=1 python -m smartmoney.api --db live.db
```
Evaluate the scoring hypothesis or fit research weights with the backtest harness:
```bash
python -m smartmoney.backtest        # synthetic demo only; not live-history validation
```
Treat default weights as heuristic until `VALIDATION_PROTOCOL.md` has been run and published
for a frozen score version.

---

## Troubleshooting
- **`403` from the SEC** → set `SEC_UA` to a string containing a real email (step 7).
- **`Address already in use`** → start the API on another port: `--port 5001`.
- **`ModuleNotFoundError: smartmoney`** → run commands from the project root (the folder
  that contains `run.py` and the `smartmoney/` directory), with the venv activated.
- **pytest can't find tests** → run `python -m pytest tests/` from the project root.
- **Dashboard shows SAMPLE not LIVE** → you opened the file directly (step 4) instead of via
  the API (step 5), or the API is on a different port.
