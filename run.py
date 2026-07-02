#!/usr/bin/env python3
"""
SmartMoney CLI — reconstruct 13F portfolios, persist them, and run cross-fund screens.

Set a real contact email (SEC requires it) for the live commands:
    export SEC_UA="SmartMoney/1.0 you@example.com"
    export OPENFIGI_APIKEY="..."   # optional, lifts CUSIP->ticker limits

Live (hit EDGAR):
    python run.py --list
    python run.py --verify
    python run.py --fund "Berkshire Hathaway" --top 15 --enrich
    python run.py --sync "Berkshire Hathaway" --enrich        # backfill into the DB
    python run.py --sync-all --max-quarters 12 --enrich       # backfill every tracked fund

Offline (DB only, no SEC_UA needed):
    python run.py --buys 2024-12-31 --min-funds 3             # who's buying (diff-based)
    python run.py --consensus 2024-12-31 --min-funds 3        # who's holding (SQL)
    python run.py --timeline "Berkshire Hathaway" --cusip 037833100
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from smartmoney import EdgarClient, Move, Tracker
from smartmoney.figi import OpenFigiClient, TickerCache
from smartmoney.resolver import CusipResolver, ResolutionCache, load_sec_ticker_index
from smartmoney.db import Store
from smartmoney.analytics import consensus_moves
from smartmoney.prices import StooqProvider, MassiveProvider
from smartmoney.valuation import value_portfolio
from smartmoney.alerts import AlertEngine
from smartmoney.channels import ConsoleChannel, WebhookChannel, EmailChannel
from smartmoney.tracker import Tier
from smartmoney.accounts import AccountStore, EmailTaken, PasswordPolicyError
from smartmoney.hibp import default_breach_checker
from smartmoney.pro import ProAPIStore
from smartmoney.preflight import run_preflight
from smartmoney.quality import data_quality_report
from smartmoney.registry import SUPERINVESTORS, by_label


def _fmt_usd(x: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x/div:,.1f}{unit}"
    return f"${x:,.0f}"


def cmd_list() -> None:
    print("Tracked superinvestors:\n")
    for f in SUPERINVESTORS:
        print(f"  {f.label:<22} {f.manager:<24} CIK={f.cik or '(resolve by name)'}")


def cmd_verify(client: EdgarClient) -> None:
    print("Verifying seed CIKs against EDGAR entity names:\n")
    for f in SUPERINVESTORS:
        if not f.cik:
            print(f"  {f.label:<22} (no CIK; will resolve by name)")
            continue
        try:
            name = client.entity_name(f.cik)
            print(f"  {f.label:<22} CIK={f.cik:<10} -> {name}")
        except Exception as e:  # noqa: BLE001
            print(f"  {f.label:<22} CIK={f.cik:<10} -> ERROR: {e}")


def cmd_fund(client: EdgarClient, label: str, top: int, enrich: bool) -> None:
    fund = by_label(label)
    if fund is None:
        print(f"Unknown fund '{label}'. Try --list.", file=sys.stderr)
        sys.exit(2)

    figi = cache = None
    if enrich:
        figi = OpenFigiClient(api_key=os.environ.get("OPENFIGI_APIKEY"))
        cache = TickerCache()
    tracker = Tracker(client, figi=figi, cache=cache)
    filings = tracker.latest_filings(fund, limit=2)
    if not filings:
        print(f"No 13F-HR filings found for {fund.label}.")
        return

    curr_pf = tracker.portfolio_for_filing(fund, filings[0])
    print(f"\n=== {fund.label} ({fund.manager}) ===")
    print(f"Period {curr_pf.report_date} | form {curr_pf.form} | "
          f"{len(curr_pf.positions)} positions | AUM(13F) {_fmt_usd(curr_pf.total_value)}\n")

    print(f"Top {top} holdings:")
    print(f"  {'Ticker':<8}{'Issuer':<34}{'Type':<6}{'Value':>12}{'Weight':>9}")
    for p in curr_pf.top(top):
        tag = p.put_call or p.title_of_class[:5]
        tkr = (p.ticker or "—")[:7]
        print(f"  {tkr:<8}{p.issuer[:33]:<34}{tag:<6}{_fmt_usd(p.value_usd):>12}{p.weight*100:>8.1f}%")

    if len(filings) < 2:
        print("\n(Only one quarter on file — no diff available.)")
        return

    report = tracker.latest_diff(fund)
    print(f"\nMoves {report.prev_period} -> {report.curr_period}:")
    for move, header in [
        (Move.NEW, "NEW positions"),
        (Move.EXIT, "EXITED"),
        (Move.ADD, "ADDED to"),
        (Move.TRIM, "TRIMMED"),
    ]:
        rows = report.by_move(move)
        if not rows:
            continue
        print(f"\n  {header}:")
        for c in rows[:top]:
            pc = f" [{c.put_call}]" if c.put_call else ""
            tkr = f"{c.ticker} " if c.ticker else ""
            if move in (Move.NEW, Move.EXIT):
                v = c.curr_value if move == Move.NEW else c.prev_value
                print(f"    {tkr}{c.issuer[:34]:<36}{pc} {_fmt_usd(v)}")
            else:
                pct = c.share_change_pct
                pct_s = f"{pct*100:+.0f}%" if pct is not None else "n/a"
                print(f"    {tkr}{c.issuer[:34]:<36}{pc} {pct_s} shares "
                      f"({_fmt_usd(c.prev_value)} -> {_fmt_usd(c.curr_value)})")


def _build_resolver(enrich: bool):
    """Full CUSIP->ticker chain: OpenFIGI + SEC name index + prefix + cache. None if off."""
    if not enrich:
        return None
    figi = OpenFigiClient(api_key=os.environ.get("OPENFIGI_APIKEY"))
    sec_index = {}
    ua = os.environ.get("SEC_UA")
    if ua:
        try:
            sec_index = load_sec_ticker_index(ua)
        except Exception as e:  # noqa: BLE001
            print(f"  (SEC ticker index unavailable: {e})", file=sys.stderr)
    overrides = {}
    if os.path.exists("cusip_overrides.json"):
        import json
        overrides = json.load(open("cusip_overrides.json"))
    return CusipResolver(openfigi=figi, sec_index=sec_index, overrides=overrides,
                         cache=ResolutionCache())


def cmd_sync(client, labels, db_path, enrich, max_quarters, force, report_date,
             sleep_between_funds) -> None:
    tracker = Tracker(client, resolver=_build_resolver(enrich))
    with Store(db_path) as store:
        for i, label in enumerate(labels):
            fund = by_label(label)
            if fund is None:
                print(f"  skip unknown fund '{label}'", file=sys.stderr)
                continue
            n = tracker.sync_fund(store, fund, max_quarters=max_quarters, force=force,
                                  report_date=report_date)
            qs = store.quarters(tracker.cik_for(fund))
            action = "processed" if force else "new"
            print(f"  {fund.label:<22} +{n} {action} filing(s); {len(qs)} quarter(s) stored")
            if sleep_between_funds > 0 and i < len(labels) - 1:
                time.sleep(sleep_between_funds)


def cmd_coverage(db_path, basis) -> None:
    with Store(db_path) as store:
        cov = store.coverage(basis)
        tail = store.unresolved_holdings(basis)
    print(f"\nTicker coverage{(' @ '+basis) if basis else ''}: "
          f"{cov['overall_value_share']*100:.2f}% of 13F value resolved "
          f"({_fmt_usd(cov['value_unresolved'])} unresolved)\n")
    print(f"  {'Fund':<24}{'Resolved':>10}{'Positions':>12}")
    for r in cov["per_fund"]:
        share = f"{r['value_share']*100:.1f}%" if r["value_share"] is not None else "—"
        print(f"  {r['fund'][:23]:<24}{share:>10}   {r['n_res']}/{r['n']}")
    if tail:
        print("\n  Largest unresolved (the tail to attack):")
        for t in tail[:12]:
            print(f"    {t['cusip']}  {_fmt_usd(t['value']):>10}  "
                  f"{(t['issuer'] or '')[:40]} ({t['n_funds']} funds)")


def cmd_quality(db_path, threshold, top) -> None:
    with Store(db_path) as store:
        report = data_quality_report(store, aum_jump_threshold=threshold, limit=top)
    s = report["summary"]
    print("\nData quality report:")
    print(f"  status: {s['status']}")
    print(f"  funds scanned: {s['funds_scanned']}")
    print(f"  series points: {s['series_points']}")
    print(f"  AUM jump warnings: {s['aum_jump_warnings']}")
    print(f"  unit-scale candidates: {s['unit_scale_candidates']}")
    if report["warnings"]:
        print(f"\nTop AUM jumps (threshold {threshold:g}x):")
        for w in report["warnings"][:top]:
            frm, to = w["from"], w["to"]
            print(f"  {w['fund']['label']:<22} {frm['report_date']} {_fmt_usd(frm['total_value'])}"
                  f" -> {to['report_date']} {_fmt_usd(to['total_value'])}"
                  f"  ratio={w['ratio']:.1f}x  {w['severity']}")
    if report["unit_scale_candidates"]:
        print("\nStrict unit-scale candidates:")
        for c in report["unit_scale_candidates"][:top]:
            cur = c["current"]
            print(f"  {c['action']:<13} {c['fund']['label']:<22} {cur['report_date']}"
                  f" {cur['accession']} ratio={c['ratio_to_neighbor_geomean']:.6f}")


def cmd_create_api_key(pro_db, label, scopes, rate_per_min, rate_per_day, expires_days) -> None:
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    with ProAPIStore(pro_db) as pro:
        token, key = pro.create_key(
            label=label,
            scopes=scope_list,
            rate_per_min=rate_per_min,
            rate_per_day=rate_per_day,
            expires_days=expires_days,
        )
    print("Created API key:")
    print(f"  id: {key.key_id}")
    print(f"  label: {key.label}")
    print(f"  scopes: {' '.join(key.scopes)}")
    print(f"  rate: {key.rate_per_min}/min, {key.rate_per_day}/day")
    print("\nCopy this token now; only the SHA-256 hash is stored:")
    print(token)


def cmd_list_api_keys(pro_db) -> None:
    with ProAPIStore(pro_db) as pro:
        rows = pro.list_keys()
    if not rows:
        print("No API keys.")
        return
    print("API keys:\n")
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        print(f"  {r['key_id']}  {state:<7}  {r['label']:<24}  scopes={r['scopes']}  "
              f"rate={r['rate_per_min']}/min,{r['rate_per_day']}/day  "
              f"last_used={r['last_used_at'] or '-'}")


def cmd_revoke_api_key(pro_db, key_id) -> None:
    with ProAPIStore(pro_db) as pro:
        ok = pro.revoke_key(key_id)
    print("revoked" if ok else "not found or already revoked")


def cmd_preflight(db_path, pro_db, require_pro, expected_sha, audit_recent_hours,
                  token_env, as_json) -> None:
    from smartmoney.api import _git_sha
    report = run_preflight(
        db_path,
        pro_db_path=pro_db,
        require_pro=require_pro,
        expected_sha=expected_sha,
        current_sha=_git_sha(),
        audit_recent_hours=audit_recent_hours,
        api_token=os.environ.get(token_env, ""),
    )
    if as_json:
        import json
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n13FLOW production preflight:\n")
        for c in report["checks"]:
            badge = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[c["status"]]
            print(f"  [{badge}] {c['name']:<26} {c['detail']}")
        counts = report["counts"]
        print(f"\nSummary: {report['status'].upper()} "
              f"({counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail)")
    if report["status"] == "fail":
        sys.exit(1)


def cmd_resolve_sweep(client, db_path) -> None:
    resolver = _build_resolver(True)
    with Store(db_path) as store:
        tail = store.unresolved_holdings()
        if not tail:
            print("Nothing unresolved. Coverage is complete.")
            return
        print(f"Sweeping {len(tail)} unresolved CUSIPs through the resolver chain...")
        res = resolver.resolve([(t["cusip"], t["issuer"]) for t in tail])
        by_source, applied = {}, 0
        for cusip, r in res.items():
            if r.ticker and store.apply_resolution(cusip, r.ticker, r.name, r.source, r.confidence):
                applied += 1
                by_source[r.source] = by_source.get(r.source, 0) + 1
    newly = ", ".join(f"{k}:{v}" for k, v in by_source.items()) or "none"
    print(f"Resolved {applied}/{len(tail)} (by source: {newly}). "
          f"Remaining tail retries after the cache TTL.")


def cmd_buys(db_path, report_date, min_funds) -> None:
    with Store(db_path) as store:
        ciks = [r["cik"] for r in store.conn.execute("SELECT cik FROM funds")]
        rows = consensus_moves(store, ciks, report_date, min_funds=min_funds)
    if not rows:
        print(f"No consensus buys at {report_date} with >= {min_funds} funds "
              f"(have you synced enough quarters?).")
        return
    print(f"\nConsensus BUYS at {report_date} (>= {min_funds} funds opening/adding):\n")
    for m in rows:
        tkr = (m.ticker or m.cusip)
        print(f"  {tkr:<10}{m.issuer[:34]:<36} {m.n_funds} funds: {', '.join(m.funds)}")


def cmd_consensus(db_path, report_date, min_funds) -> None:
    with Store(db_path) as store:
        rows = store.consensus_holdings(report_date, min_funds=min_funds)
    if not rows:
        print(f"No consensus holdings at {report_date} with >= {min_funds} funds.")
        return
    print(f"\nConsensus HOLDINGS at {report_date} (>= {min_funds} funds):\n")
    for r in rows:
        tkr = (r["ticker"] or r["cusip"])
        print(f"  {tkr:<10}{(r['issuer'] or '')[:34]:<36} {r['n_funds']} funds  "
              f"{_fmt_usd(r['total_value'])}  [{r['funds']}]")


def cmd_timeline(db_path, label, cusip) -> None:
    fund = by_label(label)
    if fund is None or not fund.cik:
        print(f"Unknown fund or no CIK for '{label}'.", file=sys.stderr)
        sys.exit(2)
    with Store(db_path) as store:
        rows = store.conviction_timeline(fund.cik, cusip)
    if not rows:
        print(f"No stored history for {label} / {cusip}.")
        return
    print(f"\n{fund.label} — conviction in {cusip.upper()}:\n")
    print(f"  {'Quarter':<12}{'Shares':>14}{'Value':>12}{'Weight':>9}")
    for r in rows:
        print(f"  {r['report_date']:<12}{r['shares']:>14,.0f}"
              f"{_fmt_usd(r['value_usd']):>12}{r['weight']*100:>8.1f}%")


def cmd_value(db_path, label, provider_name, basis, fundamentals, top) -> None:
    fund = by_label(label)
    if fund is None or not fund.cik:
        print(f"Unknown fund or no CIK for '{label}'.", file=sys.stderr)
        sys.exit(2)

    if provider_name == "massive":
        key = os.environ.get("MASSIVE_API_KEY")
        if not key:
            print("Set MASSIVE_API_KEY for --provider massive.", file=sys.stderr)
            sys.exit(1)
        base = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
        provider = MassiveProvider(api_key=key, base_url=base)
    else:
        provider = StooqProvider()

    with Store(db_path) as store:
        pf = store.load_portfolio(fund.cik, basis)  # basis None -> latest stored quarter
    if pf is None:
        print(f"No stored portfolio for {label}. Run --sync first.")
        return

    vp = value_portfolio(pf, provider, with_fundamentals=fundamentals)
    pnl_pct = f"{vp.pnl_pct*100:+.1f}%" if vp.pnl_pct is not None else "n/a"
    print(f"\n=== {vp.fund_label} — valued vs {vp.basis_date} (reported quarter {vp.report_date}) ===")
    print(f"Reported {_fmt_usd(vp.reported_total)} | current {_fmt_usd(vp.current_total)} | "
          f"priced-sleeve P&L {_fmt_usd(vp.pnl_abs)} ({pnl_pct})")
    if vp.unpriced_value > 0:
        print(f"  (unpriced/option sleeve carried at reported: {_fmt_usd(vp.unpriced_value)})")

    print(f"\nTop {top} by current value:")
    head = f"  {'Ticker':<8}{'Issuer':<30}{'Cur.wt':>8}{'Px now':>10}{'Since basis':>13}{'Reconcile':>11}"
    print(head)
    for p in vp.top(top):
        tkr = (p.ticker or p.cusip)[:7]
        if p.status != "priced":
            print(f"  {tkr:<8}{p.issuer[:29]:<30}{p.current_weight*100:>7.1f}%{'—':>10}"
                  f"{('['+p.status+']'):>13}{'—':>11}")
            continue
        since = f"{p.pnl_pct*100:+.1f}%" if p.pnl_pct is not None else "n/a"
        rec = f"{p.reconcile_ratio:.2f}x" if p.reconcile_ratio is not None else "—"
        print(f"  {tkr:<8}{p.issuer[:29]:<30}{p.current_weight*100:>7.1f}%"
              f"{p.px_now:>10.2f}{since:>13}{rec:>11}")
    print("\n  Reconcile ~1.00x = ticker mapping looks right; far from 1 = suspect CUSIP->ticker.")
    print("  P&L is paper/implied: assumes holdings unchanged since the filing (they aren't).")


def _alert_channels():
    """Channel map for the CLI. webhook/email are per-subscription factories."""
    chans = {"console": ConsoleChannel(), "webhook": lambda sub: WebhookChannel(sub["target"])}
    host = os.environ.get("SMTP_HOST")
    if host:
        chans["email"] = lambda sub: EmailChannel(
            host=host, port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ.get("SMTP_USER", ""), password=os.environ.get("SMTP_PASS", ""),
            sender=os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")),
        )
    return chans


def _fund_by_cik(cik):
    cik = cik.zfill(10)
    for f in SUPERINVESTORS:
        if f.cik and f.cik.zfill(10) == cik:
            return f
    return None


def cmd_subscribe(db_path, label, channel, target, user, free, prime) -> None:
    fund = by_label(label)
    if fund is None or not fund.cik:
        print(f"Unknown fund or no CIK for '{label}'.", file=sys.stderr)
        sys.exit(2)
    if channel in ("webhook", "email") and not target:
        print(f"--channel {channel} requires --target", file=sys.stderr)
        sys.exit(2)
    tier = Tier("free" if free else "paid", [])
    with Store(db_path) as store:
        engine = AlertEngine(store, channels=_alert_channels())
        try:
            sub_id = engine.subscribe(tier, user, fund, channel, target=target, prime=prime)
        except Exception as e:  # EntitlementError etc.
            print(f"Could not subscribe: {e}", file=sys.stderr)
            sys.exit(1)
    primed = " (primed: only future filings)" if prime else ""
    print(f"Subscribed #{sub_id}: {user} -> {fund.label} via {channel}"
          f"{(' '+target) if target else ''}{primed}")


def cmd_list_subs(db_path) -> None:
    with Store(db_path) as store:
        subs = store.active_subscriptions()
        if not subs:
            print("No active subscriptions.")
            return
        print("Active subscriptions:\n")
        for s in subs:
            fund = _fund_by_cik(s["cik"])
            name = fund.label if fund else s["cik"]
            tgt = f" {s['target']}" if s["target"] else ""
            print(f"  #{s['id']:<4} {s['user_id']:<10} {name:<22} {s['channel']}{tgt}")


def cmd_alerts(client, db_path, dispatch_only) -> None:
    with Store(db_path) as store:
        ciks = store.subscribed_ciks()
        if not ciks:
            print("No subscriptions to process.")
            return
        engine = AlertEngine(store, channels=_alert_channels())
        if dispatch_only:
            results = []
            for cik in ciks:
                results.extend(engine.dispatch_for_fund(cik))
        else:
            engine.tracker = Tracker(client)
            funds = [f for f in (_fund_by_cik(c) for c in ciks) if f is not None]
            results = engine.run_once(funds)
    sent = sum(1 for r in results if r["status"] == "sent")
    failed = sum(1 for r in results if r["status"] == "failed")
    print(f"Dispatched: {sent} sent, {failed} failed, {len(results)} total.")


def cmd_create_user(db_path, email, tier) -> None:
    import getpass
    pw = getpass.getpass("Password (min 12 chars): ")
    if pw != getpass.getpass("Confirm password: "):
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    acc = AccountStore(db_path, breach_checker=default_breach_checker())
    try:
        user = acc.register(email, pw, verified=True)   # operator-created -> pre-verified
        if tier == "paid":
            acc.set_tier(user.id, "paid")
        print(f"Created {user.email} (tier={tier}, verified).")
    except (EmailTaken, PasswordPolicyError, ValueError) as e:
        print(f"Could not create user: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        acc.close()


def cmd_verify_user(db_path, email) -> None:
    acc = AccountStore(db_path)
    try:
        row = acc.get_by_email(email.strip().lower())
        if not row:
            print(f"No such user: {email}", file=sys.stderr)
            sys.exit(1)
        acc.mark_verified(row["id"])
        print(f"{email} -> email verified")
    finally:
        acc.close()


def cmd_set_tier(db_path, email, tier) -> None:
    acc = AccountStore(db_path)
    try:
        row = acc.get_by_email(email.strip().lower())
        if not row:
            print(f"No such user: {email}", file=sys.stderr)
            sys.exit(1)
        acc.set_tier(row["id"], tier)
        print(f"{email} -> tier {tier}")
    finally:
        acc.close()


def cmd_confluence(db_path: str, ua: str, windows) -> None:
    """Precompute the Confluence screen (13F accumulation x live Form 4 buys) and write one
    cache file per window into SMARTMONEY_CACHE_DIR (or next to the DB). The web tier serves
    these instantly, so visitors never trigger EDGAR fetches."""
    import json
    from smartmoney.api import _StoreConfluence
    from smartmoney.api_signals import confluence_payload
    outdir = os.environ.get("SMARTMONEY_CACHE_DIR") or os.path.dirname(os.path.abspath(db_path)) or "."
    prov = _StoreConfluence(db_path, ua)
    for w in windows:
        signals = prov.confluence(w)
        payload = confluence_payload(signals, w)
        path = os.path.join(outdir, f"confluence-{w}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        k = payload["kpis"]
        print(f"  confluence[{w}d]: {k['n_signals']} signals, {k['n_conviction']} conviction "
              f"-> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="SmartMoney 13F tracker")
    ap.add_argument("--list", action="store_true", help="list tracked superinvestors")
    ap.add_argument("--verify", action="store_true", help="verify seed CIKs vs EDGAR")
    ap.add_argument("--fund", help="show a fund's latest portfolio + diff")
    ap.add_argument("--sync", help="backfill one fund into the DB")
    ap.add_argument("--sync-all", action="store_true", help="backfill every tracked fund")
    ap.add_argument("--buys", metavar="YYYY-MM-DD", help="consensus buys at a quarter (DB only)")
    ap.add_argument("--consensus", metavar="YYYY-MM-DD", help="consensus holdings at a quarter (DB only)")
    ap.add_argument("--timeline", metavar="FUND", help="conviction timeline for a fund (with --cusip)")
    ap.add_argument("--cusip", help="CUSIP for --timeline")
    ap.add_argument("--value", metavar="FUND", help="revalue a stored portfolio at current prices (DB + prices)")
    ap.add_argument("--coverage", action="store_true", help="ticker-resolution coverage report (DB only)")
    ap.add_argument("--quality", action="store_true", help="data-quality warnings report (DB only)")
    ap.add_argument("--quality-threshold", type=float, default=100.0,
                    help="AUM jump threshold for --quality (default 100x)")
    ap.add_argument("--preflight", action="store_true",
                    help="offline production preflight: SHA, market DB, Pro DB, audit, quality")
    ap.add_argument("--preflight-json", action="store_true",
                    help="print --preflight as JSON")
    ap.add_argument("--expected-sha", default=os.environ.get("SMARTMONEY_GIT_SHA"),
                    help="expected deployed SHA for --preflight")
    ap.add_argument("--require-pro", action="store_true",
                    help="make Pro DB/API-key/audit checks mandatory in --preflight")
    ap.add_argument("--audit-recent-hours", type=int, default=24,
                    help="audit freshness window for --preflight (default 24h)")
    ap.add_argument("--preflight-token-env", default="SMARTMONEY_PRO_TOKEN",
                    help="env var containing a Pro token for --preflight API-contract checks")
    ap.add_argument("--create-api-key", metavar="LABEL", help="create a Pro API key")
    ap.add_argument("--list-api-keys", action="store_true", help="list Pro API keys")
    ap.add_argument("--revoke-api-key", metavar="KEY_ID", help="revoke a Pro API key")
    ap.add_argument("--pro-db", default=os.environ.get("SMARTMONEY_PRO_DB", "13flow-pro.db"),
                    help="Pro API control-plane SQLite path")
    ap.add_argument("--api-key-scopes", default="funds:read,quality:read",
                    help="comma-separated scopes for --create-api-key")
    ap.add_argument("--api-key-rate-per-min", type=int, default=120,
                    help="requests per minute for --create-api-key")
    ap.add_argument("--api-key-rate-per-day", type=int, default=10000,
                    help="requests per day for --create-api-key")
    ap.add_argument("--api-key-expires-days", type=int, default=None,
                    help="optional expiration in days for --create-api-key")
    ap.add_argument("--resolve-sweep", action="store_true",
                    help="re-run the resolver chain over the unresolved tail and back-fill")
    ap.add_argument("--create-user", metavar="EMAIL", help="create an account (password prompted)")
    ap.add_argument("--verify-user", metavar="EMAIL", help="mark a user's email as verified")
    ap.add_argument("--set-tier", metavar="EMAIL", help="set a user's tier (with --tier)")
    ap.add_argument("--tier", choices=["free", "paid"], default="free", help="tier for --create-user/--set-tier")
    ap.add_argument("--provider", choices=["stooq", "massive"], default="stooq",
                    help="price source for --value (default stooq; massive needs MASSIVE_API_KEY)")
    ap.add_argument("--basis", metavar="YYYY-MM-DD", default=None,
                    help="stored quarter to value (default: latest)")
    ap.add_argument("--fundamentals", action="store_true",
                    help="also fetch market cap / %% of company owned (massive only)")
    ap.add_argument("--subscribe", metavar="FUND", help="subscribe to a fund's filing alerts")
    ap.add_argument("--channel", choices=["console", "webhook", "email"], default="console",
                    help="alert delivery channel for --subscribe")
    ap.add_argument("--target", default="", help="webhook URL or email address for --subscribe")
    ap.add_argument("--user", default="local", help="user id for subscriptions")
    ap.add_argument("--free", action="store_true", help="subscribe as free tier (demonstrates paywall)")
    ap.add_argument("--no-prime", action="store_true",
                    help="deliver the current latest filing immediately instead of priming")
    ap.add_argument("--list-subs", action="store_true", help="list active subscriptions")
    ap.add_argument("--alerts-dispatch", action="store_true",
                    help="deliver pending alerts from already-stored filings (offline)")
    ap.add_argument("--alerts-run", action="store_true",
                    help="sync subscribed funds from EDGAR, then deliver new-filing alerts")
    ap.add_argument("--db", default="smartmoney.db", help="SQLite path (default smartmoney.db)")
    ap.add_argument("--top", type=int, default=20, help="rows to show")
    ap.add_argument("--min-funds", type=int, default=3, help="threshold for consensus screens")
    ap.add_argument("--max-quarters", type=int, default=None, help="limit quarters when syncing")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch and replace filings already stored (use after parser/data fixes)")
    ap.add_argument("--report-date", metavar="YYYY-MM-DD",
                    help="sync/repair only one report_date quarter")
    ap.add_argument("--sleep-between-funds", type=float,
                    default=float(os.environ.get("SMARTMONEY_SYNC_SLEEP_SEC", "0")),
                    help="pause between funds during sync/backfill (seconds)")
    ap.add_argument("--enrich", action="store_true",
                    help="resolve CUSIP->ticker via OpenFIGI (set OPENFIGI_APIKEY for higher limits)")
    ap.add_argument("--confluence", action="store_true",
                    help="precompute the Confluence screen (13F x live Form 4) into cache JSON")
    ap.add_argument("--confluence-windows", default="30,90,180",
                    help="comma-separated day windows to precompute (default 30,90,180)")
    args = ap.parse_args()

    # DB-only commands: no EDGAR, no SEC_UA required.
    if args.list:
        return cmd_list()
    if args.buys:
        return cmd_buys(args.db, args.buys, args.min_funds)
    if args.consensus:
        return cmd_consensus(args.db, args.consensus, args.min_funds)
    if args.timeline:
        if not args.cusip:
            print("--timeline requires --cusip", file=sys.stderr)
            sys.exit(2)
        return cmd_timeline(args.db, args.timeline, args.cusip)
    if args.value:
        return cmd_value(args.db, args.value, args.provider, args.basis,
                         args.fundamentals, args.top)
    if args.subscribe:
        return cmd_subscribe(args.db, args.subscribe, args.channel, args.target,
                             args.user, args.free, prime=not args.no_prime)
    if args.list_subs:
        return cmd_list_subs(args.db)
    if args.alerts_dispatch:
        return cmd_alerts(None, args.db, dispatch_only=True)
    if args.coverage:
        return cmd_coverage(args.db, args.basis)
    if args.quality:
        return cmd_quality(args.db, args.quality_threshold, args.top)
    if args.preflight:
        return cmd_preflight(args.db, args.pro_db, args.require_pro, args.expected_sha,
                             args.audit_recent_hours, args.preflight_token_env,
                             args.preflight_json)
    if args.create_api_key:
        return cmd_create_api_key(args.pro_db, args.create_api_key, args.api_key_scopes,
                                  args.api_key_rate_per_min, args.api_key_rate_per_day,
                                  args.api_key_expires_days)
    if args.list_api_keys:
        return cmd_list_api_keys(args.pro_db)
    if args.revoke_api_key:
        return cmd_revoke_api_key(args.pro_db, args.revoke_api_key)
    if args.create_user:
        return cmd_create_user(args.db, args.create_user, args.tier)
    if args.verify_user:
        return cmd_verify_user(args.db, args.verify_user)
    if args.set_tier:
        return cmd_set_tier(args.db, args.set_tier, args.tier)

    # Live commands: need EDGAR.
    ua = os.environ.get("SEC_UA")
    if not ua:
        print("Set SEC_UA env var, e.g. export SEC_UA='SmartMoney/1.0 you@example.com'",
              file=sys.stderr)
        sys.exit(1)
    client = EdgarClient(user_agent=ua)

    if args.verify:
        cmd_verify(client)
    elif args.fund:
        cmd_fund(client, args.fund, args.top, args.enrich)
    elif args.sync or args.sync_all:
        labels = [f.label for f in SUPERINVESTORS] if args.sync_all else [args.sync]
        print("Syncing into", args.db)
        cmd_sync(client, labels, args.db, args.enrich, args.max_quarters, args.force,
                 args.report_date, args.sleep_between_funds)
    elif args.alerts_run:
        cmd_alerts(client, args.db, dispatch_only=False)
    elif args.resolve_sweep:
        cmd_resolve_sweep(client, args.db)
    elif args.confluence:
        windows = [int(w) for w in args.confluence_windows.split(",") if w.strip()]
        print("Precomputing confluence windows:", windows)
        cmd_confluence(args.db, ua, windows)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
