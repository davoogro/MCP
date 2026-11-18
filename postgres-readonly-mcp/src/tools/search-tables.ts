import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionsField, resolveConnectionList, toLikePattern } from "./common.js";

export const searchTablesInputSchema = z.object({
  search: z.string().min(1).describe("Texto o patrón ILIKE (%/_); busca en table_name"),
  connections: connectionsField,
  limitPerConnection: z.number().int().positive().max(200).default(50),
});

export const searchTablesTool = {
  name: "search_tables",
  title: "Search tables across PostgreSQL connections",
  description:
    "Busca tablas, vistas y vistas materializadas por nombre en pg_catalog/pg_class (en paralelo en uno o todos los aliases). " +
    "Devuelve schema, nombre, tipo y filas estimadas (pg_stat_user_tables si existe).",
  inputSchema: searchTablesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ search, connections, limitPerConnection }: z.infer<typeof searchTablesInputSchema>) => {
      const aliases = resolveConnectionList(manager, connections);
      const pattern = toLikePattern(search);

      const results = await Promise.all(
        aliases.map(async (alias) => {
          try {
            const data = await manager.withClient(alias, async (client) => {
              const result = await client.query(
                `
                  SELECT
                    n.nspname AS schema_name,
                    c.relname AS table_name,
                    CASE c.relkind
                      WHEN 'r' THEN 'table'
                      WHEN 'p' THEN 'partitioned_table'
                      WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized_view'
                      WHEN 'f' THEN 'foreign_table'
                      ELSE c.relkind::text
                    END AS object_type,
                    st.n_live_tup AS estimated_rows
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid
                  WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                    AND n.nspname NOT LIKE 'pg_%'
                    AND n.nspname <> 'information_schema'
                    AND c.relname ILIKE $1
                  ORDER BY n.nspname, c.relname
                  LIMIT $2
                `,
                [pattern, limitPerConnection],
              );
              return result.rows.map((row) => ({
                schema: row.schema_name,
                table: row.table_name,
                objectType: row.object_type,
                estimatedRows: row.estimated_rows,
              }));
            });

            return { connection: alias, ok: true as const, matchCount: data.length, matches: data };
          } catch (error) {
            return {
              connection: alias,
              ok: false as const,
              error: error instanceof Error ? error.message : String(error),
            };
          }
        }),
      );

      return jsonText({
        search,
        connectionCount: aliases.length,
        results,
      });
    },
};
