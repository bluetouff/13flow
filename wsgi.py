"""
WSGI entrypoint for production (gunicorn wsgi:app).

Configured via environment:
  SMARTMONEY_DB     path to the SQLite database (default: smartmoney.db)
  SMARTMONEY_VALUE  "1" to enable live valuation in /api/fund?value=1 (hits price API per
                    request; off by default)

Run, from the project root:
  gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app
"""

import os

from smartmoney.api import create_app

_provider = None
if os.environ.get("SMARTMONEY_VALUE", "").lower() in ("1", "true", "yes"):
    from smartmoney.prices import StooqProvider
    _provider = StooqProvider()

app = create_app(os.environ.get("SMARTMONEY_DB", "smartmoney.db"), provider=_provider)
