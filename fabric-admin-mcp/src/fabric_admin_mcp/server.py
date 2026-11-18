"""Servidor MCP sobre Microsoft Fabric CLI (`fab`) para administrar Fabric y Power BI.

A diferencia de fabric-sql-mcp (solo T-SQL), este servidor crea, modifica, mueve, exporta,
importa y borra workspaces e items, ejecuta jobs, gestiona ACLs y llama a las REST API de
Fabric / Power BI / OneLake / Azure con el token de la sesión `fab auth login`.

Cada tool lanza `fab` como subproceso no interactivo con `--output_format json`. No hay
contexto persistente (`cd`): usa rutas absolutas, p.ej. /Ventas.Workspace/Modelo.SemanticModel.

Uso:
  python -m fabric_admin_mcp               # ejecutar como MCP server (stdio)
  python -m fabric_admin_mcp --status      # estado del login de fab y sale
  python -m fabric_admin_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

__version__ = "1.0.0"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fabric_admin_mcp")

FAB = os.environ.get("FAB_PATH") or shutil.which("fab") or r"C:\DVB\python\Scripts\fab.exe"
DEFAULT_TIMEOUT = int(os.environ.get("FAB_MCP_TIMEOUT", "300"))
MAX_TIMEOUT = 3600
MAX_CHARS = int(os.environ.get("FAB_MCP_MAX_CHARS", "80000"))
EXPORT_DIR = os.environ.get("FAB_MCP_EXPORT_DIR", r"C:\DVB\exports\fabric-admin")

# Comandos que necesitan una terminal o no tienen sentido sin contexto persistente.
INTERACTIVE = {"cd", "pwd", "open", "clear", "cls"}
# Comandos que destruyen o reemplazan algo: exigen confirm=True.
DESTRUCTIVE = {"rm", "del", "unassign"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE_HINT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


# ============================================================
# Ejecución de fab
# ============================================================


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    half = MAX_CHARS // 2
    return text[:half] + f"\n... [{len(text) - MAX_CHARS} chars recortados] ...\n" + text[-half:], True


def _parse(stdout: str) -> Any:
    """fab --output_format json imprime un objeto JSON; si no, devolvemos el texto."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # A veces hay líneas de aviso antes del JSON.
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start > 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                pass
    return text


def run_fab(args: list[str], timeout: Optional[int] = None, json_output: bool = True) -> dict[str, Any]:
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    cmd = [FAB, *args]
    if json_output and "--output_format" not in args:
        cmd += ["--output_format", "json"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "TERM": "dumb"}
    logger.info("fab %s", " ".join(shlex.quote(a) for a in args))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": _display(args), "error": f"timeout tras {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "command": _display(args), "error": f"no se encontró fab en {FAB} (usa FAB_PATH)"}

    stdout, out_trunc = _truncate(ANSI.sub("", proc.stdout))
    stderr, _ = _truncate(ANSI.sub("", proc.stderr).strip())
    result = _parse(stdout)
    ok = proc.returncode == 0
    # fab devuelve {"status": "Failure", ...} con rc 0 en algunos errores.
    if isinstance(result, dict) and str(result.get("status", "")).lower() == "failure":
        ok = False
    # Sobre {timestamp, status, command, result: {data, hidden_data}} -> solo lo útil.
    if ok and isinstance(result, dict) and isinstance(result.get("result"), dict):
        inner = result["result"]
        if set(inner) <= {"data", "hidden_data", "message"}:
            result = inner.get("data") if set(inner) == {"data"} else inner
    out: dict[str, Any] = {"ok": ok, "command": _display(args), "exit_code": proc.returncode, "result": result}
    if stderr:
        out["stderr"] = stderr
    if out_trunc:
        out["response_truncated"] = True
    if not ok and _looks_unauthenticated(stdout + stderr):
        out["hint"] = ("fab no tiene sesión válida. El usuario debe ejecutar en su terminal: "
                       "`fab auth logout` y luego `fab auth login` (modo interactivo, navegador).")
    return out


def _display(args: list[str]) -> str:
    return "fab " + " ".join(shlex.quote(a) for a in args)


