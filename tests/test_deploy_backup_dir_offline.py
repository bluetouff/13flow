"""Exercise the actual default-directory setup in isolated Linux fixtures."""
import os
import platform
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/deploy-code-safe.sh"
pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or os.geteuid() != 0,
    reason="Linux ownership tests run as root inside an isolated container",
)


@pytest.fixture
def anchors(tmp_path):
    var = tmp_path / "var"
    backups = var / "backups"
    backups.mkdir(parents=True)
    var.chmod(0o755)
    backups.chmod(0o755)
    return var, backups, backups / "13flow-deploy"


def prepare(anchors, override=None):
    var, _backups, _target = anchors
    script = SCRIPT.read_text(encoding="utf-8")
    variables = script.split("backup_dir_is_default=0\n", 1)[1].split("\nMCP_UNIT=", 1)[0]
    function = script.split("prepare_default_backup_dir() {\n", 1)[1].split("\n}\n", 1)[0]
    # Remap only the fixed /var anchors; ownership, symlink checks, install and
    # stat execute unchanged against real private files, never the host's /var.
    variables = variables.replace("/var/backups/13flow-deploy", str(anchors[2]))
    function = function.replace(
        'in / /var /var/backups "$BACKUP_DIR"; do',
        'in / "$TEST_ROOT/var" "$TEST_ROOT/var/backups" "$BACKUP_DIR"; do',
    )
    env = {**os.environ, "TEST_ROOT": str(var.parent)}
    env.pop("BACKUP_DIR", None)
    if override is not None:
        env["BACKUP_DIR"] = override
    return subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\nbackup_dir_is_default=0\n" + variables
         + "\nprepare_default_backup_dir() {\n" + function + "\n}\n"
         + 'prepare_default_backup_dir\nprintf "PREPARED\\n"\n'],
        env=env, capture_output=True, text=True,
    )


@pytest.mark.parametrize("override", [None, ""])
def test_default_created_private_before_copy(anchors, override):
    result = prepare(anchors, override)
    assert result.returncode == 0, result.stderr
    target = anchors[2]
    metadata = target.stat()
    assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (0, 0, 0o700)
    assert "PREPARED" in result.stdout


def test_existing_safe_default_is_hardened_without_touching_contents(anchors):
    target = anchors[2]
    target.mkdir(mode=0o750)
    sentinel = target / "previous-backup"
    sentinel.write_text("preserved", encoding="utf-8")
    sentinel.chmod(0o640)
    before = sentinel.stat()
    result = prepare(anchors)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert sentinel.read_text(encoding="utf-8") == "preserved"
    after = sentinel.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns) == (
        before.st_ino, before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns,
    )


@pytest.mark.parametrize("anchor_index", [0, 1, 2])
@pytest.mark.parametrize("unsafe", ["symlink", "dangling", "file", "owner", "group", "group-write", "other-write"])
def test_unsafe_anchor_rejected_before_install(anchors, tmp_path, anchor_index, unsafe):
    anchor = anchors[anchor_index]
    if anchor_index == 2:
        anchor.mkdir(mode=0o700)
    if unsafe in {"symlink", "dangling", "file"}:
        if anchor_index == 0:
            anchors[1].rmdir()
        anchor.rmdir()
        if unsafe == "file":
            anchor.write_text("not a directory", encoding="utf-8")
        else:
            outside = tmp_path / "outside"
            if unsafe == "symlink":
                outside.mkdir(mode=0o700)
            anchor.symlink_to(outside, target_is_directory=True)
    elif unsafe == "owner":
        os.chown(anchor, 12345, 0)
    elif unsafe == "group":
        os.chown(anchor, 0, 12345)
    else:
        anchor.chmod(0o720 if unsafe == "group-write" else 0o702)
    before = anchor.lstat()
    result = prepare(anchors)
    assert result.returncode != 0
    assert "Refusing deploy:" in result.stderr
    assert "PREPARED" not in result.stdout
    assert anchor.lstat() == before
    if anchor_index != 2 and unsafe not in {"symlink", "dangling", "file"}:
        assert not anchors[2].exists()
    if unsafe == "symlink":
        assert list((tmp_path / "outside").iterdir()) == []


@pytest.mark.parametrize("kind", ["existing", "absent", "symlink", "confluence"])
def test_explicit_override_retains_existing_behavior(anchors, tmp_path, kind):
    target = tmp_path / "explicit backup override"
    if kind == "existing":
        target.mkdir(mode=0o750)
        os.chown(target, 12345, 12345)
    elif kind == "symlink":
        target.symlink_to(tmp_path / "not-created", target_is_directory=True)
    elif kind == "confluence":
        # Existing activate-confluence.py supplies a root-owned work subdir
        # below its ingestion data directory. This patch does not change it.
        ingestion = tmp_path / "ingestion"
        ingestion.mkdir(mode=0o2750)
        os.chown(ingestion, 12345, 12345)
        target = ingestion / "13flow-cache-fixture" / "deployment"
        target.mkdir(parents=True, mode=0o700)
    before = target.lstat() if target.exists() or target.is_symlink() else None
    result = prepare(anchors, str(target))
    assert result.returncode == 0, result.stderr
    after = target.lstat() if target.exists() or target.is_symlink() else None
    assert before == after
    assert not anchors[2].exists()


def test_setup_fails_before_rollback_trap_and_backup_copy():
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.index("\nprepare_default_backup_dir\n") < script.index("\ntrap rollback ERR\n")
    assert script.index("\ntrap rollback ERR\n") < script.index('cp -a "$APP_DIR" "$backup"')
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
