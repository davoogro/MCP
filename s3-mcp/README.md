# s3-mcp

Servidor MCP (stdio) de **solo lectura** para explorar buckets de Amazon S3: listar archivos,
ver pesos, metadata y previsualizar contenido (CSV, JSON, JSONL, Parquet).

No ejecuta operaciones de escritura ni borrado y no genera URLs prefirmadas.

## Instalación

El intérprete de `C:\DVB\python` ya incluye `mcp` 2.0 y `boto3`. Para desarrollar:

```bash
C:\DVB\python\python.exe -m pip install -e "C:\DVB\mcp\s3-mcp[dev]"
```

Parquet es opcional en runtime (`pip install pyarrow`). Si falta, `preview_table` lo indica
sin tumbar el servidor.

También se puede usar sin instalar, con `PYTHONPATH`:

```bash
set PYTHONPATH=C:\DVB\mcp\s3-mcp\src
C:\DVB\python\python.exe -m s3_mcp --handshake
```

## Configuración

Carga `s3-mcp\.env` y, si no pisa variables ya definidas, `C:\DVB\mcp\.env`.
`S3_MCP_ENV_FILE` apunta a otro archivo y se lee primero. Ver [.env.example](.env.example).

Precedencia de credenciales: `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` >
`access_key` / `secret_key` > `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
`AWS_SESSION_TOKEN` solo acompaña al par `AWS_*`. `S3_SESSION_TOKEN` acompaña al par `S3_*`.

| Variable | Uso | Default |
|---|---|---|
| `S3_REGION` | Región por defecto | `us-east-1` |
| `S3_ENDPOINT_URL` | Endpoint compatible (MinIO, etc.) | vacío |
| `S3_BUCKETS` | Buckets conocidos, separados por comas | vacío |
| `S3_BUCKET_ALLOWLIST_ONLY` | Rechazar buckets que no estén en `S3_BUCKETS` | `false` |
| `S3_DEFAULT_BUCKET` | Bucket si la tool no recibe `bucket` | el primero de `S3_BUCKETS` |
| `S3_MAX_SCAN_OBJECTS` | Tope de objetos en stats, búsqueda y comparación | `200000` |
| `S3_MAX_READ_BYTES` | Tope de bytes de `read_object` / preview de texto | `20971520` (20 MB) |
| `S3_MAX_PARQUET_READ_BYTES` | Tope descargado al previsualizar un parquet por rangos | `268435456` (256 MB) |
| `S3_MAX_COUNT_BYTES` | Tope de bytes comprimidos por archivo en `count_rows` | `2147483648` (2 GB) |
| `S3_QUERY_MAX_BYTES` | Descarga máxima si DuckDB no usa httpfs (`query_data` / `sample_rows`) | `2147483648` (2 GB) |
| `S3_MAX_DOWNLOAD_BYTES` | Tope de `download_object` | `5368709120` (5 GB) |
| `S3_MAX_QUERY_ROWS` | Filas máximas de `query_data` | `5000` |
| `S3_MAX_RESPONSE_CHARS` | Tope de la respuesta JSON | `80000` |

`S3_BUCKETS` hace falta cuando el usuario IAM no tiene `s3:ListAllMyBuckets`.
La región de cada bucket se detecta con `head_bucket` (`x-amz-bucket-region`, también en 301/403)
y, si no viene, con `get_bucket_location`.

Los parámetros `bucket`, `key` y `prefix` aceptan URIs `s3://bucket/ruta`.

## Tools

Todas son de solo lectura (`readOnlyHint`, `openWorldHint`). Cada respuesta es
`{"ok": true, ...}` o `{"ok": false, "error": "...", "error_code": "..."}`.
Los tamaños van en `size` (bytes) y `size_human` (base 1024). Las fechas van en ISO-8601 UTC.
Si el JSON supera el tope, las listas se recortan y aparece `"response_truncated": true`.
Si un escaneo llega al tope, `"scan_truncated": true`: conviene acotar el prefix.

| Tool | Descripción |
|---|---|
| `test_connection` | Identidad STS, access key enmascarado, región y acceso a cada bucket configurado |
| `list_buckets` | Une `list_buckets` con `S3_BUCKETS` (`include_region` opcional) |
| `get_bucket_info` | Versioning, cifrado, lifecycle, tags, block public access, policy, logging, object lock |
| `list_objects` | Carpetas y archivos (`delimiter="/"`, o `""` para listado plano) |
| `get_object_metadata` | `head_object` + tags. Si la key parece carpeta, sugiere `list_objects` |
| `prefix_stats` | Totales, extensión, storage class, subcarpeta, mayores y más recientes |
| `find_objects` | Glob, regex, extensión (`csv.gz`), tamaño, fecha y storage class |
| `read_object` | Lectura por Range; descomprime `.gz`; no vuelca binarios |
| `preview_table` | CSV, TSV, JSONL, JSON y Parquet (parquet por rangos: footer + primer row group) |
| `count_rows` | Cuenta filas de un objeto o de un prefix. En parquet lee solo el footer |
| `query_data` | SQL DuckDB sobre parquet/csv/json en S3 (`read_parquet`, etc.) |
| `schema_of_prefix` | Esquema parquet canónico y archivos con drift |
| `sample_rows` | Muestreo de filas con DuckDB (`USING SAMPLE`) |
| `freshness` | Última modificación y objetos recientes por subcarpeta |
| `download_object` | Descarga a disco local (p. ej. `C:\DVB\exports\s3`) |
| `list_object_versions` | Versiones y delete markers |
| `compare_prefixes` | Diferencias entre dos prefijos (copias / migraciones) |

Flujo recomendado: `test_connection` → `list_buckets` → `list_objects` → `prefix_stats` →
`find_objects` → `get_object_metadata` → `read_object` / `preview_table`.
Para preguntas de cuántas filas, `count_rows`; en parquet es instantáneo.

## Registro en Claude

```bash
claude mcp add --scope user s3 -e PYTHONPATH=C:\DVB\mcp\s3-mcp\src -e PYTHONIOENCODING=utf-8 -- C:\DVB\python\python.exe -m s3_mcp
```

El ejemplo para Claude Desktop / Cursor está en [claude-mcp.example.json](claude-mcp.example.json).

## CLI

```bash
set PYTHONPATH=C:\DVB\mcp\s3-mcp\src
python -m s3_mcp --list        # configuración, sin secretos
python -m s3_mcp --test        # test_connection
python -m s3_mcp --handshake   # lista las 17 tools
python -m pytest               # tests offline (moto); no llama a AWS
python -m ruff check src tests
```
