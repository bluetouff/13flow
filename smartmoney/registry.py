"""
Superinvestor registry.

CIKs are the stable key — names drift and collide ("XYZ Capital Management"
exists many times over). The CIKs below are seeds; ALWAYS verify on first run
with `EdgarClient.entity_name(cik)` (run.py does this with --verify). If a CIK is
wrong, set it to None and the tracker will fall back to name resolution.

Berkshire (1067983) is confirmed. Treat the rest as 'verify before trusting'.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fund:
    label: str          # human-friendly, what the UI shows
    manager: str
    cik: str | None     # 10-digit not required here; client zero-pads
    search_name: str    # fallback for resolve_cik if cik is None/wrong


SUPERINVESTORS: list[Fund] = [
    Fund("Berkshire Hathaway", "Warren Buffett", "1067983", "Berkshire Hathaway"),
    Fund("Scion Asset Mgmt", "Michael Burry", "1649339", "Scion Asset Management"),
    Fund("Pershing Square", "Bill Ackman", "1336528", "Pershing Square Capital Management"),
    Fund("Bridgewater", "Ray Dalio (firm)", "1350694", "Bridgewater Associates"),
    Fund("Baupost Group", "Seth Klarman", "1061768", "Baupost Group"),
    Fund("Duquesne FO", "Stanley Druckenmiller", "1536411", "Duquesne Family Office"),
    Fund("Himalaya Capital", "Li Lu", "1709323", "Himalaya Capital Management"),
    Fund("Appaloosa", "David Tepper", "1656456", "Appaloosa LP"),
]


def by_label(label: str) -> Fund | None:
    for f in SUPERINVESTORS:
        if f.label.lower() == label.lower():
            return f
    return None
