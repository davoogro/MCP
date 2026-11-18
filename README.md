# MCP servers

Colección de servidores [Model Context Protocol](https://modelcontextprotocol.io) para Cursor, Kiro, Codex,
Claude Code, Claude Desktop y VS Code. Cada carpeta es un proyecto independiente con su propio `README.md`,
dependencias y `.env.example`.

**Convención de nombres:** `<tecnología>-<función o acceso>-mcp`. El nombre dice contra qué se conecta y
qué tan peligroso es: `-readonly` no puede modificar nada; `-readwrite` / `-admin` sí (y pide confirmación
para lo destructivo). En el cliente MCP se registran sin el sufijo `-mcp` (p. ej. `sqlserver-readonly`).

Acceso: 🟢 solo lectura · 🟡 lectura y escritura · 🔴 administración (cambios en servidores)

### Bases de datos y analítica

| MCP | Acceso | Lenguaje | Qué hace |
|---|---|---|---|
| [sqlserver-readonly-mcp](sqlserver-readonly-mcp) | 🟢 | Python | SQL Server multi-servidor: explorar esquemas, consultar, exportar a CSV/Parquet/S3 |
| [sqlserver-readwrite-mcp](sqlserver-readwrite-mcp) | 🟡 | Python | SQL Server con escritura transaccional, dry-run y túnel SSH automático |
| [postgres-readonly-mcp](postgres-readonly-mcp) | 🟢 | TypeScript | Varias bases PostgreSQL |
| [mariadb-readonly-mcp](mariadb-readonly-mcp) | 🟢 | TypeScript | Varias bases MariaDB / MySQL |
| [oracle-alarms-readonly-mcp](oracle-alarms-readonly-mcp) | 🟢 | Python | Oracle de alarmas de red (OSS FM de un vendor telco), varios collectors en paralelo |
| [fabric-sql-mcp](fabric-sql-mcp) | 🟢 | Python | Microsoft Fabric Data Warehouse / SQL analytics endpoints (T-SQL) |
| [redshift-readonly-mcp](redshift-readonly-mcp) | 🟢 | Python | Amazon Redshift vía Data API |
| [athena-quicksight-mcp](athena-quicksight-mcp) | 🟡 | Python | Athena, S3, Glue, Lambda, Step Functions, CloudWatch y QuickSight (escrituras con dry-run) |

### Datos, archivos y pipelines

| MCP | Acceso | Lenguaje | Qué hace |
|---|---|---|---|
| [s3-readonly-mcp](s3-readonly-mcp) | 🟢 | Python | Buckets S3: listar, pesos, previsualizar y SQL sobre Parquet/CSV con DuckDB |
| [windows-fileshare-mcp](windows-fileshare-mcp) | 🟢 | Python | Carpetas compartidas de Windows (rutas UNC): buscar, leer, comparar |
| [nifi-readonly-mcp](nifi-readonly-mcp) | 🟢 | Python | Apache NiFi multi-servidor: flujos, colas, provenance |

### Servidores Linux y Windows

| MCP | Acceso | Lenguaje | Qué hace |
|---|---|---|---|
| [linux-ssh-readonly-mcp](linux-ssh-readonly-mcp) | 🟢 | Python | Diagnóstico de Linux por SSH con allowlist de comandos |
| [ssh-inventory-mcp](ssh-inventory-mcp) | 🟡 | Node | Un solo MCP para todos los hosts de `~/.ssh/config` (Linux/Windows, por etiquetas) |
| [windows-ssh-admin-mcp](windows-ssh-admin-mcp) | 🔴 | Python | Administración completa de Windows Server por SSH + PowerShell |
| [windows-winrm-fleet-mcp](windows-winrm-fleet-mcp) | 🔴 | Python | Flota de Windows Server por WinRM, permisos opt-in por servidor |
| [windows-guarded-admin-mcp](windows-guarded-admin-mcp) | 🔴 | Python | Administración de producción con allowlist, scripts firmados y reinicio en 2 pasos |
| [windows-psremoting-mcp](windows-psremoting-mcp) | 🔴 | TypeScript | Administración de Windows Server vía PowerShell remoting |
| [wsl-mcp](wsl-mcp) | 🟡 | Python | Comandos, archivos y jobs dentro de WSL |

### Plataformas y productividad

| MCP | Acceso | Lenguaje | Qué hace |
|---|---|---|---|
| [fabric-admin-mcp](fabric-admin-mcp) | 🔴 | Python | Administración de Microsoft Fabric y Power BI con Fabric CLI (`fab`) |
| [gitlab-selfhosted-mcp](gitlab-selfhosted-mcp) | 🟡 | Node | GitLab self-hosted (wrapper de `@zereight/mcp-gitlab`, CA corporativa, SAML) |
| [notion-mcp](notion-mcp) | 🟡 | Python | Notion vía integration token |
| [office365-mcp](office365-mcp) | 🟢 | Python | Correo de Microsoft 365 (Graph `Mail.Read`) |

### Otros

| Carpeta | Qué es |
|---|---|
| [clients](clients) | Generador de configuración para Cursor, Kiro, Codex, Claude Code, Claude Desktop y VS Code |
| [legacy](legacy) | Versiones antiguas de un solo archivo (Fabric, Postgres, WSL) |

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
python clients\generate.py --list
python clients\generate.py --root C:\ruta\al\repo --python C:\ruta\python.exe --only sqlserver-readonly,s3-readonly --client cursor,kiro,codex --install
```

Ejemplos listos para copiar en [`clients/examples/`](clients/examples). Detalles en [`clients/README.md`](clients/README.md).
