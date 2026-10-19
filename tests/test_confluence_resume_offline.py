"""Process death, bounded yields, source expiry and complete-only publication."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run
from smartmoney.api_signals import confluence_cache_metadata
from smartmoney.confluence_checkpoint import ConfluenceCheckpoint, ConfluencePending, atomic_json
from smartmoney.forms4 import Form4Client
from tests.test_forms4_offline import CEO_BUY_XML


CIK = "0001234567"
ROOT = Path(__file__).resolve().parents[1]


def test_process_kill_resumes_completed_issuers_then_new_batch_checks_new_filings(tmp_path):
    # Each call really starts another interpreter, losing ALL in-memory state.
    program = '''
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import run
from smartmoney.api import _StoreConfluence
from smartmoney.crosssignal import InstitutionalSignal
from smartmoney.forms4 import Form4Client
from tests.test_forms4_offline import CEO_BUY_XML
root, phase = Path(sys.argv[1]), sys.argv[2]
issuers = {"FIRST": "0001234567", "SECOND": "0001234568", "THIRD": "0001234569"}
_StoreConfluence._issuer_index = lambda self: issuers
_StoreConfluence._institutional = lambda self, **kw: {
    t: InstitutionalSignal(ticker=t, funds_accumulating=3) for t in issuers}
def listed(self, cik, **kw):
    with (root / "requests.jsonl").open("a") as log:
        log.write(json.dumps([phase, cik]) + "\\n")
    if phase == "killed" and cik == issuers["SECOND"]:
        os._exit(9)
    count = 2 if phase == "next" and cik == issuers["FIRST"] else 1
    return [{"accession": f"{cik}-26-{n:06d}",
             "filing_date": datetime.now(timezone.utc).date().isoformat()}
            for n in range(1, count + 1)]
Form4Client.list_form4_accessions = listed
Form4Client.fetch_ownership_xml = lambda self, accession, cik: CEO_BUY_XML.replace("0001234567", cik)
run.cmd_confluence(str(root / "market.db"), "fixture test@example.com", [30, 90, 180])
'''
    caches = [tmp_path / f"confluence-{window}.json" for window in (30, 90, 180)]
    for path in caches:
        path.write_text('{"previous":true}')
    env = {**os.environ, "PYTHONPATH": str(ROOT), "SMARTMONEY_CACHE_DIR": str(tmp_path)}

    def execute(phase):
        return subprocess.run([sys.executable, "-c", program, str(tmp_path), phase],
                              env=env, cwd=ROOT, capture_output=True, text=True)

    first = execute("killed")
    assert first.returncode == 9, first.stderr
    assert all(path.read_text() == '{"previous":true}' for path in caches)
    checkpoint = tmp_path / "confluence-progress" / f"issuer-{CIK}.json"
    original = checkpoint.read_bytes()
    checked_at = json.loads(original)["checked_at"]
    resumed = execute("resumed")
    assert resumed.returncode == 0, resumed.stderr
    assert checkpoint.read_bytes() == original
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert requests == [["killed", CIK], ["killed", "0001234568"],
                        ["resumed", "0001234568"], ["resumed", "0001234569"]]
    for path in caches:
        payload = json.loads(path.read_text())
        receipt = payload["metadata"]["edgar_refresh"]
        assert receipt["issuers_requested"] == receipt["issuers_checked"] == 3
        assert receipt["oldest_source_checked_at"] == checked_at
        assert payload["metadata"]["edgar_refresh_verified"] is True
        assert len(payload["signals"]) == 3
    following = execute("next")
    assert following.returncode == 0, following.stderr
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert requests[-3:] == [["next", CIK], ["next", "0001234568"], ["next", "0001234569"]]
    assert len(json.loads(checkpoint.read_text())["filings"]) == 2


def saved_issuer(tmp_path, monkeypatch):
    batch = ConfluenceCheckpoint(tmp_path / "progress")
    batch.prepare({"scope": "fixture", "window": 180})
    client = Form4Client(user_agent="fixture test@example.com", cache_dir=tmp_path / "xml")
    today = datetime.now(timezone.utc).date()
    meta = {"accession": f"{CIK}-26-000001", "filing_date": today.isoformat()}
    monkeypatch.setattr(client, "list_form4_accessions", lambda *args, **kw: [meta])
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args, **kw: CEO_BUY_XML)
    client.insider_filings(CIK, window_days=180, strict=True, as_of=today)
    batch.save(CIK, client.last_listing)
    return batch, client, today


@pytest.mark.parametrize("damage", ["expired", "future", "naive", "wrong_window", "json", "missing_xml", "invalid_xml", "scope", "complete"])
def test_invalid_or_incompatible_issuer_progress_is_never_reused(tmp_path, monkeypatch, damage):
    batch, client, today = saved_issuer(tmp_path, monkeypatch)
    path = batch.directory / f"issuer-{CIK}.json"
    record = json.loads(path.read_text())
    if damage in ("expired", "future", "naive"):
        checked = datetime.now(timezone.utc) + timedelta(hours={"expired": -25, "future": 1, "naive": 0}[damage])
        record["checked_at"] = (checked.replace(tzinfo=None) if damage == "naive" else checked).isoformat()
    elif damage == "wrong_window":
        record["since"] = today.isoformat()
    elif damage in ("missing_xml", "invalid_xml"):
        source = next((tmp_path / "xml").glob("*.json"))
        if damage == "missing_xml":
            source.unlink()
        else:
            source.write_text("{}")
    elif damage == "scope":
        batch.prepare({"scope": "changed", "window": 180})
    elif damage == "complete":
        batch.complete()
        batch.prepare({"scope": "fixture", "window": 180})
    path.write_text("{" if damage == "json" else json.dumps(record))
    monkeypatch.setattr(client, "fetch_ownership_xml", lambda *args, **kw: pytest.fail("Resume must not fetch a partial old list"))
    assert batch.load(CIK, client, 180, today) is None


def test_zero_filings_are_a_durable_result_and_budget_does_not_publish(tmp_path, monkeypatch):
    batch = ConfluenceCheckpoint(tmp_path / "progress")
    batch.prepare({"scope": 1})
    client = Form4Client(user_agent="fixture test@example.com")
    today = datetime.now(timezone.utc).date()
    checked = datetime.now(timezone.utc).isoformat()
    batch.save(CIK, {"checked_at": checked, "since": (today - timedelta(days=180)).isoformat(), "filings": []})
    assert batch.load(CIK, client, 180, today) == ([], checked)
    paths = [tmp_path / f"confluence-{w}.json" for w in (30, 90, 180)]
    for path in paths:
        path.write_text("previous")

    def yield_slice(*args, **kwargs):
        raise ConfluencePending("saved")

    monkeypatch.setattr(run, "_precompute_confluence", yield_slice)
    assert run.cmd_confluence(str(tmp_path / "market.db"), "fixture test@example.com", [30, 90, 180]) == 75
    assert all(path.read_text() == "previous" for path in paths)
    assert not (tmp_path / "confluence-history.jsonl").exists()


def test_lock_and_symlink_protection_and_expired_work_budget(tmp_path):
    batch = ConfluenceCheckpoint(tmp_path / "progress", 1)
    with batch.locked():
        with pytest.raises(RuntimeError, match="already running"):
            with ConfluenceCheckpoint(batch.directory).locked():
                pass
    batch.deadline = 0
    with pytest.raises(ConfluencePending):
        batch.check_budget()
    target = tmp_path / "outside"
    target.write_text("untouched")
    (batch.directory / "batch.json").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        batch.prepare({})
    with pytest.raises(ValueError, match="symlink"):
        atomic_json(batch.directory / "batch.json", {})
    assert target.read_text() == "untouched"


def test_budget_yield_inside_an_issuer_retains_only_validated_xml(tmp_path, monkeypatch):
    batch = ConfluenceCheckpoint(tmp_path / "progress")
    batch.prepare({"scope": 1})
    client = Form4Client(user_agent="fixture test@example.com", cache_dir=tmp_path / "xml",
                         check_budget=batch.check_budget)
    today = datetime.now(timezone.utc).date()
    metas = [{"accession": f"{CIK}-26-{n:06d}", "filing_date": today.isoformat()} for n in (1, 2)]
    monkeypatch.setattr(client, "list_form4_accessions", lambda *a, **kw: metas)

    def downloaded(*args, **kwargs):
        batch.deadline = 0
        return CEO_BUY_XML

    monkeypatch.setattr(client, "fetch_ownership_xml", downloaded)
    with pytest.raises(ConfluencePending):
        client.insider_filings(CIK, strict=True)
    assert len(list((tmp_path / "xml").glob("*.json"))) == 1
    assert not (batch.directory / f"issuer-{CIK}.json").exists()


@pytest.mark.parametrize("age,status", [(25, "fresh"), (27, "stale"), (-1, "unknown")])
def test_cache_freshness_uses_oldest_listing_not_new_calculation_time(age, status):
    now = datetime.now(timezone.utc)
    payload = {"generated_at": now.isoformat(), "metadata": {"edgar_refresh_verified": True,
               "edgar_refresh": {"oldest_source_checked_at": (now - timedelta(hours=age)).isoformat()}}}
    metadata = confluence_cache_metadata(payload, now=now)
    assert metadata["cache_status"] == status
    assert metadata["edgar_refresh_verified"] is (status == "fresh")
    assert metadata["cache_age_seconds"] == 0


def test_scheduled_worker_does_not_contact_sec_while_sources_are_current(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    for window in (30, 90, 180):
        (tmp_path / f"confluence-{window}.json").write_text(json.dumps({
            "generated_at": now, "metadata": {"edgar_refresh_verified": True,
            "edgar_refresh": {"oldest_source_checked_at": now}}}))
    monkeypatch.setenv("SMARTMONEY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(run, "_precompute_confluence", lambda *a, **kw: pytest.fail("unnecessary SEC request"))
    assert run.cmd_confluence(str(tmp_path / "market.db"), "fixture test@example.com", [30, 90, 180], if_due=True) == 0


def test_scheduled_worker_defers_to_the_shared_13f_lock_without_sec_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_precompute_confluence", lambda *a, **kw: pytest.fail("overlapping SEC collectors"))
    with ConfluenceCheckpoint(tmp_path / "confluence-progress").locked():
        assert run.cmd_confluence(str(tmp_path / "market.db"), "fixture test@example.com", [30, 90, 180], if_due=True) == 0
