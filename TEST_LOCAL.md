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

## 4. Preview the dashboard with explicit demo data
Just open the file in a browser — double-click `dashboard.html`, or:
```bash
open 'dashboard.html?demo=1'          # macOS   (Linux: xdg-open 'dashboard.html?demo=1')
```
Without `?demo=1`, API failures are displayed as errors and no sample data is substituted.
With `?demo=1`, the browser renders built-in sample data. This is enough to judge the look
of the UI without confusing demo data for live production data.

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

## 5b. Browser accounts and checkout
Core V1 has no browser account, e-mail verification, mock checkout or Stripe
flow. Test Pro access through the Pro API onboarding and workspace smoke scripts
instead.

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
Open the dashboard (step 5) and click **✦ Confluence** in the sidebar. Without a precomputed
cache or live provider, the tab shows a clear `confluence_unavailable` error. For UI previews
only, start the server with `SMARTMONEY_CONFLUENCE_DEMO=1` or open the browser with `?demo=1`.

To run it **live** (pulls real Form 4s from EDGAR for the tickers your funds are accumulating):
```bash
SEC_UA="you@example.com" SMARTMONEY_CONFLUENCE_LIVE=1 \
  SMARTMONEY_INSECURE_COOKIES=1 python -m smartmoney.api --db live.db
```
Evaluate the scoring hypothesis or fit research weights with the backtest harness:
```bash
python -m smartmoney.backtest        # synthetic demo only; not live-history validation
```
Gate a real point-in-time feature table before publication:
```bash
python run.py --db live.db \
  --build-validation-dataset /tmp/confluence_features.csv \
  --validation-prices /path/to/adjusted_prices.csv \
  --validation-code-commit "$SHA" \
  --validation-json

python run.py --validation-dataset /path/to/confluence_features.csv --validation-json
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
