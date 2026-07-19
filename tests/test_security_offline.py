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


def test_apache_method_policy_separates_public_and_pro_api():
    root = Path(__file__).resolve().parents[1]
    public_conf = (root / "deploy" / "apache-13flow.conf").read_text(encoding="utf-8")
    pro_conf = (root / "deploy" / "apache-13flow-pro.conf").read_text(encoding="utf-8")

    assert "<LimitExcept GET HEAD OPTIONS>" in public_conf
    assert "<LimitExcept GET HEAD OPTIONS POST PUT PATCH DELETE>" in pro_conf
    assert "ProxyPass        /api/pro/ http://127.0.0.1:8001/api/pro/" in pro_conf
    assert "<LimitExcept GET HEAD OPTIONS POST PUT PATCH DELETE>" in public_conf
    assert "ProxyPass        /api/pro/ http://127.0.0.1:8001/api/pro/" in public_conf
    assert "api/pro/|pro/admin/login" in public_conf


def test_private_stats_apache_boundary_and_csp_are_isolated():
    root = Path(__file__).resolve().parents[1]
    public_conf = (root / "deploy" / "apache-13flow.conf").read_text(encoding="utf-8")
    stats_conf = (root / "deploy" / "apache-13flow-stats.conf").read_text(encoding="utf-8")

    assert "IncludeOptional /etc/apache2/13flow-stats.conf" in public_conf
    assert "combined env=!13flow_stats_request" in public_conf
    assert 'LogFormat "%h %l - %t \\"%m %U\\" %>s %b" 13flow_stats_security' in public_conf
    assert "13flow_stats_access.log 13flow_stats_security env=13flow_stats_request" in public_conf
    assert '^/(?!stats(?:/|$)|api/mcp$|api/pro/' in public_conf
    assert 'ProxyPassMatch "^/stats(?:/|$)" !' in stats_conf
    assert 'RedirectMatch 302 "^/stats$" "/stats/"' in stats_conf
    stats_directory, stats_location = stats_conf.split('<LocationMatch "^/stats(?:/|$)">', 1)
    assert "AuthType Basic" not in stats_directory
    assert "AuthType Basic" in stats_location
    assert "AuthBasicProvider file" in stats_location
    assert "AuthUserFile /etc/apache2/13flow-stats.htpasswd" in stats_location
    assert "<Limit GET HEAD>\n        Require valid-user\n    </Limit>" in stats_location
    assert "<LimitExcept GET HEAD>" in stats_conf
    assert "private, no-store, max-age=0" in stats_conf
    assert "noindex, nofollow, noarchive" in stats_conf
    assert "Header onsuccess unset Content-Security-Policy" in stats_conf
    assert "Header always unset Content-Security-Policy" in stats_conf
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in stats_conf
    assert "style-src 'self' 'unsafe-inline'" in stats_conf
    assert "connect-src 'none'" in stats_conf
    assert "object-src 'none'" in stats_conf
    assert "frame-ancestors 'none'" in stats_conf


def test_private_stats_generation_minimizes_data_and_is_sandboxed():
    root = Path(__file__).resolve().parents[1]
    generator = (root / "deploy" / "generate-stats.sh").read_text(encoding="utf-8")
    installer = (root / "deploy" / "install-stats.sh").read_text(encoding="utf-8")
    smoke = (root / "deploy" / "smoke-private-stats.sh").read_text(encoding="utf-8")
    service = (root / "deploy" / "13flow-stats.service").read_text(encoding="utf-8")
    timer = (root / "deploy" / "13flow-stats.timer").read_text(encoding="utf-8")

    for script_name in ("generate-stats.sh", "install-stats.sh", "smoke-private-stats.sh"):
        assert (root / "deploy" / script_name).stat().st_mode & 0o111

    for option in ("--anonymize-ip", "--anonymize-level=2", "--no-query-string", "--keep-last=90"):
        assert option in generator
    assert "--external-assets" in generator
    assert "mktemp --suffix=.html" in generator
    assert 'mv -f -- "$temporary_report"' in generator
    assert "htpasswd -i -B -C 12" in installer
    assert "${#password} -lt 16" in installer
    assert "groupadd --system \"$stats_user_name\"" in installer
    assert "--gid \"$stats_user_name\" --groups adm" in installer
    assert "chmod 640 \"$password_stage\"" in installer
    assert "chown root:www-data \"$password_stage\"" in installer
    assert "base64 --wrap=0" in installer
    assert 'header = "Authorization: Basic %s"' in installer
    assert "unset basic_auth" in installer
    assert "runuser -u \"$stats_user_name\" -- test -r /var/log/apache2/13flow_access.log" in installer
    assert "Refusing symlinked statistics runtime path" in installer
    assert "goaccess.css" in installer and "goaccess.js" in installer
    assert 'status" != "401"' in smoke
    assert "expected exactly one CSP header" in smoke
    assert 'redirect_location" != "/stats/"' in smoke
    assert "User=flowstats" in service
    assert "SupplementaryGroups=adm" in service
    assert "UMask=0077" in service
    for control in (
        "NoNewPrivileges=yes", "PrivateNetwork=yes", "ProtectSystem=strict",
        "ProtectHome=yes", "ProtectProc=invisible", "RestrictNamespaces=yes",
        "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_UNIX",
        "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
        "IPAddressDeny=any",
        "StateDirectoryMode=0700", "ReadOnlyPaths=/var/log/apache2",
        "InaccessiblePaths=-/etc/13flow -/etc/apache2/13flow-stats.htpasswd",
    ):
        assert control in service
    assert "OnCalendar=*:0/15" in timer
    assert "Persistent=true" in timer


def test_safe_deploy_restarts_and_stamps_pro_service():
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "deploy-code-safe.sh").read_text(encoding="utf-8")

    assert "systemctl stop 13flow-pro" in script
    assert "stamp_sha 13flow-pro.service" in script
    assert "systemctl restart 13flow-pro" in script
    assert "http://127.0.0.1:8001/api/pro/v1/openapi.json" in script
    assert "sudo EXPECTED_SHA=$SHA" in script
    assert "statistics installation is incomplete" in script
    assert "statistics password file ownership or mode is unsafe" in script
    assert '"$APP_DIR/deploy/apache-13flow-stats.conf" "$STATS_APACHE_FRAGMENT"' in script
    assert "systemctl restart 13flow-stats.timer" in script
    assert "systemctl start 13flow-stats.service" in script


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
