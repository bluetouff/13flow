"""
Offline tests for the Form 4 x 13F confluence feature (13FLOW). No network.
Run: PYTHONPATH=. python tests/test_forms4_offline.py
"""

from __future__ import annotations

import sys
from datetime import date

from smartmoney.forms4 import parse_form4
from smartmoney.crosssignal import (
    InstitutionalSignal, aggregate_insider_activity, build_confluence,
    score_confluence, DEFAULT_WEIGHTS,
)
from smartmoney.sample_confluence import sample_signals
from smartmoney.backtest import make_synthetic, evaluate, optimize_weights, spearman_ic

AS_OF = date(2026, 5, 31)

CEO_BUY_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-05-29</periodOfReport>
  <issuer>
    <issuerCik>0001234567</issuerCik><issuerName>Atlantic Union Holdings</issuerName>
    <issuerTradingSymbol>ATLC</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0009990001</rptOwnerCik><rptOwnerName>Marlin Jordan</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-29</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>50000</value></transactionShares>
        <transactionPricePerShare><value>84.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>220000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-29</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>10.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

CFO_BUY_XML = CEO_BUY_XML.replace("Marlin Jordan", "Devereux Sara").replace(
    "Chief Executive Officer", "Chief Financial Officer").replace(
    "0009990001", "0009990002").replace("<value>50000</value>", "<value>18000</value>")


def check(cond, msg):
    if not cond:
        print(f"  FAIL: {msg}"); raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_parse():
    print("parse_form4:")
    f = parse_form4(CEO_BUY_XML, accession="x-26-1", filing_date="2026-05-30")
    check(f.issuer_ticker == "ATLC", "issuer ticker parsed")
    check(f.owner_name == "Marlin Jordan", "owner name parsed")
    check(f.is_officer and f.is_c_suite, "CEO flagged as C-suite")
    check(len(f.open_market_buys) == 1, "only the P transaction is an open-market buy")
    check(f.open_market_buys[0].shares == 50000, "buy share count")
    check(abs(f.open_market_buys[0].value_usd - 4_200_000) < 1, "buy value = shares*price")


def test_aggregate_and_score():
    print("aggregate + score (fresh C-suite cluster):")
    ceo, cfo = parse_form4(CEO_BUY_XML), parse_form4(CFO_BUY_XML)
    act = aggregate_insider_activity("ATLC", [ceo, cfo], issuer_name="Atlantic Union", as_of=AS_OF)
    check(act.n_buyers == 2 and act.n_c_suite_buyers == 2, "two C-suite buyers")
    check(act.is_cluster, "flagged as cluster")
    check(act.days_since_last_buy == 2, "freshness computed from txn date (2d)")
    check(act.recent_cluster_n == 2, "both inside the 14d recent window")
    check(act.conviction_units > 0, "conviction units accumulated")
    check(act.max_stake_increase_pct > 0.25, "CEO stake increase ~29% picked up")

    inst = InstitutionalSignal("ATLC", funds_accumulating=4, conviction_funds=2, avg_weight_pct=4.0)
    sig = score_confluence(inst, act)
    check(sig.quadrant == "conviction", "both buying -> Conviction")
    check(sig.score > 84, f"strong fresh confluence scores high (got {sig.score:.1f})")
    bd = sig.breakdown
    check(abs(sum(bd.values()) - sig.score) < 0.05, "breakdown sums to score (unclamped)")


def test_recency_moves_the_needle():
    print("temporal freshness changes the score:")
    ceo = parse_form4(CEO_BUY_XML)  # txn 2026-05-29
    fresh = aggregate_insider_activity("ATLC", [ceo], as_of=date(2026, 5, 31))   # 2d old
    stale = aggregate_insider_activity("ATLC", [ceo], as_of=date(2026, 8, 29))   # ~92d old
    check(fresh.conviction_units > stale.conviction_units, "fresh buy carries more conviction")
    inst = InstitutionalSignal("ATLC", funds_accumulating=2)
    sf = score_confluence(inst, fresh).score
    ss = score_confluence(inst, stale).score
    check(sf > ss + 3, f"fresh outscores stale ({sf:.1f} > {ss:.1f})")


def test_quadrants_and_ranking():
    print("sample quadrants + ranking:")
    sigs = {s.ticker: s for s in sample_signals(90, as_of=AS_OF)}
    check(sigs["ATLC"].quadrant == "conviction", "ATLC = Conviction")
    check(sigs["CMPO"].quadrant == "institutional", "CMPO = Institutional bid")
    check(sigs["PARA"].quadrant in ("distribution", "divergent"), "PARA = Distribution/Divergent")
    ranked = sample_signals(90, as_of=AS_OF)
    check(ranked[0].ticker == "ATLC", "fresh cluster ranks #1")
    check(ranked[1].ticker == "VRNS", "fresh single CEO buy (VRNS) beats older 2-buyer NVCR")


def test_backtest_optimizer():
    print("backtest optimiser improves rank-IC on synthetic data:")
    obs = make_synthetic(n=400, seed=7)
    base = evaluate(obs, DEFAULT_WEIGHTS)
    tuned_w, report = optimize_weights(obs)
    after = report["after"]
    check(base["ic"] > 0, f"default weights already have signal (IC={base['ic']})")
    check(after["ic"] >= base["ic"], f"tuning does not hurt IC ({base['ic']} -> {after['ic']})")
    check(after["ic"] > base["ic"] + 0.005, f"tuning measurably improves IC (+{after['ic']-base['ic']:.3f})")
    check(after["quantile_spread"] >= base["quantile_spread"] - 1e-6, "top-bottom spread preserved/improved")


if __name__ == "__main__":
    try:
        test_parse()
        test_aggregate_and_score()
        test_recency_moves_the_needle()
        test_quadrants_and_ranking()
        test_backtest_optimizer()
    except AssertionError:
        sys.exit(1)
    print("\nAll Form4 x 13F confluence + backtest tests passed.")
