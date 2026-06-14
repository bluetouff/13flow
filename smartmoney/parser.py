"""
Parse a 13F information-table XML into a list of raw holdings.

The XML is namespaced (e.g. http://www.sec.gov/edgar/document/thirteenf/informationtable)
but agents are inconsistent about prefixes, so we strip namespaces and match on
local tag names — the robust, battle-tested approach.

IMPORTANT — the value-units gotcha:
  For periods BEFORE 2023-01-03, the <value> field is in THOUSANDS of dollars.
  For periods on/after the 2022 rule amendments, <value> is in WHOLE DOLLARS.
  We do NOT normalize here (the parser stays dumb); portfolio.py applies the
  correct multiplier based on the filing's report_date.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Prefer defusedxml (blocks XXE, entity-expansion / billion-laughs, external DTDs).
# Fall back to a hardened stdlib path that refuses any DTD/ENTITY declaration.
try:
    from defusedxml.ElementTree import fromstring as _safe_fromstring  # type: ignore
    _HAVE_DEFUSED = True
except ImportError:  # pragma: no cover - depends on environment
    _HAVE_DEFUSED = False

# 13F info tables are bounded; cap input to stop a hostile/oversized payload up front.
_MAX_XML_BYTES = 64 * 1024 * 1024


def _parse_xml(xml: str) -> ET.Element:
    if xml is None:
        raise ValueError("empty XML")
    if len(xml.encode("utf-8", "ignore")) > _MAX_XML_BYTES:
        raise ValueError("XML exceeds size limit")
    if _HAVE_DEFUSED:
        return _safe_fromstring(xml)
    # Hardened fallback: ElementTree resolves no external entities, but a DTD with
    # internal entity definitions still enables billion-laughs. Refuse them outright.
    head = xml[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ValueError("XML DTD/ENTITY declarations are not allowed")
    return ET.fromstring(xml)


@dataclass
class RawHolding:
    name_of_issuer: str
    title_of_class: str
    cusip: str
    value: float          # as reported (thousands OR dollars — see module docstring)
    shares: float         # sshPrnamt
    sh_prn_type: str       # SH (shares) or PRN (principal)
    put_call: str          # '', 'Put', or 'Call'


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(el, tag: str, default: str = "") -> str:
    found = el.find(tag)
    return found.text.strip() if found is not None and found.text else default


def parse_info_table(xml: str) -> list[RawHolding]:
    root = _parse_xml(xml)
    _strip_ns(root)

    holdings: list[RawHolding] = []
    for it in root.iter("infoTable"):
        shrs = it.find("shrsOrPrnAmt")
        shares = _text(shrs, "sshPrnamt", "0") if shrs is not None else "0"
        sh_type = _text(shrs, "sshPrnamtType", "SH") if shrs is not None else "SH"
        try:
            value = float(_text(it, "value", "0").replace(",", ""))
        except ValueError:
            value = 0.0
        try:
            shares_f = float(shares.replace(",", ""))
        except ValueError:
            shares_f = 0.0

        holdings.append(
            RawHolding(
                name_of_issuer=_text(it, "nameOfIssuer"),
                title_of_class=_text(it, "titleOfClass"),
                cusip=_text(it, "cusip").upper(),
                value=value,
                shares=shares_f,
                sh_prn_type=sh_type,
                put_call=_text(it, "putCall"),
            )
        )
    return holdings
