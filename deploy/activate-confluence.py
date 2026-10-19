#!/usr/bin/env python3
"""User-run activation: qualify caches before changing the served release."""
import argparse
import fcntl
import json
import re
import math
import os
import pwd
import grp
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/opt/13flow")
DATA = Path("/var/lib/13flow")
DB = DATA / "13flow.db"
DROPINS = Path("/etc/systemd/system/13flow-refresh.service.d")
TEMP_UNIT = DROPINS / "zzz-release-precompute.conf"
TARGETS = [DROPINS / "zz-confluence.conf", Path("/etc/13flow/13flow-confluence.env")]
CONFLUENCE_UNITS = [Path("/etc/systemd/system") / f"13flow-confluence.{kind}" for kind in ("service", "timer")]
WORKER_ENV = Path("/etc/13flow/13flow-confluence-worker.env")
STAMPED_FILES = [Path("/etc/systemd/system/13flow.service.d/version.conf"),
                 Path("/etc/systemd/system/13flow-pro.service.d/version.conf"),
                 Path("/etc/13flow/13flow-mcp.env")]
CACHES = [f"confluence-{w}.json" for w in (30, 90, 180)] + ["confluence-history.jsonl"]


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def active(unit):
    result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    # A running Type=oneshot ingest is 'activating' until it finishes.
    return result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}


def public(path):
    with urllib.request.urlopen("https://13flow.eu" + path, timeout=20) as response:
        return json.load(response)


def install(source, target, mode=0o644):
    run(["install", "-o", "root", "-g", "root", "-m", f"{mode:o}", source, target])


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="exact release commit")
    parser.add_argument("--previous-sha", required=True, help="expected currently served commit")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Git checkout containing the release (default: this script's checkout)")
    args = parser.parse_args(argv)
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (args.sha, args.previous_sha)):
        parser.error("Both revisions must be exact 40-character hexadecimal commits.")
    return args


def precompute_with_progress():
    # Follow only this invocation's new logs; never print service environment files.
    journal = subprocess.Popen(["journalctl", "--unit=13flow-refresh.service", "--follow",
                                "--lines=0", "--no-pager", "--output=cat"])
    try:
        run(["systemctl", "start", "13flow-refresh.service"])
        status = int(subprocess.check_output(
            ["systemctl", "show", "13flow-refresh.service", "-p", "ExecMainStatus", "--value"], text=True).strip())
        if status not in (0, 75):
            raise RuntimeError(f"Unexpected precompute exit status: {status}")
        return status
    except subprocess.CalledProcessError:
        run(["journalctl", "--unit=13flow-refresh.service", "--lines=60",
             "--no-pager", "--output=cat"])
        raise
    finally:
        journal.terminate()
        try:
            journal.wait(timeout=5)
        except subprocess.TimeoutExpired:
            journal.kill()
            journal.wait()


def precompute_slices():
    # 75 is a planned yield, NEVER an upstream failure or a killed process.
    # A failed start propagates immediately without retrying provider errors.
    for number in range(1, 25):
        print(f"Confluence work slice {number}/24 (50 minute budget).", flush=True)
        if precompute_with_progress() != 75:
            return
    raise RuntimeError("Confluence remains incomplete after 24 slices; checkpoints retained.")


