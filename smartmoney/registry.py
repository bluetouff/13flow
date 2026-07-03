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
    Fund("Greenlight",     "David Einhorn",   "1489933",         "DME Capital Management"),
    Fund("Lone Pine",      "Stephen Mandel",  "1061165",         "Lone Pine Capital"),
    Fund("Third Point",    "Dan Loeb",        "1040273",         "Third Point LLC"),
    Fund("Akre Capital",   "Chuck Akre",      "1112520",         "Akre Capital Management"),
    # --- value / concentrés (riches en signal) ---
    Fund("Value Act",       "Jeff Ubben/ValueAct", "1418814", "ValueAct Holdings"),
    Fund("Oakmark/Harris",   "Bill Nygren",         "813917", "Harris Associates"),
    # --- growth / tech (gros déposants, utiles au consensus) ---
    Fund("Tiger Global",     "Chase Coleman",       "1167483", "Tiger Global Management"),
    Fund("Coatue",           "Philippe Laffont",    "1135730", "Coatue Management"),
    Fund("Viking Global",    "Andreas Halvorsen",   "1103804", "Viking Global Investors"),
    # --- activistes / macro ---
    Fund("Icahn",            "Carl Icahn",          "0921669", "Icahn Capital"),
    Fund("Trian",            "Nelson Peltz",        "1345471", "Trian Fund Management"),
    Fund("Soros",            "Soros (FO)",          "1029160", "Soros Fund Management"),
    # --- value / deep value ---
    Fund("Fairholme",        "Bruce Berkowitz",     "1056831", "Fairholme Capital"),
    Fund("Oaktree",          "Howard Marks",        "949509",  "Oaktree Capital Management"),
    Fund("FPA / Romick",     "Steven Romick",       "1377581", "First Pacific Advisors"),
    Fund("Dodge & Cox",      "Dodge & Cox",         "200217",  "Dodge & Cox"),
    Fund("Davis Advisors",   "Chris Davis",         "1036325", "Davis Selected Advisers"),
    # --- quality / compounders ---
    Fund("Ruane Cunniff",    "Sequoia Fund",        "89043",   "Ruane Cunniff"),
    Fund("Gardner Russo",    "Tom Russo",           "860643",  "Gardner Russo & Quinn"),
    Fund("TCI",              "Chris Hohn",          "1647251", "TCI Fund Management"),
    Fund("Polen Capital",    "Polen Capital",       "1034524", "Polen Capital Management"),
    # --- activistes / event-driven ---
    Fund("Elliott",          "Paul Singer",         "1791786", "Elliott Investment Management"),
    Fund("Starboard Value",  "Jeff Smith",          "1517137", "Starboard Value"),
    Fund("JANA Partners",    "Barry Rosenstein",    "1998597", "JANA Partners Management"),
    Fund("Sachem Head",      "Scott Ferguson",      "1582090", "Sachem Head Capital"),
    Fund("Eminence",         "Ricky Sandler",       "1107310", "Eminence Capital"),
    # --- growth / tech / multistrat ---
    Fund("Whale Rock",       "Alex Sacerdote",      "1387322", "Whale Rock Capital"),
    Fund("Altimeter",        "Brad Gerstner",       "1541617", "Altimeter Capital"),
    Fund("D1 Capital",       "Dan Sundheim",        "1747057", "D1 Capital Partners"),
    Fund("Light Street",     "Glen Kacher",         "1569049", "Light Street Capital"),
    # --- quant / multistrat / market structure (coverage-critical for Pro) ---
    Fund("Renaissance Tech",  "Jim Simons (firm)",   "1037389", "Renaissance Technologies"),
    Fund("Citadel Advisors",  "Ken Griffin",         "1423053", "Citadel Advisors"),
    Fund("Millennium",        "Izzy Englander",      "1273087", "Millennium Management"),
    Fund("AQR Capital",       "Cliff Asness",        "1167557", "AQR Capital Management"),
    Fund("Two Sigma",         "Two Sigma",           "1179392", "Two Sigma Investments"),
    Fund("D. E. Shaw",        "D. E. Shaw",          "1009207", "D. E. Shaw & Co."),
    Fund("Point72",           "Steve Cohen",         "1603466", "Point72 Asset Management"),
    Fund("Farallon",          "Farallon",            "0909661", "Farallon Capital Management"),
]


def by_label(label: str) -> Fund | None:
    for f in SUPERINVESTORS:
        if f.label.lower() == label.lower():
            return f
    return None


def active_ciks() -> set[str]:
    """CIKs that belong to the current public/product registry."""
    return {f.cik.zfill(10) for f in SUPERINVESTORS if f.cik}