def _looks_unauthenticated(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("not logged in", "authenticationfailed", "unauthorized",
                                "failed to acquire token", "auth login"))


def _kv(params: Optional[dict[str, Any]]) -> list[str]:
    """Convierte un dict en el formato -P clave=valor,clave2=valor2 que usa fab."""
    if not params:
        return []
    parts = []
    for k, v in params.items():
        if isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"))
        parts.append(f"{k}={v}")
    return ["-P", ",".join(parts)]


def _job_params(params: Optional[dict[str, Any]]) -> list[str]:
    """Parámetros de job en formato nombre:tipo=valor; infiere el tipo si la clave no lo trae."""
    if not params:
        return []
    parts = []
    for k, v in params.items():
        if ":" not in k:
            kind = ("bool" if isinstance(v, bool) else "int" if isinstance(v, int)
                    else "float" if isinstance(v, float) else "string")
            k = f"{k}:{kind}"
        if isinstance(v, bool):
            v = str(v).lower()
        parts.append(f"{k}={v}")
    return ["-P", ",".join(parts)]


def _ensure_dir(path: Optional[str], sub: str) -> str:
    target = path or os.path.join(EXPORT_DIR, sub)
    os.makedirs(target, exist_ok=True)
    return target


mcp = MCPServer(
    "fabric-admin",
    title="Microsoft Fabric CLI (fab)",
    version=__version__,
    instructions=(
        "Administración completa de Microsoft Fabric y Power BI vía Fabric CLI (`fab`), con la "
        "identidad de la sesión `fab auth login`. Rutas tipo sistema de archivos, siempre absolutas: "
        "/<Workspace>.Workspace/<Item>.<Tipo>[/Tables|Files/...], p.ej. /Ventas.Workspace/"
        "Modelo.SemanticModel, /Ventas.Workspace/LH.Lakehouse/Files/raw. Elementos ocultos: "
        "/.capacities, /.connections, /.gateways, /.domains, /<ws>.Workspace/.managedidentities, etc. "
        "Flujo: auth_status -> ls('/') -> ls(workspace) -> get/describe -> acción. desc_element "
        "dice qué comandos soporta cada tipo. Crear: mkdir (con params). Modificar: set_property. "
        "Copiar/mover: cp/mv. Definiciones: export_item/import_item (PBIP/TMDL/notebooks). Jobs: "
        "job_run/job_status. Permisos: acl_list/acl_set/acl_remove. Lo que no tenga tool usa "
        "fab_command (cualquier comando fab) o api (REST Fabric/Power BI/OneLake/Azure). Borrar (rm) "
        "y unassign requieren confirm=true: pídele confirmación explícita al usuario antes. "
        "Si un comando falla por autenticación, el usuario debe correr `fab auth login` en su terminal."
    ),
)


# ============================================================
# Tools: sesión y descubrimiento
# ============================================================


@mcp.tool(annotations=READ_ONLY)
def auth_status() -> dict[str, Any]:
    """
    Estado del login de fab: cuenta, tenant, tokens disponibles (Fabric/Power BI, Storage, Azure).
    Llama esto primero si algo falla con errores de autenticación.
    """
    res = run_fab(["auth", "status"], timeout=60, json_output=False)
    text = res.get("result") or ""
    res["logged_in"] = isinstance(text, str) and "logged in: true" in text.lower()
    if not res["logged_in"]:
        res["hint"] = ("No hay sesión. El usuario debe ejecutar en su terminal `fab auth logout` y "
                       "`fab auth login` (elegir 'Interactive with a web browser').")
    return res


