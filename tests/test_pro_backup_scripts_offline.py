import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from smartmoney.pro import ProAPIStore


REQUIRED_TOOLS = ("gpg", "sqlite3", "sha256sum", "tar")


def _missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


def _gpg_symmetric_batch_error() -> str:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        gpg_home = tmp / "gnupg"
        passphrase = tmp / "pass"
        payload = tmp / "payload.txt"
        encrypted = tmp / "payload.txt.gpg"
        gpg_home.mkdir(mode=0o700)
        passphrase.write_text("test-passphrase\n", encoding="utf-8")
        payload.write_text("payload\n", encoding="utf-8")
        env = os.environ.copy()
        env["GNUPGHOME"] = str(gpg_home)
        result = subprocess.run(
            [
                "gpg", "--batch", "--yes",
                "--pinentry-mode", "loopback",
                "--passphrase-file", str(passphrase),
                "--no-symkey-cache",
                "--symmetric", "--cipher-algo", "AES256",
                "--output", str(encrypted),
                str(payload),
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        return "" if result.returncode == 0 else (result.stderr.strip() or "gpg batch failed")


def test_restore_verify_skips_cleanly_without_private_key():
    root = Path(__file__).resolve().parents[1]
    verify_script = root / "deploy" / "verify-pro-db-backup.sh"

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bin_dir = tmp / "bin"
        verify_dir = tmp / "verify"
        backup_file = tmp / "13flow-pro-test.tar.gz.gpg"
        fake_gpg = bin_dir / "gpg"
        bin_dir.mkdir()
        backup_file.write_bytes(b"not really encrypted")
        fake_gpg.write_text(
            "#!/usr/bin/env bash\n"
            "echo 'gpg: encrypted with rsa4096 key, ID EBDDF6E279A16C74' >&2\n"
            "echo 'gpg: echec du dechiffrement par clef publique: Pas de clef secrete' >&2\n"
            "echo 'gpg: decryption failed: No secret key' >&2\n"
            "exit 2\n",
            encoding="utf-8",
        )
        fake_gpg.chmod(0o700)

        env = os.environ.copy()
        env.update({
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "VERIFY_WORK_DIR": str(verify_dir),
        })
        result = subprocess.run(
            ["bash", str(verify_script), str(backup_file)],
            env=env,
            text=True,
            capture_output=True,
        )

        assert result.returncode == 77
        assert "RESTORE VERIFY SKIPPED" in result.stderr
        assert "private key" in result.stderr


@pytest.mark.skipif("sha256sum" in _missing_tools(), reason="sha256sum is unavailable")
def test_prepare_restore_check_selects_latest_backup_and_writes_checksum():
    root = Path(__file__).resolve().parents[1]
    prepare_script = root / "deploy" / "prepare-pro-backup-restore-check.sh"

    with tempfile.TemporaryDirectory() as d:
        backup_dir = Path(d)
        old_backup = backup_dir / "13flow-pro-20260703T010000Z.tar.gz.gpg"
        latest_backup = backup_dir / "13flow-pro-20260703T020000Z.tar.gz.gpg"
        old_backup.write_bytes(b"old encrypted archive")
        latest_backup.write_bytes(b"latest encrypted archive")

        env = os.environ.copy()
        env.update({
            "BACKUP_DIR": str(backup_dir),
            "WRITE_SHA256": "1",
            "RESTORE_WORK_DIR": "./restore-work",
        })
        result = subprocess.run(
            ["bash", str(prepare_script)],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

        sidecar = latest_backup.with_suffix(latest_backup.suffix + ".sha256")
        assert f"backup_file={latest_backup}" in result.stdout
        assert f"backup_name={latest_backup.name}" in result.stdout
        assert f"sha256_sidecar={sidecar}" in result.stdout
        assert "sha256sum -c" in result.stdout
        assert "RESTORE VERIFY OK" in result.stdout
        assert sidecar.exists()
        assert latest_backup.name in sidecar.read_text(encoding="utf-8")


@pytest.mark.skipif(_missing_tools(), reason="backup verification tools are unavailable")
def test_encrypted_pro_backup_can_be_restore_verified():
    gpg_error = _gpg_symmetric_batch_error()
    if gpg_error:
        pytest.skip(f"gpg symmetric batch mode unavailable: {gpg_error}")

    root = Path(__file__).resolve().parents[1]
    backup_script = root / "deploy" / "backup-pro-db.sh"
    verify_script = root / "deploy" / "verify-pro-db-backup.sh"

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pro_db = tmp / "13flow-pro.db"
        backup_dir = tmp / "backups"
        work_dir = tmp / "work"
        verify_dir = tmp / "verify"
        gpg_home = tmp / "gnupg"
        passphrase_file = tmp / "backup.pass"
        gpg_home.mkdir(mode=0o700)
        passphrase_file.write_text("temporary-test-passphrase\n", encoding="utf-8")
        passphrase_file.chmod(0o600)

        with ProAPIStore(str(pro_db)) as pro:
            _token, key = pro.create_key(
                "Backup Test", scopes=("funds:read", "workspace:write"),
            )
            watchlist = pro.create_watchlist(
                key.key_id,
                "Backup smoke",
                ["AAPL"],
                filters={"action": ["alert"]},
                alert_policy={"enabled": False, "frequency": "manual"},
            )
            snapshot = pro.create_signal_snapshot(
                key.key_id,
                watchlist["id"],
                {
                    "metadata": {"version": "saved_watchlist_signals_v1"},
                    "summary": {"alerts": 0},
                    "items": [],
                },
            )
            pro.upsert_workspace_alerts(key.key_id, watchlist["id"], snapshot["id"], {"items": []})
            pro.record_workspace_activity(
                key.key_id,
                "backup.test",
                "watchlist",
                watchlist["id"],
                "Backup test event",
            )

        env = os.environ.copy()
        env.update({
            "PRO_DB": str(pro_db),
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_WORK_DIR": str(work_dir),
            "VERIFY_WORK_DIR": str(verify_dir),
            "BACKUP_PASSPHRASE_FILE": str(passphrase_file),
            "GPG_HOMEDIR": str(gpg_home),
        })

        backup = subprocess.run(
            ["bash", str(backup_script)],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        backup_file = Path(backup.stdout.strip().splitlines()[-1])
        assert backup_file.exists()
        assert backup_file.suffix == ".gpg"

        verify = subprocess.run(
            ["bash", str(verify_script), str(backup_file)],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "sqlite_integrity=ok" in verify.stdout
        assert "saved_watchlists=1" in verify.stdout
        assert "signal_snapshots=1" in verify.stdout
        assert "workspace_activity=1" in verify.stdout
        assert "RESTORE VERIFY OK" in verify.stdout
