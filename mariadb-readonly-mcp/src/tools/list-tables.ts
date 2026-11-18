import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField, toLikePattern } from "./common.js";

export const listTablesInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  pattern: z
    .string()
    .min(1)
    .optional()
    .describe("Optional case-insensitive filter on the table name (LIKE; plain text matches anywhere)"),
  limit: z.number().int().positive().max(5000).default(1000).describe("Maximum number of objects to return"),
});

export const listTablesTool = {
  name: "list_tables",
  title: "List MariaDB tables",
  description:
    "List tables and views in a database (schema; defaults to the alias database) with engine, approximate row " +
    "count and comment. Next step: describe_table or sample_data.",
  inputSchema: listTablesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, pattern, limit }: z.infer<typeof listTablesInputSchema>) => {
      try {
        const resolvedSchema = resolveSchema(manager, connection, schema);

        const { rows } = await manager.query<RowDataPacket>(
          connection,
          `
            SELECT
              TABLE_SCHEMA AS table_schema,
              TABLE_NAME AS table_name,
              TABLE_TYPE AS table_type,
              ENGINE AS engine,
              TABLE_ROWS AS estimated_rows,
              NULLIF(TABLE_COMMENT, '') AS comment
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = ?
              AND TABLE_TYPE IN ('BASE TABLE', 'VIEW', 'SYSTEM VIEW')
              AND (? IS NULL OR TABLE_NAME LIKE ?)
            ORDER BY TABLE_NAME
            LIMIT ?
          `,
          [resolvedSchema, pattern ?? null, pattern ? toLikePattern(pattern) : null, limit + 1],
        );

        const truncated = rows.length > limit;
        return jsonText({
          connection,
          schema: resolvedSchema,
          tables: truncated ? rows.slice(0, limit) : rows,
          truncated,
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
