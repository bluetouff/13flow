"""
Network-egress safety helpers.

These guard the two places where user-supplied destinations leave the server:
  - webhook alert URLs  -> SSRF risk (could point at cloud metadata / internal services)
  - email recipients    -> header-injection risk (CRLF) and malformed addresses

SSRF note: validate_public_url with resolve_dns=True resolves the hostname and rejects
private/loopback/link-local/reserved IPs. This narrows but does not fully close DNS
rebinding (the name could resolve differently at request time). For a hard guarantee,
run outbound traffic through an egress allowlist/proxy (see SECURITY.md).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse


class SSRFError(ValueError):
    pass


class AddressError(ValueError):
    pass


_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localdomain")
_BLOCKED_HOST_NAMES = {"localhost", "metadata", "metadata.google.internal"}


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable -> treat as unsafe
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def validate_public_url(url: str, *, resolve_dns: bool = False) -> str:
    """
    Return the URL if it is a safe public http(s) target, else raise SSRFError.

    Syntactic checks (always): scheme is http/https, a hostname exists, the host is
    not localhost / a blocked suffix / a literal private or loopback IP.
    resolve_dns=True additionally resolves the name and rejects internal IPs.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise SSRFError("invalid URL")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise SSRFError(f"scheme '{p.scheme}' not allowed (http/https only)")
    host = (p.hostname or "").lower()
    if not host:
        raise SSRFError("missing host")
    if host in _BLOCKED_HOST_NAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise SSRFError(f"host '{host}' is not a public address")

    # Literal IP in the URL -> validate directly (no DNS needed).
    is_literal_ip = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_literal_ip = False
    if is_literal_ip:
        if _ip_is_blocked(host):
            raise SSRFError(f"host IP '{host}' is private/reserved")
        return url

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
        except socket.gaierror as e:
            raise SSRFError(f"cannot resolve host '{host}': {e}")
        for info in infos:
            ip = info[4][0]
            if _ip_is_blocked(ip):
                raise SSRFError(f"host '{host}' resolves to internal IP {ip}")
    return url


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email_recipient(addr: str) -> str:
    """Reject CRLF/control chars (header injection) and obviously-malformed addresses."""
    if not isinstance(addr, str) or not addr or len(addr) > 254:
        raise AddressError("invalid email address")
    if any(c in addr for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in addr):
        raise AddressError("control characters not allowed in recipient")
    if not _EMAIL_RE.match(addr):
        raise AddressError(f"malformed email address: {addr!r}")
    return addr
