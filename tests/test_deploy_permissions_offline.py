"""Exercise deployment permissions without root or changes to host services."""
import os
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("import_status", [0, 1])
def test_restrictive_install_permissions_and_service_import_gate(tmp_path, import_status):
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy/deploy-code-safe.sh").read_text(encoding="utf-8")
    # Execute the actual permissions/import phase. Only ownership changes and
    # identity switching are stubbed; chmod/find run against a private fixture.
    phase = script.split('echo "==> [5/8]', 1)[1].split('echo "==> [6/8]', 1)[0]
    phase = phase.split("\n", 1)[1]
    app = tmp_path / "app with spaces"
    sdk = app / "mcp-server/node_modules/@modelcontextprotocol/sdk"
    sdk.mkdir(parents=True)
    package = sdk / "package.json"
    package.write_text("{}", encoding="utf-8")
    executable = sdk / "cli.js"
    executable.write_text("// fixture", encoding="utf-8")
    source = app / "application.py"
    source.write_text("# fixture", encoding="utf-8")
    venv = app / ".venv"
    venv.mkdir()
    runtime = venv / "python"
    runtime.write_text("# preserved runtime", encoding="utf-8")
    (app / "deploy").mkdir()
    deploy = app / "deploy/test.sh"
    deploy.write_text("#!/bin/sh", encoding="utf-8")
    for path in [app, *app.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    executable.chmod(0o700)
    runtime.chmod(0o711)
    outside = tmp_path / "private"
    outside.mkdir(mode=0o700)
    private = outside / "secret"
    private.write_text("fixture", encoding="utf-8")
    private.chmod(0o600)
    (sdk / "outside").symlink_to(outside, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    chown = bin_dir / "chown"
    chown.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chown.chmod(0o700)

    result = subprocess.run(
        ["bash", "-c", '''
set -Eeuo pipefail
umask 077
runuser() {
  [[ "$1 $2 $3 $4 $5 $6" == "-u flowmcp -- /usr/bin/node --input-type=module -e" ]]
  [[ "$7" == *"@modelcontextprotocol/sdk/server/mcp.js"* ]]
  return "$IMPORT_STATUS"
}
''' + phase + '\nprintf "RESTART_ALLOWED\\n"\n'],
        cwd=app / "mcp-server",
        env={**os.environ, "APP_DIR": str(app), "WEB_GROUP": "unused",
             "MCP_GROUP": "unused", "IMPORT_STATUS": str(import_status),
             "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True, text=True,
    )
    assert result.returncode == import_status, result.stderr
    assert ("RESTART_ALLOWED" in result.stdout) == (import_status == 0)
    for path in [sdk, sdk.parent, sdk.parent.parent, app / "mcp-server"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
    assert stat.S_IMODE(package.stat().st_mode) == 0o640
    assert stat.S_IMODE(executable.stat().st_mode) == 0o750
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    assert stat.S_IMODE(deploy.stat().st_mode) == 0o750
    assert stat.S_IMODE(venv.stat().st_mode) == 0o700
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o711
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
