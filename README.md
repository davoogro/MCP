# MCP servers

Colección de servidores [Model Context Protocol](https://modelcontextprotocol.io) para usar con Claude / Cursor.
Cada carpeta es un proyecto independiente con su propio `README.md`, dependencias y `.env.example`.

| Servidor | Lenguaje | Descripción |
|---|---|---|
| [mssql-mcp](mssql-mcp) | Python | SQL Server de solo lectura (multi-servidor) |
| [opt-mssql-mcp](opt-mssql-mcp) | Python | SQL Server lectura/escritura con túnel SSH automático |
| [fabric-mcp](fabric-mcp) | Python | Microsoft Fabric SQL (warehouses / SQL analytics endpoints) |
| [fabric-cli-mcp](fabric-cli-mcp) | Python | Administración de Fabric y Power BI vía Fabric CLI (`fab`) |
| [multi-postgres-mcp](multi-postgres-mcp) | TypeScript | Varias bases PostgreSQL, solo lectura |
| [multi-mariadb-mcp](multi-mariadb-mcp) | TypeScript | Varias bases MariaDB/MySQL, solo lectura |
| [oss-fm-oracle-mcp](oss-fm-oracle-mcp) | Python | Oracle de alarmas TelcoVendorB OSS FM, solo lectura |
| [nifi-mcp](nifi-mcp) | Python | Apache NiFi multi-servidor, solo lectura |
| [aws-data-mcp](aws-data-mcp) | Python | Athena, S3, Glue, Lambda, Step Functions, CloudWatch y QuickSight (escrituras con dry-run) |
| [redshift-mcp](redshift-mcp) | Python | Amazon Redshift vía Data API, solo lectura |
| [s3-mcp](s3-mcp) | Python | Exploración de buckets S3 + SQL con DuckDB |
| [unc-fs-mcp](unc-fs-mcp) | Python | Directorios UNC de Windows |
| [ssh-linux-mcp](ssh-linux-mcp) | Python | Servidores Linux por SSH (solo lectura, allowlist de comandos) |
| [ssh-winserver-mcp](ssh-winserver-mcp) | Python | Administración de Windows Server por SSH + PowerShell |
| [ssh-multiserver-mcp](ssh-multiserver-mcp) | Node | Un solo MCP para todos los alias de `~/.ssh/config` (Linux/Windows, read_only/managed) |
| [windows-fleet-mcp](windows-fleet-mcp) | Python | Flota Windows Server vía WinRM |
| [windows-server-admin-mcp](windows-server-admin-mcp) | Python | Administración controlada de Windows Server (allowlist, tokens de reinicio) |
| [windows-admin-mcp](windows-admin-mcp) | TypeScript | Administración de Windows Server vía PowerShell remoting |
| [wsl-mcp](wsl-mcp) | Python | Comandos y archivos dentro de WSL |
| [gitlab-mcp](gitlab-mcp) | Node | Wrapper local de `@zereight/mcp-gitlab` para GitLab self-hosted |
| [notion-mcp](notion-mcp) | Python | Notion vía integration token |
| [office365-mcp](office365-mcp) | Python | Lectura de correo Microsoft 365 (Graph, `Mail.Read`, caché DPAPI) |
| [legacy](legacy) | Python | Versiones antiguas de un solo archivo (Fabric, Postgres, WSL) |
| [clients](clients) | Python | Generador de config para Cursor, Kiro, Codex, Claude, VS Code |

## Configuración

Ningún archivo de este repo contiene credenciales ni servidores reales: los hosts, IPs y usuarios que aparecen
(`appsrv01`, `dbsrv01`, `sqlhost01`, `192.0.2.x`, ...) son ejemplos.

1. Copia `.env.example` a `.env` en la carpeta del servidor y rellena tus valores.
2. Copia los `config/*.example.*` a su nombre real (p. ej. `databases.yaml`) y edítalos.
3. Registra los servidores en tu cliente MCP con el generador (ver abajo).

Los `.env`, configs reales, logs y certificados están en `.gitignore`.

## Clientes: Cursor, Kiro, Codex, Claude Code, Claude Desktop, VS Code

[`clients/generate.py`](clients) genera la configuración de todos (o algunos) MCP para cada cliente con tus rutas,
y con `--install` la fusiona en tu config de usuario sin borrar lo que ya tienes (deja backup):

```powershell
python clients\generate.py --root C:\ruta\al\repo --python C:\ruta\python.exe --only mssql,s3 --client cursor,kiro,codex --install
```

Ejemplos listos para copiar en [`clients/examples/`](clients/examples). Detalles en [`clients/README.md`](clients/README.md).
