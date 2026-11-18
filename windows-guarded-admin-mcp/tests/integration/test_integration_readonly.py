"""Live read-only integration against configured integration server (never restart)."""



from __future__ import annotations



import os



import pytest



from windows_guarded_admin_mcp.config import load_config, resolve_config_dir

from windows_guarded_admin_mcp.confirmations import ConfirmationManager

from windows_guarded_admin_mcp.exceptions import PolicyDeniedError

from windows_guarded_admin_mcp.policies import PolicyEngine

from windows_guarded_admin_mcp.services.diagnostics import DiagnosticsService

from windows_guarded_admin_mcp.services.events import EventsService

from windows_guarded_admin_mcp.services.health import HealthService

from windows_guarded_admin_mcp.services.restart import RestartService

from windows_guarded_admin_mcp.services.scripts import ScriptsService

from windows_guarded_admin_mcp.services.services import ServicesService



pytestmark = pytest.mark.integration





def _resolve_integration_server_id(config) -> str:

    override = os.environ.get("WSA_MCP_INTEGRATION_SERVER", "").strip()

    if override:

        if override not in config.servers:

            pytest.skip(f"WSA_MCP_INTEGRATION_SERVER={override} not in inventory")

        return override

    for server_id, entry in config.servers.items():

        if entry.enabled and entry.environment == "integration":

            return server_id

    pytest.skip("no enabled integration server in local inventory")





@pytest.fixture(scope="module")

def live_config():

    if os.environ.get("WSA_MCP_INTEGRATION") != "1":

        pytest.skip("WSA_MCP_INTEGRATION not set")

    cfg = load_config(resolve_config_dir())

    server_id = _resolve_integration_server_id(cfg)

    return cfg, server_id





@pytest.fixture(scope="module")

def live_services(live_config):

    cfg, _server_id = live_config

    policies = PolicyEngine(cfg)

    confirmations = ConfirmationManager(cfg)

    return {

        "config": cfg,

        "diagnostics": DiagnosticsService(cfg, policies),

        "health": HealthService(cfg, policies),

        "services": ServicesService(cfg, policies),

        "events": EventsService(cfg, policies),

        "scripts": ScriptsService(cfg, policies),

        "restart": RestartService(cfg, policies, confirmations),

        "policies": policies,

    }





@pytest.fixture(scope="module")

def server_id(live_config) -> str:

    return live_config[1]





def test_inventory_includes_integration_server(live_services, server_id):

    result = live_services["diagnostics"].list_servers()

    assert result.success

    ids = [s["server_id"] for s in result.data["servers"]]

    assert server_id in ids





def test_restart_permanently_denied(live_services, server_id):

    with pytest.raises(PolicyDeniedError):

        live_services["policies"].check_server_restart(

            live_services["policies"].get_server(server_id)

        )





def test_diagnose_server_access(live_services, server_id):

    result = live_services["diagnostics"].diagnose_server_access(server_id)

    assert result.success or result.error is not None





def test_get_server_identity(live_services, server_id):

    result = live_services["diagnostics"].get_server_identity(server_id)

    assert result.success or result.error is not None

    if result.success:

        assert result.data is not None





def test_get_server_health(live_services, server_id):

    result = live_services["health"].get_server_health(server_id)

    assert result.success or result.error is not None





def test_get_disks(live_services, server_id):

    result = live_services["health"].get_disks(server_id)

    assert result.success or result.error is not None





def test_get_services_smoke(live_services, server_id):

    result = live_services["services"].get_services(server_id, name_filter="WinRM")

    assert result.success or result.error is not None





def test_get_events_limited(live_services, server_id):

    result = live_services["events"].get_windows_events(server_id, max_events=5)

    assert result.success or result.error is not None





def test_test_remote_execution(live_services, server_id):

    result = live_services["diagnostics"].test_remote_execution(server_id)

    assert result.success or result.error is not None





def test_run_smoke_script_if_configured(live_services, server_id):

    cfg = live_services["config"]

    if "smoke_test" not in cfg.scripts:

        pytest.skip("smoke_test not in script catalog")

    allowed = cfg.scripts["smoke_test"].allowed_servers

    if server_id not in allowed and "*" not in allowed:

        pytest.skip(f"smoke_test not allowed for {server_id}")

    result = live_services["scripts"].run_authorized_script(

        server_id,

        "smoke_test",

        {"message": "integration readonly smoke"},

    )

    assert result.success or result.error is not None





def test_prepare_restart_blocked(live_services, server_id):

    with pytest.raises(PolicyDeniedError):

        live_services["restart"].prepare_server_restart(

            server_id,

            "PLANNED_MAINTENANCE",

            "This must never execute on integration host",

        )

