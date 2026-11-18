#!/usr/bin/env python3
"""
Notion MCP Server
=================
MCP server personalizado para la API de Notion.

Funcionalidades:
- Leer páginas/bloques de Notion
- Buscar en bases de datos de Notion
- Crear nuevas páginas
- Actualizar páginas existentes
- Consultar bases de datos
- Obtener información de usuarios

Uso:
  pip install mcp notion-client python-dotenv
  python notion_mcp_server.py            # ejecutar como MCP server
  python notion_mcp_server.py --test     # probar conexión

Variables de entorno requeridas:
  NOTION_API_KEY        - API key de Notion (integration secret)

Opcionales:
  NOTION_DEFAULT_DATABASE_ID - ID de base de datos por defecto
  NOTION_DEFAULT_PAGE_ID     - ID de página por defecto
  NOTION_TIMEOUT_SECONDS     - Timeout para requests (default: 30)
  NOTION_MAX_RESULTS         - Máximo de resultados por query (default: 100)
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, date
from typing import Any, Optional, List, Dict

from dotenv import load_dotenv

load_dotenv()

try:
    from notion_client import Client
    from notion_client.errors import APIResponseError
    HAS_NOTION = True
except ImportError:
    HAS_NOTION = False

from mcp.server.fastmcp import FastMCP

# ============================================================
# Configuración
# ============================================================

class NotionConfig:
    def __init__(self):
        self.api_key = os.environ.get("NOTION_API_KEY", "")
        self.default_database_id = os.environ.get("NOTION_DEFAULT_DATABASE_ID", "")
        self.default_page_id = os.environ.get("NOTION_DEFAULT_PAGE_ID", "")
        self.timeout = int(os.environ.get("NOTION_TIMEOUT_SECONDS", "30"))
        self.max_results = int(os.environ.get("NOTION_MAX_RESULTS", "100"))
        
    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError("Variable de entorno NOTION_API_KEY es requerida")
        if not HAS_NOTION:
            raise RuntimeError("Paquete 'notion-client' no está instalado. Ejecuta: pip install notion-client")

CFG = NotionConfig()

# Logging
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("notion-mcp")

# ============================================================
# Cliente Notion
# ============================================================

class NotionClientWrapper:
    def __init__(self):
        self.client = None
        
    def _init_client(self):
        # Do not make MCP initialization depend on an external credential.
        # Codex can still discover the server and every tool will report a
        # clear configuration error until the credential is supplied.
        CFG.validate()
        self.client = Client(auth=CFG.api_key, timeout_seconds=CFG.timeout)
        
    def get_client(self):
        if not self.client:
            self._init_client()
        return self.client

NOTION = NotionClientWrapper()

# ============================================================
# Serialización de tipos
# ============================================================

def serialize_notion_object(obj: Any) -> Any:
    """Convierte objetos de Notion a JSON-friendly."""
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: serialize_notion_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialize_notion_object(item) for item in obj]
    if hasattr(obj, '__dict__'):
        return serialize_notion_object(obj.__dict__)
    return str(obj)

def simplify_page_properties(properties: Dict) -> Dict:
    """Simplifica las propiedades de una página de Notion a un formato más legible."""
    simplified = {}
    for prop_name, prop_value in properties.items():
        if not prop_value:
            simplified[prop_name] = None
            continue
            
        prop_type = prop_value.get('type')
        if not prop_type:
            simplified[prop_name] = prop_value
            continue
            
        # Extraer el valor según el tipo
        if prop_type == 'title':
            title_text = prop_value.get('title', [])
            if title_text:
                simplified[prop_name] = ''.join([t.get('plain_text', '') for t in title_text])
            else:
                simplified[prop_name] = ''
        elif prop_type == 'rich_text':
            rich_text = prop_value.get('rich_text', [])
            if rich_text:
                simplified[prop_name] = ''.join([t.get('plain_text', '') for t in rich_text])
            else:
                simplified[prop_name] = ''
        elif prop_type == 'number':
            simplified[prop_name] = prop_value.get('number')
        elif prop_type == 'select':
            simplified[prop_name] = prop_value.get('select', {}).get('name')
        elif prop_type == 'multi_select':
            items = prop_value.get('multi_select', [])
            simplified[prop_name] = [item.get('name') for item in items]
        elif prop_type == 'date':
            date_value = prop_value.get('date')
            if date_value:
                simplified[prop_name] = date_value.get('start')
        elif prop_type == 'people':
            people = prop_value.get('people', [])
            simplified[prop_name] = [p.get('name', p.get('id', '')) for p in people]
        elif prop_type == 'files':
            files = prop_value.get('files', [])
            simplified[prop_name] = [f.get('name', f.get('url', '')) for f in files]
        elif prop_type == 'checkbox':
            simplified[prop_name] = prop_value.get('checkbox', False)
        elif prop_type == 'url':
            simplified[prop_name] = prop_value.get('url')
        elif prop_type == 'email':
            simplified[prop_name] = prop_value.get('email')
        elif prop_type == 'phone_number':
            simplified[prop_name] = prop_value.get('phone_number')
        elif prop_type == 'formula':
            formula = prop_value.get('formula', {})
            formula_type = formula.get('type')
            if formula_type == 'string':
                simplified[prop_name] = formula.get('string')
            elif formula_type == 'number':
                simplified[prop_name] = formula.get('number')
            elif formula_type == 'boolean':
                simplified[prop_name] = formula.get('boolean')
            elif formula_type == 'date':
                simplified[prop_name] = formula.get('date')
            else:
                simplified[prop_name] = formula
        elif prop_type == 'relation':
            relations = prop_value.get('relation', [])
            simplified[prop_name] = [r.get('id') for r in relations]
        elif prop_type == 'rollup':
            rollup = prop_value.get('rollup', {})
            rollup_type = rollup.get('type')
            if rollup_type == 'array':
                simplified[prop_name] = rollup.get('array', [])
            elif rollup_type == 'number':
                simplified[prop_name] = rollup.get('number')
            elif rollup_type == 'date':
                simplified[prop_name] = rollup.get('date')
            else:
                simplified[prop_name] = rollup
        else:
            simplified[prop_name] = prop_value
            
    return simplified

# ============================================================
# MCP Server + Tools
# ============================================================

mcp = FastMCP("notion")

def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False)

def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

@mcp.tool()
def test_connection() -> str:
    """
    Prueba la conexión a la API de Notion.
    
    Returns:
        JSON con estado de conexión y información del bot/integration.
    """
    try:
        CFG.validate()
        client = NOTION.get_client()
        
        # Obtener información del bot
        bot_info = client.users.me()
        
        # Intentar listar algunas bases de datos para verificar permisos
        search_result = client.search(
            filter={"property": "object", "value": "database"},
            page_size=1
        )
        
        return _ok({
            "status": "connected",
            "bot": {
                "id": bot_info.get("id"),
                "name": bot_info.get("name"),
                "type": bot_info.get("type"),
                "avatar_url": bot_info.get("avatar_url"),
            },
            "databases_count": search_result.get("results", []),
            "api_key_configured": bool(CFG.api_key),
            "default_database_id": CFG.default_database_id or "No configurado",
            "default_page_id": CFG.default_page_id or "No configurado",
        })
    except RuntimeError as e:
        logger.warning("Notion no estÃ¡ configurado: %s", e)
        return _err(str(e), status="error")
    except Exception as e:
        logger.exception("Error en test_connection")
        return _err(str(e), status="error")

@mcp.tool()
def get_page(page_id: str) -> str:
    """
    Obtiene una página de Notion por su ID.
    
    Args:
        page_id: ID de la página de Notion.
        
    Returns:
        JSON con información detallada de la página.
    """
    try:
        client = NOTION.get_client()
        page = client.pages.retrieve(page_id=page_id)
        
        # Obtener bloques hijos si existen
        try:
            blocks = client.blocks.children.list(block_id=page_id)
            page["blocks"] = blocks.get("results", [])
        except Exception as e:
            logger.debug(f"No se pudieron obtener bloques hijos: {e}")
            page["blocks"] = []
        
        # Simplificar propiedades
        if "properties" in page:
            page["simplified_properties"] = simplify_page_properties(page["properties"])
        
        return _ok(serialize_notion_object(page))
    except APIResponseError as e:
        logger.error(f"API Error en get_page: {e}")
        return _err(f"API Error: {e}", page_id=page_id)
    except Exception as e:
        logger.exception("Error en get_page")
        return _err(str(e), page_id=page_id)

@mcp.tool()
def search_pages(query: str = "", max_results: int = 50) -> str:
    """
    Busca páginas en Notion.
    
    Args:
        query: Término de búsqueda (opcional).
        max_results: Máximo número de resultados (default: 50, max: 100).
        
    Returns:
        JSON con resultados de búsqueda.
    """
    try:
        max_results = min(max(1, int(max_results)), CFG.max_results)
        client = NOTION.get_client()
        
        filter_obj = {"property": "object", "value": "page"}
        if query:
            filter_obj = {"property": "object", "value": "page"}
            # Notion search usa el parámetro 'query' directamente
            results = client.search(
                query=query,
                filter=filter_obj,
                page_size=max_results
            )
        else:
            results = client.search(
                filter=filter_obj,
                page_size=max_results
            )
        
        pages = []
        for page in results.get("results", []):
            simplified = serialize_notion_object(page)
            if "properties" in page:
                simplified["simplified_properties"] = simplify_page_properties(page["properties"])
            pages.append(simplified)
        
        return _ok({
            "query": query,
            "total_results": results.get("total", len(pages)),
            "has_more": results.get("has_more", False),
            "next_cursor": results.get("next_cursor"),
            "pages": pages,
        })
    except Exception as e:
        logger.exception("Error en search_pages")
        return _err(str(e), query=query)

@mcp.tool()
def query_database(database_id: str, filter_obj: Optional[Dict] = None, 
                   sorts: Optional[List] = None, max_results: int = 100) -> str:
    """
    Consulta una base de datos de Notion.
    
    Args:
        database_id: ID de la base de datos.
        filter_obj: Filtro de consulta en formato Notion (opcional).
        sorts: Ordenamiento de resultados (opcional).
        max_results: Máximo número de resultados (default: 100).
        
    Returns:
        JSON con resultados de la consulta.
    """
    try:
        max_results = min(max(1, int(max_results)), CFG.max_results)
        client = NOTION.get_client()
        
        # Parsear filter_obj si es string
        if isinstance(filter_obj, str):
            try:
                filter_obj = json.loads(filter_obj)
            except json.JSONDecodeError:
                return _err("filter_obj debe ser un JSON válido o dict")
        
        # Parsear sorts si es string
        if isinstance(sorts, str):
            try:
                sorts = json.loads(sorts)
            except json.JSONDecodeError:
                return _err("sorts debe ser un JSON válido o lista")
        
        results = client.databases.query(
            database_id=database_id,
            filter=filter_obj,
            sorts=sorts,
            page_size=max_results
        )
        
        pages = []
        for page in results.get("results", []):
            simplified = serialize_notion_object(page)
            if "properties" in page:
                simplified["simplified_properties"] = simplify_page_properties(page["properties"])
            pages.append(simplified)
        
        return _ok({
            "database_id": database_id,
            "total_results": len(pages),
            "has_more": results.get("has_more", False),
            "next_cursor": results.get("next_cursor"),
            "pages": pages,
        })
    except APIResponseError as e:
        logger.error(f"API Error en query_database: {e}")
        return _err(f"API Error: {e}", database_id=database_id)
    except Exception as e:
        logger.exception("Error en query_database")
        return _err(str(e), database_id=database_id)

@mcp.tool()
def create_page(parent_id: str, properties: Dict, 
                children: Optional[List] = None) -> str:
    """
    Crea una nueva página en Notion.
    
    Args:
        parent_id: ID de la página padre o base de datos.
        properties: Propiedades de la página en formato Notion.
        children: Bloques hijos (opcional).
        
    Returns:
        JSON con la página creada.
    """
    try:
        client = NOTION.get_client()
        
        # Parsear properties si es string
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except json.JSONDecodeError:
                return _err("properties debe ser un JSON válido o dict")
        
        # Parsear children si es string
        if isinstance(children, str):
            try:
                children = json.loads(children)
            except json.JSONDecodeError:
                return _err("children debe ser un JSON válido o lista")
        
        # Determinar si parent es database o page
        parent_type = "database_id" if len(parent_id.replace('-', '')) == 32 else "page_id"
        
        page_data = {
            "parent": {parent_type: parent_id},
            "properties": properties
        }
        
        if children:
            page_data["children"] = children
        
        page = client.pages.create(**page_data)
        
        # Simplificar propiedades para respuesta
        simplified_page = serialize_notion_object(page)
        if "properties" in page:
            simplified_page["simplified_properties"] = simplify_page_properties(page["properties"])
        
        return _ok({
            "status": "created",
            "page_id": page.get("id"),
            "url": page.get("url"),
            "page": simplified_page,
        })
    except APIResponseError as e:
        logger.error(f"API Error en create_page: {e}")
        return _err(f"API Error: {e}", parent_id=parent_id)
    except Exception as e:
        logger.exception("Error en create_page")
        return _err(str(e), parent_id=parent_id)

@mcp.tool()
def update_page(page_id: str, properties: Optional[Dict] = None, 
                archived: Optional[bool] = None) -> str:
    """
    Actualiza una página existente en Notion.
    
    Args:
        page_id: ID de la página a actualizar.
        properties: Nuevas propiedades (opcional).
        archived: Si se debe archivar la página (opcional).
        
    Returns:
        JSON con la página actualizada.
    """
    try:
        client = NOTION.get_client()
        
        update_data = {}
        
        if properties is not None:
            # Parsear properties si es string
            if isinstance(properties, str):
                try:
                    properties = json.loads(properties)
                except json.JSONDecodeError:
                    return _err("properties debe ser un JSON válido o dict")
            update_data["properties"] = properties
        
        if archived is not None:
            update_data["archived"] = archived
        
        if not update_data:
            return _err("Debe proporcionar properties o archived para actualizar")
        
        page = client.pages.update(page_id=page_id, **update_data)
        
        # Simplificar propiedades para respuesta
        simplified_page = serialize_notion_object(page)
        if "properties" in page:
            simplified_page["simplified_properties"] = simplify_page_properties(page["properties"])
        
        return _ok({
            "status": "updated",
            "page_id": page.get("id"),
            "url": page.get("url"),
            "page": simplified_page,
        })
    except APIResponseError as e:
        logger.error(f"API Error en update_page: {e}")
        return _err(f"API Error: {e}", page_id=page_id)
    except Exception as e:
        logger.exception("Error en update_page")
        return _err(str(e), page_id=page_id)

@mcp.tool()
def get_database(database_id: str) -> str:
    """
    Obtiene información de una base de datos de Notion.
    
    Args:
        database_id: ID de la base de datos.
        
    Returns:
        JSON con información de la base de datos.
    """
    try:
        client = NOTION.get_client()
        database = client.databases.retrieve(database_id=database_id)
        
        # Simplificar propiedades
        if "properties" in database:
            database["simplified_properties"] = simplify_page_properties(database["properties"])
        
        return _ok(serialize_notion_object(database))
    except APIResponseError as e:
        logger.error(f"API Error en get_database: {e}")
        return _err(f"API Error: {e}", database_id=database_id)
    except Exception as e:
        logger.exception("Error en get_database")
        return _err(str(e), database_id=database_id)

@mcp.tool()
def search_databases(query: str = "", max_results: int = 50) -> str:
    """
    Busca bases de datos en Notion.
    
    Args:
        query: Término de búsqueda (opcional).
        max_results: Máximo número de resultados (default: 50, max: 100).
        
    Returns:
        JSON con resultados de búsqueda.
    """
    try:
        max_results = min(max(1, int(max_results)), CFG.max_results)
        client = NOTION.get_client()
        
        filter_obj = {"property": "object", "value": "database"}
        if query:
            results = client.search(
                query=query,
                filter=filter_obj,
                page_size=max_results
            )
        else:
            results = client.search(
                filter=filter_obj,
                page_size=max_results
            )
        
        databases = []
        for db in results.get("results", []):
            simplified = serialize_notion_object(db)
            if "properties" in db:
                simplified["simplified_properties"] = simplify_page_properties(db["properties"])
            databases.append(simplified)
        
        return _ok({
            "query": query,
            "total_results": results.get("total", len(databases)),
            "has_more": results.get("has_more", False),
            "next_cursor": results.get("next_cursor"),
            "databases": databases,
        })
    except Exception as e:
        logger.exception("Error en search_databases")
        return _err(str(e), query=query)

@mcp.tool()
def get_user(user_id: str) -> str:
    """
    Obtiene información de un usuario de Notion.
    
    Args:
        user_id: ID del usuario.
        
    Returns:
        JSON con información del usuario.
    """
    try:
        client = NOTION.get_client()
        user = client.users.retrieve(user_id=user_id)
        return _ok(serialize_notion_object(user))
    except APIResponseError as e:
        logger.error(f"API Error en get_user: {e}")
        return _err(f"API Error: {e}", user_id=user_id)
    except Exception as e:
        logger.exception("Error en get_user")
        return _err(str(e), user_id=user_id)

@mcp.tool()
def list_users(max_results: int = 100) -> str:
    """
    Lista usuarios en el workspace de Notion.
    
    Args:
        max_results: Máximo número de resultados (default: 100).
        
    Returns:
        JSON con lista de usuarios.
    """
    try:
        max_results = min(max(1, int(max_results)), CFG.max_results)
        client = NOTION.get_client()
        users = client.users.list(page_size=max_results)
        
        return _ok({
            "total_results": len(users.get("results", [])),
            "has_more": users.get("has_more", False),
            "next_cursor": users.get("next_cursor"),
            "users": serialize_notion_object(users.get("results", [])),
        })
    except Exception as e:
        logger.exception("Error en list_users")
        return _err(str(e))

@mcp.tool()
def get_block_children(block_id: str, max_results: int = 100) -> str:
    """
    Obtiene los bloques hijos de un bloque de Notion.
    
    Args:
        block_id: ID del bloque padre.
        max_results: Máximo número de resultados (default: 100).
        
    Returns:
        JSON con bloques hijos.
    """
    try:
        max_results = min(max(1, int(max_results)), CFG.max_results)
        client = NOTION.get_client()
        blocks = client.blocks.children.list(
            block_id=block_id,
            page_size=max_results
        )
        
        return _ok({
            "block_id": block_id,
            "total_results": len(blocks.get("results", [])),
            "has_more": blocks.get("has_more", False),
            "next_cursor": blocks.get("next_cursor"),
            "blocks": serialize_notion_object(blocks.get("results", [])),
        })
    except APIResponseError as e:
        logger.error(f"API Error en get_block_children: {e}")
        return _err(f"API Error: {e}", block_id=block_id)
    except Exception as e:
        logger.exception("Error en get_block_children")
        return _err(str(e), block_id=block_id)

# ============================================================
# Entry point + modo CLI
# ============================================================

def cli_test() -> int:
    """Modo CLI: prueba la conexión sin levantar el server MCP."""
    try:
        CFG.validate()
    except Exception as e:
        print(f"❌ Config inválida: {e}", file=sys.stderr)
        return 2
    
    print(f"Probando conexión a Notion API...")
    output = test_connection()
    print(output)
    try:
        parsed = json.loads(output)
        if parsed.get("status") == "connected":
            print("✅ Conexión OK")
            return 0
        print("❌ Conexión falló")
        return 1
    except Exception:
        return 1

def main() -> int:
    parser = argparse.ArgumentParser(description="Notion MCP Server")
    parser.add_argument("--test", action="store_true", help="Prueba la conexión y sale")
    args = parser.parse_args()
    
    if args.test:
        return cli_test()
    
    if not HAS_NOTION:
        logger.warning("notion-client no estÃ¡ instalado; las herramientas devolverÃ¡n un error de configuraciÃ³n")
    elif not CFG.api_key:
        logger.warning("NOTION_API_KEY no configurada; las herramientas devolverÃ¡n un error de configuraciÃ³n")
    
    logger.info("Iniciando Notion MCP Server")
    logger.info(f"  Bot configurado: {bool(CFG.api_key)}")
    logger.info(f"  Default database: {CFG.default_database_id or 'No configurado'}")
    logger.info(f"  Default page: {CFG.default_page_id or 'No configurado'}")
    
    mcp.run()
    return 0

if __name__ == "__main__":
    sys.exit(main())
