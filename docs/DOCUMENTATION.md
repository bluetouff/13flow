<!-- 13FLOW — documentation technique & fonctionnelle / technical & functional documentation.
     Bilingue : Français d'abord, English below. Niveau contributeur (modules, schéma DB, score). -->

# 13FLOW — Documentation

> 🇫🇷 **Français ci-dessous.** &nbsp;·&nbsp; 🇬🇧 [**Jump to English**](#-13flow--documentation-english)

Données : **SEC EDGAR** (13F-HR + Form 4), domaine public US. 13FLOW est un **écran d'analyse**, pas un conseil en investissement.

---

## Présentation

13FLOW croise deux signaux publics pour repérer où l'argent intelligent se positionne :

- **13F-HR** — les positions trimestrielles des grands gérants institutionnels (« superinvestors »).
- **Form 4** — les transactions des initiés (dirigeants, administrateurs) sur leur propre société.

L'idée centrale (**Confluence**) : un titre est d'autant plus intéressant que **des fonds accumulent** *et* que **des initiés achètent sur le marché** en même temps. Chaque page du produit éclaire un angle de cette piste.

Stack : **Python 3.10+ / Flask / SQLite (WAL) / Gunicorn**, derrière **Apache2** en reverse-proxy TLS. Aucune dépendance propriétaire pour le cœur (EDGAR et OpenFIGI sont gratuits).

---

## Documentation fonctionnelle

### Les quatre vues

| Vue | Question à laquelle elle répond | Source |
|-----|----------------------------------|--------|
| **Consensus** | Sur quels titres plusieurs fonds convergent-ils (achats/ventes) à un trimestre donné ? | 13F |
| **Funds** | Que détient chaque superinvestor, et comment son portefeuille évolue-t-il ? | 13F |
| **Compare** | Comment se recoupent les portefeuilles de plusieurs fonds ? | 13F |
| **Confluence** | Où **fonds *et* initiés** achètent-ils en même temps ? (le cœur du produit) | 13F × Form 4 |

### Le score de Confluence (0–100)

Chaque titre reçoit un score transparent, somme de **quatre piliers** moins des **pénalités**, borné à 0–100. Les poids par défaut (modifiables, voir `crosssignal.py · Weights`) :

| Pilier | Plafond | Ce qu'il mesure |
|--------|---------|-----------------|
| **Institutional breadth** | 36 | nombre de fonds qui accumulent × conviction (poids moyen, fonds à forte conviction) × **récence du 13F** (décote `0.6^trimestres`) |
| **Insider cluster** | 36 | conviction des initiés (taille, **récence**, séniorité — un achat de CEO/CFO pèse plus, un cluster récent aussi) |
| **Dollar conviction** | 18 | montant $ engagé par les initiés, **échelle log**, pondéré par la récence (plafond 25 M$) |
| **Agreement bonus** | 18 | bonus de confluence quand **les deux côtés achètent**, amplifié par la fraîcheur et le clustering |
| *Pénalités* | −14 / −14 | fonds qui **allègent** plus qu'ils n'accumulent ; initiés **vendeurs** nets |

> Les plafonds **somment à plus de 100 volontairement** : chaque pilier sature (fonction `_saturating`), donc aucun titre réel ne maxe tout. Le score final est clampé 0–100.

### Les quadrants (la « carte »)

La carte place chaque titre selon deux axes :
- **x = funds accumulating →** (net de fonds acheteurs vs vendeurs, saturé)
- **y = ↑ insiders buying** (net d'initiés acheteurs vs vendeurs, saturé)

D'où quatre cadrans :
- **Conviction** (haut-droite) : fonds **et** initiés achètent — le signal le plus fort.
- **Institutions only** (bas-droite) : fonds accumulent, initiés silencieux.
- **Insider only** (haut-gauche) : initiés achètent, fonds absents.
- **Distribution** (bas-gauche) : tout le monde sort.

> ⚠️ **Lecture honnête.** Le compteur « signals » inclut tout titre où **au moins un fonds accumule** (seuil `min_funds=1`), même sans achat d'initié. Ce qui a de la valeur, c'est la **zone Conviction** et le **haut du classement par score**, pas le total brut.

> ⚠️ **Périmètre data.** Le 13F ne montre pas un portefeuille complet : pas les shorts,
> pas la plupart des lignes internationales, pas les obligations, pas tout le livre
> dérivé, pas les mouvements intra-trimestre ni les positions sous traitement
> confidentiel. Le rail Form 4 public est aussi borné : l'univers insider live dépend
> du seuil d'activité 13F, les quadrants « insider only » / « distribution » ne sont
> pas exhaustifs, et les dérivés Table II, flags 10b5-1, attributions multi-owners et
> footnotes de prix moyens restent des limites explicites tant qu'ils ne sont pas
> modélisés.

Détails de la méthodologie côté produit : voir `/faq`. Spécifique Form 4 : voir [`FORMS4_INTEGRATION.md`](FORMS4_INTEGRATION.md).

---

## Documentation technique

### Arborescence

```
13flow/
├── dashboard.html          # source de l'app de recherche, servie à `/app`
├── faq.html                # source FAQ, servie à `/faq`
├── mentions-legales.html   # source legal, servie à `/legal`
├── run.py                  # CLI (ingestion, vérif CIK, pré-calcul Confluence, comptes…)
├── wsgi.py                 # entrée Gunicorn (create_app via variables d'env)
├── seed_demo.py            # base de démo hors-ligne (5 fonds, zéro réseau)
├── requirements.txt / pyproject.toml
├── smartmoney/             # le package applicatif (~28 modules, voir ci-dessous)
├── deploy/                 # systemd, vhost Apache, scripts (backfill, refresh, preflight)
├── tests/                  # suites hors-ligne (mocks réseau)
├── SECURITY.md             # architecture de sécurité
├── INSTALL_SERVER.md       # déploiement Debian + Apache (13flow.eu)
├── TEST_LOCAL.md           # lancer en local
└── docs/DOCUMENTATION.md   # ce fichier
```

> Le package interne s'appelle `smartmoney` (nom historique) ; le produit, lui, est **13FLOW**.

### Pipeline de données

```
EDGAR (13F-HR)                                   EDGAR (Form 4)
   │  edgar.py (RateLimiter 8 req/s)                │  forms4.py (_safe_xml: anti-XXE)
   ▼                                                ▼
parser.py (defusedxml)  ──►  portfolio.py     aggregate_insider_activity()
   │ info-table XML → positions                     │  achats open-market (P/S) only
   ▼                                                ▼
resolver.py + figi.py (CUSIP → ticker)        InstitutionalSignal × InsiderActivity
   │ cache: SMARTMONEY_CACHE_DIR                     │
   ▼                                                ▼
db.py (Store, SQLite WAL)  ◄── tracker.py     crosssignal.build_confluence() ──► ConfluenceSignal[]
                                                    │  (run.py --confluence)
                                                    ▼
                                          cache JSON confluence-{30,90,180}.json
```

**Côté 13F** : `tracker.sync_fund` liste les dépôts d'un fonds (`edgar`), parse l'info-table (`parser`), résout les CUSIP en tickers (`resolver` + `figi`/OpenFIGI, avec cache), et persiste (`db.Store`). Idempotent : les dépôts déjà stockés sont sautés.

**Côté Form 4** : le provider live (`api._StoreConfluence`) prend les tickers que les fonds **accumulent au dernier trimestre**, mappe ticker → CIK émetteur via `company_tickers.json`, récupère les Form 4 récents et agrège **uniquement les achats open-market** (codes P/S ; les attributions/exercices d'options sont exclus).

### Schéma SQLite

```sql
funds(cik PK, label, manager)
filings(accession PK, cik→funds, form, filing_date, report_date,
        total_value, n_positions, fetched_at)            -- index (cik, report_date)
holdings(accession→filings ON DELETE CASCADE, cusip, put_call,
         issuer, title_of_class, ticker, figi_name,
         ticker_source, ticker_confidence,
         value_usd, shares, weight,
         PK(accession, cusip, put_call))                  -- index cusip, ticker
subscriptions(...)  deliveries(...)                       -- alertes (build complet only)
VIEW latest_filings(cik, report_date, accession)          -- dernier accession par trimestre
```

Le mode **WAL** accélère l'ingestion ; mais le tier web ouvre la base en `mode=ro`, ce qui exige de **« publier »** la base après écriture : `PRAGMA wal_checkpoint(TRUNCATE)` **+** `PRAGMA journal_mode=DELETE` (sinon l'ouverture lecture-seule échoue → page Funds cassée). Les scripts `deploy/backfill.sh` et `deploy/refresh-data.sh` le font automatiquement.

### Inventaire des modules (`smartmoney/`)

| Module | Rôle |
|--------|------|
| `edgar.py` | Client EDGAR, rate-limiter 8 req/s, récupération des dépôts/info-tables |
| `parser.py` | Parse XML durci (defusedxml) des info-tables 13F |
| `portfolio.py` | Modèle de portefeuille, agrégation des positions |
| `diff.py` | Diff inter-trimestres → mouvements `NEW / ADD / TRIM / EXIT` |
| `figi.py` | Client OpenFIGI (CUSIP→ticker), cache `TickerCache` |
| `resolver.py` | Résolution CUSIP→ticker (index SEC + OpenFIGI), cache de résolution |
| `valuation.py` | Revalorisation d'un portefeuille à un prix donné |
| `prices.py` | Fournisseurs de prix (Stooq gratuit par défaut ; Massive en option, clé en header) |
| `db.py` | `Store` SQLite (schéma, migrations, `read_only`), analytics consensus |
| `analytics.py` | Requêtes de consensus (accumulation/distribution) |
| `tracker.py` | Orchestration de l'ingestion d'un fonds |
| `registry.py` | Liste `SUPERINVESTORS` = `Fund(label, manager, cik|None, search_name)` |
| `forms4.py` | Client Form 4, parse XML durci, extraction des transactions d'initiés |
| `crosssignal.py` | **Cœur du score** : `Weights`, `score_confluence`, quadrants, `build_confluence` |
| `api.py` | App Flask (`create_app`), routes, `_StoreConfluence` (provider live), CSP à nonce |
| `api_signals.py` | Blueprint `/api/signals/confluence`, `confluence_payload`, cache + providers |
| `sample_confluence.py` | Données de démo « live-shaped » (zéro réseau) |
| `backtest.py` | Harnais de calibration des poids (IC de rang, spread quintiles, hit-rate) |
| `accounts.py` `auth.py` `pwhash.py` `hibp.py` `billing.py` `notify.py` `alerts.py` `channels.py` `netsec.py` | Briques du **build complet** (comptes, sessions/CSRF, hash Argon2id, k-anonymity HIBP, Stripe, e-mail, alertes, garde SSRF). **Non chargées en mode ouvert.** |

### API HTTP (lecture seule en mode ouvert)

| Route | Description |
|-------|-------------|
| `GET /api/config` | drapeaux du build (`open`, features) |
| `GET /api/funds` | liste des fonds suivis + sparkline AUM |
| `GET /api/fund/<cik>` | portefeuille + diff du dernier trimestre |
| `GET /api/consensus/holdings` · `…/buys` | consensus à une date (`?min_funds=`, borné) |
| `GET /api/compare?ciks=…` | recoupement de portefeuilles (≤ 12 CIK) |
| `GET /api/coverage` | couverture de résolution ticker |
| `GET /api/signals/confluence?window=30\|90\|180` | signaux de confluence (sert le **cache** si présent) |
| `GET /` · `/faq` · `/legal` | pages HTML (CSP à **nonce** par requête) |
| *(build complet)* `…/auth/*`, `…/billing/*`, `…/subscriptions`, `…/alerts/*` | **404 en mode ouvert** |

### Interface en ligne de commande (`run.py`)

```bash
run.py --list                      # lister les fonds suivis (hors-ligne)
run.py --verify                    # vérifier les CIK contre EDGAR
run.py --sync "Fund" --enrich [--max-quarters N]   # ingérer un fonds
run.py --sync-all --enrich --max-quarters 8        # ingérer tous (toujours borner !)
run.py --confluence [--confluence-windows 30,90,180]  # pré-calculer le cache Confluence
run.py --consensus YYYY-MM-DD --min-funds 3        # consensus (hors-ligne)
```
> `--sync*` et `--confluence` accèdent à EDGAR et exigent `SEC_UA`. **Toujours** borner avec `--max-quarters` (sans borne = historique complet = très long).

### Modes de déploiement

- **Ouvert** (`SMARTMONEY_OPEN=1`, `SMARTMONEY_DB_READONLY=1`) : public, lecture seule, sans comptes/Stripe/alertes. C'est le mode de 13flow.eu.
- **Complet** : ajoute comptes, vérification e-mail, Stripe, alertes Form 4. Nécessite secrets (`STRIPE_*`, `SMARTMONEY_PW_PEPPER`, SMTP) + `ProxyFix` derrière le proxy.

Le provider Confluence se résout dans cet ordre : **cache JSON** (`SMARTMONEY_CACHE_DIR/confluence-{window}.json`) → **live** (`SMARTMONEY_CONFLUENCE_LIVE=1` + EDGAR) → **démo**.

### Sécurité (résumé — détail dans [`SECURITY.md`](SECURITY.md))

CSP stricte à nonce par requête (pas d'`unsafe-inline` côté script), `default-src 'none'` sur le JSON, échappement systématique, GET-only + allow-list Apache, base en lecture seule, XML durci (anti-XXE), erreurs JSON génériques, sandbox systemd, TLS+HSTS, aucun secret côté front. Le fichier d'env réel et les bases **ne sont jamais commités** (voir `.gitignore`).

### Déploiement & dev local

- Production Debian + Apache : [`INSTALL_SERVER.md`](INSTALL_SERVER.md).
- Lancer en local : [`TEST_LOCAL.md`](TEST_LOCAL.md).
- Tests : `pytest tests/` (suites hors-ligne, réseau mocké).

### Ajouter un fonds

1. Trouver le **CIK du déposant 13F** (l'institution, pas l'émetteur) sur EDGAR.
2. Ajouter une ligne `Fund("Label", "Manager", "CIK", "Search name")` dans `registry.py` (`cik=None` si non confirmé → résolution par nom).
3. `run.py --verify` (doit afficher le bon nom EDGAR).
4. Ingérer : `deploy/backfill.sh 8` (ingestion + publication WAL + permissions + restart) — jamais un `--sync-all` nu.
5. Recalculer la Confluence (`run.py --confluence`) puis vérifier (`deploy/preflight.sh`).

---
---

# 🇬🇧 13FLOW — Documentation (English)

> [**Version française ci-dessus**](#13flow--documentation)

Data: **SEC EDGAR** (13F-HR + Form 4), US public domain. 13FLOW is an **analysis screen**, not investment advice.

---

## Overview

13FLOW crosses two public signals to spot where smart money is positioning:

- **13F-HR** — quarterly holdings of large institutional managers ("superinvestors").
- **Form 4** — insider (officer/director) transactions in their own company.

Core idea (**Confluence**): a name matters more when **funds are accumulating** *and* **insiders are buying open-market** at the same time. Each page of the product lights up one angle of that trail.

Stack: **Python 3.10+ / Flask / SQLite (WAL) / Gunicorn**, behind **Apache2** as a TLS reverse proxy. No proprietary core dependency (EDGAR and OpenFIGI are free).

---

## Functional documentation

### The four views

| View | Question it answers | Source |
|------|---------------------|--------|
| **Consensus** | Which names do several funds converge on (buys/sells) in a given quarter? | 13F |
| **Funds** | What does each superinvestor hold, and how is the portfolio moving? | 13F |
| **Compare** | How do several funds' portfolios overlap? | 13F |
| **Confluence** | Where do **funds *and* insiders** buy at the same time? (the core) | 13F × Form 4 |

### The Confluence score (0–100)

Each name gets a transparent score: the sum of **four pillars** minus **penalties**, clamped to 0–100. Default weights (tunable, see `crosssignal.py · Weights`):

| Pillar | Cap | What it measures |
|--------|-----|------------------|
| **Institutional breadth** | 36 | number of accumulating funds × conviction (avg weight, high-conviction funds) × **13F recency** (`0.6^quarters` decay) |
| **Insider cluster** | 36 | insider conviction (size, **recency**, seniority — a CEO/CFO buy weighs more; a recent cluster too) |
| **Dollar conviction** | 18 | $ committed by insiders, **log scale**, recency-weighted (25M$ ceiling) |
| **Agreement bonus** | 18 | confluence bonus when **both sides buy**, amplified by freshness + clustering |
| *Penalties* | −14 / −14 | funds **trimming** more than accumulating; net insider **sellers** |

> Caps **sum above 100 on purpose**: each pillar saturates (`_saturating`), so no real name maxes them all. Final score clamps 0–100.

### Quadrants (the "map")

The map places each name on two axes:
- **x = funds accumulating →** (net buying vs trimming funds, saturated)
- **y = ↑ insiders buying** (net buying vs selling insiders, saturated)

Four quadrants: **Conviction** (top-right, both buying — strongest), **Institutions only** (bottom-right), **Insider only** (top-left), **Distribution** (bottom-left, everyone exiting).

> ⚠️ **Honest reading.** The "signals" counter includes any name where **at least one fund accumulates** (`min_funds=1`), even with no insider buy. What matters is the **Conviction zone** and the **top of the score ranking**, not the raw total.

> ⚠️ **Data scope.** 13F does not show a complete portfolio: no shorts, most
> international lines, bonds, full derivative books, intra-quarter moves or
> confidential-treatment omissions. The public Form 4 rail is also bounded: the
> live insider universe depends on the 13F activity threshold, "insider only" /
> "distribution" quadrants are not exhaustive, and Table II derivatives, 10b5-1
> flags, multi-owner attribution and weighted-average price footnotes remain
> explicit limitations until modeled.

Product methodology: see `/faq`. Form 4 specifics: [`FORMS4_INTEGRATION.md`](FORMS4_INTEGRATION.md).

---

## Technical documentation

### Layout

```
13flow/
├── dashboard.html          # research app HTML source, served at `/app`
├── faq.html · mentions-legales.html  # sources served at `/faq` and `/legal`
├── run.py                  # CLI (ingest, verify CIKs, precompute Confluence, accounts…)
├── wsgi.py                 # Gunicorn entrypoint (create_app from env)
├── seed_demo.py            # offline demo DB (5 funds, no network)
├── smartmoney/             # the application package (~28 modules)
├── deploy/                 # systemd, Apache vhost, scripts (backfill, refresh, preflight)
├── tests/                  # offline suites (network mocked)
└── docs/DOCUMENTATION.md   # this file
```

> The internal package is named `smartmoney` (legacy); the product is **13FLOW**.

### Data pipeline

```
EDGAR (13F-HR)                                   EDGAR (Form 4)
   │  edgar.py (8 req/s limiter)                    │  forms4.py (_safe_xml: anti-XXE)
   ▼                                                ▼
parser.py (defusedxml) → portfolio.py        aggregate_insider_activity()  (open-market P/S only)
   ▼                                                ▼
resolver.py + figi.py (CUSIP→ticker)         InstitutionalSignal × InsiderActivity
   ▼ cache: SMARTMONEY_CACHE_DIR                    ▼
db.py (Store, SQLite WAL) ◄ tracker.py        crosssignal.build_confluence() → ConfluenceSignal[]
                                                    ▼  (run.py --confluence)
                                          confluence-{30,90,180}.json (cache)
```

**13F side:** `tracker.sync_fund` lists a fund's filings (`edgar`), parses the info-table (`parser`), resolves CUSIPs to tickers (`resolver` + `figi`/OpenFIGI, cached), and persists (`db.Store`). Idempotent: stored filings are skipped.

**Form 4 side:** the live provider (`api._StoreConfluence`) takes tickers funds **accumulated last quarter**, maps ticker → issuer CIK via `company_tickers.json`, fetches recent Form 4s, and aggregates **open-market buys only** (P/S codes; option grants/exercises excluded).

### SQLite schema

```sql
funds(cik PK, label, manager)
filings(accession PK, cik→funds, form, filing_date, report_date,
        total_value, n_positions, fetched_at)            -- index (cik, report_date)
holdings(accession→filings ON DELETE CASCADE, cusip, put_call,
         issuer, title_of_class, ticker, figi_name,
         ticker_source, ticker_confidence,
         value_usd, shares, weight,
         PK(accession, cusip, put_call))                  -- index cusip, ticker
subscriptions(...)  deliveries(...)                       -- alerts (full build only)
VIEW latest_filings(cik, report_date, accession)
```

**WAL** speeds ingestion, but the web tier opens the DB `mode=ro`, which requires **"publishing"** the DB after writes: `PRAGMA wal_checkpoint(TRUNCATE)` **+** `PRAGMA journal_mode=DELETE` (otherwise the read-only open fails → broken Funds page). `deploy/backfill.sh` and `deploy/refresh-data.sh` do this automatically.

### Module inventory (`smartmoney/`)

| Module | Role |
|--------|------|
| `edgar.py` | EDGAR client, 8 req/s limiter, filing/info-table fetch |
| `parser.py` | Hardened XML parse (defusedxml) of 13F info-tables |
| `portfolio.py` · `diff.py` | Portfolio model; quarter diff → `NEW/ADD/TRIM/EXIT` moves |
| `figi.py` · `resolver.py` | OpenFIGI client + CUSIP→ticker resolution, caches |
| `valuation.py` · `prices.py` | Revaluation; price providers (Stooq free default, Massive optional) |
| `db.py` · `analytics.py` | SQLite `Store` (schema, migrations, `read_only`); consensus queries |
| `tracker.py` · `registry.py` | Ingestion orchestration; `SUPERINVESTORS` list |
| `forms4.py` | Form 4 client, hardened XML, insider-transaction extraction |
| `crosssignal.py` | **Scoring core**: `Weights`, `score_confluence`, quadrants, `build_confluence` |
| `api.py` · `api_signals.py` | Flask app, routes, live provider, nonce CSP; confluence blueprint + cache |
| `sample_confluence.py` | Live-shaped demo data (no network) |
| `backtest.py` | Weight-calibration harness (rank IC, quintile spread, hit-rate) |
| `accounts/auth/pwhash/hibp/billing/notify/alerts/channels/netsec` | **Full-build** bricks (accounts, sessions/CSRF, Argon2id, HIBP, Stripe, email, alerts, SSRF guard). **Not loaded in open mode.** |

### HTTP API (read-only in open mode)

`GET /api/config` · `…/funds` · `…/fund/<cik>` · `…/consensus/holdings|buys` (`?min_funds=`, bounded) · `…/compare?ciks=` (≤12) · `…/coverage` · `…/signals/confluence?window=30|90|180` (serves the **cache** if present). HTML pages `/`, `/app`, `/faq`, `/legal` carry a **per-request nonce CSP**. Legacy aliases `/dashboard.html`, `/faq.html`, `/mentions-legales` and `/mentions-legales.html` redirect to canonical URLs. Full-build routes (`auth/billing/subscriptions/alerts`) return **404 in open mode**.

### CLI (`run.py`)

```bash
run.py --list                                     # list tracked funds (offline)
run.py --verify                                   # verify CIKs vs EDGAR
run.py --sync "Fund" --enrich [--max-quarters N]  # ingest one fund
run.py --sync-all --enrich --max-quarters 8       # ingest all (always bound!)
run.py --confluence [--confluence-windows 30,90,180]  # precompute Confluence cache
run.py --consensus YYYY-MM-DD --min-funds 3       # consensus (offline)
```
> `--sync*` and `--confluence` hit EDGAR and need `SEC_UA`. **Always** bound with `--max-quarters` (unbounded = full history = very long).

### Deployment modes

- **Open** (`SMARTMONEY_OPEN=1`, `SMARTMONEY_DB_READONLY=1`): public, read-only, no accounts/Stripe/alerts. This is 13flow.eu.
- **Full**: adds accounts, email verification, Stripe, Form 4 alerts. Needs secrets (`STRIPE_*`, `SMARTMONEY_PW_PEPPER`, SMTP) + `ProxyFix` behind the proxy.

Confluence provider resolution order: **cache JSON** → **live** (`SMARTMONEY_CONFLUENCE_LIVE=1` + EDGAR) → **demo**.

### Security (summary — detail in [`SECURITY.md`](SECURITY.md))

Strict per-request nonce CSP (no script `unsafe-inline`), `default-src 'none'` on JSON, systematic escaping, GET-only + Apache allow-list, read-only DB, hardened XML (anti-XXE), generic JSON errors, systemd sandbox, TLS+HSTS, no secret on the front. The real env file and databases are **never committed** (see `.gitignore`).

### Deployment & local dev

- Debian + Apache production: [`INSTALL_SERVER.md`](INSTALL_SERVER.md).
- Run locally: [`TEST_LOCAL.md`](TEST_LOCAL.md).
- Tests: `pytest tests/` (offline, network mocked).

### Adding a fund

1. Find the **13F filer CIK** (the manager, not the issuer) on EDGAR.
2. Add `Fund("Label", "Manager", "CIK", "Search name")` to `registry.py` (`cik=None` if unconfirmed → name resolution).
3. `run.py --verify` (must show the right EDGAR name).
4. Ingest with `deploy/backfill.sh 8` (ingest + WAL publish + permissions + restart) — never a bare `--sync-all`.
5. Recompute Confluence (`run.py --confluence`), then verify (`deploy/preflight.sh`).
