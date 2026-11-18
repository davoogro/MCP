import type { AppConfig } from "./types.js";

const MAX_LISTED_ALIASES = 40;

// Server-level instructions sent during the MCP handshake so the client knows the aliases up front.
export function buildServerInstructions(appConfig: AppConfig): string {
  const connections = [...appConfig.connections.values()].sort((a, b) => a.alias.localeCompare(b.alias));
  const listed = connections.slice(0, MAX_LISTED_ALIASES).map((connection) => {
    const target = `${connection.host}:${connection.port}/${connection.database}`;
    const description = connection.description ? ` - ${connection.description}` : "";
    const mode = connection.readonly ? "read-only" : "read-write";
    return `- ${connection.alias}: ${target} (${mode}, maxRows ${connection.maxRows})${description}`;
  });

  if (connections.length > MAX_LISTED_ALIASES) {
    listed.push(`- ... and ${connections.length - MAX_LISTED_ALIASES} more (call list_connections)`);
  }

  return [
    `Read-only SQL access to ${connections.length} PostgreSQL connection(s). Every tool takes a 'connection' alias:`,
    ...listed,
    "",
    "Recommended flow: list_connections -> list_schemas -> list_tables -> describe_table -> sample_data / execute_query.",
    "Use search_columns to find columns by name across schemas, and list_databases to see the other databases on the same server",
    "(PostgreSQL cannot query across databases: each database needs its own alias in config/databases.yaml).",
    "Diagnostics: explain_query, table_stats, list_indexes, get_definition, list_functions, get_activity, get_locks, server_info.",
    "Multi-alias: query_all (same SQL), search_tables (by name). Large extracts: export_query -> CSV under C:\\DVB\\exports\\postgres\\.",
    "",
    "SQL dialect: PostgreSQL. Use LIMIT n [OFFSET m] (or FETCH FIRST n ROWS ONLY), ILIKE for case-insensitive matching,",
    "double quotes for mixed-case identifiers, and schema-qualified names (schema.table). The default schema is usually 'public'.",
    "execute_query accepts one statement: SELECT, WITH, EXPLAIN [ANALYZE], SHOW, TABLE or VALUES. Optional timeoutMs (cap 300000)",
    "and maxRows (cap 10000). When truncated, returns cursor/nextOffset — use fetch_more to page (server cursor, TTL 10 min).",
    "Writes, DDL, SELECT INTO, FOR UPDATE/SHARE and side-effect functions (pg_sleep, set_config, dblink, ...) are rejected.",
    "Results cap row count and shorten long cells; aggregate or filter instead of paging huge tables when possible.",
    "YAML readonly:false is ignored with a stderr warning: this MCP always enforces read-only SQL and transactions.",
  ].join("\n");
}
