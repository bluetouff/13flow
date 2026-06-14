"""
Outbound transactional email (verification links, etc.).

A `mailer` is just a callable `(to, subject, body) -> None`. In production it sends over
authenticated TLS; with no SMTP configured it logs the message (so local/dev still works and
nothing silently breaks). Recipient is validated (CRLF-safe) and the subject is stripped of
newlines to prevent header injection — same discipline as channels.py.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

from .netsec import validate_email_recipient

log = logging.getLogger("smartmoney.notify")

Mailer = Callable[[str, str, str], None]


def _log_mailer(to: str, subject: str, body: str) -> None:
    validate_email_recipient(to)   # still validate, even when only logging
    log.info("EMAIL (not sent — no SMTP configured)\n  to=%s\n  subject=%s\n  %s",
             to, subject, body.replace("\n", "\n  "))


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str,
                 use_tls: bool = True, smtp_factory=None):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.sender = sender
        self.use_tls = use_tls
        self.factory = smtp_factory or smtplib.SMTP

    def __call__(self, to: str, subject: str, body: str) -> None:
        recipient = validate_email_recipient(to)
        msg = EmailMessage()
        msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")
        msg["From"] = self.sender
        msg["To"] = recipient
        msg.set_content(body)
        smtp = self.factory(self.host, self.port)
        try:
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
        finally:
            smtp.quit()


def make_default_mailer() -> Mailer:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return _log_mailer
    return SmtpMailer(
        host,
        int(os.environ.get("SMTP_PORT", "587")),
        os.environ.get("SMTP_USER", ""),
        os.environ.get("SMTP_PASS", ""),
        os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "no-reply@localhost")),
    )
