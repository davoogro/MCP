"""Servidor MCP de solo lectura para Amazon S3.

Uso:
  python -m s3_mcp               # ejecutar como MCP server (stdio)
  python -m s3_mcp --list        # mostrar configuración (sin secretos)
  python -m s3_mcp --test        # probar la conexión y salir
  python -m s3_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional, Union

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from s3_mcp import __version__, ops
from s3_mcp.config import LOADED_ENV_FILES, get_settings
from s3_mcp.duck import query_data as duck_query_data
from s3_mcp.duck import sample_rows as duck_sample_rows
from s3_mcp.duck import schema_of_prefix as duck_schema_of_prefix
from s3_mcp.download import download_object as s3_download_object
from s3_mcp.freshness import freshness as s3_freshness
from s3_mcp.ops import ToolError, error_code, error_message
from s3_mcp.util import bound_response

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("s3-mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _instructions() -> str:
    settings = get_settings()
    buckets = ", ".join(settings.buckets) or "ninguno (se intentará list_buckets si el IAM lo permite)"
    allow = " Solo se aceptan buckets de S3_BUCKETS." if settings.allowlist_only else ""
    return (
        "Servidor de solo lectura para explorar buckets de Amazon S3: listar archivos, "
        "revisar pesos, metadata y previsualizar contenido. No escribe, no borra y no genera "
        f"URLs prefirmadas.{allow} "
        f"Buckets configurados: {buckets}. Bucket por defecto: '{settings.default_bucket or '-'}'. "
        "Flujo recomendado: test_connection → list_buckets → list_objects (navegando por prefix) → "
        "prefix_stats para pesos → find_objects para buscar → get_object_metadata → "
        "read_object o preview_table para el contenido. "
        "Para SQL analítico sobre parquet/csv/json en S3 usa query_data (DuckDB); "
        "schema_of_prefix detecta drift de esquema parquet; sample_rows muestrea filas; "
        "freshness resume antigüedad por subcarpeta; download_object guarda un objeto en disco local. "
        "Para preguntas de cuántas filas usa count_rows; en parquet es instantáneo. "
        "Los parámetros bucket, key y prefix aceptan URIs s3://bucket/ruta. "
        f"Los escaneos recorren como máximo {settings.max_scan_objects} objetos; "
        "si scan_truncated es true, conviene acotar el prefix. "
        f"Las lecturas de texto se limitan a {settings.max_read_bytes} bytes, "
        f"el preview parquet a {settings.max_parquet_read_bytes} bytes por rangos y "
        f"count_rows a {settings.max_count_bytes} bytes por archivo, "
        f"query_data/sample_rows descargan hasta {settings.max_query_bytes} bytes si httpfs no está "
        f"disponible, download_object hasta {settings.max_download_bytes} bytes. "
        f"query_data devuelve como máximo {settings.max_query_rows} filas (truncated). "
        f"Las respuestas se limitan a {settings.max_response_chars} caracteres "
        "(response_truncated indica un recorte)."
    )


mcp = MCPServer(
    "s3",
    title="Amazon S3 (solo lectura)",
    version=__version__,
    instructions=_instructions(),
)


def _invoke(fn) -> dict[str, Any]:
    try:
        data = fn()
    except ToolError as exc:
        logger.warning("error %s: %s", exc.code, exc)
        body: dict[str, Any] = {"ok": False, "error": str(exc), "error_code": exc.code}
        body.update(exc.extra)
        return body
    except ops.ClientError as exc:
        code = error_code(exc)
        logger.warning("AWS %s", code)
        return {"ok": False, "error": error_message(exc), "error_code": code}
    except ops.BotoCoreError as exc:
        logger.warning("BotoCoreError %s", type(exc).__name__)
        return {"ok": False, "error": str(exc), "error_code": type(exc).__name__}
    except Exception as exc:
        logger.warning("error inesperado %s", type(exc).__name__)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_code": type(exc).__name__}
    if not isinstance(data, dict):
        data = {"result": data}
    payload = {"ok": True}
    payload.update(data)
    return bound_response(payload)


@mcp.tool(annotations=READ_ONLY)
def test_connection() -> dict[str, Any]:
    """
    Prueba la identidad (STS), la región/endpoint y el acceso a cada bucket de S3_BUCKETS.

    Informa si ``list_buckets`` está permitido. El access key sale enmascarado.
    """
    return _invoke(ops.test_connection)


@mcp.tool(annotations=READ_ONLY)
def list_buckets(include_region: bool = False) -> dict[str, Any]:
    """
    Une ``list_buckets`` (si hay permiso) con los buckets de S3_BUCKETS.

    Args:
        include_region: Si es true, resuelve la región de cada bucket (más lento).
    """
    return _invoke(lambda: ops.list_buckets(include_region=include_region))


@mcp.tool(annotations=READ_ONLY)
def get_bucket_info(bucket: str) -> dict[str, Any]:
    """
    Región, versioning, cifrado, lifecycle, tags, public access block, policy status,
    logging y object lock. Si una subllamada responde AccessDenied o NoSuch*, el campo
    queda en null y el código entra en ``unavailable``.

    Args:
        bucket: Nombre del bucket o URI ``s3://bucket``.
    """
    return _invoke(lambda: ops.get_bucket_info(bucket))


@mcp.tool(annotations=READ_ONLY)
def list_objects(
    bucket: Optional[str] = None,
    prefix: str = "",
    delimiter: str = "/",
    max_keys: int = 200,
    continuation_token: Optional[str] = None,
    start_after: Optional[str] = None,
) -> dict[str, Any]:
    """
    Navegación tipo carpeta con list_objects_v2. ``delimiter=""`` lista en plano (recursivo).

    Args:
        bucket: Bucket o URI ``s3://bucket/prefix``. Por defecto, S3_DEFAULT_BUCKET.
        prefix: Prefijo (carpeta). Acepta ``s3://``.
        delimiter: Separador de "carpetas". Vacío para listado recursivo.
        max_keys: Objetos por página (1–1000, default 200).
        continuation_token: Token de la página anterior (``next_continuation_token``).
        start_after: Empieza después de esta key. Se ignora si hay continuation_token.
    """
    return _invoke(lambda: ops.list_objects(
        bucket=bucket,
        prefix=prefix,
        delimiter=delimiter,
        max_keys=max_keys,
        continuation_token=continuation_token,
        start_after=start_after,
    ))


@mcp.tool(annotations=READ_ONLY)
def get_object_metadata(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    version_id: Optional[str] = None,
    include_tags: bool = True,
) -> dict[str, Any]:
    """
    head_object más tags. Si la key no existe y parece una carpeta, sugiere list_objects.

    Args:
        bucket: Bucket o URI ``s3://``.
        key: Key del objeto, o URI ``s3://bucket/key``.
        version_id: Versión concreta (opcional).
        include_tags: Leer get_object_tagging. Si no hay permiso, tags queda null.
    """
    return _invoke(lambda: ops.get_object_metadata(
        bucket=bucket, key=key, version_id=version_id, include_tags=include_tags
    ))


@mcp.tool(annotations=READ_ONLY)
def prefix_stats(
    bucket: Optional[str] = None,
    prefix: str = "",
    group_by_depth: int = 1,
    top_n: int = 10,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    """
    Recorre un prefix (sin guardar todos los objetos) y resume pesos: total, promedio,
    min/max, el más antiguo, los más recientes y los más grandes, por extensión,
    storage class y subcarpeta. ``scan_truncated`` es true si se alcanzó el tope.

    Args:
        bucket: Bucket o URI ``s3://``.
        prefix: Prefijo a escanear. Acepta ``s3://``.
        group_by_depth: Niveles de subcarpeta relativos al prefix (default 1).
        top_n: Cuántos objetos devolver en ``largest`` y ``newest`` (default 10).
        max_objects: Tope de objetos a recorrer (default S3_MAX_SCAN_OBJECTS).
    """
    return _invoke(lambda: ops.prefix_stats(
        bucket=bucket,
        prefix=prefix,
        group_by_depth=group_by_depth,
        top_n=top_n,
        max_objects=max_objects,
    ))


@mcp.tool(annotations=READ_ONLY)
def find_objects(
    bucket: Optional[str] = None,
    prefix: str = "",
    pattern: Optional[str] = None,
    regex: Optional[str] = None,
    extensions: Optional[list[str]] = None,
    min_size: Optional[Union[int, str]] = None,
    max_size: Optional[Union[int, str]] = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    storage_class: Optional[str] = None,
    sort_by: str = "key",
    descending: bool = False,
    limit: int = 100,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    """
    Busca objetos con un escaneo recursivo. ``pattern`` es un glob (fnmatch, sin distinguir
    mayúsculas) sobre el nombre, o sobre la key completa si contiene ``/``. ``regex`` se aplica
    con re.search sobre la key. ``extensions`` no lleva punto y admite dobles como ``csv.gz``.
    Los tamaños aceptan bytes o textos ``10MB`` / ``1.5 GB``. Las fechas ISO sin zona son UTC.

    Args:
        bucket: Bucket o URI ``s3://``.
        prefix: Prefijo donde buscar.
        pattern: Glob, por ejemplo ``*.csv`` o ``raw/*.json``.
        regex: Expresión regular sobre la key.
        extensions: Lista, por ejemplo ["csv", "parquet"] o ["csv.gz"].
        min_size: Tamaño mínimo (int bytes o ``10MB``).
        max_size: Tamaño máximo.
        modified_after: Fecha ISO inclusive (``2026-01-31`` o con hora).
        modified_before: Fecha ISO inclusive.
        storage_class: Por ejemplo STANDARD o GLACIER.
        sort_by: ``key``, ``size`` o ``last_modified``.
        descending: Orden descendente.
        limit: Máximo de coincidencias devueltas (el total va en matched_total).
        max_objects: Tope del escaneo (default S3_MAX_SCAN_OBJECTS).
    """
    return _invoke(lambda: ops.find_objects(
        bucket=bucket,
        prefix=prefix,
        pattern=pattern,
        regex=regex,
        extensions=extensions,
        min_size=min_size,
        max_size=max_size,
        modified_after=modified_after,
        modified_before=modified_before,
        storage_class=storage_class,
        sort_by=sort_by,
        descending=descending,
        limit=limit,
        max_objects=max_objects,
    ))


@mcp.tool(annotations=READ_ONLY)
def read_object(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    offset: int = 0,
    max_bytes: int = 65536,
    encoding: str = "utf-8",
    decompress: bool = True,
    version_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Lee un objeto con Range. Si la key termina en ``.gz`` y decompress es true, descomprime
    en streaming y ``offset``/``max_bytes`` se aplican al texto. Si el fragmento parece
    binario no se devuelve el contenido: ``is_binary`` y los primeros 64 bytes en hex.

    Args:
        bucket: Bucket o URI ``s3://``.
        key: Key o URI ``s3://bucket/key``.
        offset: Byte inicial (del texto si se descomprime).
        max_bytes: Bytes a devolver (acotado por S3_MAX_READ_BYTES, hasta 20 MB, default 65536).
        encoding: Codificación del texto.
        decompress: Descomprimir ``.gz``.
        version_id: Versión concreta (opcional).
    """
    return _invoke(lambda: ops.read_object(
        bucket=bucket,
        key=key,
        offset=offset,
        max_bytes=max_bytes,
        encoding=encoding,
        decompress=decompress,
        version_id=version_id,
    ))


@mcp.tool(annotations=READ_ONLY)
def preview_table(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    rows: int = 20,
    format: Optional[str] = None,
    delimiter: Optional[str] = None,
    quotechar: Optional[str] = None,
    columns: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Previsualiza datos tabulares: csv, tsv, jsonl/ndjson, json o parquet (pyarrow).
    El formato se infiere por la extensión, incluidos los ``.gz`` de texto.
    Parquet se abre con lecturas por rango (el footer está al final): devuelve num_rows,
    num_row_groups, schema, metadata, created_by, bytes_fetched y las primeras filas del
    primer row group. Si el primer row group superaría S3_MAX_PARQUET_READ_BYTES, vienen
    los metadatos sin filas y un aviso. En texto, ``partial_read`` es true si el objeto
    supera S3_MAX_READ_BYTES.

    Args:
        bucket: Bucket o URI ``s3://``.
        key: Key del archivo o URI ``s3://bucket/key``.
        rows: Filas a devolver (default 20).
        format: csv, tsv, jsonl, ndjson, json o parquet. None = inferir.
        delimiter: Separador CSV/TSV. None = sniffer (o tab si es tsv).
        quotechar: Comilla CSV/TSV. None = sniffer; si no es útil, prueba ``"`` y ``'``.
        columns: Subconjunto de columnas, en ese orden.
    """
    return _invoke(lambda: ops.preview_table(
        bucket=bucket,
        key=key,
        rows=rows,
        format=format,
        delimiter=delimiter,
        quotechar=quotechar,
        columns=columns,
    ))


@mcp.tool(annotations=READ_ONLY)
def count_rows(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    prefix: Optional[str] = None,
    pattern: Optional[str] = None,
    extensions: Optional[list[str]] = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    max_files: int = 50,
    format: Optional[str] = None,
    delimiter: Optional[str] = None,
    quotechar: Optional[str] = None,
    has_header: bool = True,
) -> dict[str, Any]:
    """
    Cuenta filas de un objeto o de varios bajo un prefix. Hay que indicar exactamente
    uno de ``key`` o ``prefix``. En parquet lee solo el footer (pocos KB) y es instantáneo.
    CSV/TSV y JSONL se recorren en streaming, también ``.gz``. Un error en un archivo
    (formato no soportado, AccessDenied, GLACIER/DEEP_ARCHIVE) no aborta al resto.

    Args:
        bucket: Bucket o URI ``s3://``. Por defecto, S3_DEFAULT_BUCKET.
        key: Un solo objeto. No combinar con prefix.
        prefix: Prefijo a recorrer, con el mismo filtro glob/extensión/fecha que find_objects.
        pattern: Glob (fnmatch, sin distinguir mayúsculas) sobre el nombre o la key.
        extensions: Lista sin punto, por ejemplo ["csv", "parquet"] o ["csv.gz"].
        modified_after: Fecha ISO inclusive. Sin zona horaria es UTC.
        modified_before: Fecha ISO inclusive.
        max_files: Máximo de archivos contados (default 50). ``files_truncated`` si hay más.
        format: csv, tsv, jsonl, ndjson, json o parquet. None = inferir por la extensión.
        delimiter: Separador CSV/TSV. None = el mismo sniffer que preview_table.
        quotechar: Comilla CSV/TSV. None = sniffer; si no es útil, prueba ``"`` y ``'``.
        has_header: En CSV/TSV resta 1 a la cuenta de registros.
    """
    return _invoke(lambda: ops.count_rows(
        bucket=bucket,
        key=key,
        prefix=prefix,
        pattern=pattern,
        extensions=extensions,
        modified_after=modified_after,
        modified_before=modified_before,
        max_files=max_files,
        format=format,
        delimiter=delimiter,
        quotechar=quotechar,
        has_header=has_header,
    ))


@mcp.tool(annotations=READ_ONLY)
def list_object_versions(
    bucket: Optional[str] = None,
    prefix: str = "",
    max_items: int = 100,
) -> dict[str, Any]:
    """
    Versiones y delete markers de un prefix. AccessDenied se devuelve como error controlado.

    Args:
        bucket: Bucket o URI ``s3://``.
        prefix: Prefijo a listar.
        max_items: Máximo de elementos por llamada (1–1000, default 100).
    """
    return _invoke(lambda: ops.list_object_versions(bucket=bucket, prefix=prefix, max_items=max_items))


@mcp.tool(annotations=READ_ONLY)
def compare_prefixes(
    prefix_a: str,
    prefix_b: str,
    bucket: Optional[str] = None,
    bucket_b: Optional[str] = None,
    max_objects: Optional[int] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Compara dos carpetas por clave relativa: solo en A, solo en B, distintos (size o etag)
    y cuántos son iguales. Sirve para validar copias o migraciones.

    Args:
        prefix_a: Prefijo A (acepta ``s3://``).
        prefix_b: Prefijo B (acepta ``s3://``).
        bucket: Bucket de A. Por defecto, S3_DEFAULT_BUCKET.
        bucket_b: Bucket de B. Por defecto, el mismo que A.
        max_objects: Tope de objetos por lado.
        limit: Máximo de claves listadas en each diferencia (los totales no se recortan).
    """
    return _invoke(lambda: ops.compare_prefixes(
        prefix_a=prefix_a,
        prefix_b=prefix_b,
        bucket=bucket,
        bucket_b=bucket_b,
        max_objects=max_objects,
        limit=limit,
    ))


@mcp.tool(annotations=READ_ONLY)
def query_data(sql: str, bucket: Optional[str] = None) -> dict[str, Any]:
    """
    Ejecuta SQL de solo lectura con DuckDB sobre datos en S3 (``read_parquet``,
    ``read_csv_auto``, ``read_json_auto`` con URIs ``s3://``). Configura httpfs
    con las credenciales actuales; si no puede, descarga los objetos a un temporal
    (tope ``S3_QUERY_MAX_BYTES``). Solo ``SELECT``/``WITH``/``DESCRIBE``/``SUMMARIZE``.
    Máximo 5000 filas (``truncated``).

    Args:
        sql: Sentencia SQL (una sola).
        bucket: Si se indica, todas las URIs ``s3://`` deben ser de ese bucket.
    """
    return _invoke(lambda: duck_query_data(sql=sql, bucket=bucket))


@mcp.tool(annotations=READ_ONLY)
def schema_of_prefix(
    prefix: str,
    bucket: Optional[str] = None,
    sample_files: int = 20,
) -> dict[str, Any]:
    """
    Esquema parquet de un prefijo: toma hasta ``sample_files`` archivos recientes,
    infiere el esquema canónico y lista archivos con esquema distinto (drift).

    Args:
        prefix: Prefijo o URI ``s3://bucket/prefix/``.
        bucket: Bucket por defecto si no va en la URI.
        sample_files: Cuántos parquet muestrear (default 20).
    """
    return _invoke(lambda: duck_schema_of_prefix(prefix=prefix, bucket=bucket, sample_files=sample_files))


@mcp.tool(annotations=READ_ONLY)
def sample_rows(
    key_or_prefix: str,
    bucket: Optional[str] = None,
    n: int = 20,
    random: bool = True,
) -> dict[str, Any]:
    """
    Muestrea filas de parquet/csv/json bajo una key o prefijo usando DuckDB
    (``USING SAMPLE`` si ``random`` es true, si no ``LIMIT``).

    Args:
        key_or_prefix: Objeto, prefijo o URI ``s3://``.
        bucket: Bucket por defecto.
        n: Filas a devolver (default 20, máx. 500).
        random: Muestreo aleatorio DuckDB (default true).
    """
    return _invoke(lambda: duck_sample_rows(
        key_or_prefix=key_or_prefix,
        bucket=bucket,
        n=n,
        random=random,
    ))


@mcp.tool(annotations=READ_ONLY)
def freshness(
    prefixes: Union[str, list[str]],
    bucket: Optional[str] = None,
    group_by_depth: int = 1,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    """
    Por subcarpeta bajo cada prefijo: último objeto modificado, antigüedad en horas,
    conteo total y cuántos objetos se modificaron en las últimas 24 h. Sirve para
    monitorear cargas.

    Args:
        prefixes: Un prefijo, URI ``s3://``, lista o cadena separada por comas.
        bucket: Bucket por defecto.
        group_by_depth: Niveles de subcarpeta (default 1).
        max_objects: Tope de objetos a escanear (default S3_MAX_SCAN_OBJECTS).
    """
    return _invoke(lambda: s3_freshness(
        prefixes=prefixes,
        bucket=bucket,
        group_by_depth=group_by_depth,
        max_objects=max_objects,
    ))


@mcp.tool(annotations=READ_ONLY)
def download_object(
    key: str,
    bucket: Optional[str] = None,
    local_dir: str = r"C:\DVB\exports\s3",
) -> dict[str, Any]:
    """
    Descarga un objeto S3 a disco local (no pasa el contenido al LLM). Tope 5 GB
    (``S3_MAX_DOWNLOAD_BYTES``). Devuelve la ruta local y los bytes escritos.

    Args:
        key: Key o URI ``s3://bucket/key``.
        bucket: Bucket por defecto.
        local_dir: Carpeta destino (default ``C:\\DVB\\exports\\s3``).
    """
    return _invoke(lambda: s3_download_object(key=key, bucket=bucket, local_dir=local_dir))


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cli_test() -> int:
    out = test_connection()
    _print(out)
    if not out.get("ok"):
        return 1
    if out.get("identity_error"):
        return 1
    return 0


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        _print({"ok": True, "tool_count": len(names), "tools": names})
        return 0 if len(names) == 17 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor MCP de solo lectura para Amazon S3")
    parser.add_argument("--list", action="store_true", help="Muestra la configuración sin secretos")
    parser.add_argument("--test", action="store_true", help="Prueba la conexión y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista las tools vía cliente MCP")
    args = parser.parse_args()

    if args.list:
        _print(get_settings().public_dict())
        return 0
    if args.test:
        return cli_test()
    if args.handshake:
        return asyncio.run(_handshake())

    settings = get_settings()
    logger.info(
        "s3-mcp v%s — env: %s",
        __version__,
        [str(path) for path in LOADED_ENV_FILES],
    )
    logger.info(
        "región=%s endpoint=%s buckets=%s default=%s allowlist_only=%s access_key=%s",
        settings.region,
        settings.endpoint_url or "-",
        ",".join(settings.buckets) or "-",
        settings.default_bucket or "-",
        settings.allowlist_only,
        settings.masked_access_key() or "-",
    )
    logger.info(
        "límites scan=%s read=%s parquet=%s count=%s response=%s",
        settings.max_scan_objects,
        settings.max_read_bytes,
        settings.max_parquet_read_bytes,
        settings.max_count_bytes,
        settings.max_query_bytes,
        settings.max_download_bytes,
        settings.max_query_rows,
        settings.max_response_chars,
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
