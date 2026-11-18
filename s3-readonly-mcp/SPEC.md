# s3-readonly-mcp — especificación

Servidor MCP (stdio) **de solo lectura** para explorar buckets de Amazon S3: listar archivos,
revisar pesos, metadata y leer/previsualizar contenido para responder preguntas sobre los datos.

## Restricciones generales

- Python ≥ 3.10, paquete `s3_readonly_mcp` en `C:\DVB\mcp\s3-readonly-mcp\src\s3_readonly_mcp`, mismo layout que
  `C:\DVB\mcp\fabric-sql-mcp` (pyproject con hatchling, `__main__.py`, `config.py`, `server.py`,
  tests en `tests/`). Leer `fabric-sql-mcp/src/fabric_sql_mcp/server.py` y `config.py` como referencia de estilo.
- SDK `mcp` 2.0: `from mcp.server import MCPServer` y `from mcp.types import ToolAnnotations`
  (NO existe `mcp.server.fastmcp`). Todas las tools con `ToolAnnotations(readOnlyHint=True, openWorldHint=True)`.
- Dependencias: `mcp>=2.0.0`, `boto3>=1.34`, `python-dotenv>=1.0`. Opcional: `pyarrow` (parquet) —
  importarlo de forma perezosa; si no está, la tool lo indica sin romperse.
- **Nunca** llamar operaciones que escriban/borren (Put*, Delete*, Copy*, Restore*, etc.).
  Ninguna tool genera URLs prefirmadas.
- Logging solo a stderr. Mensajes, docstrings e `instructions` del servidor en español.
- Cada tool devuelve un `dict` JSON-serializable. Errores: `{"ok": false, "error": "...", "error_code": "AccessDenied", ...}`
  — capturar `botocore.exceptions.ClientError`/`BotoCoreError`, nunca propagar excepciones ni stacktraces.
  Éxito: `{"ok": true, ...}`.
- Nunca exponer secretos (access/secret key) en respuestas ni logs; como mucho el access key enmascarado (`AKIA…XXXX`).
- Fechas en ISO-8601 UTC. Tamaños siempre como `size` (bytes, int) + `size_human` (ej. `"12.4 MB"`, base 1024).
- Respuestas acotadas: límite global `S3_MAX_RESPONSE_CHARS` (default 80000); si se excede, recortar listas y
  marcar `"response_truncated": true`.

## Configuración (`config.py`)

Carga `.env` propio (`s3-readonly-mcp/.env`) y luego el compartido `C:\DVB\mcp\.env` (`override=False`),
más `S3_MCP_ENV_FILE` si está definido (igual que `fabric_sql_mcp.config.load_env_files`).

| Variable | Uso | Default |
|---|---|---|
| `access_key` / `secret_key` | credenciales (nombres que ya existen en el `.env` compartido) | — |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | alternativa estándar (precedencia: S3_* > minúsculas > AWS_*) | — |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | alternativa explícita | — |
| `S3_REGION` | región por defecto | `us-east-1` |
| `S3_ENDPOINT_URL` | endpoint compatible (MinIO, etc.) | vacío |
| `S3_BUCKETS` | lista separada por comas de buckets conocidos. Necesaria porque el usuario IAM puede no tener `s3:ListAllMyBuckets` | vacío |
| `S3_BUCKET_ALLOWLIST_ONLY` | `true` ⇒ rechazar buckets que no estén en `S3_BUCKETS` | `false` |
| `S3_DEFAULT_BUCKET` | bucket por defecto cuando la tool no recibe `bucket` | primero de `S3_BUCKETS` |
| `S3_MAX_SCAN_OBJECTS` | máximo de objetos que recorre una tool de escaneo (stats/búsqueda) | `200000` |
| `S3_MAX_READ_BYTES` | máximo de bytes que lee `read_object`/`preview_table` | `5242880` (5 MB) |
| `S3_MAX_RESPONSE_CHARS` | ver arriba | `80000` |

Clientes boto3 por región, cacheados. Región por bucket autodetectada: `head_bucket` y leer el header
`x-amz-bucket-region` (también presente en el error 301/403), fallback `get_bucket_location`, cachear el resultado.
`botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=60)`.

`bucket` acepta también URIs `s3://bucket/prefix` en cualquier parámetro `bucket`/`key`/`prefix`
(helper `parse_s3_uri`), para que el usuario pueda pegar rutas.

## Tools

1. **`test_connection()`** — `sts.get_caller_identity` (ARN, account), access key enmascarado, región/endpoint,
   y por cada bucket de `S3_BUCKETS`: accesible sí/no, región, error si lo hay. Intenta `list_buckets` e informa si está permitido.
2. **`list_buckets()`** — une `list_buckets` (si hay permiso) con `S3_BUCKETS`; por bucket: nombre, origen
   (`configured`/`account`), fecha creación si se conoce, región (lazy, opcional con `include_region: bool=False`).
3. **`get_bucket_info(bucket)`** — región, versioning, encryption, lifecycle (reglas resumidas), tagging,
   public access block, policy status, logging, object lock. Cada sub-llamada tolerante: si da AccessDenied
   o NoSuch*, poner `null` + entrada en `"unavailable": {"lifecycle": "AccessDenied"}`.
4. **`list_objects(bucket=None, prefix="", delimiter="/", max_keys=200, continuation_token=None, start_after=None)`** —
   navegación tipo carpeta con `list_objects_v2`. Devuelve `folders` (common prefixes), `files`
   (`key`, `name` relativo al prefix, `size`, `size_human`, `last_modified`, `storage_class`, `etag`),
   `is_truncated`, `next_continuation_token`, totales de la página (`files_count`, `page_size_total` + human).
   `delimiter=""` ⇒ listado plano recursivo. `max_keys` ≤ 1000.
