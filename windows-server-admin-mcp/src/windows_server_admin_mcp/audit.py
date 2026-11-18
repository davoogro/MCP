from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from windows_server_admin_mcp.models import AuditEvent

REDACT_KEYS = re.compile(
    r"^(password|passwd|secret|token|confirmation_token|apikey|authorization|credential)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"(password|passwd|pwd|secret|token|apikey)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
]


def audit_log_dir() -> Path:
    """Audit directory: WSA_MCP_AUDIT_DIR if set, else %LOCALAPPDATA%/WindowsServerAdminMCP/logs.

    Never depends on the process cwd.
    """
    env_dir = os.environ.get("WSA_MCP_AUDIT_DIR", "").strip()
    if env_dir:
        log_dir = Path(env_dir).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    log_dir = base / "WindowsServerAdminMCP" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _sanitize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        redacted = value
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        if len(redacted) > 32 and re.fullmatch(r"[A-Za-z0-9_-]{20,}", redacted):
            return redacted[:8] + "…[REDACTED]"
        return redacted
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if REDACT_KEYS.match(k) else _sanitize_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


class AuditLogger:
    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or audit_log_dir()

    def _log_path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"audit-{day}.jsonl"

    def write(self, event: AuditEvent) -> None:
        payload = event.model_dump()
        if payload.get("parameters"):
            payload["parameters"] = _sanitize_value(payload["parameters"])
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log_tool(
        self,
        *,
        tool: str,
        outcome: str,
        duration_ms: int = 0,
        server_id: str | None = None,
        operation_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        error_code: str | None = None,
        message: str | None = None,
        event_type: str = "tool.result",
    ) -> None:
        self.write(
            AuditEvent(
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                event_type=event_type,
                tool=tool,
                server_id=server_id,
                outcome=outcome,
                duration_ms=duration_ms,
                operation_id=operation_id,
                parameters=parameters,
                error_code=error_code,
                message=message,
                actor=os.environ.get("USERNAME"),
            )
        )

    def query(
        self,
        *,
        tool: str | None = None,
        server_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        files = sorted(self.log_dir.glob("audit-*.jsonl"), reverse=True)
        since_dt = None
        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

        for path in files:
            if len(rows) >= limit:
                break
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
            # Newest first: files are sorted newest-first and lines are appended chronologically.
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tool and row.get("tool") != tool:
                    continue
                if server_id and row.get("server_id") != server_id:
                    continue
                if since_dt:
                    ts = row.get("timestamp", "")
                    try:
                        row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if row_dt < since_dt:
                            continue
                    except ValueError:
                        pass
                rows.append(row)
                if len(rows) >= limit:
                    break
        rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return rows[:limit]
