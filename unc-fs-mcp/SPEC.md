# unc-fs-mcp — especificación

Servidor MCP (stdio) para acceder a directorios de red UNC de Windows (ej.
`\\dbsrv02\OneDrive - Company\execution\test`): listar, ver pesos/metadata, buscar,
leer contenido y (solo si se habilita) escribir.

## Restricciones generales

- Python >= 3.10 (el intérprete real es `C:\DVB\python\python.exe`, 3.14). Paquete `unc_fs_mcp` en
  `C:\DVB\mcp\unc-fs-mcp\src\unc_fs_mcp`, MISMO layout que `C:\DVB\mcp\s3-mcp`: `pyproject.toml` (hatchling),
  `__init__.py` (`__version__ = "1.0.0"`), `__main__.py`, `config.py`, `ops.py`, `util.py`, `server.py`,
  `tests/`, `README.md`, `.env.example`, `.gitignore`, `claude-mcp.example.json`.
  Lee `C:\DVB\mcp\s3-mcp\src\s3_mcp\server.py`, `config.py` y `util.py` y copia su estilo (CLI `--list`,
  `--test`, `--handshake`, `bound_response`, carga de .env).
- SDK `mcp` 2.0: `from mcp.server import MCPServer` y `from mcp.types import ToolAnnotations`
  (NO existe `mcp.server.fastmcp`). Tools de lectura con `ToolAnnotations(readOnlyHint=True, openWorldHint=True)`.
- Dependencias: `mcp>=2.0.0`, `python-dotenv>=1.0`. Solo stdlib para el resto (`pathlib`, `os`, `fnmatch`,
  `shutil`, `concurrent.futures`). Nada de pywin32.
- Logging solo a stderr. Mensajes, docstrings e `instructions` en español.
- Cada tool devuelve `dict` JSON-serializable. Éxito `{"ok": true, ...}`; error
  `{"ok": false, "error": "...", "error_code": "NotFound|AccessDenied|NotAllowed|Timeout|InvalidPath|IOError"}`.
  Nunca propagar excepciones ni stacktraces.
- Fechas ISO-8601 con zona local. Tamaños: `size` (int bytes) + `size_human` (base 1024, ej. `"12.4 MB"`).
- Respuesta global acotada por `UNC_MAX_RESPONSE_CHARS` (default 80000) → `"response_truncated": true`.
- **Timeouts**: los shares de red pueden colgarse. TODA operación de filesystem se ejecuta en un
  `ThreadPoolExecutor` con timeout (`UNC_OP_TIMEOUT`, default 30 s; los escaneos `UNC_SCAN_TIMEOUT`, default 60 s).
  Si vence: `error_code: "Timeout"` (o resultados parciales con `scan_truncated: true` en escaneos).
  El servidor nunca debe quedarse bloqueado.

## Configuración (`config.py`)

Carga `unc-fs-mcp/.env` propio, luego el compartido `C:\DVB\mcp\.env` (`override=False`), y `UNC_MCP_ENV_FILE`
si existe. Rutas relativas al archivo, no al cwd.

| Variable | Uso | Default |
|---|---|---|
| `UNC_ROOTS` | alias de raíces: `alias=\\server\share\ruta;alias2=\\otro\share` (separador `;`) | `share01=\\dbsrv02\OneDrive - Company` |
| `UNC_DEFAULT_ROOT` | alias por defecto | primer alias |
| `UNC_ALLOWLIST_ONLY` | `true` ⇒ solo rutas dentro de alguna raíz de `UNC_ROOTS` | `false` |
| `UNC_ALLOW_WRITE` | `true` ⇒ registra las tools de escritura | `false` |
| `UNC_ALLOW_LOCAL` | `true` ⇒ acepta también rutas locales `C:\...` | `false` |
| `UNC_MAX_READ_BYTES` | máximo por lectura | 2097152 |
| `UNC_MAX_SCAN_ENTRIES` | máximo de entradas recorridas en escaneos | 50000 |
| `UNC_MAX_WRITE_BYTES` | máximo por escritura | 10485760 |
| `UNC_REDACT_SECRETS` | redactar secretos al leer (ver abajo) | `true` |
| `UNC_OP_TIMEOUT` / `UNC_SCAN_TIMEOUT` / `UNC_MAX_RESPONSE_CHARS` | ver arriba | 30 / 60 / 80000 |

Crea `C:\DVB\mcp\unc-fs-mcp\.env` con los defaults de arriba (es config, no secretos) y `.env.example` igual.

## Resolución de rutas (`util.resolve_path(path, root=None)`) — crítico, con tests