def activate(args):
    SHA, PREVIOUS_SHA = args.sha, args.previous_sha
    CHECKOUT = args.source.resolve()
    if os.geteuid() != 0:
        raise SystemExit("Run this activation personally with sudo.")
    if TEMP_UNIT.exists() or TEMP_UNIT.is_symlink():
        raise SystemExit("A temporary refresh override already exists; no changes made.")
    if active("13flow-refresh.service") or active("13flow-confluence.service"):
        raise SystemExit("Ingestion is already running; no changes made.")
    if public("/api/version").get("git_sha") != PREVIOUS_SHA:
        raise SystemExit("Unexpected production revision; no changes made.")
    run(["flock", "--version"], stdout=subprocess.DEVNULL)
    work = Path(tempfile.mkdtemp(prefix=f"13flow-cache-{SHA[:7]}-", dir=DATA))
    gid = grp.getgrnam("flowapp").gr_gid
    uid = pwd.getpwnam("flowingest").pw_uid
    os.chown(work, 0, gid)
    work.chmod(0o750)
    saved = work / "saved"
    saved.mkdir(mode=0o700)
    source = work / "source"
    source.mkdir(mode=0o750)
    archive = saved / "source.tar"
    with archive.open("wb") as output:
        run(["git", "--no-replace-objects", "-c", f"safe.directory={CHECKOUT}",
             "-C", CHECKOUT, "archive", "--format=tar", SHA], stdout=output)
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter="data")
    for path in [source, *source.rglob("*")]:
        if path.is_symlink():
            raise SystemExit("Unexpected source symlink; no changes made.")
        os.chown(path, 0, gid)
        path.chmod(0o750 if path.is_dir() else 0o640)
    candidate = work / "candidate"
    candidate.mkdir(mode=0o750)
    os.chown(candidate, uid, gid)
    existing = {}
    for index, target in enumerate([*(DATA / name for name in CACHES), *TARGETS, *STAMPED_FILES,
                                    *CONFLUENCE_UNITS, WORKER_ENV]):
        if target.is_symlink():
            raise SystemExit("Unexpected target symlink; no changes made.")
        backup = saved / str(index)
        existing[target] = backup if target.exists() else None
        if target.exists():
            shutil.copy2(target, backup)
    history = DATA / CACHES[-1]
    if history.exists():
        shutil.copy2(history, candidate / history.name)
        os.chown(candidate / history.name, uid, gid)
        (candidate / history.name).chmod(0o640)
    preflight = work / "preflight.py"
    preflight.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from smartmoney.api import _StoreConfluence\n"
        "from smartmoney.forms4 import Form4Client\n"
        "ua = os.environ.get('SEC_UA', '')\n"
        "if not ua or 'you@example.com' in ua:\n"
        "    raise SystemExit('A configured SEC contact is required; no cache changes made.')\n"
        f"provider = _StoreConfluence({str(DB)!r}, ua)\n"
        "index = provider._issuer_index()\n"
        "if not index:\n"
        "    raise SystemExit('SEC company index unavailable; no cache changes made.')\n"
        "ticker = sorted(index)[0]\n"
        "Form4Client(user_agent=ua).insider_filings(index[ticker], window_days=30, max_filings=1, strict=True)\n"
        "print('SEC preflight passed: company index and one bounded filing lookup.', flush=True)\n"
    )
    os.chown(preflight, 0, gid)
    preflight.chmod(0o640)
    driver = work / "precompute.sh"
    driver.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        f'[[ "${{SMARTMONEY_DB:-{DB}}}" == {shlex.quote(str(DB))} ]] || exit 3\n'
        f"export SMARTMONEY_CACHE_DIR={shlex.quote(str(candidate))}\n"
        f"export SMARTMONEY_GIT_SHA={SHA}\n"
        'export SMARTMONEY_EDGAR_RATE_PER_SEC=${SMARTMONEY_EDGAR_RATE_PER_SEC:-1.0}\n'
        f"cd {shlex.quote(str(source))}\n"
        f"{APP}/.venv/bin/python {preflight}\n"
        f"exec {APP}/.venv/bin/python -u run.py --db {DB} --confluence --confluence-budget-seconds 3000\n"
    )
    os.chown(driver, 0, gid)
    driver.chmod(0o750)
    timer_was_active = active("13flow-refresh.timer")
    confluence_timer_was_active = active("13flow-confluence.timer")
    changed = False
    deployed = False
    attempted_deploy = False
    phase = "precompute"
    try:
        run(["systemctl", "stop", "13flow-refresh.timer"])
        if confluence_timer_was_active:
            run(["systemctl", "stop", "13flow-confluence.timer"])
        if active("13flow-refresh.service") or active("13flow-confluence.service"):
            raise RuntimeError("Ingestion started during preparation; refusing to overlap it.")
        TEMP_UNIT.write_text(
            "[Service]\nTimeoutStartSec=2h\nSuccessExitStatus=75\nExecStart=\n"
            f"ExecStart=/bin/bash {driver}\n"
        )
        TEMP_UNIT.chmod(0o644)
        run(["systemctl", "daemon-reload"])
        print("Precomputing complete SEC caches; existing production caches stay served.", flush=True)
        precompute_slices()
        for window in (30, 90, 180):
            payload = json.loads((candidate / f"confluence-{window}.json").read_text())
            meta = payload.get("metadata") or {}
            receipt = meta.get("edgar_refresh") or {}
            generated = datetime.fromisoformat(payload["generated_at"])
            age = (datetime.now(timezone.utc) - generated).total_seconds()
            oldest = datetime.fromisoformat(receipt["oldest_source_checked_at"])
            source_age = (datetime.now(timezone.utc) - oldest).total_seconds()
            signals = payload.get("signals") or []
            assert 0 <= age <= 26 * 3600
            assert age <= source_age <= 26 * 3600
            assert meta.get("edgar_refresh_verified") is True
            assert receipt.get("issuers_checked", 0) > 0
            assert receipt.get("issuers_checked") == receipt.get("issuers_requested")
            assert receipt.get("issuer_failures") == 0
            assert signals and payload["kpis"]["n_signals"] == len(signals)
            assert all(math.isfinite(s["score"]) and 0 <= s["score"] <= 100 for s in signals)
            print(json.dumps({"window_days": window, "signals": len(signals),
                              "issuers_checked": receipt["issuers_checked"],
                              "unmapped_tickers": len((meta.get("universe_coverage") or {}).get("excluded_tickers", [])),
                              "generated_at": payload["generated_at"]}), flush=True)
        print("All three cache windows qualified.", flush=True)
        # Preserve the actual candidate's scan threshold and SEC cadence, including
        # settings inherited from host-specific refresh-service drop-ins.
        policy = meta["collection_policy"]
        scan_min, rate = policy["scan_min_funds"], policy["sec_rate_per_sec"]
        assert type(scan_min) is int and scan_min >= 1
        assert type(rate) in (int, float) and math.isfinite(rate) and 0 < rate <= 10
        worker_env = work / "confluence-worker.env"
        worker_env.write_text(f"SMARTMONEY_CONFLUENCE_SCAN_MIN_FUNDS={scan_min}\n"
                              f"SMARTMONEY_EDGAR_RATE_PER_SEC={rate}\n"
                              f"SMARTMONEY_GIT_SHA={SHA}\n"
                              f"SMARTMONEY_CACHE_DIR={DATA}\n")
        worker_env.chmod(0o600)
        phase = "publication"
        changed = True
        for name in CACHES:
            os.replace(candidate / name, DATA / name)
        deploy_dir = work / "deployment"
        deploy_dir.mkdir(mode=0o700)
        log = saved / "deploy.log"
        attempted_deploy = True
        with log.open("w") as output:
            run(["bash", source / "deploy/deploy-code-safe.sh"],
                env={**os.environ, "SHA": SHA, "SRC": str(source), "BACKUP_DIR": str(deploy_dir)},
                stdout=output, stderr=subprocess.STDOUT)
        deployed = True
        install(source / "deploy/13flow-refresh-confluence.conf", TARGETS[0])
        install(source / "deploy/13flow-confluence.env", TARGETS[1])
        install(worker_env, WORKER_ENV, mode=0o600)
        for unit in CONFLUENCE_UNITS:
            install(source / "deploy" / unit.name, unit)
        run(["systemd-analyze", "verify", *CONFLUENCE_UNITS])
        TEMP_UNIT.unlink()
        run(["systemctl", "daemon-reload"])
        budget = subprocess.check_output(
            ["systemctl", "show", "13flow-refresh.service", "-p", "TimeoutStartUSec", "--value"], text=True
        ).strip()
        assert budget == "2h", f"Unexpected refresh budget: {budget}"
        phase = "public verification"
        with (saved / "smoke.log").open("w") as output:
            run(["bash", APP / "deploy/smoke-public.sh"],
                env={**os.environ, "EXPECTED_SHA": SHA}, stdout=output, stderr=subprocess.STDOUT)
        print(json.dumps({"activated_sha": SHA, "public_smoke": "passed",
                          "cache_windows": [30, 90, 180], "backup_directory": str(work)}), flush=True)
    except BaseException:
        if active("13flow-refresh.service"):
            subprocess.run(["systemctl", "stop", "13flow-refresh.service"])
        if changed:
            for target, backup in existing.items():
                if backup is None:
                    target.unlink(missing_ok=True)
                elif target.parent == DATA:
                    restored = work / ("restore-" + target.name)
                    shutil.copy2(backup, restored)
                    os.chown(restored, uid, gid)
                    os.replace(restored, target)
                else:
                    shutil.copy2(backup, target)
            if deployed:
                previous = next(path for path in (work / "deployment").glob("13flow-backup-before-safe-deploy-*")
                                if path.is_dir() and not path.name.endswith("-etc"))
                rollback_dir = work / "rollback"
                rollback_dir.mkdir(mode=0o700)
                with (saved / "rollback.log").open("w") as output:
                    run(["bash", previous / "deploy/deploy-code-safe.sh"],
                        env={**os.environ, "SHA": PREVIOUS_SHA, "SRC": str(previous),
                             "BACKUP_DIR": str(rollback_dir)}, stdout=output, stderr=subprocess.STDOUT)
            elif attempted_deploy:
                run(["systemctl", "daemon-reload"])
                run(["systemctl", "restart", "13flow", "13flow-pro", "13flow-mcp"])
        print(f"Activation failed during {phase}; previous caches retained/restored. Logs: {work}", file=sys.stderr)
        if phase == "precompute":
            print("Completed issuer checkpoints and validated Form 4 documents remain cached. "
                  "Fix the reported cause before resuming this activation.", file=sys.stderr)
        raise
    finally:
        TEMP_UNIT.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"])
        if timer_was_active:
            run(["systemctl", "start", "13flow-refresh.timer"])
        if confluence_timer_was_active:
            run(["systemctl", "start", "13flow-confluence.timer"])
    run(["systemctl", "enable", "--now", "13flow-confluence.timer"])
    # Reuse the existing aggregator job; unrelated producer warnings do not undo 13FLOW.
    result = subprocess.run(["systemctl", "start", "l0g-risk.service"])
    print(json.dumps({"l0g_refresh_exit_status": result.returncode}), flush=True)


def main(argv=None):
    args = arguments(argv)
    if os.geteuid() != 0:
        raise SystemExit("Run this activation personally with sudo.")
    descriptor = os.open(DATA / "confluence-release.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another Confluence activation is running; no changes made.")
        activate(args)


if __name__ == "__main__":
    main()
