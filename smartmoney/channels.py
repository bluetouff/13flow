"""
Delivery channels. A channel takes an Alert and delivers it, raising on failure
(the engine catches and logs the failure so it can retry next run).

  ConsoleChannel  — prints; the zero-config default for dev/testing.
  WebhookChannel  — HTTP POST the alert JSON to a URL.
  EmailChannel    — SMTP; plug in your provider's host/credentials.
  CallableChannel — wrap any function; used in tests and for custom sinks.
"""

from __future__ import annotations

import smtplib
import ssl
import sys
from email.message import EmailMessage
from typing import Callable, Optional

import requests

from .netsec import validate_email_recipient, validate_public_url


class Channel:
    name = "base"

    def send(self, alert) -> None:  # alert: Alert
        raise NotImplementedError


class ConsoleChannel(Channel):
    name = "console"

    def __init__(self, stream=sys.stdout):
        self._stream = stream

    def send(self, alert) -> None:
        print(alert.to_text(), file=self._stream)
        print("-" * 60, file=self._stream)


class CallableChannel(Channel):
    name = "callable"

    def __init__(self, fn: Callable[[object], None]):
        self._fn = fn

    def send(self, alert) -> None:
        self._fn(alert)


class WebhookChannel(Channel):
    name = "webhook"

    def __init__(self, url: str, session: Optional[requests.Session] = None, timeout: int = 15,
                 validate: bool = True, resolve_dns: bool = True):
        self._url = url
        self._session = session or requests.Session()
        self._timeout = timeout
        self._validate = validate
        self._resolve_dns = resolve_dns

    def send(self, alert) -> None:
        if self._validate:
            # Re-check at send time: defends against a target that became internal,
            # and against entries that bypassed creation-time validation.
            validate_public_url(self._url, resolve_dns=self._resolve_dns)
        resp = self._session.post(self._url, json=alert.to_dict(), timeout=self._timeout,
                                  allow_redirects=False)  # no redirect -> internal hops
        resp.raise_for_status()


class EmailChannel(Channel):
    name = "email"

    def __init__(self, host: str, port: int, username: str, password: str,
                 sender: str, use_tls: bool = True, smtp_factory=None):
        self._host, self._port = host, port
        self._user, self._password = username, password
        self._sender = sender
        self._use_tls = use_tls
        # Injectable for testing; defaults to smtplib.SMTP.
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, alert, to_addr: Optional[str] = None) -> None:
        recipient = validate_email_recipient(to_addr or alert.target)
        # EmailMessage handles header encoding/folding safely (defends header injection).
        msg = EmailMessage()
        msg["Subject"] = alert.subject().replace("\r", " ").replace("\n", " ")
        msg["From"] = self._sender
        msg["To"] = recipient
        msg.set_content(alert.to_text())
        smtp = self._smtp_factory(self._host, self._port)
        try:
            if self._use_tls:
                smtp.starttls(context=ssl.create_default_context())  # verifies cert
            if self._user:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)
        finally:
            smtp.quit()
