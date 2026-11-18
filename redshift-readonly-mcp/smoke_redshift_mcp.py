"""Read-only Redshift smoke test through the actual MCP stdio protocol."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parent
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(root / (sys.argv[1] if len(sys.argv) > 1 else "redshift_mcp_server.py"))],
        cwd=str(root),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"test_connection", "run_query", "list_tables", "describe_table"} <= names
            print(json.dumps({"test": "mcp_protocol", "status": "ok", "tools": sorted(names)}), flush=True)

            async def call(name, args):
                reply = await session.call_tool(name, args)
                assert not reply.isError, reply
                payload = json.loads(reply.content[0].text)
                print(json.dumps({"test": name, "result": payload}, ensure_ascii=True), flush=True)
                return payload

            rejected = await call("run_query", {"sql": "SELECT 1; SELECT 2", "timeout_s": 15})
            assert rejected.get("error_type") == "ReadOnlyViolation"

            connected = await call("test_connection", {})
            if "error" in connected:
                raise RuntimeError("Redshift connection failed; see test_connection result above")
            assert connected["row_count"] == 1

            data = await call("run_query", {
                "sql": "SELECT 1 AS n, 'mcp-ok' AS message, CAST(NULL AS INTEGER) AS empty_value UNION ALL SELECT 2, 'mcp-ok', NULL UNION ALL SELECT 3, 'mcp-ok', NULL ORDER BY n",
                "max_rows": 2, "timeout_s": 30,
            })
            assert "error" not in data, data
            assert data["row_count"] == 2 and data["truncated"] is True
            assert data["rows"][0] == {"n": 1, "message": "mcp-ok", "empty_value": None}

            schemas = await call("run_query", {
                "sql": "SELECT DISTINCT table_schema FROM svv_tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema LIMIT 10",
                "max_rows": 10, "timeout_s": 30,
            })
            assert "error" not in schemas, schemas
            # Pick one accessible table without reading business records.
            table = await call("run_query", {
                "sql": "SELECT table_schema, table_name FROM svv_tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema, table_name LIMIT 1",
                "max_rows": 1, "timeout_s": 30,
            })
            assert "error" not in table, table
            if table["rows"]:
                selected = table["rows"][0]
                listed = await call("list_tables", {"schema": selected["table_schema"], "pattern": selected["table_name"]})
                assert "error" not in listed and listed["row_count"] > 0, listed
                described = await call("describe_table", {"schema": selected["table_schema"], "table": selected["table_name"]})
                assert "error" not in described and described["row_count"] > 0, described
            else:
                print(json.dumps({"test": "table_metadata", "status": "skipped", "reason": "No accessible tables"}), flush=True)
            print(json.dumps({"status": "passed"}), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=150))
