"""Exercise the release transaction locally, with no root commands or network."""
import ast
import importlib.util
import io
import json
import os
import shlex
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "activate-confluence.py"
SHA, PREVIOUS = "a" * 40, "b" * 40


@pytest.fixture
def release():
    spec = importlib.util.spec_from_file_location("confluence_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_revisions_are_required_and_running_oneshot_is_busy(release, monkeypatch):
    with pytest.raises(SystemExit):
        release.arguments(["--sha", "main", "--previous-sha", PREVIOUS])
    assert release.arguments(["--sha", SHA, "--previous-sha", PREVIOUS]).sha == SHA
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="activating\n", returncode=3))
    assert release.active("13flow-refresh.service") is True


def test_journal_follower_is_reaped_on_a_refresh_failure(release, monkeypatch):
    actions = []
    process = SimpleNamespace(terminate=lambda: actions.append("terminate"), wait=lambda **kwargs: actions.append("wait"))
    monkeypatch.setattr(release.subprocess, "Popen", lambda *args, **kwargs: process)

    def run(args, **kwargs):
        if args[0] == "systemctl":
            raise subprocess.CalledProcessError(1, args)
        actions.append("diagnostics")

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        release.precompute_with_progress()
    assert actions == ["diagnostics", "terminate", "wait"]


@pytest.mark.parametrize("failure_phase", ["precompute", "smoke", None])
def test_release_restores_caches_and_timer_on_failure_and_retains_source_checkpoints(release, tmp_path, monkeypatch, failure_phase):
    data, app, dropins = (tmp_path / name for name in ("data", "app", "dropins"))
    for path in (data, app, dropins):
        path.mkdir()
    targets = [dropins / "zz-confluence.conf", tmp_path / "confluence.env"]
    stamps = [tmp_path / "version.conf"]
    for name, value in {"DATA": data, "APP": app, "DB": data / "market.db", "DROPINS": dropins,
                        "TEMP_UNIT": dropins / "zzz-release-precompute.conf", "TARGETS": targets,
                        "STAMPED_FILES": stamps}.items():
        monkeypatch.setattr(release, name, value)
    previous_files = [*(data / name for name in release.CACHES), *targets, *stamps]
    for path in previous_files:
        path.write_text("previous content")
    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setattr(release.os, "chown", lambda *args: None)
    monkeypatch.setattr(release.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setattr(release.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(release, "public", lambda path: {"git_sha": PREVIOUS})
    monkeypatch.setattr(release, "active", lambda unit: unit == "13flow-refresh.timer")
    commands = []

    def run(args, **kwargs):
        args = [str(item) for item in args]
        commands.append(args)
        if args[0] == "git":
            with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                for name in ("deploy/13flow-refresh-confluence.conf", "deploy/13flow-confluence.env"):
                    body = b"candidate config"
                    entry = tarfile.TarInfo(name)
                    entry.size = len(body)
                    archive.addfile(entry, io.BytesIO(body))
        elif args[0] == "install":
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
        elif args[0] == "bash" and args[1].endswith("deploy-code-safe.sh"):
            work = Path(kwargs["env"]["BACKUP_DIR"])
            (work / "13flow-backup-before-safe-deploy-fixture").mkdir()
        elif args[0] == "bash" and args[1].endswith("smoke-public.sh") and failure_phase == "smoke":
            assert json.loads((data / "confluence-90.json").read_text())["signals"]
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release.subprocess, "check_output", lambda *args, **kwargs: "2h\n")
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    def precompute():
        assert release.TEMP_UNIT.exists()
        checkpoint = data / "form4-filings"
        checkpoint.mkdir()
        (checkpoint / "validated-fixture.json").write_text("checkpoint retained")
        if failure_phase == "precompute":
            raise subprocess.CalledProcessError(1, ["systemctl", "start"])
        candidate = next(data.glob("13flow-cache-*/candidate"))
        generated = datetime.now(timezone.utc).isoformat()
        for window in (30, 90, 180):
            payload = {"generated_at": generated, "signals": [{"score": 10}], "kpis": {"n_signals": 1},
                       "metadata": {"edgar_refresh_verified": True, "edgar_refresh": {
                           "issuers_checked": 1, "issuers_requested": 1, "issuer_failures": 0}}}
            (candidate / f"confluence-{window}.json").write_text(json.dumps(payload))
        (candidate / "confluence-history.jsonl").write_text("candidate history")

    monkeypatch.setattr(release, "precompute_with_progress", precompute)
    args = SimpleNamespace(sha=SHA, previous_sha=PREVIOUS, source=tmp_path / "checkout")
    if failure_phase:
        with pytest.raises(subprocess.CalledProcessError):
            release.activate(args)
        assert all(path.read_text() == "previous content" for path in previous_files)
    else:
        release.activate(args)
        assert json.loads((data / "confluence-90.json").read_text())["kpis"]["n_signals"] == 1
    assert not release.TEMP_UNIT.exists()
    assert ["systemctl", "start", "13flow-refresh.timer"] in commands
    assert (data / "form4-filings" / "validated-fixture.json").read_text() == "checkpoint retained"
    deploys = [cmd for cmd in commands if cmd[0] == "bash" and cmd[1].endswith("deploy-code-safe.sh")]
    assert len(deploys) == {"precompute": 0, "smoke": 2, None: 1}[failure_phase]


def test_generated_preflight_and_driver_are_valid_and_bounded(release):
    scope = {"source": Path("/var/lib/13flow/fixture/source"), "candidate": Path("/var/lib/13flow/fixture/candidate"),
             "preflight": Path("/var/lib/13flow/fixture/preflight.py"), "APP": release.APP, "DB": release.DB,
             "SHA": SHA, "shlex": shlex}
    checked = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "write_text" or not isinstance(node.func.value, ast.Name)):
            continue
        name = node.func.value.id
        if name not in {"preflight", "driver"}:
            continue
        body = eval(compile(ast.Expression(node.args[0]), "<release-body>", "eval"), scope)
        if name == "preflight":
            compile(body, "<preflight>", "exec")
            assert "max_filings=1, strict=True" in body
        else:
            subprocess.run(["bash", "-n"], input=body, text=True, check=True)
        checked.add(name)
    assert checked == {"preflight", "driver"}
