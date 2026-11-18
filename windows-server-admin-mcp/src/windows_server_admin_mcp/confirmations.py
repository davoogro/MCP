from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone

from windows_server_admin_mcp.config import AppConfig
from windows_server_admin_mcp.exceptions import (
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationUsedError,
)
from windows_server_admin_mcp.models import ConfirmationRecord


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _params_hash(params: dict) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfirmationManager:
    """One-time confirmation tokens for mutating restart operations."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._records: dict[str, ConfirmationRecord] = {}

    def _ttl_seconds(self, requested: int | None) -> int:
        policies = self.config.policies
        default = policies.confirmation_token_ttl_seconds
        maximum = policies.confirmation_token_max_ttl_seconds
        ttl = requested if requested is not None else default
        return max(60, min(ttl, maximum))

    def issue(
        self,
        *,
        server_id: str,
        action: str,
        params: dict,
        ttl_seconds: int | None = None,
    ) -> tuple[str, ConfirmationRecord]:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        prefix = token[:8]
        now = datetime.now(timezone.utc)
        ttl = self._ttl_seconds(ttl_seconds)
        record = ConfirmationRecord(
            token_hash=token_hash,
            token_prefix=prefix,
            server_id=server_id,
            action=action,
            params_hash=_params_hash(params),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        with self._lock:
            self._records[token_hash] = record
        return token, record

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._records.items() if v.expires_at < now]
        for key in expired:
            del self._records[key]

    def consume(
        self,
        token: str,
        *,
        server_id: str,
        action: str,
        params: dict,
    ) -> ConfirmationRecord:
        token_hash = _hash_token(token)
        params_digest = _params_hash(params)
        with self._lock:
            self._purge_expired()
            record = self._records.get(token_hash)
            if not record:
                raise ConfirmationError("Invalid confirmation token")
            if record.used:
                raise ConfirmationUsedError("Confirmation token already used")
            if record.expires_at < datetime.now(timezone.utc):
                del self._records[token_hash]
                raise ConfirmationExpiredError("Confirmation token expired")
            if record.server_id != server_id:
                raise ConfirmationError("Token not valid for this server")
            if record.action != action:
                raise ConfirmationError("Token not valid for this action")
            if record.params_hash != params_digest:
                raise ConfirmationError("Token not valid for these parameters")
            record.used = True
            del self._records[token_hash]
            return record

    def cancel(self, token: str) -> bool:
        token_hash = _hash_token(token)
        with self._lock:
            if token_hash in self._records:
                del self._records[token_hash]
                return True
            return False

    def pending_for_server(self, server_id: str) -> list[ConfirmationRecord]:
        with self._lock:
            self._purge_expired()
            return [r for r in self._records.values() if r.server_id == server_id and not r.used]
