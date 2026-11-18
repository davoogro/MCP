# Registra los MCP en Claude Code (scope user). Ejecutar en PowerShell.

claude mcp remove mssql --scope user 2>$null
claude mcp add-json mssql '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "mssql_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\mssql-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove opt-mssql --scope user 2>$null
claude mcp add-json opt-mssql '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "opt_mssql_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\opt-mssql-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove fabric --scope user 2>$null
claude mcp add-json fabric '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "fabric_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\fabric-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove fabric-cli --scope user 2>$null
claude mcp add-json fabric-cli '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "fabric_cli_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\fabric-cli-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove oss-fm-oracle --scope user 2>$null
claude mcp add-json oss-fm-oracle '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "oss_fm_oracle_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\oss-fm-oracle-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove nifi --scope user 2>$null
claude mcp add-json nifi '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "nifi_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\nifi-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove s3 --scope user 2>$null
claude mcp add-json s3 '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "s3_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\s3-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove unc-fs --scope user 2>$null
claude mcp add-json unc-fs '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "unc_fs_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\unc-fs-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove ssh-linux --scope user 2>$null
claude mcp add-json ssh-linux '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "ssh_linux_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\ssh-linux-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove ssh-winserver --scope user 2>$null
claude mcp add-json ssh-winserver '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "ssh_winserver_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\ssh-winserver-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-fleet --scope user 2>$null
claude mcp add-json windows-fleet '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_fleet_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-fleet-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove windows-server-admin --scope user 2>$null
claude mcp add-json windows-server-admin '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "windows_server_admin_mcp.server"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\windows-server-admin-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "WSA_MCP_CONFIG_DIR": "C:\\path\\to\\MCP\\windows-server-admin-mcp\\config"}}' --scope user
claude mcp remove wsl --scope user 2>$null
claude mcp add-json wsl '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["-m", "wsl_mcp"], "env": {"PYTHONPATH": "C:\\path\\to\\MCP\\wsl-mcp\\src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove multi-postgres --scope user 2>$null
claude mcp add-json multi-postgres '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\multi-postgres-mcp\\dist\\src\\index.js"], "env": {"PG_MCP_CONFIG": "C:\\path\\to\\MCP\\multi-postgres-mcp\\config\\databases.yaml"}}' --scope user
claude mcp remove multi-mariadb --scope user 2>$null
claude mcp add-json multi-mariadb '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\multi-mariadb-mcp\\dist\\src\\index.js"], "env": {"MARIADB_MCP_CONFIG": "C:\\path\\to\\MCP\\multi-mariadb-mcp\\config\\databases.yaml"}}' --scope user
claude mcp remove windows-admin --scope user 2>$null
claude mcp add-json windows-admin '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\windows-admin-mcp\\dist\\src\\index.js"], "env": {"WINMCP_CONFIG": "C:\\path\\to\\MCP\\windows-admin-mcp\\config\\servers.json", "WINMCP_AUDIT_DIR": "C:\\path\\to\\MCP\\windows-admin-mcp\\logs\\audit"}}' --scope user
claude mcp remove gitlab --scope user 2>$null
claude mcp add-json gitlab '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\gitlab-mcp\\scripts\\launch.mjs"], "env": {}}' --scope user
claude mcp remove aws-data --scope user 2>$null
claude mcp add-json aws-data '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\aws-data-mcp\\athena_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove redshift --scope user 2>$null
claude mcp add-json redshift '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\redshift-mcp\\redshift_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove notion --scope user 2>$null
claude mcp add-json notion '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\notion-mcp\\notion_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove office365 --scope user 2>$null
claude mcp add-json office365 '{"type": "stdio", "command": "C:\\path\\to\\python.exe", "args": ["C:\\path\\to\\MCP\\office365-mcp\\office365_mcp_server.py"], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}' --scope user
claude mcp remove ssh-multiserver --scope user 2>$null
claude mcp add-json ssh-multiserver '{"type": "stdio", "command": "node", "args": ["C:\\path\\to\\MCP\\ssh-multiserver-mcp\\index.js"], "env": {}}' --scope user

claude mcp list
