"""
Offline correctness test — no network. Run: python -m pytest tests/ -q  (or just run this file).

Builds two synthetic quarters and asserts the diff classifies NEW/EXIT/ADD/TRIM/HOLD
correctly and that value-unit normalization fires for the modern (dollars) regime.
"""

from smartmoney.parser import parse_info_table
from smartmoney.portfolio import build_portfolio
from smartmoney.diff import diff_portfolios, Move

NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _table(rows):
    # rows: list of (issuer, cusip, value, shares, putCall)
    body = ""
    for issuer, cusip, value, shares, pc in rows:
        pc_tag = f"<ns1:putCall>{pc}</ns1:putCall>" if pc else ""
        body += f"""
      <ns1:infoTable>
        <ns1:nameOfIssuer>{issuer}</ns1:nameOfIssuer>
        <ns1:titleOfClass>COM</ns1:titleOfClass>
        <ns1:cusip>{cusip}</ns1:cusip>
        <ns1:value>{value}</ns1:value>
        <ns1:shrsOrPrnAmt>
          <ns1:sshPrnamt>{shares}</ns1:sshPrnamt>
          <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
        </ns1:shrsOrPrnAmt>
        {pc_tag}
      </ns1:infoTable>"""
    return f'<?xml version="1.0"?><ns1:informationTable xmlns:ns1="{NS}">{body}</ns1:informationTable>'


def test_parse_and_aggregate():
    # Two rows for AAPL (different managers) must aggregate into one position.
    xml = _table([
        ("APPLE INC", "037833100", 100, 1000, ""),
        ("APPLE INC", "037833100", 50, 500, ""),
    ])
    raw = parse_info_table(xml)
    assert len(raw) == 2
    pf = build_portfolio("0000000000", "Test", "2024-03-31", "13F-HR", raw)
    assert len(pf.positions) == 1
    pos = pf.positions[("037833100", "")]
    assert pos.shares == 1500
    assert pos.value_usd == 150  # modern regime: dollars, no x1000
    assert abs(pos.weight - 1.0) < 1e-9


def test_value_units_legacy_vs_modern():
    xml = _table([("KO", "191216100", 1000, 10, "")])
    legacy = build_portfolio("0", "T", "2019-12-31", "13F-HR", parse_info_table(xml))
    modern = build_portfolio("0", "T", "2024-12-31", "13F-HR", parse_info_table(xml))
    assert legacy.positions[("191216100", "")].value_usd == 1_000_000  # thousands
    assert modern.positions[("191216100", "")].value_usd == 1_000       # dollars


def test_diff_moves():
    prev = build_portfolio("0", "T", "2023-12-31", "13F-HR", parse_info_table(_table([
        ("APPLE INC", "037833100", 1000, 1000, ""),   # will TRIM
        ("COCA COLA", "191216100", 500, 500, ""),      # will EXIT
        ("MICROSOFT", "594918104", 800, 800, ""),      # will HOLD
        ("NVIDIA", "67066G104", 200, 200, ""),         # will ADD
    ])))
    curr = build_portfolio("0", "T", "2024-03-31", "13F-HR", parse_info_table(_table([
        ("APPLE INC", "037833100", 700, 700, ""),      # trimmed 1000 -> 700
        ("MICROSOFT", "594918104", 900, 800, ""),      # shares flat -> HOLD (value moved)
        ("NVIDIA", "67066G104", 600, 600, ""),         # added 200 -> 600
        ("ALPHABET", "02079K305", 300, 300, ""),       # NEW
    ])))
    rep = diff_portfolios(prev, curr)
    moves = {(c.issuer): c.move for c in rep.changes}
    assert moves["APPLE INC"] == Move.TRIM
    assert moves["COCA COLA"] == Move.EXIT
    assert moves["MICROSOFT"] == Move.HOLD
    assert moves["NVIDIA"] == Move.ADD
    assert moves["ALPHABET"] == Move.NEW

    nvidia = next(c for c in rep.changes if c.issuer == "NVIDIA")
    assert abs(nvidia.share_change_pct - 2.0) < 1e-9  # +200%


if __name__ == "__main__":
    test_parse_and_aggregate()
    test_value_units_legacy_vs_modern()
    test_diff_moves()
    print("All offline tests passed.")
