# Déploiement de 13FLOW sur Debian 12 + Apache2 — `13flow.eu` (full open)

Cette doc déploie **toute l'application en accès libre** : les écrans publics en lecture seule
**Consensus / Funds / Compare / Confluence** + la **FAQ**, pour tout le monde, **sans compte,
sans paiement, sans alertes**. C'est le « build ouvert » (`SMARTMONEY_OPEN=1`) — donc aucune
brique d'authentification, de Stripe ni d'envoi d'email n'est chargée.

Données : **SEC EDGAR** (13F + Form 4), domaine public US. Le tier web est en **lecture seule** ;
l'ingestion tourne séparément sur un timer.

```
                 HTTPS (TLS, HSTS)                  proxy 127.0.0.1:8000
  visiteur ───────────────────────▶  Apache2  ───────────────────────▶  gunicorn (wsgi:app, open)
  13flow.eu                          (headers, GET-only)                 lit ◀─ /var/lib/13flow/13flow.db (RO)
                                                                          écrit ◀─ refresh-data.sh (flowingest, timer)
```

> **Pré-requis** : un serveur Debian 12 avec `sudo`, et le domaine **13flow.eu** dont vous
> contrôlez le DNS.

---

## 1. DNS

Pointez l'apex **et** le `www` vers l'IP du serveur :

| Type  | Nom             | Valeur                    |
|-------|-----------------|---------------------------|
| A     | `13flow.eu`     | `VOTRE_IPV4`              |
| A     | `www.13flow.eu` | `VOTRE_IPV4`              |
| AAAA  | `13flow.eu`     | `VOTRE_IPV6` *(si dispo)* |
| AAAA  | `www.13flow.eu` | `VOTRE_IPV6` *(si dispo)* |

Vérifiez avant de demander le certificat : `dig +short 13flow.eu`.

## 2. Préparer le serveur (mises à jour, pare-feu)

```bash
sudo apt update && sudo apt -y full-upgrade

# Patchs de sécurité automatiques
sudo apt -y install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# Pare-feu : SSH + web uniquement
sudo apt -y install ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 3. Paquets

```bash
sudo apt -y install python3-venv python3-pip git apache2
sudo a2enmod proxy proxy_http headers ssl rewrite
sudo a2dissite 000-default.conf
```

## 4. Utilisateurs et dossiers

Trois comptes système séparent le web, l'ingestion et le MCP. **`flowapp`** fait tourner le
web en lecture seule, **`flowingest`** écrit la base et **`flowmcp`** ne lit que le code MCP.
Aucun compte n'a de shell.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin flowapp
sudo useradd --system --no-create-home --shell /usr/sbin/nologin flowingest
sudo useradd --system --no-create-home --shell /usr/sbin/nologin flowmcp
sudo usermod -aG flowapp flowingest          # l'ingest écrit, le web lit (groupe commun)

sudo mkdir -p /opt/13flow /var/lib/13flow /etc/13flow
```

## 5. Code + environnement Python

Copiez le projet dans `/opt/13flow` (git clone / scp / rsync), puis :

```bash
cd /opt/13flow
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt gunicorn
# (le paquet 'stripe' de requirements n'est jamais importé en mode ouvert)

# Le MCP exige Node 20 minimum. Vérifier avant l'installation verrouillée.
node --version
cd /opt/13flow/mcp-server
sudo npm ci --omit=dev
cd /opt/13flow

sudo chown -R root:flowapp /opt/13flow
sudo chmod -R o-rwx /opt/13flow
sudo chmod o+x /opt/13flow
sudo chown -R root:flowmcp /opt/13flow/mcp-server
sudo chmod +x deploy/refresh-data.sh deploy/preflight.sh
```

## 6. Fichier d'environnement

```bash
sudo cp deploy/13flow.env /etc/13flow/13flow.env
sudo chown root:flowapp /etc/13flow/13flow.env
sudo chmod 640 /etc/13flow/13flow.env
sudo nano /etc/13flow/13flow.env
```

Contenu attendu (ouvert + lecture seule) :

