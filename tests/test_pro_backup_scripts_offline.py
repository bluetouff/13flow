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
