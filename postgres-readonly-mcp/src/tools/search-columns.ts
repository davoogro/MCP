import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, toLikePattern } from "./common.js";

export const searchColumnsInputSchema = z.object({
  connection: connectionField,
  pattern: z
    .string()
    .min(1)
    .describe("Column name to search, case-insensitive. Plain text matches anywhere ('cust' -> '%cust%'); % and _ are ILIKE wildcards"),
  schema: z.string().min(1).optional().describe("Optional schema to restrict the search (default: all non-system schemas)"),
  limit: z.number().int().positive().max(1000).default(200).describe("Maximum matches to return (default 200)"),
});

export const searchColumnsTool = {
  name: "search_columns",
  title: "Search PostgreSQL columns",
  description:
    "Find columns whose name matches a pattern across all schemas (or one schema) of the alias database. Returns " +
    "schema, table, column, type and object type. Useful to locate where an entity or key (e.g. 'customer_id') lives.",
  inputSchema: searchColumnsInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, pattern, schema, limit }: z.infer<typeof searchColumnsInputSchema>) => {
      try {
        if (schema) {
          validateIdentifier(schema, "schema");
        }

        const { rows } = await manager.query<{
          table_schema: string;
          table_name: string;
          column_name: string;
          data_type: string;
          object_type: string;
        }>(
          connection,
          `
            SELECT
              n.nspname AS table_schema,
              c.relname AS table_name,
              a.attname AS column_name,
              pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
              CASE c.relkind
                WHEN 'r' THEN 'table'
                WHEN 'p' THEN 'partitioned_table'
                WHEN 'v' THEN 'view'
                WHEN 'm' THEN 'materialized_view'
                WHEN 'f' THEN 'foreign_table'
              END AS object_type
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE a.attnum > 0
              AND NOT a.attisdropped
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND NOT c.relispartition
              AND a.attname ILIKE $1
              AND ($2::text IS NULL OR n.nspname = $2)
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND n.nspname NOT LIKE 'pg_toast_temp_%'
            ORDER BY n.nspname, c.relname, a.attnum
            LIMIT $3
          `,
          [toLikePattern(pattern), schema ?? null, limit + 1],
        );

        const truncated = rows.length > limit;
        return jsonText({
          connection,
          pattern,
          schema: schema ?? null,
          matches: truncated ? rows.slice(0, limit) : rows,
          truncated,
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
