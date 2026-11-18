"""Tokens de un solo uso para reinicio de servidor (TTL 10 min)."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _params_hash(params: dict) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class RestartTokenRecord:
    token_hash: str
    token_prefix: str
    server_id: str
    params_hash: str
    created_at: datetime
    expires_at: datetime
    used: bool = False


class RestartConfirmationManager:
    TTL_SECONDS = 600

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, RestartTokenRecord] = {}

    def issue(self, *, server_id: str, params: dict) -> tuple[str, RestartTokenRecord]:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = datetime.now(timezone.utc)
        record = RestartTokenRecord(
            token_hash=token_hash,
            token_prefix=token[:8],
            server_id=server_id,
            params_hash=_params_hash(params),
            created_at=now,
            expires_at=now + timedelta(seconds=self.TTL_SECONDS),
        )
        with self._lock:
            self._records[token_hash] = record
        return token, record

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._records.items() if v.expires_at < now]
        for key in expired:
            del self._records[key]

    def consume(self, token: str, *, server_id: str, params: dict) -> RestartTokenRecord:
        token_hash = _hash_token(token)
        digest = _params_hash(params)
        with self._lock:
            record = self._records.get(token_hash)
            if record and record.expires_at < datetime.now(timezone.utc):
                del self._records[token_hash]
                raise ValueError("token de reinicio expirado")
            self._purge_expired()
            record = self._records.get(token_hash)
            if not record:
                raise ValueError("token de reinicio inválido")
            if record.used:
                raise ValueError("token de reinicio ya utilizado")
            if record.server_id != server_id:
                raise ValueError("token no válido para este servidor")
            if record.params_hash != digest:
                raise ValueError("token no válido para estos parámetros")
            record.used = True
            del self._records[token_hash]
            return record


_restart_mgr: RestartConfirmationManager | None = None


def get_restart_confirmations() -> RestartConfirmationManager:
    global _restart_mgr
    if _restart_mgr is None:
        _restart_mgr = RestartConfirmationManager()
    return _restart_mgr
