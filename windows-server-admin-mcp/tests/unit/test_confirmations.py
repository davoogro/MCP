"""Confirmation token lifecycle: issue, expire, reuse, parameter binding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from windows_server_admin_mcp.exceptions import (
    ConfirmationError,
    ConfirmationExpiredError,
)


def test_issue_and_consume(confirmation_manager):
    params = {"reasonCode": "PATCHING", "delaySeconds": 300}
    token, record = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    assert len(token) > 20
    assert record.token_prefix == token[:8]
    assert record.used is False

    consumed = confirmation_manager.consume(
        token,
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    assert consumed.token_hash == record.token_hash


def test_reuse_rejected(confirmation_manager):
    params = {"delaySeconds": 300}
    token, _ = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    confirmation_manager.consume(
        token,
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    # Token is deleted after successful consume; reuse surfaces as invalid token.
    with pytest.raises(ConfirmationError, match="Invalid confirmation token"):
        confirmation_manager.consume(
            token,
            server_id="integration-server-01",
            action="server_restart",
            params=params,
        )


def test_expired_token_rejected(confirmation_manager, monkeypatch):
    params = {"delaySeconds": 300}
    token, record = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
        ttl_seconds=60,
    )
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(confirmation_manager, "_purge_expired", lambda: None)

    with pytest.raises(ConfirmationExpiredError):
        confirmation_manager.consume(
            token,
            server_id="integration-server-01",
            action="server_restart",
            params=params,
        )


def test_wrong_server_rejected(confirmation_manager):
    params = {"delaySeconds": 300}
    token, _ = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    with pytest.raises(ConfirmationError, match="not valid for this server"):
        confirmation_manager.consume(
            token,
            server_id="lab-server-02",
            action="server_restart",
            params=params,
        )


def test_wrong_action_rejected(confirmation_manager):
    params = {"delaySeconds": 300}
    token, _ = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    with pytest.raises(ConfirmationError, match="not valid for this action"):
        confirmation_manager.consume(
            token,
            server_id="integration-server-01",
            action="restart_service",
            params=params,
        )


def test_params_binding_rejected(confirmation_manager):
    params = {"delaySeconds": 300}
    token, _ = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    with pytest.raises(ConfirmationError, match="not valid for these parameters"):
        confirmation_manager.consume(
            token,
            server_id="integration-server-01",
            action="server_restart",
            params={"delaySeconds": 600},
        )


def test_invalid_token_rejected(confirmation_manager):
    with pytest.raises(ConfirmationError, match="Invalid confirmation token"):
        confirmation_manager.consume(
            "totally-invalid-token",
            server_id="integration-server-01",
            action="server_restart",
            params={},
        )


def test_cancel_removes_pending(confirmation_manager):
    params = {"delaySeconds": 300}
    token, _ = confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params=params,
    )
    assert confirmation_manager.cancel(token) is True
    with pytest.raises(ConfirmationError):
        confirmation_manager.consume(
            token,
            server_id="integration-server-01",
            action="server_restart",
            params=params,
        )


def test_ttl_clamped(app_config, confirmation_manager):
    with patch.object(
        confirmation_manager,
        "_ttl_seconds",
        wraps=confirmation_manager._ttl_seconds,
    ):
        _, record = confirmation_manager.issue(
            server_id="integration-server-01",
            action="server_restart",
            params={},
            ttl_seconds=99999,
        )
    max_ttl = app_config.policies.confirmation_token_max_ttl_seconds
    delta = (record.expires_at - record.created_at).total_seconds()
    assert delta <= max_ttl


def test_pending_for_server(confirmation_manager):
    confirmation_manager.issue(
        server_id="integration-server-01",
        action="server_restart",
        params={"a": 1},
    )
    pending = confirmation_manager.pending_for_server("integration-server-01")
    assert len(pending) == 1
