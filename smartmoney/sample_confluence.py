"""
Synthetic confluence data for offline preview & tests (13FLOW). NOT real filings — these
are illustrative figures, but they run through the REAL pipeline (forms4.Form4 ->
aggregate_insider_activity -> score_confluence), so the enriched features (recency,
stake-size, cluster timing) are exercised exactly as in production.
"""

from __future__ import annotations

from datetime import date, timedelta

from .forms4 import Form4, Form4Transaction
from .crosssignal import (
    ConfluenceSignal, InstitutionalSignal, aggregate_insider_activity, build_confluence,
)


def _buy(owner, title, days_ago, shares, price, owned_after, *,
         director=False, officer=True, ten_pct=False, as_of=None):
    as_of = as_of or date.today()
    d = (as_of - timedelta(days=days_ago)).isoformat()
    return Form4(
        accession="", filing_date=d, period_of_report=d,
        issuer_cik="", issuer_name="", issuer_ticker="",
        owner_cik=owner, owner_name=owner,
        is_director=director, is_officer=officer, is_ten_percent_owner=ten_pct,
        officer_title=title,
        transactions=(Form4Transaction(
            security_title="Common Stock", txn_date=d, code="P",
            acquired_disposed="A", shares=shares, price_per_share=price,
            direct=True, shares_owned_after=owned_after),),
    )


def _sell(owner, title, days_ago, shares, price, as_of=None):
    as_of = as_of or date.today()
    d = (as_of - timedelta(days=days_ago)).isoformat()
    return Form4(
        accession="", filing_date=d, period_of_report=d,
        issuer_cik="", issuer_name="", issuer_ticker="",
        owner_cik=owner, owner_name=owner,
        is_director=True, is_officer=False, is_ten_percent_owner=False, officer_title="",
        transactions=(Form4Transaction(
            security_title="Common Stock", txn_date=d, code="S",
            acquired_disposed="D", shares=shares, price_per_share=price,
            direct=True, shares_owned_after=0),),
    )


def sample_signals(window_days: int = 90, as_of: date | None = None) -> list[ConfluenceSignal]:
    as_of = as_of or date.today()

    inst = {
        "ATLC": InstitutionalSignal("ATLC", funds_accumulating=4, funds_trimming=0,
                                    total_value_usd=612_000_000, conviction_funds=2, avg_weight_pct=4.1,
                                    fund_labels=("Pershing Square", "Baupost", "Greenlight", "Third Point")),
        "NVCR": InstitutionalSignal("NVCR", funds_accumulating=2, funds_trimming=0,
                                    total_value_usd=188_000_000, conviction_funds=1, avg_weight_pct=2.4,
                                    fund_labels=("Berkshire", "Appaloosa")),
        "RDFN": InstitutionalSignal("RDFN", funds_accumulating=3, funds_trimming=1,
                                    total_value_usd=95_000_000, conviction_funds=1, avg_weight_pct=1.2,
                                    fund_labels=("Scion", "Tiger Global", "Coatue")),
        "VRNS": InstitutionalSignal("VRNS", funds_accumulating=2, funds_trimming=0,
                                    total_value_usd=240_000_000, conviction_funds=2, avg_weight_pct=3.6,
                                    fund_labels=("Lone Pine", "Viking")),
        "CMPO": InstitutionalSignal("CMPO", funds_accumulating=1, funds_trimming=0,
                                    total_value_usd=42_000_000, avg_weight_pct=0.6, fund_labels=("Altimeter",)),
        "PARA": InstitutionalSignal("PARA", funds_accumulating=0, funds_trimming=3,
                                    total_value_usd=0.0),
    }

    # issuer -> list of Form 4s (with realistic dates + prior holdings for stake sizing)
    forms = {
        "ATLC": [  # fresh cluster: CEO + CFO within the last fortnight, big stake bumps
            _buy("J. Marlin", "Chief Executive Officer", 4, 50000, 84.0, 270000, as_of=as_of),
            _buy("S. Devereux", "Chief Financial Officer", 9, 18000, 83.3, 96000, as_of=as_of),
            _buy("R. Holt", "Director", 21, 9000, 84.4, 140000, director=True, officer=False, as_of=as_of),
        ],
        "NVCR": [
            _buy("A. Brandt", "President & COO", 12, 12000, 81.7, 60000, as_of=as_of),
            _buy("L. Sayed", "Director", 30, 6000, 81.7, 220000, director=True, officer=False, as_of=as_of),
        ],
        "VRNS": [  # single but very fresh CEO buy, meaningful stake increase
            _buy("D. Acheson", "Chief Executive Officer", 3, 8000, 140.0, 32000, as_of=as_of),
        ],
        "RDFN": [  # older, small director buy
            _buy("M. Osei", "Director", 61, 40000, 8.0, 900000, director=True, officer=False, as_of=as_of),
        ],
        "CMPO": [],
        "PARA": [_sell("T. Vance", "Director", 18, 30000, 80.0, as_of=as_of)],
    }
    names = {"ATLC": "Atlantic Union Holdings", "NVCR": "Novacor Therapeutics",
             "VRNS": "Veridian Networks", "RDFN": "Redfront Realty",
             "CMPO": "Compass Optics", "PARA": "Parallax Media"}

    insider = {
        t: aggregate_insider_activity(t, fs, window_days=window_days,
                                      issuer_name=names[t], as_of=as_of)
        for t, fs in forms.items()
    }
    return build_confluence(inst, insider)
