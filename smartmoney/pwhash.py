"""
Password hashing.

Defense in depth on the single most attacked secret in the system:
  - Memory-hard KDF: Argon2id when `argon2-cffi` is installed, else stdlib `scrypt`.
    Both are GPU/ASIC-resistant; plain SHA/PBKDF2 are not the default for a reason.
  - Per-password random salt (in the hash string).
  - Optional server-side PEPPER (env): an HMAC key mixed in before hashing, so a stolen
    database alone is not enough to mount an offline attack. Keep it OUT of the DB.
  - Constant-time verification; verify never raises on a wrong password (returns False).
  - Rehash detection so stored hashes can be upgraded transparently on next login.

Hash strings are self-describing:
  Argon2:  $argon2id$v=19$m=...,t=...,p=...$salt$hash   (PHC format from argon2-cffi)
  scrypt:  scrypt$<n>$<r>$<p>$<salt_b64>$<dk_b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

try:
    from argon2 import PasswordHasher as _Argon2
    from argon2 import Type as _Argon2Type
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
    _HAVE_ARGON2 = True
except ImportError:  # pragma: no cover - depends on environment
    _HAVE_ARGON2 = False

# scrypt cost: ~64 MiB per hash (128 * n * r). Strong and tunable.
_SCRYPT_N = 2 ** 16
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    # scrypt needs ~128*n*r*p bytes; OpenSSL's default cap (32 MiB) is too low for our params.
    return 128 * n * r * p + (2 << 20)

MAX_PASSWORD_LEN = 1024   # cap raw input so a huge password can't DoS the KDF


class PasswordHasher:
    def __init__(self, prefer: str = "auto", pepper: Optional[bytes] = None):
        env_pepper = os.environ.get("SMARTMONEY_PW_PEPPER")
        self._pepper = pepper or (env_pepper.encode() if env_pepper else None)
        self._use_argon2 = (prefer == "argon2") or (prefer == "auto" and _HAVE_ARGON2)
        if self._use_argon2 and not _HAVE_ARGON2:
            raise RuntimeError("argon2-cffi not installed but argon2 requested")
        if self._use_argon2:
            self._ph = _Argon2(time_cost=3, memory_cost=64 * 1024, parallelism=4,
                               hash_len=32, salt_len=16, type=_Argon2Type.ID)

    # -- input conditioning -------------------------------------------------
    def _pre(self, password: str) -> bytes:
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if len(password) > MAX_PASSWORD_LEN:
            raise ValueError("password too long")
        raw = password.encode("utf-8")
        if self._pepper:
            # HMAC with the pepper -> fixed-size, and useless to an attacker without the key.
            return hmac.new(self._pepper, raw, hashlib.sha256).hexdigest().encode()
        return raw

    # -- API ----------------------------------------------------------------
    def hash(self, password: str) -> str:
        pre = self._pre(password)
        if self._use_argon2:
            return self._ph.hash(pre)
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(pre, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=_SCRYPT_DKLEN, maxmem=_scrypt_maxmem(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P))
        b = lambda x: base64.b64encode(x).decode()
        return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b(salt)}${b(dk)}"

    def verify(self, stored: str, password: str) -> bool:
        try:
            pre = self._pre(password)
        except ValueError:
            return False
        if not stored:
            return False
        if stored.startswith("$argon2"):
            if not _HAVE_ARGON2:
                return False
            try:
                return _Argon2().verify(stored, pre)
            except (VerifyMismatchError, InvalidHashError, Exception):
                return False
        if stored.startswith("scrypt$"):
            try:
                _, n, r, p, salt_b64, dk_b64 = stored.split("$")
                salt = base64.b64decode(salt_b64)
                expected = base64.b64decode(dk_b64)
                dk = hashlib.scrypt(pre, salt=salt, n=int(n), r=int(r), p=int(p),
                                    dklen=len(expected), maxmem=_scrypt_maxmem(int(n), int(r), int(p)))
                return hmac.compare_digest(dk, expected)
            except Exception:
                return False
        return False

    def needs_rehash(self, stored: str) -> bool:
        if self._use_argon2:
            if not stored.startswith("$argon2"):
                return True                      # upgrade scrypt -> argon2
            try:
                return self._ph.check_needs_rehash(stored)
            except Exception:
                return True
        # scrypt mode: upgrade if params are weaker than current target
        if not stored.startswith("scrypt$"):
            return True
        try:
            _, n, r, p, _, _ = stored.split("$")
            return (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        except Exception:
            return True


# A precomputed hash of a random value, used to equalize timing when a login is
# attempted for a non-existent account (mitigates user-enumeration via timing).
_DUMMY = PasswordHasher().hash(secrets.token_hex(16))


def dummy_verify(hasher: PasswordHasher, password: str) -> None:
    """Spend ~the same time as a real verify, then discard the result."""
    try:
        hasher.verify(_DUMMY, password)
    except Exception:
        pass
