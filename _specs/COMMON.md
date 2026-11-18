# Reglas comunes para mejorar un MCP existente (léelas antes que el IMPROVE.md)

Estás mejorando un servidor MCP que YA FUNCIONA y está registrado en Claude Code. No lo rompas.

1. Trabaja SOLO dentro de la carpeta del MCP indicada. No toques otras carpetas, `~/.claude.json`,
   `claude_desktop_config.json`, `~/.cursor/mcp.json` ni ningún `.env` con credenciales (puedes
   añadir claves NUEVAS a `.env.example`).
2. Mantén los nombres y parámetros de las tools existentes (compatibilidad). Puedes AÑADIR parámetros
   opcionales con default igual al comportamiento actual, salvo que el IMPROVE.md diga otra cosa.
3. Primero lee el código existente y copia su estilo (estructura, nombres, formato de respuesta
   `{"ok": ...}`, manejo de errores, `bound_response`/truncado, annotations, `instructions`).
4. Python: intérprete `C:\DVB\python\python.exe` (3.14), SDK `mcp` 2.0 (`from mcp.server import MCPServer`,
   NO existe `mcp.server.fastmcp`). Paquetes ya instalados disponibles: pyodbc, oracledb, boto3, duckdb,
   pyarrow, pyyaml, python-dotenv, httpx/requests. No instales nada nuevo sin necesidad; si lo haces,
   añádelo a `pyproject.toml` y dilo en DONE.md.
   Node/TS: `npm run build` y `npm test` deben pasar; el entry compilado sigue en `dist/src/index.js`.
5. Logging solo a stderr. Nada a stdout en modo servidor. Nunca depender del cwd.
6. Todas las tools nuevas: docstring/descripción clara en español (es lo que lee el LLM), annotations
   correctas (`readOnlyHint=True` en lecturas), y mencionarlas en las `instructions` del servidor y en el README.
7. Tests: añade tests unitarios (sin red) para lo nuevo; la suite completa debe quedar en verde.
   Puedes hacer pruebas EN VIVO de solo lectura contra los sistemas reales (la config/credenciales ya
   existen) para validar; nunca ejecutes escrituras contra sistemas reales.
8. Al terminar escribe `DONE.md` en la carpeta del MCP: cambios hechos por punto del IMPROVE.md, tools
   nuevas, resultado de tests (comando + resumen), pruebas en vivo hechas, y lo que NO pudiste hacer.
