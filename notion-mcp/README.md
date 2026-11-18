# notion-mcp

Servidor MCP (Python, FastMCP) para Notion usando una *internal integration* y `notion-client`.

> Notion tiene un MCP oficial remoto. Este servidor es útil si prefieres un token local
> cuyo acceso se limita a las páginas que compartas explícitamente con la integración.

## Tools

`test_connection`, `search_pages`, `get_page`, `get_block_children`, `create_page`, `update_page`,
`search_databases`, `get_database`, `query_database`, `list_users`, `get_user`.

## Instalación

1. Crea una integración en <https://www.notion.so/my-integrations> y copia el token.
2. Comparte con la integración las páginas/bases que quieras exponer (menú `...` → *Connections*).
3. Configura y prueba:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
python test_notion.py
```

```json
{ "mcpServers": { "notion": {
  "command": "C:/path/to/notion-mcp/.venv/Scripts/python.exe",
  "args": ["C:/path/to/notion-mcp/notion_mcp_server.py"],
  "cwd": "C:/path/to/notion-mcp" } } }
```

El `.env` se lee del directorio de trabajo; por eso el `cwd`.