@mcp.tool(annotations=READ_ONLY)
def ls(path: str = "/", long: bool = False, all: bool = False, query: Optional[str] = None,
       timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Lista workspaces ('/'), items de un workspace, carpetas/archivos de OneLake o elementos ocultos
    (all=True muestra .capacities, .connections, .gateways, .domains, .sparkpools, etc.).
    long=True agrega detalles (id, tipo, capacidad...). query: filtro JMESPath opcional.
    """
    args = ["ls", path]
    if long:
        args.append("-l")
    if all:
        args.append("-a")
    if query:
        args += ["-q", query]
    return run_fab(args, timeout)


@mcp.tool(annotations=READ_ONLY)
def get(path: str, query: Optional[str] = None, verbose: bool = False,
        timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Propiedades de un workspace/item/capacidad/conexión (JSON). query: JMESPath, p.ej. 'id',
    'properties', 'definition' (en items con definición). verbose=True trae todo.
    """
    args = ["get", path]
    if query:
        args += ["-q", query]
    if verbose:
        args.append("-v")
    return run_fab(args, timeout)


@mcp.tool(annotations=READ_ONLY)
def exists(path: str) -> dict[str, Any]:
    """Indica si existe un workspace, item, carpeta o archivo."""
    return run_fab(["exists", path], timeout=120)


@mcp.tool(annotations=READ_ONLY)
def find(query: str, item_type: Optional[str] = None, timeout: Optional[int] = None) -> dict[str, Any]:
    """Busca items en el catálogo de OneLake por nombre/descripción en todos los workspaces."""
    args = ["find", query, "-l"]
    if item_type:
        args += ["-P", f"type={item_type}"]
    return run_fab(args, timeout)


@mcp.tool(annotations=READ_ONLY)
def desc_element(element: str) -> dict[str, Any]:
    """
    Describe qué comandos soporta un tipo de elemento o ruta (p.ej. '.Lakehouse', '.SemanticModel',
    '.Workspace', '.capacities' o una ruta concreta). Úsalo antes de operar sobre un tipo nuevo.
    """
    return run_fab(["desc", element], timeout=120, json_output=False)


@mcp.tool(annotations=READ_ONLY)
def fab_help(command: str = "") -> dict[str, Any]:
    """Ayuda de fab o de un subcomando (p.ej. 'mkdir', 'job run', 'acl set', 'table load')."""
    return run_fab([*shlex.split(command), "--help"], timeout=60, json_output=False)


# ============================================================
# Tools: crear / modificar / mover
# ============================================================


@mcp.tool(annotations=MUTATING)
def mkdir(path: str, params: Optional[dict[str, Any]] = None, timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Crea un workspace, item, carpeta, capacidad, conexión, gateway, dominio, etc.
    Ejemplos de path: /Nuevo.Workspace, /Nuevo.Workspace/LH.Lakehouse, /WS.Workspace/R.Report,
    /WS.Workspace/LH.Lakehouse/Files/raw, /.capacities/cap1.Capacity, /.connections/c1.Connection.
    params (-P) depende del tipo: p.ej. {'capacityName': 'micap'} para workspaces,
    {'enableSchemas': True} para lakehouse. Consulta fab_help('mkdir') o desc_element('.Tipo').
    """
    return run_fab(["mkdir", path, *_kv(params)], timeout)


@mcp.tool(annotations=MUTATING)
def set_property(path: str, query: str, value: str, force: bool = True,
                 timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Cambia una propiedad: displayName, description, sensitivityLabel, definición parcial, settings
    de capacidad/workspace, etc. query es la ruta JMESPath de la propiedad; value el nuevo valor
    (JSON en texto si es objeto).
    """
    args = ["set", path, "-q", query, "-i", value]
    if force:
        args.append("-f")
    return run_fab(args, timeout)


@mcp.tool(annotations=MUTATING)
def cp(source: str, destination: str, force: bool = True, recursive: bool = False,
       timeout: Optional[int] = None) -> dict[str, Any]:
    """Copia items entre workspaces o archivos en/desde OneLake (también local <-> OneLake)."""
    args = ["cp", source, destination]
    if force:
        args.append("-f")
    if recursive:
        args.append("-r")
    return run_fab(args, timeout)


@mcp.tool(annotations=MUTATING)
def mv(source: str, destination: str, force: bool = True, recursive: bool = False,
       timeout: Optional[int] = None) -> dict[str, Any]:
    """Mueve/renombra items entre workspaces o archivos dentro de OneLake."""
    args = ["mv", source, destination]
    if force:
        args.append("-f")
    if recursive:
        args.append("-r")
    return run_fab(args, timeout)


@mcp.tool(annotations=DESTRUCTIVE_HINT)
def rm(path: str, confirm: bool = False, hard: bool = False, timeout: Optional[int] = None) -> dict[str, Any]:
    """
    BORRA un workspace, item, carpeta o archivo. Requiere confirm=True, y solo debe usarse con
    confirmación explícita del usuario para esa ruta. hard=True borra items de forma permanente
    (sin papelera de recuperación); por defecto el borrado de items es recuperable.
    """
    if not confirm:
        return {"ok": False, "error": f"rm {path} requiere confirm=true tras confirmación explícita del usuario"}
    return run_fab(["rm", path, "-f", *(["--hard"] if hard else [])], timeout)


@mcp.tool(annotations=MUTATING)
def assign(resource: str, workspace: str, timeout: Optional[int] = None) -> dict[str, Any]:
    """Asigna una capacidad o dominio a un workspace (p.ej. resource='/.capacities/cap1.Capacity')."""
    return run_fab(["assign", resource, "-W", workspace, "-f"], timeout)


@mcp.tool(annotations=MUTATING)
def start_stop(action: str, path: str, timeout: Optional[int] = None) -> dict[str, Any]:
    """action='start'|'stop' sobre capacidades, mirrored databases, etc."""
    if action not in ("start", "stop"):
        return {"ok": False, "error": "action debe ser 'start' o 'stop'"}
    return run_fab([action, path, "-f"], timeout)


# ============================================================
# Tools: definiciones (export / import)
# ============================================================


@mcp.tool(annotations=MUTATING)
def export_item(path: str, output_dir: Optional[str] = None, format: Optional[str] = None,
                timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Exporta la definición de un item (o todos los de un workspace) a disco: reports/semantic
    models en PBIP/TMDL, notebooks (.ipynb/.py), pipelines JSON, etc. Por defecto en
    C:\\DVB\\exports\\fabric-admin\\export. format: p.ej. '.ipynb', '.py', 'TMDL'.
    """
    target = _ensure_dir(output_dir, "export")
    args = ["export", path, "-o", target, "-f"]
    if format:
        args += ["--format", format]
    res = run_fab(args, timeout)
    res["output_dir"] = target
    return res


@mcp.tool(annotations=MUTATING)
def import_item(path: str, input_dir: str, format: Optional[str] = None,
                timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Crea o ACTUALIZA un item desde una definición local (carpeta exportada / PBIP / notebook).
    path es el destino, p.ej. /WS.Workspace/Ventas.Report. Si existe, se sobrescribe su definición.
    """
    args = ["import", path, "-i", input_dir, "-f"]
    if format:
        args += ["--format", format]
    return run_fab(args, timeout)


# ============================================================
# Tools: jobs y tablas
# ============================================================


@mcp.tool(annotations=MUTATING)
def job_run(path: str, params: Optional[dict[str, Any]] = None, config: Optional[dict[str, Any]] = None,
            input: Optional[dict[str, Any]] = None, wait: bool = True,
            timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Ejecuta un notebook, pipeline, Spark job definition, refresh de semantic model, etc.
    wait=True espera a que termine (hasta timeout, máx 3600 s); wait=False lanza y devuelve el id
    (consulta luego con job_status). params: parámetros del notebook/pipeline, p.ej.
    {'fecha': '2026-10-01', 'dias': 7} (el tipo se infiere; o usa claves 'nombre:tipo').
    config: configuración Spark del notebook (-C); input: payload JSON completo del job (-i).
    """
    args = ["job", "run" if wait else "start", path, *_job_params(params)]
    if config:
        args += ["-C", json.dumps(config, separators=(",", ":"))]
    if input is not None:
        args += ["-i", json.dumps(input, separators=(",", ":"))]
    if wait and timeout:
        args += ["--timeout", str(int(timeout))]
    return run_fab(args, (timeout or DEFAULT_TIMEOUT) + 60 if wait else 120)


@mcp.tool(annotations=READ_ONLY)
def job_status(path: str, job_id: Optional[str] = None) -> dict[str, Any]:
    """Historial de ejecuciones de un item (sin job_id) o estado de una ejecución concreta."""
    args = ["job", "run-status", path, "--id", job_id] if job_id else ["job", "run-list", path]
    return run_fab(args, timeout=120)


@mcp.tool(annotations=MUTATING)
def job_cancel(path: str, job_id: str) -> dict[str, Any]:
    """Cancela una ejecución en curso."""
    return run_fab(["job", "run-cancel", path, "--id", job_id], timeout=120)


@mcp.tool(annotations=MUTATING)
def job_schedule(path: str, action: str, schedule_id: Optional[str] = None,
                 schedule_type: Optional[str] = None, interval: Optional[str] = None,
                 start: Optional[str] = None, end: Optional[str] = None, days: Optional[str] = None,
                 enable: Optional[bool] = None, input: Optional[dict[str, Any]] = None,
                 confirm: bool = False) -> dict[str, Any]:
    """
    Programaciones de un item. action: 'list' | 'create' | 'update' | 'delete' (delete requiere
    confirm=True). schedule_type: cron|daily|weekly; interval: minutos (cron) o lista de horas
    ('08:00,20:00'); start/end en UTC ('2026-11-01T00:00:00'); days: 'Monday,Friday'.
    input: payload JSON completo alternativo.
    """
    sub = {"list": "run-list", "create": "run-sch", "update": "run-update", "delete": "run-rm"}.get(action)
    if not sub:
        return {"ok": False, "error": "action debe ser list|create|update|delete"}
    if action == "delete" and not confirm:
        return {"ok": False, "error": "borrar una programación requiere confirm=true"}
    args = ["job", sub, path]
    if action == "list":
        args.append("--schedule")
    if schedule_id:
        args += ["--id", schedule_id]
    for flag, val in (("--type", schedule_type), ("--interval", interval), ("--start", start),
                      ("--end", end), ("--days", days)):
        if val:
            args += [flag, val]
    if input is not None:
        args += ["-i", json.dumps(input, separators=(",", ":"))]
    if enable is not None:
        args.append("--enable" if enable else "--disable")
    if action == "delete":
        args.append("-f")
    return run_fab(args, timeout=120)


@mcp.tool(annotations=MUTATING)
def table(action: str, path: str, extra_args: Optional[list[str]] = None,
          timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Operaciones sobre tablas Delta de un lakehouse. action: 'schema' | 'load' | 'optimize' |
    'vacuum'. path: /WS.Workspace/LH.Lakehouse/Tables/[schema/]tabla. extra_args: flags extra,
    p.ej. ['--file', 'Files/raw/data.csv', '--mode', 'append'] para load, ['--vorder'] para optimize.
    """
    if action not in ("schema", "load", "optimize", "vacuum"):
        return {"ok": False, "error": "action debe ser schema|load|optimize|vacuum"}
    return run_fab(["table", action, path, *(extra_args or [])], timeout)


# ============================================================
# Tools: permisos
# ============================================================


@mcp.tool(annotations=READ_ONLY)
def acl_list(path: str) -> dict[str, Any]:
    """Lista permisos (roles) de un workspace, item, conexión o gateway."""
    return run_fab(["acl", "ls", path, "-l"], timeout=120)


@mcp.tool(annotations=MUTATING)
def acl_set(path: str, identity: str, role: str) -> dict[str, Any]:
    """
    Concede o cambia el rol de una identidad (objectId de Entra: usuario, grupo o service
    principal). Roles: workspace Admin|Member|Contributor|Viewer; conexión Owner|User|
    UserWithReshare; gateway Admin|ConnectionCreator|ConnectionCreatorWithResharing.
    """
    return run_fab(["acl", "set", path, "-I", identity, "-R", role, "-f"], timeout=120)


@mcp.tool(annotations=DESTRUCTIVE_HINT)
def acl_remove(path: str, identity: str, confirm: bool = False) -> dict[str, Any]:
    """Quita el acceso de una identidad. Requiere confirm=True tras confirmación del usuario."""
    if not confirm:
        return {"ok": False, "error": "acl_remove requiere confirm=true tras confirmación explícita"}
    return run_fab(["acl", "rm", path, "-I", identity, "-f"], timeout=120)


# ============================================================
# Tools: genéricas
# ============================================================


@mcp.tool(annotations=MUTATING)
def api(endpoint: str, method: str = "get", body: Optional[Any] = None, audience: str = "fabric",
        params: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None,
        query: Optional[str] = None, timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Llamada REST autenticada. audience: 'fabric' (api.fabric.microsoft.com/v1/...), 'powerbi'
    (api.powerbi.com/v1.0/myorg/... : refresh de datasets, DAX executeQueries, apps, dashboards,
    gateways, admin APIs), 'storage' (OneLake DFS) o 'azure' (ARM, p.ej. capacidades).
    endpoint es relativo, p.ej. 'workspaces', 'groups/<id>/datasets/<id>/refreshes'.
    method: get|post|put|patch|delete. Con delete pide confirmación al usuario antes.
    """
    method = method.lower()
    if method not in ("get", "post", "put", "patch", "delete"):
        return {"ok": False, "error": "method inválido"}
    args = ["api", endpoint, "-X", method, "-A", audience]
    if body is not None:
        args += ["-i", body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))]
    if params:
        args += ["-P", ",".join(f"{k}={v}" for k, v in params.items())]
    if headers:
        args += ["-H", ",".join(f"{k}={v}" for k, v in headers.items())]
    if query:
        # fab aplica el JMESPath sobre {status_code, text, headers}: el cuerpo está en 'text'.
        if not re.match(r"^(text|status_code|headers)\b", query):
            query = f"text.{query}"
        args += ["-q", query]
    res = run_fab(args, timeout)
    data = res.get("result")
    if not query and isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) \
            and "status_code" in data[0]:
        res["status_code"] = data[0].get("status_code")
        res["result"] = data[0].get("text")
        if res["status_code"] and int(res["status_code"]) >= 400:
            res["ok"] = False
    return res


@mcp.tool(annotations=DESTRUCTIVE_HINT)
def fab_command(command: str, confirm: bool = False, json_output: bool = True,
                timeout: Optional[int] = None) -> dict[str, Any]:
    """
    Ejecuta cualquier comando de fab sin el prefijo 'fab', p.ej. 'ln /WS.Workspace/LH.Lakehouse/
    Tables/x.Shortcut --type oneLake --target ...', 'deploy --config deploy.yml',
    'bulk-export /WS.Workspace -o C:\\tmp', 'label set ...', 'config set default_capacity cap1'.
    Usa comillas dobles para argumentos con espacios. Comandos destructivos (rm, unassign) y los
    que llevan -f/--force en rm requieren confirm=True. No admite cd/open/auth login (interactivos).
    """
    try:
        args = shlex.split(command, posix=True)
    except ValueError as e:
        return {"ok": False, "error": f"comando mal citado: {e}"}
    if args and args[0] == "fab":
        args = args[1:]
    if not args:
        return {"ok": False, "error": "comando vacío"}
    verb = args[0].lower()
    if verb in INTERACTIVE or (verb == "auth" and len(args) > 1 and args[1] in ("login", "logout")):
        return {"ok": False, "error": f"'{verb}' es interactivo o de sesión: el usuario debe correrlo en su terminal"}
    if (verb in DESTRUCTIVE or (verb == "acl" and "rm" in args[1:2])
            or (verb == "job" and "run-rm" in args[1:2])) and not confirm:
        return {"ok": False, "error": f"'{verb}' es destructivo: requiere confirm=true tras confirmación del usuario"}
    if verb in DESTRUCTIVE and "-f" not in args and "--force" not in args:
        args.append("-f")
    return run_fab(args, timeout, json_output=json_output)


# ============================================================
# CLI
# ============================================================


def _print(obj: Any) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _handshake() -> int:
    from mcp.client import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            print(f"- {t.name}")
        print(f"{len(tools.tools)} tools")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Microsoft Fabric CLI MCP server")
    parser.add_argument("--status", action="store_true", help="Estado del login de fab y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.status:
        _print(auth_status())
        return 0
    if args.handshake:
        return asyncio.run(_handshake())

    logger.info(f"Fabric CLI MCP v{__version__} — fab: {FAB}")
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
