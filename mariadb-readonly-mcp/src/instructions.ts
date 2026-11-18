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
    `Read-only SQL access to ${connections.length} MariaDB/MySQL connection(s). Every tool takes a 'connection' alias:`,
    ...listed,
    "",
    "Recommended flow: list_connections -> list_databases (or list_schemas) -> list_tables -> describe_table -> sample_data / execute_query.",
    "Diagnostics: explain_query, table_stats, list_indexes, get_definition, list_routines, get_activity, server_info.",
    "Multi-alias: query_all (same SQL), search_tables (by name). Large extracts: export_query -> CSV under C:\\DVB\\exports\\mariadb\\.",
    "In MariaDB schema = database. One alias reaches every database on its server: pass 'schema' to list_tables, describe_table,",
    "sample_data and search_columns, pass 'database' to execute_query, or qualify names as db.table. Use search_columns to find",
    "columns by name.",
    "",
    "SQL dialect: MariaDB. Use LIMIT n [OFFSET m] (or LIMIT m, n), backticks for unusual identifiers, and single quotes for strings.",
    "execute_query accepts one statement: SELECT, WITH, SHOW, DESCRIBE/DESC, EXPLAIN or VALUES. Optional timeoutMs (cap 300000) and",
    "maxRows (cap 10000; defaults from config). SELECT results use streaming row caps (truncated after maxRows+1). Writes, DDL,",
    "SELECT ... INTO, FOR UPDATE / LOCK IN SHARE MODE, LOAD_FILE/SLEEP/BENCHMARK/GET_LOCK and /*! */ comments are rejected.",
    "Long cell values are shortened; aggregate or filter instead of paging huge tables. DATE/DATETIME as stored; BIGINT/DECIMAL as strings.",
  ].join("\n");
}
