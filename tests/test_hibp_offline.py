"""
Offline tests for the HaveIBeenPwned breach check. No network — the range fetcher is mocked.

Verifies the privacy contract (only a 5-char prefix is requested), correct suffix matching,
that padding lines (count 0) are not treated as breaches, the fail-open vs fail-closed policy
on outage, and that AccountStore.register rejects a breached password.
"""

import hashlib
import tempfile
from pathlib import Path

import pytest

from smartmoney.hibp import (pwned_count, make_breach_checker, HIBPUnavailable)
from smartmoney.accounts import AccountStore, PasswordPolicyError

PW = "correct horse battery staple"


def _sha1_parts(password):
    h = hashlib.sha1(password.encode()).hexdigest().upper()
    return h[:5], h[5:]


def test_only_prefix_is_sent_and_suffix_matched():
    seen = {}
    prefix, suffix = _sha1_parts(PW)

    def fetcher(pfx):
        seen["prefix"] = pfx
        # API returns suffixes WITHOUT the prefix, one per line, with a count
        return f"{suffix}:42\r\n0000000000000000000000000000000000A:9\r\n"

    assert pwned_count(PW, fetcher) == 42
    assert seen["prefix"] == prefix           # only the 5-char prefix left the process
    assert len(seen["prefix"]) == 5


def test_not_found_returns_zero():
    def fetcher(pfx):
        return "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:3\nAAAA:1\n"
    assert pwned_count(PW, fetcher) == 0


def test_padding_line_count_zero_is_not_a_breach():
    _, suffix = _sha1_parts(PW)
    def fetcher(pfx):
        return f"{suffix}:0\n"                 # HIBP padding entries carry count 0
    assert pwned_count(PW, fetcher) == 0


def test_fail_open_vs_closed_on_outage():
    def boom(pfx):
        raise OSError("network down")
    with pytest.raises(HIBPUnavailable):
        pwned_count(PW, boom)
    # policy wrappers
    open_checker = make_breach_checker(fetcher=boom, fail_closed=False)
    closed_checker = make_breach_checker(fetcher=boom, fail_closed=True)
    assert open_checker(PW) is False          # fail-open: allow
    assert closed_checker(PW) is True         # fail-closed: reject


def test_disabled_checker_is_none():
    assert make_breach_checker(enabled=False) is None


def test_register_rejects_breached_password():
    _, suffix = _sha1_parts("hunter2hunter2")
    def fetcher(pfx):
        return f"{suffix}:1337\n"
    checker = make_breach_checker(fetcher=fetcher)
    with tempfile.TemporaryDirectory() as d:
        s = AccountStore(str(Path(d) / "a.db"), breach_checker=checker)
        with pytest.raises(PasswordPolicyError):
            s.register("u@example.com", "hunter2hunter2")
        # a non-breached password (fetcher returns no match) still registers
        s2 = AccountStore(str(Path(d) / "a.db"),
                          breach_checker=make_breach_checker(fetcher=lambda p: ""))
        assert s2.register("ok@example.com", PW)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
