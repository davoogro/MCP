"""Tests de jobs en segundo plano (mock Popen)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wsl_mcp import jobs


@pytest.fixture
def jobs_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("WSL_DISTRO", "testdistro")
    yield tmp_path


@patch("wsl_mcp.jobs.subprocess.Popen")
def test_start_and_list_job(mock_popen, jobs_tmp):
    mock_popen.return_value = MagicMock(pid=4242)
    out = jobs.start_job("sleep 1")
    assert out["ok"] is True
    job_id = out["job_id"]
    listed = jobs.list_jobs()
    assert listed["count"] == 1
    assert listed["jobs"][0]["job_id"] == job_id


@patch("wsl_mcp.jobs.subprocess.Popen")
def test_get_job_tail(mock_popen, jobs_tmp):
    mock_popen.return_value = MagicMock(pid=1)
    started = jobs.start_job("echo line")
    job_id = started["job_id"]
    log = jobs.jobs_dir() / job_id / "stdout.log"
    log.write_text("line1\nline2\n", encoding="utf-8")
    meta = jobs._load_meta(job_id)
    assert meta is not None
    meta.status = "completed"
    meta.exit_code = 0
    jobs._save_meta(meta)
    got = jobs.get_job(job_id, tail_lines=1)
    assert got["ok"] is True
    assert "line2" in got["stdout"]
