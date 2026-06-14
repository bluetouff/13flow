"""
HaveIBeenPwned 'Pwned Passwords' check via the k-anonymity range API.

PRIVACY (the whole point of the range API):
  - We hash the password with SHA-1 locally and send ONLY the first 5 hex chars of that hash
    to the API — never the password, never the full hash. The API returns every suffix that
    shares the prefix; we match our suffix locally.
  - We send 'Add-Padding: true' so the response is padded to a constant-ish size, defeating
    traffic-analysis inference of which prefix was queried.
  - SHA-1 here is NOT used to protect anything — it is the index the public corpus is keyed
    on. Password storage uses Argon2id/scrypt (see pwhash); these are unrelated.

AVAILABILITY:
  - On any network/parse error the default policy is FAIL-OPEN (allow, log a warning) so an
    HIBP outage cannot block every signup; the local deny-list still blocks the worst ones.
  - Set fail_closed=True (env SMARTMONEY_HIBP_FAIL_CLOSED=1) to reject instead when unsure.
  - The whole feature can be disabled with SMARTMONEY_DISABLE_HIBP=1.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Callable, Optional

log = logging.getLogger("smartmoney.hibp")

_API = "https://api.pwnedpasswords.com/range/"
_TIMEOUT = 3


class HIBPUnavailable(Exception):
    pass


def _default_fetcher(prefix: str) -> str:
    import requests  # imported lazily; only needed when the real API is used
    resp = requests.get(_API + prefix, timeout=_TIMEOUT,
                        headers={"Add-Padding": "true", "User-Agent": "SmartMoney/1.0"})
    resp.raise_for_status()
    return resp.text


def pwned_count(password: str, fetcher: Optional[Callable[[str], str]] = None) -> int:
    """
    Return how many times `password` appears in known breaches (0 = not found).
    Raises HIBPUnavailable on any network/parse failure.

    `fetcher(prefix) -> body` is injectable for testing (the live sandbox can't reach HIBP).
    """
    fetcher = fetcher or _default_fetcher
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        body = fetcher(prefix)
    except Exception as e:                       # network, HTTP, anything
        raise HIBPUnavailable(str(e)) from e
    for line in body.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        suf, _, cnt = line.partition(":")
        if suf.strip().upper() == suffix:
            try:
                return int(cnt.strip())          # padding lines carry count 0 -> not pwned
            except ValueError:
                return 1
    return 0


def make_breach_checker(*, fetcher: Optional[Callable[[str], str]] = None,
                        fail_closed: bool = False,
                        enabled: bool = True) -> Optional[Callable[[str], bool]]:
    """
    Build a callable(password) -> bool that returns True when the password MUST be rejected.
    Returns None when disabled (so AccountStore can skip the check entirely).
    Encapsulates the fail-open / fail-closed policy so callers stay simple.
    """
    if not enabled:
        return None

    def check(password: str) -> bool:
        try:
            return pwned_count(password, fetcher) > 0
        except HIBPUnavailable as e:
            log.warning("HIBP unavailable; failing %s: %s",
                        "closed" if fail_closed else "open", e)
            return fail_closed
    return check


def default_breach_checker(fetcher: Optional[Callable[[str], str]] = None):
    """Env-driven default used by the app."""
    if os.environ.get("SMARTMONEY_DISABLE_HIBP", "").lower() in ("1", "true", "yes"):
        return None
    fail_closed = os.environ.get("SMARTMONEY_HIBP_FAIL_CLOSED", "").lower() in ("1", "true", "yes")
    return make_breach_checker(fetcher=fetcher, fail_closed=fail_closed, enabled=True)
