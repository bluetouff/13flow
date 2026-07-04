import os
import subprocess
import sys
import tempfile
from pathlib import Path


def test_pro_key_lifecycle_smoke_script_exercises_create_revoke_fail_closed():
    root = Path(__file__).resolve().parents[1]
    script = root / "deploy" / "smoke-pro-key-lifecycle.sh"
    run_py = root / "run.py"

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pro_db = tmp / "pro.db"
        bin_dir = tmp / "bin"
        fake_curl = bin_dir / "curl"
        bin_dir.mkdir()
        fake_curl.write_text(
            r'''#!/usr/bin/env python3
import json
import os
import sqlite3
import sys

args = sys.argv[1:]
out = None
write_code = None
auth = ""
url = ""
i = 0
while i < len(args):
    arg = args[i]
    if arg == "-o":
        out = args[i + 1]
        i += 2
        continue
    if arg == "-w":
        write_code = args[i + 1]
        i += 2
        continue
    if arg == "-H":
        header = args[i + 1]
        if header.lower().startswith("authorization:"):
            auth = header.split(":", 1)[1].strip()
        i += 2
        continue
    if arg.startswith("http://") or arg.startswith("https://"):
        url = arg
    i += 1

code = 404
payload = {"error": "not_found"}

if url.endswith("/api/version"):
    code = 200
    payload = {
        "app": "13flow",
        "git_sha": os.environ.get("EXPECTED_SHA") or "offline",
        "commit": os.environ.get("EXPECTED_SHA") or "offline",
        "open": True,
        "demo": False,
        "public_state": "LIVE",
    }
elif url.endswith("/api/pro/v1/status"):
    token = auth.replace("Bearer ", "", 1)
    key_id = ""
    if token.startswith("13flow_live_"):
        parts = token.split("_")
        if len(parts) >= 3:
            key_id = parts[2]
    row = None
    if key_id:
        conn = sqlite3.connect(os.environ["FAKE_CURL_PRO_DB"])
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        conn.close()
    if row and not row["revoked_at"]:
        code = 200
        payload = {
            "api": "13flow-pro",
            "key": {
                "id": row["key_id"],
                "scopes": str(row["scopes"] or "").split(),
            },
        }
    else:
        code = 401
        payload = {"error": "revoked_api_key"}

body = json.dumps(payload)
if out:
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)
else:
    sys.stdout.write(body)
if write_code:
    sys.stdout.write(write_code.replace("%{http_code}", str(code)))
sys.exit(0 if code < 400 or "-f" not in args else 22)
''',
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)

        env = os.environ.copy()
        env.update({
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "SITE": "https://offline.13flow.test",
            "PRO_DB": str(pro_db),
            "RUN_PY": str(run_py),
            "PYTHON": sys.executable,
            "FAKE_CURL_PRO_DB": str(pro_db),
        })
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "temporary pilot key created" in result.stdout
        assert "operator event api_key.created" in result.stdout
        assert "temporary key revoked" in result.stdout
        assert "operator event api_key.revoked" in result.stdout
        assert "revoked key fails closed" in result.stdout
        assert "PRO KEY LIFECYCLE SMOKE: all good" in result.stdout
        assert "13flow_live_" not in result.stdout