5. **`get_object_metadata(bucket, key, version_id=None, include_tags=True)`** — `head_object`: size, content_type,
   content_encoding, content_disposition, cache_control, etag, last_modified, storage_class, version_id,
   server_side_encryption, kms key id, `metadata` de usuario (x-amz-meta-*), restore status, archive status,
   object lock, parts_count, checksum; + `get_object_tagging` tolerante. Si la key no existe y parece carpeta,
   sugerir `list_objects` con ese prefix.
6. **`prefix_stats(bucket=None, prefix="", group_by_depth=1, top_n=10, max_objects=None)`** — recorre recursivamente
   con paginador hasta `max_objects` (default `S3_MAX_SCAN_OBJECTS`): total objetos, total bytes (+human),
   promedio, min/max, oldest/newest (`last_modified` con key), desglose por extensión (count+bytes, top 20),
   por storage class, por subcarpeta al nivel `group_by_depth` relativo al prefix (count+bytes, ordenado por bytes, top 50),
   `largest` (top_n objetos), `newest` (top_n), objetos de 0 bytes (count). `scan_truncated: true` si se alcanzó el límite.
   Usar `heapq` para tops; no guardar todos los objetos en memoria.
7. **`find_objects(bucket=None, prefix="", pattern=None, regex=None, extensions=None, min_size=None, max_size=None,
   modified_after=None, modified_before=None, storage_class=None, sort_by="key", descending=False, limit=100, max_objects=None)`** —
   búsqueda por filtros sobre un escaneo recursivo. `pattern` = glob (fnmatch, case-insensitive) sobre el nombre o la key completa
   (si contiene `/`). `regex` = `re.search` sobre la key. `extensions` lista (`["csv","parquet"]`, sin punto, case-insensitive,
   soporta dobles `csv.gz`). Tamaños aceptan int bytes o strings tipo `"10MB"`, `"1.5 GB"`. Fechas ISO (`2026-01-31` o con hora; sin tz = UTC).
   `sort_by` ∈ key|size|last_modified. Devuelve `matches` (hasta `limit`), `matched_total`, `scanned`, `scan_truncated`,
   `matched_total_bytes` (+human).
8. **`read_object(bucket, key, offset=0, max_bytes=65536, encoding="utf-8", decompress=True, version_id=None)`** —
   GET con `Range`. Si la key termina en `.gz` y `decompress`, descargar hasta `S3_MAX_READ_BYTES` y descomprimir
   (gzip streaming, cortar en `max_bytes` del texto). Detectar binario (bytes nulos / proporción no imprimible) ⇒ no devolver
   el contenido, devolver `is_binary: true`, `content_type`, primeros 64 bytes en hex. Devuelve `content`, `bytes_returned`,
   `object_size`, `truncated`, `next_offset` (para paginar), `content_type`. `max_bytes` acotado a `S3_MAX_READ_BYTES`.
9. **`preview_table(bucket, key, rows=20, format=None, delimiter=None, columns=None)`** — previsualiza datos tabulares
   para poder responder preguntas: formatos `csv`, `tsv`, `jsonl`/`ndjson`, `json` (array de objetos o objeto),
   `parquet` (requiere pyarrow; si falta, devolver error claro con `pip install pyarrow`), soportando `.gz` para los de texto.
   Formato inferido por extensión si `format` es None; delimitador inferido con `csv.Sniffer`.
   Devuelve `format`, `columns` (nombre + tipo inferido simple: int/float/bool/date/string), `rows` (lista de dicts, valores
   largos recortados a 500 chars), `rows_returned`, y para parquet `num_rows` total, `num_row_groups`, schema completo
   y metadata clave-valor. Para texto, solo lee hasta `S3_MAX_READ_BYTES` (indicar `partial_read: true`).
10. **`list_object_versions(bucket, prefix, max_items=100)`** — versiones y delete markers (key, version_id, is_latest,
    size, last_modified). Tolerante a AccessDenied.
11. **`compare_prefixes(bucket=None, prefix_a, prefix_b, bucket_b=None, max_objects=None, limit=100)`** — compara
    dos "carpetas" por clave relativa: `only_in_a`, `only_in_b`, `different` (size o etag distinto), `same_count`, con totales.
    Útil para validar copias/migraciones.

## Servidor (`server.py`)

- `MCPServer(name="s3", instructions=...)`: instrucciones en español con: buckets configurados, que es solo lectura,
  flujo recomendado (`test_connection` → `list_buckets` → `list_objects` navegando por prefix → `prefix_stats` para pesos →
  `find_objects` para buscar → `get_object_metadata` → `read_object`/`preview_table` para contenido), aceptar `s3://` URIs,
  límites de escaneo y que si `scan_truncated` es true conviene acotar el prefix.
- CLI (argparse) como en fabric: `python -m s3_readonly_mcp` (stdio), `--list` (config sin secretos), `--test` (test_connection a stdout),
  `--handshake` (cliente MCP in-process que lista tools, como hace fabric-sql-mcp).

## Tests

`tests/` con pytest usando `botocore.stub.Stubber` o `moto` (`moto[s3]`, si se usa añadirlo a `dev`): parseo de
`s3://`, parseo de tamaños/fechas, `human_size`, detección binaria, `list_objects` con folders/files, `prefix_stats`
(totales y agrupación), `find_objects` (filtros), `read_object` con Range y `.gz`, `preview_table` csv/jsonl/json,
allowlist, y que las respuestas grandes se recortan. Sin red real.

## Entregables

`pyproject.toml`, `README.md` (español: instalación, variables, tools, registro en Claude Code/Desktop),
`.env.example` (sin valores reales), `claude-mcp.example.json`, `.gitignore` (incluye `.env`), código y tests pasando,
`ruff check` limpio.
