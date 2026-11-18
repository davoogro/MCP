#!/usr/bin/env sh
# Registra los MCP en Claude Code (scope user).

claude mcp remove sqlserver-readonly --scope user 2>/dev/null
claude mcp add-json sqlserver-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "sqlserver_readonly_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\sqlserver-readonly-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove sqlserver-readwrite --scope user 2>/dev/null
claude mcp add-json sqlserver-readwrite '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "sqlserver_readwrite_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\sqlserver-readwrite-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove fabric-sql --scope user 2>/dev/null
claude mcp add-json fabric-sql '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "fabric_sql_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\fabric-sql-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove fabric-admin --scope user 2>/dev/null
claude mcp add-json fabric-admin '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "fabric_admin_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\fabric-admin-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove oracle-alarms-readonly --scope user 2>/dev/null
claude mcp add-json oracle-alarms-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "oracle_alarms_readonly_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\oracle-alarms-readonly-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove nifi-readonly --scope user 2>/dev/null
claude mcp add-json nifi-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "nifi_readonly_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\nifi-readonly-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove s3-readonly --scope user 2>/dev/null
claude mcp add-json s3-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "s3_readonly_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\s3-readonly-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-fileshare --scope user 2>/dev/null
claude mcp add-json windows-fileshare '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_fileshare_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-fileshare-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove linux-ssh-readonly --scope user 2>/dev/null
claude mcp add-json linux-ssh-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "linux_ssh_readonly_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\linux-ssh-readonly-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-ssh-admin --scope user 2>/dev/null
claude mcp add-json windows-ssh-admin '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_ssh_admin_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-ssh-admin-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-winrm-fleet --scope user 2>/dev/null
claude mcp add-json windows-winrm-fleet '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_winrm_fleet_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-winrm-fleet-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-guarded-admin --scope user 2>/dev/null
claude mcp add-json windows-guarded-admin '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_guarded_admin_mcp.server"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-guarded-admin-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "WSA_MCP_CONFIG_DIR": "C:\\path\\to\\MCP\\windows-guarded-admin-mcp\\config"}}' --scope user
claude mcp remove wsl --scope user 2>/dev/null
claude mcp add-json wsl '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "wsl_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\wsl-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove postgres-readonly --scope user 2>/dev/null
claude mcp add-json postgres-readonly '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\postgres-readonly-mcp\\dist\\src\\index.js"], "env": {"PG_MCP_CONFIG": "C:\\path\\to\\MCP\\postgres-readonly-mcp\\config\\databases.yaml"}}' --scope user
claude mcp remove mariadb-readonly --scope user 2>/dev/null
claude mcp add-json mariadb-readonly '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\mariadb-readonly-mcp\\dist\\src\\index.js"], "env": {"MARIADB_MCP_CONFIG": "C:\\path\\to\\MCP\\mariadb-readonly-mcp\\config\\databases.yaml"}}' --scope user
claude mcp remove windows-psremoting --scope user 2>/dev/null
claude mcp add-json windows-psremoting '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\windows-psremoting-mcp\\dist\\src\\index.js"], "env": {"WINMCP_CONFIG": "C:\\path\\to\\MCP\\windows-psremoting-mcp\\config\\servers.json", "WINMCP_AUDIT_DIR": "C:\\path\\to\\MCP\\windows-psremoting-mcp\\logs\\audit"}}' --scope user
claude mcp remove gitlab-selfhosted --scope user 2>/dev/null
claude mcp add-json gitlab-selfhosted '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\gitlab-selfhosted-mcp\\scripts\\launch.mjs"], "env": {}}' --scope user
claude mcp remove athena-quicksight --scope user 2>/dev/null
claude mcp add-json athena-quicksight '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\athena-quicksight-mcp\\athena_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove redshift-readonly --scope user 2>/dev/null
claude mcp add-json redshift-readonly '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\redshift-readonly-mcp\\redshift_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove notion --scope user 2>/dev/null
claude mcp add-json notion '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\notion-mcp\\notion_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove office365 --scope user 2>/dev/null
claude mcp add-json office365 '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\office365-mcp\\office365_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove ssh-inventory --scope user 2>/dev/null
claude mcp add-json ssh-inventory '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\ssh-inventory-mcp\\index.js"], "env": {}}' --scope user

claude mcp list
