"""Audit log query returns the newest entries first."""

from __future__ import annotations

from pathlib import Path

from windows_server_admin_mcp.audit import AuditLogger, audit_log_dir


def test_query_returns_newest_entries(tmp_path: Path):
    logger = AuditLogger(log_dir=tmp_path)
    for i in range(5):
        logger.log_tool(tool=f"tool_{i}", outcome="success")
    rows = logger.query(limit=2)
    assert [r["tool"] for r in rows] == ["tool_4", "tool_3"]


def test_confirmation_token_never_logged(tmp_path: Path):
    logger = AuditLogger(log_dir=tmp_path)
    logger.log_tool(
        tool="confirm_server_restart",
        outcome="failure",
        parameters={"server_id": "x", "confirmation_token": "abcdefghijklmnopqrstuvwxyz0123456789"},
    )
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in text


def test_audit_dir_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WSA_MCP_AUDIT_DIR", str(tmp_path / "a"))
    assert audit_log_dir() == (tmp_path / "a").resolve()
