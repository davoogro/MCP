# Configuración para clientes MCP

`generate.py` crea la configuración de todos los MCP del repo (o de los que elijas) para:

| Cliente | Archivo generado | Dónde va (scope usuario) | Scope proyecto |
|---|---|---|---|
| **Cursor** | `cursor/mcp.json` | `~/.cursor/mcp.json` | `<proyecto>/.cursor/mcp.json` |
| **Kiro** | `kiro/mcp.json` | `~/.kiro/settings/mcp.json` | `<proyecto>/.kiro/settings/mcp.json` |
| **Codex** (CLI / IDE) | `codex/config.toml` | `~/.codex/config.toml` (o `$CODEX_HOME`) | — |
| **Claude Code** | `claude/.mcp.json`, `claude/claude-add.ps1`, `claude/claude-add.sh` | `claude mcp add-json … --scope user` | `<proyecto>/.mcp.json` |
| **Claude Desktop** | `claude-desktop/claude_desktop_config.json` | `%APPDATA%\Claude\` · `~/Library/Application Support/Claude/` | — |
| **VS Code** (Copilot) | `vscode/mcp.json` | `%APPDATA%\Code\User\mcp.json` | `<proyecto>/.vscode/mcp.json` |

En [`examples/`](examples) están ya generados con rutas de ejemplo (`C:\path\to\MCP`) por si solo quieres copiar y pegar.

## Uso

Solo necesita Python 3.9+ (librería estándar).

```powershell
# Ver los servidores disponibles y cómo se construyen
python clients\generate.py --list

# Generar todo en clients\out\ con tus rutas
python clients\generate.py --root C:\DVB\mcp --python C:\DVB\python\python.exe

# Solo algunos servidores y clientes
python clients\generate.py --only sqlserver-readonly,s3-readonly,wsl --client cursor,kiro,codex

# Fusionar directamente en la config de usuario de cada cliente
python clients\generate.py --only sqlserver-readonly,s3-readonly --client cursor,kiro,codex,vscode,claude-desktop --install
```

| Opción | Default | |
|---|---|---|
| `--root` | carpeta de este repo | Dónde están las carpetas `*-mcp` |
| `--python` | el Python que ejecuta el script | Python con las dependencias de los MCP instaladas |
| `--node` | `node` | Node.js para los MCP en TypeScript |
| `--only` / `--exclude` | todos | Lista separada por comas (nombres de `--list`) |
| `--client` | todos | `cursor,kiro,codex,claude,claude-desktop,vscode` |
| `--out` | `clients/out` (ignorado por git) | Carpeta de salida |
| `--install` | no | Fusiona en la config de usuario |

`--install` **no borra** los servidores que ya tengas: solo agrega o reemplaza los del mismo nombre, y antes
deja un backup `*.bak-<fecha>` del archivo original. Para Claude Code no edita archivos: ejecuta el
`claude-add.ps1` / `claude-add.sh` generado (usa `claude mcp add-json`).

## Notas por cliente

- **Kiro**: cada servidor sale con `"disabled": false` y `"autoApprove": []`. Agrega nombres de tools a
  `autoApprove` solo para las de solo lectura que no quieras confirmar cada vez.
- **Codex**: cada servidor lleva `startup_timeout_sec = 30` y `tool_timeout_sec = 300` (las consultas SQL
  y los comandos remotos pueden tardar más que los defaults). Las variables van en `[mcp_servers.<nombre>.env]`.
- **Cursor / VS Code / Claude**: formato estándar `command` + `args` + `env` por stdio.

## Credenciales

La config del cliente **no lleva secretos**. Cada MCP lee su `.env` y sus `config/*.yaml` desde su propia
carpeta (copia los `.env.example` / `*.example.*`). Así puedes compartir o versionar la config del cliente
sin exponer contraseñas.

## Antes de usarlos

- **Python**: instala las dependencias de cada MCP (`pip install -e <carpeta>`) en el Python que pases en `--python`.
- **Node** (`postgres-readonly`, `mariadb-readonly`, `windows-psremoting`): `npm install && npm run build` en su carpeta.
- **gitlab**: `npm install` en `gitlab-selfhosted-mcp`.

## Agregar un MCP nuevo

Añade una entrada en [`servers.json`](servers.json) (`python_module` o `node_script`, y `env` si hace falta;
`{dir}` = carpeta del MCP, `{sep}` = separador de ruta) y vuelve a generar.
