"""Servidor MCP NiFi (solo lectura) — SDK v2."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from nifi_mcp import __version__
from nifi_mcp.aggregate import AggregateService
from nifi_mcp.config import ConfigError, env_file, load_config, resolve_config_dir
from nifi_mcp.diagnostics import DiagnosticsService
from nifi_mcp.responses import max_response_chars, run_tool
from nifi_mcp.services import NiFiService

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nifi-mcp")


def _enabled_servers_hint() -> str:
    try:
        ids = list(load_config().enabled_servers().keys())
        return f"Servidores habilitados (server_id): {', '.join(ids)}. "
    except Exception:  # config inválida: el arranque lo reporta en main()
        return "Usa nifi_list_servers para ver los server_id disponibles. "


INSTRUCTIONS = (
    "Acceso de SOLO LECTURA (solo GET) a varias instancias Apache NiFi definidas en "
    "config/nifi_servers.yaml. "
    + _enabled_servers_hint()
    + "Flujo recomendado: 1) nifi_health_overview (salud de todos los servidores, resumen "
    "primero) o nifi_list_servers / nifi_test_all_connections. 2) Diagnóstico: "
    "nifi_get_errors (bulletins ERROR/WARN), nifi_find_large_queues, "
    "nifi_find_invalid_components, nifi_find_stopped_processors, "
    "nifi_find_problem_controller_services, nifi_compare_servers, nifi_list_connections, "
    "nifi_cluster_status, nifi_system_diagnostics, nifi_flow_versions. 3) Profundidad: "
    "nifi_get_provenance (eventos; POST/GET/DELETE /provenance solo consulta), "
    "nifi_list_queue (flowfiles en cola; POST/GET/DELETE listing-requests), "
    "nifi_get_controller_service, nifi_get_parameter_context. 4) Navegación: "
    "nifi_search(query) o nifi_find_processors(name/processor_type/state) -> "
    "nifi_get_processor / nifi_get_process_group / nifi_list_processors(process_group_id) "
    "-> nifi_get_processor_status / nifi_processor_status_history. "
    "Las herramientas cuyo server_id es opcional consultan todos los servidores habilitados "
    "en paralelo y etiquetan cada item con 'server'. Las listas aplican limit (default 100) e "
    "informan total/returned/truncated; toda respuesta se acota a ~"
    f"{max_response_chars() // 1000}k caracteres (response_truncated=true si se recortó). "
    "Propiedades sensibles redactadas. Excepción al solo-GET: provenance y listado de cola "
    "usan POST+GET+DELETE read-only (no alteran el flow). Caché en memoria: inventario ~5 min, "
    "estado ~60 s; refresh=true fuerza lectura. nifi_debug_get es un último recurso (GET allowlist)."
)

mcp = MCPServer(
    "nifi",
    title="NiFi MCP (read-only)",
    instructions=INSTRUCTIONS,
    version=__version__,
)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

_service: NiFiService | None = None
_aggregate: AggregateService | None = None
_diagnostics: DiagnosticsService | None = None


def _get_aggregate() -> AggregateService:
    global _service, _aggregate, _diagnostics
    if _aggregate is None:
        _service = NiFiService()
        _aggregate = AggregateService(_service)
        _diagnostics = DiagnosticsService(_service)
    return _aggregate


def _get_service() -> NiFiService:
    _get_aggregate()
    assert _service is not None
    return _service


def _get_diagnostics() -> DiagnosticsService:
    _get_aggregate()
    assert _diagnostics is not None
    return _diagnostics


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_servers() -> dict[str, Any]:
    """Lista los servidores NiFi configurados (inventario estático, sin llamadas de red).

    Devuelve server_id, host, environment, enabled, auth_type y tags. Usa los server_id
    devueltos en el resto de herramientas.
    """
    return await run_tool(lambda: _get_aggregate().list_servers(), context="nifi_list_servers")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_server_info(server_id: str) -> dict[str, Any]:
    """Información en vivo de un servidor: versión NiFi, cluster, usuario actual y estado general.

    Args:
        server_id: Id del servidor (ver nifi_list_servers).
    """
    return await run_tool(
        lambda: _get_service().server_info(server_id),
        context="nifi_server_info",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_test_connection(server_id: str) -> dict[str, Any]:
    """Prueba conectividad de un servidor: URL efectiva, autenticación, versión y modo cluster.

    Args:
        server_id: Id del servidor (ver nifi_list_servers).
    """
    return await run_tool(
        lambda: _get_service().test_connection(server_id),
        context="nifi_test_connection",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_test_all_connections(server_ids: list[str] | None = None) -> dict[str, Any]:
    """Prueba la conexión de todos los servidores habilitados (o un subconjunto) en paralelo.

    Args:
        server_ids: Lista opcional de server_id; por defecto todos los habilitados.
    """
    return await run_tool(
        lambda: _get_aggregate().test_all_connections(server_ids),
        context="nifi_test_all_connections",
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_root_flow(server_id: str, refresh: bool = False) -> dict[str, Any]:
    """Process group raíz normalizado (id, nombre, estado y detalles de primer nivel).

    Args:
        server_id: Id del servidor.
        refresh: true para ignorar la caché de inventario (~5 min).
    """
    return await run_tool(
        lambda: _get_service().get_root_flow(server_id, refresh=refresh),
        context="nifi_get_root_flow",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_process_groups(
    server_id: str,
    process_group_id: str = "root",
    recursive: bool = False,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Lista los process groups hijos de un process group, con su ruta (breadcrumb).

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group padre ("root" por defecto).
        recursive: true para todo el árbol (solo desde "root").
        limit: Máximo de items (default 100).
        refresh: true para ignorar la caché de inventario.
    """
    return await run_tool(
        lambda: _get_service().list_process_groups(
            server_id,
            process_group_id=process_group_id,
            recursive=recursive,
            limit=limit,
            refresh=refresh,
        ),
        context="nifi_list_process_groups",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_process_group(
    server_id: str,
    process_group_id: str,
    include_details: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """Detalle de un process group (estado, ruta; con include_details, sus atributos).

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group (o "root").
        include_details: true para incluir atributos adicionales del componente.
        refresh: true para ignorar la caché de inventario.
    """
    return await run_tool(
        lambda: _get_service().get_process_group(
            server_id, process_group_id, include_details=include_details, refresh=refresh
        ),
        context="nifi_get_process_group",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_processors(
    server_id: str,
    process_group_id: str = "root",
    recursive: bool = False,
    limit: int | None = None,
    include_details: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """Lista procesadores de un process group (id, nombre, estado, ruta, component_type).

    Para buscar por nombre/tipo/estado en todo el flow es mejor nifi_find_processors.

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group ("root" por defecto).
        recursive: true para todo el flow (solo desde "root").
        limit: Máximo de items (default 100).
        include_details: true para incluir propiedades (redactadas) y scheduling; pesado.
        refresh: true para ignorar la caché de inventario.
    """
    return await run_tool(
        lambda: _get_service().list_processors(
            server_id,
            process_group_id=process_group_id,
            recursive=recursive,
            limit=limit,
            include_details=include_details,
            refresh=refresh,
        ),
        context="nifi_list_processors",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_processor(
    server_id: str,
    processor_id: str,
    include_details: bool = True,
) -> dict[str, Any]:
    """Detalle de un procesador: tipo, estado, scheduling, propiedades (sensibles redactadas)
    y errores de validación.

    Args:
        server_id: Id del servidor.
        processor_id: UUID del procesador.
        include_details: false para solo id/nombre/estado.
    """
    return await run_tool(
        lambda: _get_service().get_processor(
            server_id, processor_id, include_details=include_details
        ),
        context="nifi_get_processor",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_search(
    query: str,
    server_id: str | None = None,
    component_type: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Búsqueda de componentes con el buscador nativo de NiFi (nombre, id, propiedades…).

    Sin server_id busca en todos los servidores habilitados en paralelo.

    Args:
        query: Texto a buscar.
        server_id: Limita la búsqueda a un servidor.
        component_type: processor, process_group, connection, controller_service,
            input_port, output_port o remote_process_group.
        limit: Máximo de resultados (default 100).
        refresh: Ignorado (la búsqueda siempre es en vivo); se mantiene por compatibilidad.
    """
    del refresh

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_service().search(
                server_id, query, component_type=component_type, limit=limit
            )
        return await agg.search_multi(query, component_type=component_type, limit=limit)

    return await run_tool(_run, context="nifi_search", query=query)


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_status(
    server_id: str, recursive: bool = False, refresh: bool = False
) -> dict[str, Any]:
    """Estado del flow raíz: colas, hilos activos, contadores running/stopped/invalid y
    snapshots de los hijos directos.

    Args:
        server_id: Id del servidor.
        recursive: true incluye TODO el árbol de estados (puede ser >1 MB y se recortará);
            mejor usar nifi_find_large_queues o nifi_get_process_group_status.
        refresh: true para ignorar la caché de estado (~60 s).
    """
    return await run_tool(
        lambda: _get_service().get_status(server_id, recursive=recursive, refresh=refresh),
        context="nifi_get_status",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_process_group_status(
    server_id: str,
    process_group_id: str,
    recursive: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """Estado (colas, throughput, hilos) de un process group concreto.

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group (o "root").
        recursive: true para incluir descendientes (puede ser grande).
        refresh: Ignorado (siempre en vivo).
    """
    return await run_tool(
        lambda: _get_service().get_process_group_status(
            server_id, process_group_id, recursive=recursive, refresh=refresh
        ),
        context="nifi_get_process_group_status",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_processor_status(server_id: str, processor_id: str) -> dict[str, Any]:
    """Estado en tiempo real de un procesador (in/out, bytes, tareas, hilos activos; 5 min).

    Args:
        server_id: Id del servidor.
        processor_id: UUID del procesador.
    """
    return await run_tool(
        lambda: _get_service().get_processor_status(server_id, processor_id),
        context="nifi_get_processor_status",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_connection_status(server_id: str, connection_id: str) -> dict[str, Any]:
    """Estado de una conexión: flowfiles/bytes en cola y uso de backpressure.

    Args:
        server_id: Id del servidor.
        connection_id: UUID de la conexión.
    """
    return await run_tool(
        lambda: _get_service().get_connection_status(server_id, connection_id),
        context="nifi_get_connection_status",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_bulletins(server_id: str, limit: int | None = None) -> dict[str, Any]:
    """Bulletins recientes (todos los niveles) de un servidor. NiFi solo retiene ~5 min.

    Args:
        server_id: Id del servidor.
        limit: Máximo de bulletins (default 100).
    """
    return await run_tool(
        lambda: _get_service().get_bulletins(server_id, limit=limit),
        context="nifi_get_bulletins",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_errors(server_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Bulletins ERROR/WARN recientes; sin server_id, de todos los servidores en paralelo.

    Args:
        server_id: Limita a un servidor.
        limit: Máximo de bulletins a revisar/devolver (default 100).
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().get_errors(server_id, limit=limit)
        return await agg.get_errors_multi(limit=limit)

    return await run_tool(_run, context="nifi_get_errors")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_health_check(
    server_id: str | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Chequeo de salud con hallazgos (conectividad, cluster, bulletins, colas, componentes
    inválidos/detenidos). Sin server_id: todos los servidores, resumen primero.

    Args:
        server_id: Limita a un servidor.
        refresh: true para ignorar cachés.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().health_check(server_id, refresh=refresh)
        return await agg.health_check_multi(refresh=refresh)

    return await run_tool(_run, context="nifi_health_check")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_health_overview(refresh: bool = False) -> dict[str, Any]:
    """Resumen de salud de TODOS los servidores habilitados. Buen primer paso.

    Args:
        refresh: true para ignorar cachés.
    """
    return await run_tool(
        lambda: _get_aggregate().health_overview(refresh=refresh),
        context="nifi_health_overview",
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_find_processors(
    name: str | None = None,
    processor_type: str | None = None,
    state: str | None = None,
    server_id: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
    include_details: bool = False,
) -> dict[str, Any]:
    """Filtra procesadores de todo el flow; sin server_id, en todos los servidores.

    Args:
        name: Subcadena del nombre (sin distinguir mayúsculas).
        processor_type: Clase o subcadena, p.ej. "ExecuteSQL" o "org.apache...PutKafka".
        state: RUNNING, STOPPED, DISABLED o INVALID.
        server_id: Limita a un servidor.
        limit: Máximo de items (default 100); total informa el número real.
        refresh: true para ignorar la caché de inventario.
        include_details: true para incluir propiedades (redactadas); pesado.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().find_processors(
                server_id,
                name=name,
                processor_type=processor_type,
                state=state,
                limit=limit,
                refresh=refresh,
                include_details=include_details,
            )
        return await agg.find_processors_multi(
            name=name,
            processor_type=processor_type,
            state=state,
            limit=limit,
            refresh=refresh,
            include_details=include_details,
        )

    return await run_tool(_run, context="nifi_find_processors")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_find_large_queues(
    server_id: str | None = None,
    min_flowfiles: int | None = None,
    min_bytes: int | None = None,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Conexiones con colas grandes o cerca de backpressure; sin server_id, en todos.

    Args:
        server_id: Limita a un servidor.
        min_flowfiles: Umbral de flowfiles (default: umbrales warning/critical del YAML).
        min_bytes: Umbral de bytes (default: umbrales del YAML).
        limit: Máximo de items (default 100).
        refresh: true para ignorar la caché de estado.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().find_large_queues(
                server_id,
                min_flowfiles=min_flowfiles,
                min_bytes=min_bytes,
                limit=limit,
                refresh=refresh,
            )
        return await agg.find_large_queues_multi(
            min_flowfiles=min_flowfiles,
            min_bytes=min_bytes,
            limit=limit,
            refresh=refresh,
        )

    return await run_tool(_run, context="nifi_find_large_queues")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_find_stopped_processors(
    server_id: str | None = None, refresh: bool = False, limit: int | None = None
) -> dict[str, Any]:
    """Procesadores en estado STOPPED (informativo); sin server_id, en todos los servidores.

    Args:
        server_id: Limita a un servidor.
        refresh: true para ignorar la caché de inventario.
        limit: Máximo de items (default 100); total informa el número real.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().find_stopped_processors(
                server_id, refresh=refresh, limit=limit
            )
        return await agg.find_stopped_processors_multi(refresh=refresh, limit=limit)

    return await run_tool(_run, context="nifi_find_stopped_processors")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_find_invalid_components(
    server_id: str | None = None, refresh: bool = False, limit: int | None = None
) -> dict[str, Any]:
    """Procesadores y controller services inválidos o deshabilitados, con sus errores de
    validación; sin server_id, en todos los servidores.

    Args:
        server_id: Limita a un servidor.
        refresh: true para ignorar la caché de inventario.
        limit: Máximo de items (default 100).
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().find_invalid_components(
                server_id, refresh=refresh, limit=limit
            )
        return await agg.find_invalid_components_multi(refresh=refresh, limit=limit)

    return await run_tool(_run, context="nifi_find_invalid_components")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_compare_servers(
    server_ids: list[str] | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Compara métricas clave entre servidores: versión, nº de procesadores por estado,
    process groups, colas y bulletins ERROR/WARN.

    Args:
        server_ids: Subconjunto opcional; por defecto todos los habilitados.
        refresh: true para ignorar cachés.
    """
    return await run_tool(
        lambda: _get_aggregate().compare_servers(server_ids, refresh=refresh),
        context="nifi_compare_servers",
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_controller_services(
    server_id: str,
    process_group_id: str = "root",
    include_details: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Lista controller services de un process group (estado; propiedades redactadas).

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group ("root" por defecto).
        include_details: true para incluir tipo, propiedades y errores de validación.
        limit: Máximo de items (default 100).
    """
    return await run_tool(
        lambda: _get_service().list_controller_services(
            server_id,
            process_group_id=process_group_id,
            include_details=include_details,
            limit=limit,
        ),
        context="nifi_list_controller_services",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_find_problem_controller_services(
    server_id: str | None = None,
) -> dict[str, Any]:
    """Controller services DISABLED/INVALID con errores de validación; sin server_id, en todos.

    Args:
        server_id: Limita a un servidor.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_diagnostics().find_problem_controller_services(server_id)
        return await agg.find_problem_controller_services_multi()

    return await run_tool(_run, context="nifi_find_problem_controller_services")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_parameter_contexts(server_id: str, limit: int | None = None) -> dict[str, Any]:
    """Parameter contexts y sus parámetros (valores sensibles redactados).

    Args:
        server_id: Id del servidor.
        limit: Máximo de contextos (default 100).
    """
    return await run_tool(
        lambda: _get_service().list_parameter_contexts(server_id, limit=limit),
        context="nifi_list_parameter_contexts",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_processor_status_history(
    server_id: str, processor_id: str, max_points: int = 30
) -> dict[str, Any]:
    """Serie temporal de métricas de un procesador (snapshots agregados más recientes).

    Args:
        server_id: Id del servidor.
        processor_id: UUID del procesador.
        max_points: Nº de snapshots más recientes a devolver (default 30).
    """
    return await run_tool(
        lambda: _get_service().processor_status_history(
            server_id, processor_id, max_points=max_points
        ),
        context="nifi_processor_status_history",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_process_group_status_history(
    server_id: str, process_group_id: str, max_points: int = 30
) -> dict[str, Any]:
    """Serie temporal de métricas de un process group (snapshots agregados más recientes).

    Args:
        server_id: Id del servidor.
        process_group_id: Id del process group (o "root").
        max_points: Nº de snapshots más recientes a devolver (default 30).
    """
    return await run_tool(
        lambda: _get_service().process_group_status_history(
            server_id, process_group_id, max_points=max_points
        ),
        context="nifi_process_group_status_history",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_connections(
    server_id: str | None = None,
    process_group_id: str | None = None,
    min_queued: int = 0,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Lista conexiones con colas, backpressure (% objetos/tamaño) y origen/destino.

    Sin server_id consulta todos los servidores habilitados en paralelo.

    Args:
        server_id: Limita a un servidor.
        process_group_id: Solo conexiones en ese process group y descendientes.
        min_queued: Mínimo de flowfiles en cola (default 0).
        limit: Máximo de items (default 100).
        refresh: true para ignorar caché de inventario/estado.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_service().list_connections(
                server_id,
                process_group_id=process_group_id,
                min_queued=min_queued,
                limit=limit,
                refresh=refresh,
            )
        return await agg.list_connections_multi(
            process_group_id=process_group_id,
            min_queued=min_queued,
            limit=limit,
            refresh=refresh,
        )

    return await run_tool(_run, context="nifi_list_connections")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_provenance(
    server_id: str,
    component_id: str | None = None,
    max_results: int = 100,
    since_minutes: int = 60,
) -> dict[str, Any]:
    """Eventos de provenance (auditoría de flowfiles) en una ventana temporal.

    NiFi exige POST /provenance (crear consulta), GET /provenance/{id} (leer) y
    DELETE /provenance/{id} (limpiar la consulta). Esas tres llamadas son read-only
    y no modifican el flow.

    Args:
        server_id: Id del servidor.
        component_id: UUID opcional de componente (processor, etc.) para filtrar.
        max_results: Máximo de eventos (default 100).
        since_minutes: Ventana hacia atrás en minutos (default 60).
    """
    return await run_tool(
        lambda: _get_service().get_provenance(
            server_id,
            component_id=component_id,
            max_results=max_results,
            since_minutes=since_minutes,
        ),
        context="nifi_get_provenance",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_list_queue(
    server_id: str, connection_id: str, max_results: int = 100
) -> dict[str, Any]:
    """Resumen de flowfiles en la cola de una conexión (sin descargar contenido).

    Usa POST+GET+DELETE en /flowfile-queues/.../listing-requests (solo listado;
    no elimina flowfiles de la cola).

    Args:
        server_id: Id del servidor.
        connection_id: UUID de la conexión.
        max_results: Máximo de flowfiles listados (default 100).
    """
    return await run_tool(
        lambda: _get_service().list_queue(
            server_id, connection_id, max_results=max_results
        ),
        context="nifi_list_queue",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_cluster_status(server_id: str | None = None) -> dict[str, Any]:
    """Estado del cluster NiFi: nodos, dirección, roles y conectividad.

    Sin server_id consulta todos los servidores habilitados en paralelo.

    Args:
        server_id: Limita a un servidor.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_service().cluster_status(server_id)
        return await agg.cluster_status_multi()

    return await run_tool(_run, context="nifi_cluster_status")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_controller_service(server_id: str, id: str) -> dict[str, Any]:
    """Detalle de un controller service: propiedades (sensibles redactadas) y referencias.

    Args:
        server_id: Id del servidor.
        id: UUID del controller service.
    """
    return await run_tool(
        lambda: _get_service().get_controller_service(server_id, id),
        context="nifi_get_controller_service",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_flow_versions(
    server_id: str | None = None, limit: int | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Process groups bajo control de versiones (registry, versión, estado LOCALLY_MODIFIED/STALE/UP_TO_DATE).

    Sin server_id consulta todos los servidores habilitados en paralelo.

    Args:
        server_id: Limita a un servidor.
        limit: Máximo de items (default 100).
        refresh: true para re-leer inventario del flow.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_service().flow_versions(server_id, limit=limit, refresh=refresh)
        return await agg.flow_versions_multi(limit=limit, refresh=refresh)

    return await run_tool(_run, context="nifi_flow_versions")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_get_parameter_context(server_id: str, id: str) -> dict[str, Any]:
    """Un parameter context con todos sus parámetros (valores sensibles redactados).

    Args:
        server_id: Id del servidor.
        id: UUID del parameter context.
    """
    return await run_tool(
        lambda: _get_service().get_parameter_context(server_id, id),
        context="nifi_get_parameter_context",
        server_id=server_id,
    )


@mcp.tool(annotations=_READ_ONLY)
async def nifi_system_diagnostics(server_id: str | None = None) -> dict[str, Any]:
    """Diagnóstico JVM: heap, threads, repos (content/flowfile/provenance) y uso de disco.

    Sin server_id consulta todos los servidores habilitados en paralelo.

    Args:
        server_id: Limita a un servidor.
    """

    async def _run() -> dict[str, Any]:
        agg = _get_aggregate()
        if server_id:
            return await _get_service().system_diagnostics(server_id)
        return await agg.system_diagnostics_multi()

    return await run_tool(_run, context="nifi_system_diagnostics")


@mcp.tool(annotations=_READ_ONLY)
async def nifi_debug_get(server_id: str, endpoint: str) -> dict[str, Any]:
    """GET crudo de solo lectura a un endpoint relativo de la API NiFi (último recurso).

    Allowlist de prefijos: /flow/about, /flow/current-user, /flow/cluster/summary,
    /controller/cluster, /flow/process-groups/, /flow/processors/, /flow/connections/,
    /flow/bulletin-board, /flow/search-results, /flow/parameter-contexts, /flow/controller/,
    /authentication/configuration, /access/config, /counters, /resources. Sin query string;
    se bloquean contenido de flowfiles, state, config, tokens, etc.

    Args:
        server_id: Id del servidor.
        endpoint: Ruta relativa, p.ej. "/flow/about".
    """
    return await run_tool(
        lambda: _get_service().debug_get(server_id, endpoint),
        context="nifi_debug_get",
        server_id=server_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NiFi MCP (solo lectura)")
    parser.add_argument("--version", action="store_true", help="Muestra versión")
    args = parser.parse_args(argv)

    if args.version:
        print(__version__, file=sys.stderr)
        return 0

    try:
        cfg = load_config()
        enabled = list(cfg.enabled_servers().keys())
        logger.info("NiFi MCP v%s — servidores habilitados: %s", __version__, ", ".join(enabled))
        logger.info(
            "  config_dir=%s env_file=%s (exists=%s)",
            resolve_config_dir(),
            env_file(),
            env_file().exists(),
        )
    except ConfigError as exc:
        logger.error("Configuración inválida: %s", exc)
        return 1

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
