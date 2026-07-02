"""
Offline adjusted-price export tests for Confluence validation.
"""

import csv
import subprocess
import sys
from datetime import date
from pathlib import Path

from smartmoney.prices import PriceProvider, StooqProvider
from smartmoney.validation_prices import (
    PRICE_COLUMNS,
    build_validation_price_file,
    provider_symbol,
)


class FakePriceProvider(PriceProvider):
    DATA = {
        "AAPL": {
            date(2024, 1, 2): 100.0,
            date(2024, 1, 3): 101.0,
        },
        "BRK.B": {
            date(2024, 1, 2): 300.0,
            date(2024, 1, 3): 303.0,
        },
    }

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def daily_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        self.calls.append(ticker)
        return {
            d: px
            for d, px in self.DATA.get(ticker, {}).items()
            if start <= d <= end
        }


def _ticker_file(path: Path) -> None:
    path.write_text("AAPL\nBRK/B\nNOPE\nAAPL\n# ignored\n\n", encoding="utf-8")


def test_build_validation_price_file_writes_provider_neutral_csv(tmp_path):
    tickers = tmp_path / "tickers.txt"
    out = tmp_path / "prices.csv"
    _ticker_file(tickers)
    provider = FakePriceProvider()

    summary = build_validation_price_file(
        str(tickers),
        str(out),
        provider,
        provider_name="massive",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )

    assert summary["tickers_requested"] == 3
    assert summary["tickers_fetched"] == 2
    assert summary["tickers_with_no_data"] == 1
    assert summary["coverage"] == 0.666667
    assert summary["rows_total"] == 4
    assert provider.calls == ["AAPL", "BRK.B", "NOPE"]

    with out.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert tuple(reader.fieldnames or []) == PRICE_COLUMNS
    assert rows[0] == {"ticker": "AAPL", "date": "2024-01-02", "adj_close": "100"}
    assert rows[-1] == {"ticker": "BRK/B", "date": "2024-01-03", "adj_close": "303"}


def test_build_validation_price_file_resumes_existing_tickers(tmp_path):
    tickers = tmp_path / "tickers.txt"
    out = tmp_path / "prices.csv"
    _ticker_file(tickers)
    first_provider = FakePriceProvider()
    build_validation_price_file(
        str(tickers),
        str(out),
        first_provider,
        provider_name="massive",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )

    second_provider = FakePriceProvider()
    summary = build_validation_price_file(
        str(tickers),
        str(out),
        second_provider,
        provider_name="massive",
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
    )

    assert summary["tickers_cached"] == 2
    assert summary["tickers_fetched"] == 0
    assert summary["tickers_with_no_data"] == 1
    assert summary["rows_total"] == 4
    assert second_provider.calls == ["NOPE"]


def test_provider_symbol_maps_share_classes():
    assert provider_symbol("BRK/B", "massive") == "BRK.B"
    assert StooqProvider()._symbol("BRK/B") == "brk-b.us"


def test_run_py_help_exposes_validation_price_export():
    proc = subprocess.run(
        [sys.executable, "run.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--build-validation-prices" in proc.stdout
    assert "--validation-prices-out" in proc.stdout
