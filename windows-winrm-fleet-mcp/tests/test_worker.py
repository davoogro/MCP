from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from windows_winrm_fleet_mcp.worker import FakeWorker, PowerShellWorkerProcess

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKER_SCRIPT = PACKAGE_ROOT / "src" / "windows_winrm_fleet_mcp" / "worker_host.ps1"


def test_fake_worker_invoke():
    fw = FakeWorker(lambda req: {"data": {"probe": req["probe"]}})
    resp = fw.invoke(["a", "b"], "whoami", {})
    assert resp["results"]["a"]["ok"] is True
    assert resp["results"]["b"]["data"]["probe"] == "whoami"
    fw.close_sessions()
    assert fw.list_open_sessions() == set()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell worker solo en Windows")
def test_worker_host_ping():
    assert WORKER_SCRIPT.is_file()
    proc = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WORKER_SCRIPT),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    req_id = "test-ping-1"
    line = json.dumps({"id": req_id, "op": "ping"}) + "\n"
    assert proc.stdin
    proc.stdin.write(line)
    proc.stdin.flush()
    out_line = proc.stdout.readline() if proc.stdout else ""
    proc.kill()
    msg = json.loads(out_line)
    assert msg.get("ok") is True
    assert msg.get("pong") is True


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell worker solo en Windows")
def test_worker_invoke_local_whoami():
    proc = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WORKER_SCRIPT),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    req_id = "local-1"
    payload = {"id": req_id, "op": "invoke_local", "probe": "whoami", "args": {}}
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    out_line = proc.stdout.readline()
    proc.kill()
    msg = json.loads(out_line)
    assert msg.get("ok") is True
    assert "whoami" in msg.get("data", {})
