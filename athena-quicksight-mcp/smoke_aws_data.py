"""Read-only live smoke test. Never prints credentials, object contents or SQL text."""

import json
from urllib.parse import urlparse

import athena_mcp_server as server


def tool(name: str, **kwargs):
    return json.loads(server.mcp._tool_manager.get_tool(name).fn(**kwargs))


def status(value):
    return value.get("error", "ok") if isinstance(value, dict) else "ok"


def main():
    identity = tool("aws_identity")
    print("identity:", {k: identity.get(k) for k in ("account", "arn", "region")} if "account" in identity else identity)

    for name in ("athena_list_workgroups", "s3_list_buckets", "glue_list_databases"):
        result = tool(name, **({"limit": 10} if name != "s3_list_buckets" else {}))
        print(name + ":", {"status": status(result), "count": len(result) if isinstance(result, list) else None})

    databases = tool("glue_list_databases", limit=1)
    if isinstance(databases, list) and databases:
        tables = tool("glue_list_tables", database=databases[0], limit=1)
        print("glue_first_database_tables:", {"status": status(tables),
                                               "count": len(tables) if isinstance(tables, list) else None})
        if isinstance(tables, list) and tables:
            table = tool("glue_get_table", database=databases[0], table=tables[0]["name"])
            print("glue_first_table_metadata:", {"status": status(table), "has_schema": "StorageDescriptor" in table})

    location = urlparse(server.ATHENA_OUTPUT_S3)
    if location.scheme == "s3" and location.netloc:
        objects = tool("s3_list_objects", bucket=location.netloc, prefix=location.path.lstrip("/"), limit=1)
        print("s3_output_prefix:", {"status": status(objects), "count": len(objects) if isinstance(objects, list) else None})

    query = json.loads(server.query_athena("SELECT 1 AS smoke_ok", max_rows=1))
    print("athena_select_1:", {"status": status(query), "rows": query.get("row_count"),
                                "query_execution_id": query.get("query_execution_id")})

    started = tool("athena_start_query", sql="SELECT 1 AS smoke_ok", dry_run=False)
    if "query_execution_id" in started:
        execution_id = started["query_execution_id"]
        waited = tool("athena_wait_query", query_execution_id=execution_id, timeout_seconds=60)
        results = tool("athena_get_results", query_execution_id=execution_id, max_rows=10) if waited.get("state") == "SUCCEEDED" else {}
        print("athena_async_select_1:", {"state": waited.get("state"), "rows": len(results.get("rows", [])),
                                          "results_status": status(results)})
    else:
        print("athena_async_select_1:", started)


if __name__ == "__main__":
    main()
