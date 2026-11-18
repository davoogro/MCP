import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, quoteIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, schemaField, toLikePattern } from "./common.js";

export const listTablesInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  pattern: z
    .string()
    .min(1)
    .optional()
    .describe("Optional case-insensitive filter on the table name (ILIKE; plain text matches anywhere)"),
  includePartitions: z
    .boolean()
    .default(false)
    .describe("Include individual partitions of partitioned tables (default false)"),
  limit: z.number().int().positive().max(5000).default(1000).describe("Maximum number of objects to return"),
});

export const listTablesTool = {
  name: "list_tables",
  title: "List PostgreSQL tables",
  description:
    "List tables, partitioned tables, views, materialized views and foreign tables in a schema, with planner row " +
    "estimates, whether the user can SELECT, and the object comment. Next step: describe_table or sample_data.",
  inputSchema: listTablesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, pattern, includePartitions, limit }: z.infer<typeof listTablesInputSchema>) => {
      try {
        validateIdentifier(schema, "schema");

        const { rows } = await manager.query<{
          table_schema: string;
          table_name: string;
          table_type: string;
          estimated_rows: number | null;
          can_select: boolean;
          comment: string | null;
        }>(
          connection,
          `
            SELECT
              n.nspname AS table_schema,
              c.relname AS table_name,
              CASE c.relkind
                WHEN 'r' THEN 'BASE TABLE'
                WHEN 'p' THEN 'PARTITIONED TABLE'
                WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW'
                WHEN 'f' THEN 'FOREIGN TABLE'
              END AS table_type,
              CASE WHEN c.relkind IN ('r', 'm') AND c.reltuples >= 0 THEN round(c.reltuples)::float8 END AS estimated_rows,
              has_table_privilege(c.oid, 'SELECT') AS can_select,
              obj_description(c.oid, 'pg_class') AS comment
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND ($2::boolean OR NOT c.relispartition)
              AND ($3::text IS NULL OR c.relname ILIKE $3)
            ORDER BY c.relname
            LIMIT $4
          `,
          [schema, includePartitions, pattern ? toLikePattern(pattern) : null, limit + 1],
        );

        const truncated = rows.length > limit;
        return jsonText({
          connection,
          schema,
          tables: truncated ? rows.slice(0, limit) : rows,
          truncated,
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};

export { quoteIdentifier };
