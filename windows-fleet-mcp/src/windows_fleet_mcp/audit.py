"""Auditoría JSONL diaria."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from windows_fleet_mcp.config import get_settings
from windows_fleet_mcp.util import current_user

REDACT_KEYS = re.compile(
    r"^(password|passwd|secret|token|confirmation_token|restart_token|apikey|authorization|credential)$",
    re.IGNORECASE,
)
MAX_PARAM_CHARS = 20_000


def audit_dir() -> Path:
    path = get_settings().audit_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > MAX_PARAM_CHARS:
            return value[:MAX_PARAM_CHARS] + "…[truncado]"
        return value
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if REDACT_KEYS.match(k) else _sanitize_value(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item, depth + 1) for item in value[:200]]
    return value


class AuditLogger:
    def __init__(self, log_dir: Optional[Path] = None) -> None:
        self.log_dir = log_dir or audit_dir()

    def _log_path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"audit-{day}.jsonl"

    def write_event(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log_tool(
        self,
        *,
        tool: str,
        servers: list[str] | None,
        parameters: dict[str, Any] | None,
        ok: bool,
        error_code: str | None = None,
        duration_ms: int = 0,
        correlation_id: str | None = None,
    ) -> str:
        cid = correlation_id or str(uuid4())
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "correlation_id": cid,
            "actor": current_user(),
            "tool": tool,
            "servers": servers,
            "parameters": _sanitize_value(parameters or {}),
            "ok": ok,
            "error_code": error_code,
            "duration_ms": duration_ms,
        }
        self.write_event(payload)
        return cid

    def query(
        self,
        *,
        since_hours: float = 24,
        tool: str | None = None,
        server: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        rows: list[dict[str, Any]] = []
        files = sorted(self.log_dir.glob("audit-*.jsonl"), reverse=True)
        for path in files:
            if len(rows) >= limit:
                break
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = row.get("timestamp", "")
                try:
                    row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if row_dt < since_dt:
                        continue
                except ValueError:
                    pass
                if tool and row.get("tool") != tool:
                    continue
                if server:
                    srv_list = row.get("servers") or []
                    if server not in srv_list and row.get("server_id") != server:
                        continue
                rows.append(row)
                if len(rows) >= limit:
                    break
        rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return rows[:limit]


_audit: Optional[AuditLogger] = None


def get_audit() -> AuditLogger:
    global _audit
    if _audit is None:
        _audit = AuditLogger()
    return _audit
