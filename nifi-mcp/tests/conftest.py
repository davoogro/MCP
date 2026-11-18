"""Fixtures compartidas."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nifi_mcp.config import load_config
from nifi_mcp.models import AppConfig


@pytest.fixture
def sample_config_dir(tmp_path: Path) -> Path:
    cfg = {
        "global": {
            "max_parallel_servers": 2,
            "default_timeout_seconds": 5,
            "default_retries": 1,
            "default_limit": 100,
            "max_limit": 1000,
            "cache": {"inventory_ttl_seconds": 10, "status_ttl_seconds": 5},
            "health": {
                "queue_flowfiles_warning": 100,
                "queue_flowfiles_critical": 1000,
                "bulletin_age_minutes": 60,
                "stopped_processor_severity": "informational",
            },
        },
        "servers": {
            "lab01": {
                "name": "Lab 01",
                "host": "nifi-lab.local",
                "environment": "lab",
                "enabled": True,
                "scheme": "https",
                "port": 8443,
                "api_path": "/nifi-api",
                "auth_type": "none",
            },
            "lab02": {
                "name": "Lab 02",
                "host": "nifi-lab2.local",
                "environment": "lab",
                "enabled": False,
                "base_url": "http://nifi-lab2.local:8080/nifi-api",
                "auth_type": "username_password",
                "username_env": "NIFI_LAB02_USER",
                "password_env": "NIFI_LAB02_PASS",
            },
        },
    }
    path = tmp_path / "nifi_servers.yaml"
    path.write_text(yaml.dump(cfg), encoding="utf-8")
    return tmp_path


@pytest.fixture
def app_config(sample_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("NIFI_MCP_CONFIG_DIR", str(sample_config_dir))
    return load_config()


@pytest.fixture
def about_response() -> dict:
    return {"about": {"version": "1.25.0", "uri": "https://nifi/nifi-api"}}


@pytest.fixture
def root_flow_response() -> dict:
    return {
        "processGroupFlow": {
            "id": "root-id",
            "breadcrumb": {"name": "NiFi Flow"},
            "flow": {
                "processGroups": [
                    {"component": {"id": "pg1", "name": "PG1", "state": "RUNNING"}},
                ],
                "processors": [
                    {
                        "component": {
                            "id": "p1",
                            "name": "Proc1",
                            "state": "RUNNING",
                            "type": "org.apache.nifi.Test",
                            "config": {"properties": {"password": "secret"}},
                        }
                    },
                ],
            },
        },
        "breadcrumb": {
            "breadcrumb": {
                "name": "NiFi Flow",
                "ancestors": [],
            }
        },
    }
