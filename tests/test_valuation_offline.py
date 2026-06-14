"""
Offline test for the price/valuation layer. No network.

A FakeProvider serves canned closes + fundamentals so we can assert current weights,
implied P&L since the basis date, the reconcile ratio, and that option / no-ticker
positions are carried (not silently dropped). Also checks Stooq CSV parsing directly.
"""

from datetime import date

from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from smartmoney.prices import PriceProvider, StooqProvider, Fundamentals
from smartmoney.valuation import value_portfolio
from tests.test_offline import _table

AAPL, NVDA = "037833100", "67066G104"


class FakeProvider(PriceProvider):
    # ticker -> {date: close}
    DATA = {
        "AAPL": {date(2024, 6, 28): 100.0, date(2024, 9, 27): 120.0},  # +20%
        "NVDA": {date(2024, 6, 28): 50.0, date(2024, 9, 27): 40.0},    # -20%
    }
    FUND = {"AAPL": Fundamentals("AAPL", 3.0e12, 15_000_000_000.0)}

    def daily_closes(self, ticker, start, end):
        return {d: c for d, c in self.DATA.get(ticker.upper(), {}).items() if start <= d <= end}

    def latest_close(self, ticker, lookback=10):
        s = self.DATA.get(ticker.upper())
        if not s:
            return None
        d = max(s)
        return d, s[d]

    def fundamentals(self, ticker):
        return self.FUND.get(ticker.upper())


def _portfolio_with_tickers():
    pf = build_portfolio("0", "T", "2024-06-30", "13F-HR", parse_info_table(_table([
        ("APPLE INC", AAPL, 10000, 100, ""),      # 100 sh, reported $10,000 -> basis px 100
        ("NVIDIA", NVDA, 2500, 50, ""),           # 50 sh, reported $2,500  -> basis px 50
        ("APPLE INC", AAPL, 99999, 5, "Call"),    # option line -> carried, not priced
    ])))
    # Attach tickers (normally done by OpenFIGI enrichment).
    pf.positions[(AAPL, "")].ticker = "AAPL"
    pf.positions[(NVDA, "")].ticker = "NVDA"
    pf.positions[(AAPL, "Call")].ticker = "AAPL"
    return pf


def test_valuation_pnl_and_weights():
    pf = _portfolio_with_tickers()
    vp = value_portfolio(pf, FakeProvider(), basis_date="2024-06-28", with_fundamentals=True)

    aapl = next(p for p in vp.positions if p.cusip == AAPL and not p.put_call)
    nvda = next(p for p in vp.positions if p.cusip == NVDA)
    opt = next(p for p in vp.positions if p.put_call == "Call")

    # Current values: 100*120=12,000 and 50*40=2,000.
    assert aapl.current_value == 12000 and nvda.current_value == 2000
    assert abs(aapl.pnl_pct - 0.20) < 1e-9
    assert abs(nvda.pnl_pct + 0.20) < 1e-9

    # Reconcile: reported / (shares*px_basis) = 10000/(100*100)=1.0 ; 2500/(50*50)=1.0
    assert abs(aapl.reconcile_ratio - 1.0) < 1e-9
    assert abs(nvda.reconcile_ratio - 1.0) < 1e-9

    # Priced sleeve P&L: basis 10000+2500=12500 -> current 12000+2000=14000 => +1500 (+12%)
    assert abs(vp.pnl_abs - 1500) < 1e-9
    assert abs(vp.pnl_pct - (1500 / 12500)) < 1e-9

    # Option line carried at reported value, flagged, excluded from priced sleeve.
    assert opt.status == "option" and opt.current_value == 99999
    assert vp.priced_current_total == 14000

    # Fundamentals: % of Apple owned = 100 / 15e9.
    assert abs(aapl.pct_of_company - (100 / 15_000_000_000.0)) < 1e-18

    # Current weights sum to 1 across all carried positions.
    assert abs(sum(p.current_weight for p in vp.positions) - 1.0) < 1e-9


def test_unpriced_when_no_ticker():
    pf = build_portfolio("0", "T", "2024-06-30", "13F-HR", parse_info_table(_table([
        ("MYSTERY BOND", "999999999", 5000, 10, ""),  # no ticker attached
    ])))
    vp = value_portfolio(pf, FakeProvider(), basis_date="2024-06-28")
    assert vp.positions[0].status == "no_ticker"
    assert vp.positions[0].current_value == 5000
    assert vp.priced_current_total == 0.0


def test_stooq_csv_parse():
    csv_text = ("Date,Open,High,Low,Close,Volume\n"
                "2024-06-27,214.69,215.74,212.35,214.10,49772707\n"
                "2024-06-28,215.77,216.07,210.30,210.62,82542718\n")
    closes = StooqProvider._parse_csv(csv_text)
    assert closes[date(2024, 6, 28)] == 210.62
    assert closes[date(2024, 6, 27)] == 214.10
    # Garbage / 'No data' returns empty rather than throwing.
    assert StooqProvider._parse_csv("No data") == {}


if __name__ == "__main__":
    test_valuation_pnl_and_weights()
    test_unpriced_when_no_ticker()
    test_stooq_csv_parse()
    print("All valuation offline tests passed.")
