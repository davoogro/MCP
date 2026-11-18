# unc-fs-mcp

Servidor MCP (stdio) para directorios UNC de Windows: listar carpetas, metadata,
búsqueda y lectura de archivos. La escritura es opcional (`UNC_ALLOW_WRITE`).

## Instalación

```bash
C:\DVB\python\python.exe -m pip install -e "C:\DVB\mcp\unc-fs-mcp[dev]"
```

Sin instalar, con `PYTHONPATH`:

```bash
set PYTHONPATH=C:\DVB\mcp\unc-fs-mcp\src
C:\DVB\python\python.exe -m unc_fs_mcp --handshake
```

## Configuración

Carga `unc-fs-mcp\.env` y, sin pisar variables ya definidas, `C:\DVB\mcp\.env`.
`UNC_MCP_ENV_FILE` apunta a otro archivo. Ver [.env.example](.env.example).

| Variable | Uso | Default |
|---|---|---|
| `UNC_ROOTS` | `alias=\\server\share;...` | `share01=\\dbsrv02\OneDrive - Company` |
| `UNC_DEFAULT_ROOT` | Alias por defecto | primer alias |
| `UNC_ALLOWLIST_ONLY` | Solo rutas bajo `UNC_ROOTS` | `false` |
| `UNC_ALLOW_ANY_UNC` | Cualquier ruta `\\server\share\...` sin estar en raíces | `true` |
| `UNC_ALLOW_WRITE` | Registra tools de escritura | `false` |
| `UNC_ALLOW_LOCAL` | Acepta `C:\...` | `false` |
| `UNC_MAX_READ_BYTES` | Tope de bytes leídos/devueltos por operación | `10485760` |
| `UNC_MAX_SCAN_ENTRIES` | Entradas en escaneos | `50000` |
| `UNC_MAX_WRITE_BYTES` | Tope de escritura | `10485760` |
| `UNC_REDACT_SECRETS` | Redactar claves sensibles al leer | `true` |
| `UNC_OP_TIMEOUT` / `UNC_SCAN_TIMEOUT` / `UNC_MAX_RESPONSE_CHARS` | Timeouts y tamaño JSON | `30` / `60` / `80000` |

Rutas: UNC `\\servidor\recurso\...`, `alias:ruta` o relativa al alias por defecto.

## Tools

Lectura (13): `list_roots`, `test_access`, `list_directory`, `file_info`, `read_file`,
`tail_file`, `find_files`, `grep_text`, `dir_stats`, `add_root`, `read_document`,
`compare_dirs`, `recent_files`.

Escritura (4, si `UNC_ALLOW_WRITE=true`): `write_file`, `make_directory`, `copy_file`, `move_file`.

Flujo: `list_roots` → `list_directory` → `dir_stats` / `find_files` / `recent_files` →
`file_info` → `read_file` / `tail_file` / `grep_text` / `read_document`; `compare_dirs` para diffs.

## CLI

```bash
C:\DVB\python\python.exe -m unc_fs_mcp --list
C:\DVB\python\python.exe -m unc_fs_mcp --test
C:\DVB\python\python.exe -m unc_fs_mcp --handshake
```

## Desarrollo

```bash
cd C:\DVB\mcp\unc-fs-mcp
C:\DVB\python\python.exe -m pytest -q
C:\DVB\python\python.exe -m ruff check src tests
```