```ini
SMARTMONEY_OPEN=1
SMARTMONEY_DB_READONLY=1
SMARTMONEY_DB=/var/lib/13flow/13flow.db
# Pour l'ingestion EDGAR (doit contenir un email de contact) :
SEC_UA=13FLOW/1.0 contact@13flow.eu
```

> En mode ouvert il n'y a **aucun secret** ici (pas de clé Stripe, pas de pepper, pas de SMTP).
> `SEC_UA` n'est pas un secret : c'est l'identité polie exigée par la SEC.

## 7. Premières données

**D'abord**, donnez au compte d'ingestion le droit d'écrire le dossier de la base — sinon
`sqlite3.OperationalError: unable to open database file`. Le bit **setgid** (`2` dans `2750`)
fait hériter le groupe `flowapp` aux fichiers créés (base + `-wal`/`-shm`), pour que le user web
les lise. **À faire AVANT d'ingérer.**

```bash
sudo chown flowingest:flowapp /var/lib/13flow
sudo chmod 2750 /var/lib/13flow
```

**Ensuite**, soit la démo hors-ligne (valider la chaîne tout de suite), soit l'ingestion réelle EDGAR :

```bash
# Option A — démo hors-ligne (3 fonds, aucun réseau) :
sudo -u flowingest /opt/13flow/.venv/bin/python /opt/13flow/seed_demo.py \
     --db /var/lib/13flow/13flow.db

# Option B — vraies données EDGAR (tous les fonds suivis) :
sudo -u flowingest \
     SEC_UA="13FLOW/1.0 contact@13flow.eu" \
     SMARTMONEY_CACHE_DIR=/var/lib/13flow \
     /opt/13flow/.venv/bin/python /opt/13flow/run.py \
     --db /var/lib/13flow/13flow.db --sync-all --enrich
```

