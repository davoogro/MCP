"""Shared pytest fixtures for Windows Server Admin MCP."""



from __future__ import annotations



import os

from pathlib import Path



import pytest

import yaml



from windows_server_admin_mcp.catalog import ScriptCatalog

from windows_server_admin_mcp.config import AppConfig, GlobalDefaults, GlobalPolicies, load_config

from windows_server_admin_mcp.confirmations import ConfirmationManager

from windows_server_admin_mcp.models import ServerEntry

from windows_server_admin_mcp.policies import PolicyEngine





PROJECT_ROOT = Path(__file__).resolve().parents[1]

SRC_ROOT = PROJECT_ROOT / "src"



INTEGRATION_SERVER_ID = "integration-server-01"





@pytest.fixture

def project_root() -> Path:

    return PROJECT_ROOT





@pytest.fixture

def test_config_dir(tmp_path: Path, project_root: Path) -> Path:

    """Minimal isolated config for unit tests."""

    cfg = tmp_path / "config"

    cfg.mkdir()



    servers = {

        "defaults": GlobalDefaults().model_dump(),

        "servers": {

            INTEGRATION_SERVER_ID: {

                "hostname": "integration-server-01.example.local",

                "environment": "integration",

                "enabled": True,

                "description": "Example integration host",

                "connection": {

                    "transport": "winrm",

                    "authentication": "current_windows_identity",

                    "powershell_executable": "powershell.exe",

                    "use_pssession": True,

                    "allow_smb_fallback": True,

                    "port": 5985,

                    "use_ssl": False,

                },

                "allow_ip_address": False,

                "tags": ["integration", "read-only"],

                "permissions": {

                    "restart_service": False,

                    "restart_server": False,

                    "cancel_restart": False,

                    "read_operations": True,

                    "authorized_scripts": True,

                },

                "limits": {

                    "command_timeout_seconds": 120,

                    "script_timeout_seconds": 600,

                    "max_parallel_operations": 2,

                },

            },

            "lab-server-02": {

                "hostname": "lab-server-02.example.local",

                "environment": "lab",

                "enabled": True,

                "description": "Lab server with service restart",

                "connection": {

                    "transport": "winrm",

                    "authentication": "current_windows_identity",

                    "port": 5985,

                    "use_ssl": False,

                },

                "permissions": {

                    "restart_service": True,

                    "restart_server": False,

                    "cancel_restart": False,

                    "read_operations": True,

                    "authorized_scripts": True,

                },

            },

        },

    }

    policies = {

        "global": GlobalPolicies().model_dump(),

        "service_restart_denylist": ["RpcSs", "NTDS", "MSEXCHANGEIS"],

        "approved_diagnostics": ["ping", "dns_resolve", "tcp_port_check"],

        "approved_tcp_ports": [5985, 5986, 445, 135],

        "restart_reason_codes": {

            "PLANNED_MAINTENANCE": "Planned maintenance window",

            "PATCHING": "Security or operating system patching",

        },

    }

    scripts = {

        "scripts": {

            "smoke_test": {

                "description": "Smoke test script",

                "path": "authorized_scripts/smoke_test.ps1",

                "sha256": "auto",

                "timeout_seconds": 60,

                "allowed_servers": [INTEGRATION_SERVER_ID, "lab-server-02"],

                "parameters": {

                    "message": {"type": "string", "required": False},

                },

            },

            "bad_hash_script": {

                "description": "Script with wrong hash",

                "path": "authorized_scripts/smoke_test.ps1",

                "sha256": "0" * 64,

                "timeout_seconds": 60,

                "allowed_servers": [INTEGRATION_SERVER_ID],

            },

        }

    }



    (cfg / "servers.yaml").write_text(yaml.dump(servers), encoding="utf-8")

    (cfg / "policies.yaml").write_text(yaml.dump(policies), encoding="utf-8")

    (cfg / "scripts.yaml").write_text(yaml.dump(scripts), encoding="utf-8")

    return cfg





@pytest.fixture

def app_config(test_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:

    monkeypatch.setenv("WSA_MCP_PROJECT_ROOT", str(PROJECT_ROOT))

    monkeypatch.setenv("WSA_MCP_CONFIG_DIR", str(test_config_dir))

    return load_config(test_config_dir)





@pytest.fixture

def policy_engine(app_config: AppConfig) -> PolicyEngine:

    return PolicyEngine(app_config)





@pytest.fixture

def confirmation_manager(app_config: AppConfig) -> ConfirmationManager:

    return ConfirmationManager(app_config)





@pytest.fixture

def script_catalog(app_config: AppConfig) -> ScriptCatalog:

    return ScriptCatalog(app_config)





@pytest.fixture

def integration_server(app_config: AppConfig) -> ServerEntry:

    return app_config.servers[INTEGRATION_SERVER_ID]





@pytest.fixture

def lab_server(app_config: AppConfig) -> ServerEntry:

    return app_config.servers["lab-server-02"]





def pytest_configure(config: pytest.Config) -> None:

    config.addinivalue_line(

        "markers",

        "integration: live remote tests (set WSA_MCP_INTEGRATION=1)",

    )





def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:

    if os.environ.get("WSA_MCP_INTEGRATION") == "1":

        return

    skip_integration = pytest.mark.skip(

        reason="Integration tests disabled (set WSA_MCP_INTEGRATION=1)"

    )

    for item in items:

        if "integration" in item.keywords:

            item.add_marker(skip_integration)

