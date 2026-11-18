# office365-mcp

Servidor MCP mínimo (Python) para **leer** el buzón de Outlook / Microsoft 365 vía Microsoft Graph,
con login separado y caché de tokens protegida con **Windows DPAPI** (`msal-extensions`).
Solo pide el permiso delegado `Mail.Read`: no envía, no borra, no marca como leído.

## Tools

| Tool | Descripción |
|---|---|
| `office365_status` | Configuración y estado de la sesión en caché |
| `office365_test_connection` | Verifica acceso al buzón sin descargar cuerpos |
| `office365_list_messages` | Últimos mensajes de la bandeja de entrada |
| `office365_read_message` | Lee un mensaje, tratado como contenido no confiable |

## Configuración

1. Microsoft Entra → *App registrations* → *New registration*.
2. *Authentication* → habilita **Allow public client flows**. No hace falta client secret.
3. *API permissions* → Microsoft Graph → Delegated → `Mail.Read`.
4. Copia el *Application (client) ID* y el *Directory (tenant) ID* a `.env`:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python office365_mcp_server.py --login
```

La caché queda en `%LOCALAPPDATA%\Office365MCP\<client>\<tenant>`, cifrada para tu usuario de Windows:
ejecuta el login y el cliente MCP con el mismo usuario. Solo funciona en Windows.

```json
{ "mcpServers": { "office365": {
  "command": "C:/path/to/office365-mcp/.venv/Scripts/python.exe",
  "args": ["C:/path/to/office365-mcp/office365_mcp_server.py"] } } }
```