Acepta:
1. UNC absoluta `\\server\share\...` (también con `/` → normalizar a `\`).
2. `alias:ruta/relativa` o `alias:` (ej. `share01:execution\test`).
3. Ruta relativa → relativa a `root` (alias) o a `UNC_DEFAULT_ROOT`.
4. Local `C:\...` solo si `UNC_ALLOW_LOCAL`; si no → `NotAllowed`.

Reglas: normalizar con `os.path.normpath`; rechazar `InvalidPath` si tras normalizar una ruta relativa/alias
escapa de su raíz (`..`). Con `UNC_ALLOWLIST_ONLY`, rechazar (`NotAllowed`) lo que no esté bajo alguna raíz
(comparación case-insensitive, por componentes, no por prefijo de string: `\\s\share2` NO está bajo `\\s\share`).
Rechazar rutas de dispositivo `\\?\`, `\\.\`. En la respuesta devolver siempre `path` (UNC completa) y,
si aplica, `display` (`alias:relativa`).

## Redacción de secretos

Si `UNC_REDACT_SECRETS`: en `read_file`, `tail_file` y `grep_text`, para archivos cuyo nombre empiece por
`.env` o termine en `.env`, `.ini`, `.cfg`, `.conf`, `.properties`, `.yaml`, `.yml`, `.json`, reemplazar el valor
de líneas `CLAVE=valor` / `clave: valor` / `"clave": "valor"` cuya clave contenga (case-insensitive)
`pass`, `pwd`, `secret`, `token`, `key`, `credential` por `***`. Añadir `"redacted": true` si hubo cambios.

## Tools

Lectura (siempre):

1. `list_roots()` → alias, ruta, `reachable` (bool, probado con timeout corto 5 s en paralelo), default, flags
   (`allow_write`, `allowlist_only`).
2. `test_access(path=None)` → existe, es dir/archivo, puede listar, puede escribir (solo comprobar con
   `os.access`, sin crear nada), tiempo de respuesta ms, usuario actual (`USERDOMAIN\USERNAME`).
3. `list_directory(path=None, pattern="*", include_hidden=False, sort="name|size|modified", desc=False, limit=500)`
   → entradas `{name, type: file|dir|link, size, size_human, modified}` (`os.scandir`), `total`, `returned`,
   `truncated`, totales de archivos/dirs y bytes de los archivos listados.
4. `file_info(path)` → tipo, size, creado/modificado/accedido, readonly, hidden (atributo Windows vía
   `st_file_attributes`), extensión, y para texto: encoding detectado y nº de líneas si size <= max_read.
5. `read_file(path, offset=1, limit=500, encoding=None)` → líneas `offset..offset+limit-1`, `total_lines`
   (si se conoce), `truncated`. Detección de encoding: BOM → utf-8 → cp1252 (latin-1 de último recurso).
   Binarios (NUL en los primeros 8 KB) → `error_code: "Binary"` salvo `as_hex=True` (primeros 512 bytes en hex).
   Respetar `UNC_MAX_READ_BYTES`.
6. `tail_file(path, lines=100)` → últimas N líneas leyendo desde el final por bloques (logs grandes).
7. `find_files(path=None, pattern="*", recursive=True, max_depth=10, type="file|dir|any", min_size=None,
   max_size=None, modified_after=None, modified_before=None, limit=200)` → coincidencias con ruta relativa a
   `path`, size, modified. Recorrido con `os.scandir` iterativo, respetando `UNC_MAX_SCAN_ENTRIES` y
   `UNC_SCAN_TIMEOUT` (`scan_truncated`). `pattern` es glob sobre el nombre, case-insensitive.
   Fechas aceptan `YYYY-MM-DD` o ISO.
8. `grep_text(path, pattern, regex=False, ignore_case=True, file_pattern="*", recursive=True, context=0,
   max_matches=200)` → `{file, line_no, line, before[], after[]}`. Salta binarios y archivos > max_read.
   Si `path` es archivo, busca solo en él.
9. `dir_stats(path=None, max_depth=None, top=20)` → total archivos, dirs, bytes, desglose por extensión
   (count, bytes), top-N archivos más pesados, top-N subdirectorios directos por peso, más reciente/antiguo.
   Mismos límites de escaneo.

Escritura (registrar SOLO si `UNC_ALLOW_WRITE=true`; annotations `readOnlyHint=False`,
`destructiveHint` según corresponda):

10. `write_file(path, content, mode="create|overwrite|append", encoding="utf-8")` — `create` falla si existe.
11. `make_directory(path, parents=True)`.
12. `copy_file(src, dst, overwrite=False)` — `src`/`dst` resueltos con las mismas reglas (pueden ser local si
    `UNC_ALLOW_LOCAL`). No se implementa borrado ni movimiento.

## server.py

`instructions` dinámicas (como s3-mcp): raíces configuradas, alias por defecto, si la escritura está habilitada,
formatos de ruta aceptados, flujo recomendado (`list_roots → list_directory → dir_stats/find_files →
file_info → read_file/tail_file/grep_text`), límites. CLI: `--list` (config), `--test` (test_access de cada
raíz), `--handshake` (cliente in-process, lista tools, igual que s3-mcp).

## Tests (`tests/`, pytest, sin red)

Usar `tmp_path` y monkeypatch de config con raíces locales (`UNC_ALLOW_LOCAL=true`). Cubrir: resolución de
rutas (alias, relativa, `..` que escapa, allowlist por componentes, `\\?\` rechazado, `/` normalizado),
list_directory orden/limit, read_file offset/limit + encodings (utf-8, utf-8-sig, cp1252) + binario,
tail_file, find_files filtros, grep_text con contexto, dir_stats, redacción de `.env`, write tools no
registradas por defecto y funcionales cuando se habilitan, timeout (monkeypatch de una op que duerme).

## Verificación que debes ejecutar al terminar

```
cd C:\DVB\mcp\unc-fs-mcp
C:\DVB\python\python.exe -m pytest -q
C:\DVB\python\python.exe -m ruff check src tests
$env:PYTHONPATH="src"; C:\DVB\python\python.exe -m unc_fs_mcp --handshake
```
Si `pytest`/`ruff` no están instalados, instálalos con `C:\DVB\python\python.exe -m pip install pytest ruff`.
NO registres el servidor en ningún config (lo hago yo). Al terminar escribe un resumen corto en `DONE.md`.