> `SMARTMONEY_CACHE_DIR` envoie le cache de résolution CUSIP→ticker dans un dossier **écrivable**
> (à côté de la base). Sinon le resolver tente d'écrire `.smartmoney_resolution_cache.json` dans le
> répertoire courant — souvent `/opt/13flow`, en lecture seule — d'où un
> `PermissionError: … .smartmoney_resolution_cache.json`. (Le service de refresh le fait déjà via
> le fichier d'env.)

**Enfin**, verrouillez la base en lecture seule pour le groupe web :

```bash
sudo chmod 640 /var/lib/13flow/13flow.db
```

> Si vous aviez déjà lancé l'ingestion et obtenu l'erreur : appliquez juste le `chown`/`chmod 2750`
> ci-dessus puis relancez l'option A ou B — rien d'autre à nettoyer.

## 8. Service gunicorn (systemd)

```bash
sudo cp deploy/13flow.service /etc/systemd/system/13flow.service
sudo systemctl daemon-reload
sudo systemctl enable --now 13flow
sudo systemctl status 13flow --no-pager

# Sanity local (avant Apache) :
curl -s localhost:8000/api/config            # -> {"open": true, ...}
curl -s localhost:8000/api/funds | head -c 200
```

Le service tourne en `flowapp`, sandboxé (`ProtectSystem=strict`, sans capabilities,
`MemoryDenyWriteExecute`, allow-list d'appels système) et écoute uniquement sur `127.0.0.1`.

### Service MCP isolé

Le daemon Registry public ne charge aucun outil privé ou paiement. Son fichier
d'environnement appartient uniquement à `root:flowmcp`.

```bash
sudo tee /etc/13flow/13flow-mcp.env >/dev/null <<'EOF'
MCP_HOST=127.0.0.1
MCP_PORT=8849
MCP_PATH=/mcp
MCP_PUBLIC_SITE=https://13flow.eu
MCP_13FLOW_API_BASE=http://127.0.0.1:8000
MCP_ALLOWED_HOSTS=13flow.eu,www.13flow.eu,127.0.0.1,localhost
MCP_ALLOWED_ORIGINS=https://13flow.eu,https://www.13flow.eu
MCP_MAX_BODY=1048576
MCP_MAX_UPSTREAM_BODY=8388608
MCP_MAX_IN_FLIGHT=32
MCP_MAX_CONNECTIONS=128
MCP_MAX_PAYMENT_CACHE=500
MCP_RATE_MAX=120
MCP_STATS_RETENTION_DAYS=30
MCP_PRO_TOOLS_ENABLED=0
MCP_X402_ENABLED=0
MCP_GIT_SHA=<exact-40-character-deployed-git-sha>
EOF
sudo chown root:flowmcp /etc/13flow/13flow-mcp.env
sudo chmod 640 /etc/13flow/13flow-mcp.env
sudo cp mcp-server/deploy/13flow-mcp.service /etc/systemd/system/13flow-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now 13flow-mcp
curl -fsS http://127.0.0.1:8849/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:8849/stats | python3 -m json.tool
sudo systemd-analyze security 13flow-mcp.service
```

Le service tourne en `flowmcp`, lie uniquement la boucle locale et bloque toute connexion
sortante hors loopback. Cette politique empêche aussi x402 de joindre un facilitateur externe.
Ne l'assouplissez pas sur le daemon Registry public.
L'unité crée `/var/lib/13flow-mcp` en `0700` via `StateDirectory`; seul ce répertoire est
inscriptible durablement et `agent-stats.json` reste en `0600`. Ne redirigez pas ce fichier
vers `/tmp`, le dépôt ou un répertoire partagé. Les détails journaliers sont conservés 30 jours
et ne contiennent ni IP, User-Agent, version client, arguments, prompts, réponses ou clés.

## 9. Apache : vhost + TLS

```bash
sudo cp deploy/apache-13flow.conf /etc/apache2/sites-available/13flow.conf
sudo a2ensite 13flow.conf
```

Certificat **sans** laisser certbot réécrire le vhost (`certonly`) :

```bash
sudo apt -y install certbot python3-certbot-apache
sudo certbot certonly --apache -d 13flow.eu -d www.13flow.eu \
     --agree-tos -m contact@13flow.eu --no-eff-email
```

Installez ensuite le vhost par défaut avant les vhosts nommés :

```bash
sudo install -d -o root -g www-data -m 750 /var/www/html/zen-default
sudo install -o root -g www-data -m 640 deploy/zen-default/index.html /var/www/html/zen-default/index.html
sudo install -o root -g www-data -m 640 deploy/zen-default/zen.css /var/www/html/zen-default/zen.css
sudo install -o root -g root -m 644 deploy/apache-zen-default.conf /etc/apache2/sites-available/000-zen-default.conf
sudo a2ensite 000-zen-default.conf
```

Rechargez d'abord Apache avec le certificat de repli, puis émettez le certificat dédié par
webroot et activez-le transactionnellement :

```bash
sudo apache2ctl configtest && sudo systemctl reload apache2
sudo certbot certonly --webroot -w /var/www/html/zen-default --cert-name zen-default \
  -d toonux.org -d toonux.com -d l0g.me -d l0g.us -d w2p.org \
  --deploy-hook "systemctl reload apache2"
sudo /opt/13flow/deploy/activate-zen-default-cert.sh
```

`deploy-code-safe.sh` maintient ensuite automatiquement ce vhost et sa page. Le préfixe
`000-` est une barrière fonctionnelle : Apache l'évalue avant `13flow.conf`, donc un Host
inconnu reste sur la page ZEN et écrit dans `zen_default_access.log`, jamais dans
`13flow_access.log`. Le journal minimal omet query string, referrer et User-Agent.

Le déploiement valide que le certificat `zen-default` couvre les cinq noms avant de l'utiliser.
S'il est absent, le vhost TLS réutilise temporairement le certificat de 13FLOW uniquement pour
isoler les requêtes qui terminent leur handshake ; le navigateur conservera alors un
avertissement de nom. Un certificat dédié partiel ou ne couvrant pas les cinq noms bloque le
déploiement.

Les vhosts référencent désormais `/etc/letsencrypt/live/13flow.eu/…`. Test et reload :

```bash
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Ouvrez **https://13flow.eu** : le dashboard se charge **sans bouton Sign in ni onglet Alerts**
(build ouvert), la FAQ est en bas de la barre latérale. Le renouvellement TLS est automatique
(`systemctl list-timers | grep certbot`).

## 10. Statistiques privées sans cookie

Le rapport d'exploitation `/stats/` suit le modèle de CompatAir : GoAccess lit le journal
Apache de 13FLOW côté serveur, sans ajouter de cookie ni de script de suivi aux pages publiques.
Les paramètres d'URL sont supprimés, les adresses IP sont anonymisées au niveau 2 dans le
rapport et seules les 90 dernières journées sont affichées. Les « visiteurs » restent des
hôtes anonymisés estimés par GoAccess, pas des personnes ni des comptes 13FLOW.

Après avoir déployé le vhost qui contient l'`IncludeOptional`, lancez :

```bash
sudo /opt/13flow/deploy/install-stats.sh
```

L'installateur demande un identifiant et un mot de passe d'au moins 16 caractères sans les
afficher. Il stocke uniquement un hash bcrypt dans
`/etc/apache2/13flow-stats.htpasswd` (`root:www-data`, mode `0640`), crée l'utilisateur système
sans shell `flowstats`, génère le premier rapport et active un timer toutes les 15 minutes.

La CSP principale du site n'est pas assouplie. Une CSP séparée, limitée à `/stats/`, autorise
le bootstrap inline et les fichiers JS/CSS de GoAccess ; elle conserve notamment
`connect-src 'none'`, `object-src 'none'` et `frame-ancestors 'none'`. Ne copiez pas cette
exception vers les pages publiques.

Contrôles opérateur :

```bash
curl -sSI https://13flow.eu/stats/ | sed -n '1,20p'  # 401 + challenge Basic
sudo systemctl status 13flow-stats.timer --no-pager
sudo systemctl status 13flow-stats.service --no-pager
sudo systemd-analyze security 13flow-stats.service
```

Le rapport est disponible à `https://13flow.eu/stats/`. Le chemin exact `/stats` redirige vers
`/stats/`. Il est envoyé avec `private, no-store` et `X-Robots-Tag: noindex, nofollow, noarchive`.
Les accès au rapport lui-même sont exclus du journal utilisé pour les métriques. Ils restent
auditables dans `/var/log/apache2/13flow_stats_access.log`, avec une ligne volontairement
minimale qui ne contient ni identifiant Basic Auth, ni query string, ni referrer, ni User-Agent.

## 11. Rafraîchir les données automatiquement

```bash
sudo cp deploy/13flow-refresh.service /etc/systemd/system/
sudo cp deploy/13flow-refresh.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now 13flow-refresh.timer
sudo systemctl start 13flow-refresh.service          # une passe maintenant
journalctl -u 13flow-refresh.service --no-pager      # vérifier l'ingestion
```

`refresh-data.sh` ingère puis fait un `PRAGMA wal_checkpoint(TRUNCATE)` : la base servie est un
fichier autonome, lisible en `mode=ro`. Les workers prennent les nouvelles données à la
connexion suivante (`sudo systemctl reload 13flow` optionnel).

## 12. (Option) protection anti-flood

```bash
sudo apt -y install libapache2-mod-evasive
sudo a2enmod evasive
sudo nano /etc/apache2/mods-available/evasive.conf   # ex. DOSPageCount 20 / DOSSiteCount 100
sudo systemctl reload apache2
```

Un CDN devant le site (Cloudflare) est l'option la moins chère pour un site quasi statique en
lecture seule (cache au bord + protection volumétrique).

## 13. Vérification post-déploiement (preflight)

```bash
/opt/13flow/deploy/preflight.sh           # teste https://13flow.eu
```

Il vérifie : redirection http→https, build **ouvert** actif, surface privée bien absente (404),
écritures refusées au bord, en-têtes (HSTS, **CSP à nonce**, X-Frame-Options, nosniff), et que
des fonds sont servis. Tout doit afficher `OK`.

---

## Exploitation

**Logs**
```bash
journalctl -u 13flow -f
sudo tail -f /var/log/apache2/13flow_access.log /var/log/apache2/13flow_error.log
```

**Santé** : `curl -s https://13flow.eu/api/config` → `{"open": true, …}`.

**Backfill / ré-ingestion en une commande** : `deploy/backfill.sh` ingère tous les fonds du
registre, publie la base (WAL→DELETE), corrige les droits, redémarre le service et vérifie.
Idempotent (ne refait que les trimestres manquants).
```bash
sudo /opt/13flow/deploy/backfill.sh        # historique complet
sudo /opt/13flow/deploy/backfill.sh 8      # seulement les 8 derniers trimestres (1ère passe rapide)
```
Met l'`OPENFIGI_APIKEY` dans `/etc/13flow/13flow.env` pour un enrichissement ~250× plus rapide.

**Mettre à jour l'app**
```bash
cd /opt/13flow && sudo git pull            # ou rsync de la nouvelle version
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart 13flow
/opt/13flow/deploy/preflight.sh
```

**Sauvegarde** : seule `/var/lib/13flow/13flow.db` est un état — et elle est **régénérable**
depuis EDGAR. Une copie périodique suffit ; rien de sensible dedans (données publiques).
`sudo cp /var/lib/13flow/13flow.db /var/backups/13flow-$(date +%F).db`

---

## Sécurité — pourquoi c'est sûr même grand ouvert

- **Aucune brique sensible chargée.** Auth / Stripe / abonnements / alertes **non enregistrés** :
  `/api/auth/*`, `/api/billing/*`, `/api/subscriptions`, `/api/alerts/*` → **404**. Pas de session,
  cookie, CSRF, mot de passe ni paiement → rien à voler.
- **Aucune clé côté navigateur, aucun appel tiers depuis le front.** Le dashboard ne parle qu'à
  sa propre origine (`/api`), en HTTPS. Pas de clé API embarquée.
- **Lecture seule, deux fois.** Apache refuse les mutations du site et n'autorise que le POST
  JSON-RPC exact de `/api/mcp`, plafonné à 1 MiB. Le process web ne peut pas écrire la base
  (`mode=ro` + permissions Unix), et le MCP public n'expose que des outils en lecture seule.
- **CSP stricte à nonce** sur le HTML (`script-src 'self' 'nonce-…'`, sans `'unsafe-inline'`),
  `default-src 'none'` sur le JSON, `X-Frame-Options: DENY`, HSTS au bord, `frame-ancestors 'none'`.
- **Erreurs JSON génériques** (400/404/500), `debug=False`, pas de fuite de version. XML EDGAR
  parsé avec defusedxml (anti-XXE), entrées bornées/validées.
- **Bacs à sable systemd séparés**, comptes non-root, bind loopback, réseau MCP limité à la
  boucle locale, TLS obligatoire (http→https forcé).

Dtails complets dans [`../SECURITY.md`](../SECURITY.md).

## Dépannage

| Symptôme | Piste |
|---|---|
| `0 funds · $0 AUM` | base vide → refaites l'étape 7 **sur le même `--db`** que `SMARTMONEY_DB`. |
| 502 Bad Gateway | gunicorn down : `systemctl status 13flow` ; `journalctl -u 13flow`. |
| `unable to open database file` | permissions (étape 7) ; en `mode=ro` le fichier doit exister. |
| Certificat refusé | DNS pas propagé, ou port 80 injoignable (ufw / vhost http). |
| Site OK mais reste en SAMPLE | l'API n'est pas jointe derrière `/` ; vérifiez le proxy (étape 9) et `curl localhost:8000/api/config`. |
| `PermissionError: … .smartmoney_resolution_cache.json` | l'ingestion écrit son cache dans le cwd (souvent `/opt/13flow`, RO). Exportez `SMARTMONEY_CACHE_DIR=/var/lib/13flow` (déjà dans le fichier d'env / le service refresh). |
| `[ERROR] Control server error: Permission denied: '/home/flowapp'` | cosmétique (le service tourne quand même). Le `13flow.service` fourni définit déjà `HOME=/run/13flow` via `RuntimeDirectory` pour le supprimer ; si vous aviez une ancienne version, ajoutez `RuntimeDirectory=13flow` + `Environment=HOME=%t/13flow` puis `daemon-reload` + `restart`. |
| `database is locked` à l'ingestion | une ingestion tourne déjà / WAL non checkpointé ; relancez `13flow-refresh.service`. |

> Core V1 ne contient pas de build comptes/Stripe. Garder l acces payant dans le service
> Pro API avec cles operateur, quotas, audit et rotation.
