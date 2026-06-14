#!/usr/bin/env python3
"""
Seed a demo database with sample 13F data — no network, no EDGAR.

Lets you exercise the API, dashboard, and analytics commands immediately, before
wiring up live EDGAR sync. Run:

    python seed_demo.py --db demo.db
    python -m smartmoney.api --db demo.db      # then open http://localhost:5000

The numbers are illustrative, not real holdings.
"""

import argparse

from smartmoney.parser import RawHolding
from smartmoney.portfolio import build_portfolio
from smartmoney.edgar import Filing
from smartmoney.db import Store

TICKERS = {
    "037833100": "AAPL", "191216100": "KO", "67066G104": "NVDA",
    "594918104": "MSFT", "02079K305": "GOOGL", "025816109": "AXP",
}


def rh(name, cusip, value, shares, pc=""):
    return RawHolding(name, "COM", cusip, float(value), float(shares), "SH", pc)


def save(store, cik, label, manager, acc, rdate, fdate, rows):
    pf = build_portfolio(cik, label, rdate, "13F-HR", rows)
    for (cusip, pc), pos in pf.positions.items():
        if cusip in TICKERS:
            pos.ticker = TICKERS[cusip]
            pos.ticker_source = "openfigi"
            pos.ticker_confidence = 0.95
    store.save_portfolio(
        pf, Filing(cik=cik, accession=acc, form="13F-HR", filing_date=fdate,
                   report_date=rdate, primary_doc="primary_doc.xml"),
        manager=manager)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    args = ap.parse_args()
    s = Store(args.db)

    # Berkshire — two quarters; opens NVIDIA in Q2, an unresolved note shows coverage < 100%.
    save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett", "brk-q1",
         "2024-03-31", "2024-05-15",
         [rh("APPLE INC", "037833100", 84_200_000_000, 400_000_000),
          rh("COCA COLA CO", "191216100", 25_400_000_000, 400_000_000),
          rh("AMERICAN EXPRESS", "025816109", 34_000_000_000, 150_000_000)])
    save(s, "0001067983", "Berkshire Hathaway", "Warren Buffett", "brk-q2",
         "2024-06-30", "2024-08-14",
         [rh("APPLE INC", "037833100", 84_200_000_000, 400_000_000),
          rh("COCA COLA CO", "191216100", 25_400_000_000, 400_000_000),
          rh("AMERICAN EXPRESS", "025816109", 43_000_000_000, 150_000_000),
          rh("NVIDIA CORP", "67066G104", 1_200_000_000, 6_000_000),
          rh("PRIVATE NOTE 2029", "99999AA10", 300_000_000, 10_000)])  # unresolved tail

    # Pershing Square
    save(s, "0001336528", "Pershing Square", "Bill Ackman", "psq-q1",
         "2024-03-31", "2024-05-16",
         [rh("ALPHABET INC", "02079K305", 2_300_000_000, 9_400_000),
          rh("MICROSOFT CORP", "594918104", 1_900_000_000, 4_600_000)])
    save(s, "0001336528", "Pershing Square", "Bill Ackman", "psq-q2",
         "2024-06-30", "2024-08-15",
         [rh("ALPHABET INC", "02079K305", 2_500_000_000, 9_400_000),
          rh("MICROSOFT CORP", "594918104", 1_900_000_000, 4_600_000),
          rh("NVIDIA CORP", "67066G104", 900_000_000, 4_500_000)])

    # Scion (Burry)
    save(s, "0001649339", "Scion Asset Mgmt", "Michael Burry", "scn-q1",
         "2024-03-31", "2024-05-14",
         [rh("APPLE INC", "037833100", 4_500_000, 30_000)])
    save(s, "0001649339", "Scion Asset Mgmt", "Michael Burry", "scn-q2",
         "2024-06-30", "2024-08-13",
         [rh("ALPHABET INC", "02079K305", 3_900_000, 22_000),
          rh("NVIDIA CORP", "67066G104", 2_800_000, 14_000)])

    s.close()
    print(f"Seeded {args.db}: 3 funds x 2 quarters. "
          f"NVIDIA is a consensus buy in 2024-06-30; one CUSIP is intentionally unresolved.")


if __name__ == "__main__":
    main()
