from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from windows_guarded_admin_mcp.exceptions import OperationNotFoundError
from windows_guarded_admin_mcp.models import OperationRecord, OperationResult, OperationStatus


class OperationRegistry:
    """In-memory registry of long-running or mutating operations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, OperationRecord] = {}

    def create(self, tool_name: str, server_id: str | None = None) -> str:
        operation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        record = OperationRecord(
            operation_id=operation_id,
            tool_name=tool_name,
            server_id=server_id,
            status=OperationStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._operations[operation_id] = record
        return operation_id

    def update(
        self,
        operation_id: str,
        *,
        status: OperationStatus | None = None,
        message: str | None = None,
        result: OperationResult | None = None,
    ) -> OperationRecord:
        with self._lock:
            record = self._operations.get(operation_id)
            if not record:
                raise OperationNotFoundError(f"Operation not found: {operation_id}")
            data = record.model_dump()
            data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if status is not None:
                data["status"] = status
            if message is not None:
                data["message"] = message
            if result is not None:
                data["result"] = result
            updated = OperationRecord(**data)
            self._operations[operation_id] = updated
            return updated

    def get(self, operation_id: str) -> OperationRecord:
        with self._lock:
            record = self._operations.get(operation_id)
            if not record:
                raise OperationNotFoundError(f"Operation not found: {operation_id}")
            return record

    def list_recent(self, limit: int = 50) -> list[OperationRecord]:
        with self._lock:
            items = sorted(
                self._operations.values(),
                key=lambda r: r.updated_at,
                reverse=True,
            )
            return items[:limit]
