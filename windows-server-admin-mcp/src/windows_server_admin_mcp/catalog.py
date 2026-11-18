from __future__ import annotations

from pathlib import Path

from windows_server_admin_mcp.config import AppConfig, compute_file_sha256
from windows_server_admin_mcp.exceptions import ScriptHashMismatchError, ScriptNotAuthorizedError
from windows_server_admin_mcp.models import ScriptEntry


class ScriptCatalog:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def get(self, script_id: str) -> ScriptEntry:
        entry = self.config.scripts.get(script_id)
        if not entry:
            raise ScriptNotAuthorizedError(f"Script not in catalog: {script_id}")
        return entry

    def resolve_path(self, entry: ScriptEntry) -> Path:
        raw = Path(entry.path)
        if raw.is_absolute():
            resolved = raw.resolve()
        else:
            resolved = (self.config.project_root / raw).resolve()

        root = self.config.project_root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            auth_root = self.config.authorized_scripts_dir.resolve()
            try:
                resolved.relative_to(auth_root)
            except ValueError:
                ps_root = self.config.powershell_dir.resolve()
                try:
                    resolved.relative_to(ps_root)
                except ValueError:
                    raise ScriptNotAuthorizedError(
                        f"Script path escapes allowed directories: {entry.path}"
                    )

        if not resolved.exists():
            raise ScriptNotAuthorizedError(f"Script file not found: {resolved}")
        if resolved.suffix.lower() != ".ps1":
            raise ScriptNotAuthorizedError("Only .ps1 scripts are authorized")
        return resolved

    def verify_hash(self, entry: ScriptEntry, path: Path) -> str:
        actual = compute_file_sha256(path)
        expected = entry.sha256
        if expected in ("", "auto", "AUTO"):
            return actual
        if actual.lower() != expected.lower():
            raise ScriptHashMismatchError(
                f"SHA256 mismatch for script {entry.script_id}",
                details={"expected_prefix": expected[:12], "actual_prefix": actual[:12]},
            )
        return actual

    def is_allowed_for_server(self, entry: ScriptEntry, server_id: str) -> bool:
        allowed = entry.allowed_servers
        if not allowed:
            return False
        if "*" in allowed:
            return True
        return server_id in allowed

    def assert_allowed(self, script_id: str, server_id: str) -> tuple[ScriptEntry, Path, str]:
        entry = self.get(script_id)
        if not self.is_allowed_for_server(entry, server_id):
            raise ScriptNotAuthorizedError(
                f"Script {script_id} not authorized for server {server_id}"
            )
        path = self.resolve_path(entry)
        digest = self.verify_hash(entry, path)
        return entry, path, digest
