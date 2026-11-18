"""Servidor MCP para directorios UNC de Windows.

Uso:
  python -m windows_fileshare_mcp               # ejecutar como MCP server (stdio)
  python -m windows_fileshare_mcp --list        # mostrar configuración
  python -m windows_fileshare_mcp --test        # probar acceso a cada raíz
  python -m windows_fileshare_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from windows_fileshare_mcp import __version__, ops
from windows_fileshare_mcp.config import LOADED_ENV_FILES, get_settings
from windows_fileshare_mcp.ops import ToolError
from windows_fileshare_mcp.util import bound_response

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("windows-fileshare-mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE_ANN = ToolAnnotations(readOnlyHint=False, openWorldHint=True)
WRITE_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, openWorldHint=True, destructiveHint=True)


def _instructions() -> str:
    settings = get_settings()
    roots_desc = ", ".join(f"{a}={p}" for a, p in settings.roots.items()) or "ninguna"
    write_note = (
        " Escritura habilitada (write_file, make_directory, copy_file, move_file)."
        if settings.allow_write
        else " Solo lectura; la escritura está deshabilitada."
    )
    local_note = " Se aceptan rutas locales C:\\..." if settings.allow_local else ""
    unc_note = (
        " Cualquier UNC permitido (UNC_ALLOW_ANY_UNC)."
        if settings.allow_any_unc and not settings.allowlist_only
        else ""
    )
    allow_note = " Solo rutas bajo UNC_ROOTS." if settings.allowlist_only else ""
    return (
        "Servidor MCP para explorar directorios UNC de red en Windows: listar carpetas, "
        f"metadata, búsqueda y lectura de archivos.{write_note}{local_note}{allow_note}{unc_note} "
        f"Raíces: {roots_desc}. Alias por defecto: '{settings.default_root or '-'}'. "
        "Rutas: UNC absoluta \\\\servidor\\recurso\\..., alias:ruta (ej. share01:execution\\test), "
        "o ruta relativa al alias por defecto. add_root registra alias en memoria. "
        "Flujo recomendado: list_roots → list_directory → dir_stats / find_files / recent_files → "
        "file_info → read_file / tail_file / grep_text / read_document; compare_dirs para diffs. "
        f"Escaneos recorren como máximo {settings.max_scan_entries} entradas "
        f"en {settings.scan_timeout}s (scan_truncated si se alcanza el tope). "
        f"Lecturas limitadas a {settings.max_read_bytes} bytes. "
        f"Operaciones con timeout {settings.op_timeout}s. "
        f"Respuestas acotadas a {settings.max_response_chars} caracteres (response_truncated)."
    )


mcp = MCPServer(
    "windows-fileshare",
    title="UNC filesystem (Windows)",
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
    except Exception as exc:
        logger.warning("error inesperado %s", type(exc).__name__)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_code": "IOError"}
    if not isinstance(data, dict):
        data = {"result": data}
    payload = {"ok": True}
    payload.update(data)
    return bound_response(payload)


@mcp.tool(annotations=READ_ONLY)
def list_roots() -> dict[str, Any]:
    """Lista alias de raíces UNC, rutas, accesibilidad (probe 5 s) y flags de política."""
    return _invoke(ops.list_roots)


@mcp.tool(annotations=READ_ONLY)
def test_access(path: Optional[str] = None) -> dict[str, Any]:
    """
    Comprueba existencia, tipo, permisos de listado/escritura (os.access, sin crear archivos),
    latencia en ms y usuario Windows actual.

    Args:
        path: UNC, alias:ruta o relativa al alias por defecto. Vacío = raíz por defecto.
    """
    return _invoke(lambda: ops.test_access(path=path))


@mcp.tool(annotations=READ_ONLY)
def list_directory(
    path: Optional[str] = None,
    pattern: str = "*",
    include_hidden: bool = False,
    sort: str = "name",
    desc: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """
    Lista entradas de un directorio con os.scandir.

    Args:
        path: Directorio a listar.
        pattern: Glob sobre el nombre (sin distinguir mayúsculas).
        include_hidden: Incluir nombres que empiezan por punto.
        sort: name, size o modified.
        desc: Orden descendente.
        limit: Máximo de entradas devueltas.
    """
    return _invoke(
        lambda: ops.list_directory(
            path=path,
            pattern=pattern,
            include_hidden=include_hidden,
            sort=sort,
            desc=desc,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def file_info(path: str) -> dict[str, Any]:
    """
    Metadata: tipo, tamaños, fechas, readonly/hidden (atributos Windows), extensión;
    para texto pequeño, encoding y número de líneas.

    Args:
        path: Archivo o directorio.
    """
    return _invoke(lambda: ops.file_info(path=path))


@mcp.tool(annotations=READ_ONLY)
def read_file(
    path: str,
    offset: int = 1,
    limit: int = 500,
    encoding: Optional[str] = None,
    as_hex: bool = False,
    byte_offset: int = 0,
    length: Optional[int] = None,
) -> dict[str, Any]:
    """
    Lee un archivo de texto por líneas (offset/limit) o por bytes (byte_offset/length).
    Detecta encoding BOM/utf-8/cp1252. Binarios devuelven metadata + hint; encoding=base64
    devuelve hasta 2 MB en base64; as_hex muestra bytes iniciales en hex.

    Args:
        path: Archivo.
        offset: Primera línea (1-based) en modo líneas.
        limit: Cantidad de líneas.
        encoding: Codificación de texto, o base64 para binario.
        as_hex: Muestra bytes en hex.
        byte_offset: Desplazamiento en bytes (modo bytes).
        length: Cantidad de bytes a leer (modo bytes o base64).
    """
    return _invoke(
        lambda: ops.read_file(
            path=path,
            offset=offset,
            limit=limit,
            encoding=encoding,
            as_hex=as_hex,
            byte_offset=byte_offset,
            length=length,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def tail_file(path: str, lines: int = 100) -> dict[str, Any]:
    """
    Últimas N líneas leyendo desde el final por bloques (logs grandes).

    Args:
        path: Archivo de texto.
        lines: Número de líneas finales.
    """
    return _invoke(lambda: ops.tail_file(path=path, lines=lines))


@mcp.tool(annotations=READ_ONLY)
def find_files(
    path: Optional[str] = None,
    pattern: str = "*",
    recursive: bool = True,
    max_depth: int = 10,
    type: str = "any",
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Busca archivos/directorios bajo path con glob en el nombre y filtros opcionales.

    Args:
        path: Directorio base.
        pattern: Glob sobre el nombre (case-insensitive).
        recursive: Recorrer subdirectorios.
        max_depth: Profundidad máxima.
        type: file, dir o any.
        min_size: Tamaño mínimo en bytes.
        max_size: Tamaño máximo en bytes.
        modified_after: Fecha ISO o YYYY-MM-DD inclusive.
        modified_before: Fecha ISO inclusive.
        limit: Máximo de coincidencias devueltas.
    """
    return _invoke(
        lambda: ops.find_files(
            path=path,
            pattern=pattern,
            recursive=recursive,
            max_depth=max_depth,
            type=type,
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
            modified_before=modified_before,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def grep_text(
    path: str,
    pattern: str,
    regex: bool = False,
    ignore_case: bool = True,
    file_pattern: str = "*",
    recursive: bool = True,
    context: int = 0,
    max_matches: int = 200,
) -> dict[str, Any]:
    """
    Busca texto en un archivo o árbol por streaming. Informa skipped (binarios/errores).

    Args:
        path: Archivo o directorio.
        pattern: Texto o regex según regex=true.
        regex: Interpretar pattern como expresión regular.
        ignore_case: Sin distinguir mayúsculas (solo modo texto).
        file_pattern: Glob de nombres de archivo.
        recursive: Si path es directorio, recorrer subárbol.
        context: Líneas antes/después.
        max_matches: Tope de coincidencias.
    """
    return _invoke(
        lambda: ops.grep_text(
            path=path,
            pattern=pattern,
            regex=regex,
            ignore_case=ignore_case,
            file_pattern=file_pattern,
            recursive=recursive,
            context=context,
            max_matches=max_matches,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def dir_stats(path: Optional[str] = None, max_depth: Optional[int] = None, top: int = 20) -> dict[str, Any]:
    """
    Estadísticas de un árbol: totales, desglose por extensión, archivos más pesados,
    subdirectorios inmediatos por peso, más reciente y más antiguo.

    Args:
        path: Directorio raíz del análisis.
        max_depth: Profundidad de escaneo (None = sin límite práctico).
        top: Cuántos elementos en tops.
    """
    return _invoke(lambda: ops.dir_stats(path=path, max_depth=max_depth, top=top))


@mcp.tool(annotations=READ_ONLY)
def add_root(alias: str, unc_path: str) -> dict[str, Any]:
    """
    Añade un alias de raíz UNC en memoria (solo esta sesión). Valida que el directorio exista.

    Args:
        alias: Nombre corto (ej. share01).
        unc_path: Ruta UNC o local absoluta al share/carpeta.
    """
    return _invoke(lambda: ops.add_root(alias=alias, unc_path=unc_path))


@mcp.tool(annotations=READ_ONLY)
def read_document(path: str, sheet: Optional[str] = None, max_rows: int = 200) -> dict[str, Any]:
    """
    Extrae tablas/texto de .xlsx, .docx, .csv, .json o .pdf (pdf solo si pypdf está instalado).

    Args:
        path: Archivo.
        sheet: Hoja xlsx por nombre o índice (0-based).
        max_rows: Filas máximas (xlsx/csv) o páginas pdf (tope 50).
    """
    return _invoke(lambda: ops.read_document(path=path, sheet=sheet, max_rows=max_rows))


@mcp.tool(annotations=READ_ONLY)
def compare_dirs(path_a: str, path_b: str, recursive: bool = True) -> dict[str, Any]:
    """
    Compara dos directorios: archivos solo en A, solo en B, o con distinto tamaño/fecha.

    Args:
        path_a: Primer directorio.
        path_b: Segundo directorio.
        recursive: Incluir subdirectorios.
    """
    return _invoke(lambda: ops.compare_dirs(path_a=path_a, path_b=path_b, recursive=recursive))


@mcp.tool(annotations=READ_ONLY)
def recent_files(
    path: Optional[str] = None,
    hours: float = 24,
    pattern: str = "*",
    max_results: int = 200,
    max_depth: Optional[int] = None,
) -> dict[str, Any]:
    """
    Archivos modificados en las últimas N horas, ordenados por fecha descendente.

    Args:
        path: Directorio base.
        hours: Ventana hacia atrás desde ahora.
        pattern: Glob sobre el nombre de archivo.
        max_results: Máximo de resultados.
        max_depth: Profundidad máxima de recorrido (None = sin límite práctico).
    """
    return _invoke(
        lambda: ops.recent_files(
            path=path,
            hours=hours,
            pattern=pattern,
            max_results=max_results,
            max_depth=max_depth,
        )
    )


def _register_write_tools() -> None:
    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def write_file(
        path: str,
        content: str,
        mode: str = "create",
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """
        Escribe un archivo (solo si UNC_ALLOW_WRITE=true).

        Args:
            path: Destino.
            content: Contenido texto.
            mode: create (falla si existe), overwrite o append.
            encoding: Codificación de salida.
        """
        return _invoke(lambda: ops.write_file(path=path, content=content, mode=mode, encoding=encoding))

    @mcp.tool(annotations=WRITE_ANN)
    def make_directory(path: str, parents: bool = True) -> dict[str, Any]:
        """
        Crea un directorio (requiere UNC_ALLOW_WRITE=true).

        Args:
            path: Ruta del directorio.
            parents: Equivalente a mkdir -p.
        """
        return _invoke(lambda: ops.make_directory(path=path, parents=parents))

    @mcp.tool(annotations=WRITE_ANN)
    def copy_file(src: str, dst: str, overwrite: bool = False) -> dict[str, Any]:
        """
        Copia un archivo (requiere UNC_ALLOW_WRITE=true). No mueve ni borra.

        Args:
            src: Origen.
            dst: Destino.
            overwrite: Sobrescribir si destino existe.
        """
        return _invoke(lambda: ops.copy_file(src=src, dst=dst, overwrite=overwrite))

    @mcp.tool(annotations=WRITE_ANN)
    def move_file(src: str, dst: str) -> dict[str, Any]:
        """
        Mueve o renombra archivo/directorio (requiere UNC_ALLOW_WRITE=true). No borra.

        Args:
            src: Origen.
            dst: Destino (no debe existir).
        """
        return _invoke(lambda: ops.move_file(src=src, dst=dst))


if get_settings().allow_write:
    _register_write_tools()


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cli_test() -> int:
    settings = get_settings()
    all_ok = True
    for alias in settings.roots:
        out = test_access(path=f"{alias}:")
        _print(out)
        if not out.get("ok"):
            all_ok = False
    if not settings.roots:
        out = test_access()
        _print(out)
        all_ok = out.get("ok", False)
    return 0 if all_ok else 1


def _expected_tool_count() -> int:
    return 17 if get_settings().allow_write else 13


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        expected = _expected_tool_count()
        _print({"ok": True, "tool_count": len(names), "expected": expected, "tools": names})
        return 0 if len(names) == expected else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor MCP para directorios UNC de Windows")
    parser.add_argument("--list", action="store_true", help="Muestra la configuración")
    parser.add_argument("--test", action="store_true", help="Prueba acceso a cada raíz")
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
        "windows-fileshare-mcp v%s — env: %s",
        __version__,
        [str(path) for path in LOADED_ENV_FILES],
    )
    logger.info(
        "roots=%s default=%s allow_write=%s allowlist=%s allow_local=%s",
        ",".join(settings.roots.keys()) or "-",
        settings.default_root or "-",
        settings.allow_write,
        settings.allowlist_only,
        settings.allow_local,
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
