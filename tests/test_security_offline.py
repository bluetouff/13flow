"""
Security regression tests. No network (DNS resolution disabled in URL checks).

Locks in the hardening so a future refactor can't silently reopen a hole.
"""

import tempfile
from pathlib import Path

import pytest

from smartmoney.netsec import (validate_public_url, validate_email_recipient,
                               SSRFError, AddressError)
from smartmoney.parser import parse_info_table
from smartmoney.channels import EmailChannel, WebhookChannel
from tests.test_offline import _table


# ---- SSRF guard ----------------------------------------------------------
def test_ssrf_blocks_internal_targets():
    bad = [
        "http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "http://metadata/",
        "http://foo.internal/", "http://bar.local/", "ftp://example.com/", "file:///etc/passwd",
        "https://" + "a" * 4000,
    ]
    for url in bad:
        with pytest.raises(SSRFError):
            validate_public_url(url, resolve_dns=False)


def test_ssrf_allows_public_https():
    assert validate_public_url("https://hooks.example.com/abc", resolve_dns=False)
    assert validate_public_url("http://93.184.216.34/x", resolve_dns=False)  # public literal IP


def test_webhook_send_refuses_internal():
    class Boom:  # should never be reached
        def post(self, *a, **k): raise AssertionError("posted to internal URL!")
    ch = WebhookChannel("http://169.254.169.254/", session=Boom(), resolve_dns=False)
    with pytest.raises(SSRFError):
        ch.send(_FakeAlert())


# ---- email header injection ---------------------------------------------
def test_email_recipient_validation():
    validate_email_recipient("ok@example.com")
    for bad in ["a@b.com\nBcc: evil@x.com", "a@b.com\r\nSubject: x", "no-at-sign",
                "x@y", "a@@b.com", "\x00@b.com"]:
        with pytest.raises(AddressError):
            validate_email_recipient(bad)


def test_email_channel_blocks_injected_recipient():
    sent = {}
    class FakeSMTP:
        def __init__(self, *a): pass
        def starttls(self, context=None): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg
        def quit(self): pass
    ch = EmailChannel("smtp.x", 587, "u", "p", "from@x.com", smtp_factory=FakeSMTP)
    with pytest.raises(AddressError):
        ch.send(_FakeAlert(), to_addr="a@b.com\nBcc: evil@x.com")
    assert "msg" not in sent                          # nothing sent
    ch.send(_FakeAlert(), to_addr="good@x.com")       # valid recipient works
    assert sent["msg"]["To"] == "good@x.com"


# ---- XML hardening -------------------------------------------------------
def test_xml_rejects_entity_bomb():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;">]><informationTable>&lol2;</informationTable>')
    with pytest.raises(Exception):
        parse_info_table(bomb)


def test_xml_normal_still_parses():
    rows = parse_info_table(_table([("APPLE INC", "037833100", 1000, 100, "")]))
    assert len(rows) == 1 and rows[0].cusip == "037833100"


# ---- API input validation + headers --------------------------------------
def test_api_validation_and_headers():
    flask = pytest.importorskip("flask")  # noqa: F841
    from smartmoney.api import create_app
    with tempfile.TemporaryDirectory() as d:
        app = create_app(str(Path(d) / "empty.db"))
        c = app.test_client()
        assert c.get("/api/fund/not-a-cik").status_code == 400      # rejects junk CIK
        assert c.get("/api/compare?ciks=abc,def").status_code == 400
        r = c.get("/api/funds")
        assert r.status_code == 200
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"


# ---- helpers -------------------------------------------------------------
class _FakeAlert:
    target = ""
    def to_dict(self): return {"x": 1}
    def to_text(self): return "body"
    def subject(self): return "subject\r\nInjected: nope"   # CRLF must be stripped


def test_alert_subject_crlf_stripped_in_email():
    sent = {}
    class FakeSMTP:
        def __init__(self, *a): pass
        def starttls(self, context=None): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg
        def quit(self): pass
    EmailChannel("h", 587, "", "", "f@x.com", smtp_factory=FakeSMTP).send(
        _FakeAlert(), to_addr="g@x.com")
    assert "\n" not in sent["msg"]["Subject"] and "\r" not in sent["msg"]["Subject"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
