import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { SYSTEM_SCHEMAS, connectionField, errorResult, toLikePattern } from "./common.js";

export const searchColumnsInputSchema = z.object({
  connection: connectionField,
  pattern: z
    .string()
    .min(1)
    .describe("Column name to search, case-insensitive. Plain text matches anywhere ('cust' -> '%cust%'); % and _ are LIKE wildcards"),
  schema: z
    .string()
    .min(1)
    .optional()
    .describe("Optional database (schema) to restrict the search; default: all non-system databases (slower on big servers)"),
  limit: z.number().int().positive().max(1000).default(200).describe("Maximum matches to return (default 200)"),
});

export const searchColumnsTool = {
  name: "search_columns",
  title: "Search MariaDB columns",
  description:
    "Find columns whose name matches a pattern across all databases on the server (or one database). Returns " +
    "database, table, column and type. Useful to locate where an entity or key (e.g. 'customer_id') lives.",
  inputSchema: searchColumnsInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, pattern, schema, limit }: z.infer<typeof searchColumnsInputSchema>) => {
      try {
        if (schema) {
          validateIdentifier(schema, "schema");
        }

        const scope = schema
          ? { clause: "c.TABLE_SCHEMA = ?", params: [schema] }
          : { clause: `c.TABLE_SCHEMA NOT IN (${SYSTEM_SCHEMAS.map(() => "?").join(", ")})`, params: [...SYSTEM_SCHEMAS] };

        const { rows } = await manager.query<RowDataPacket>(
          connection,
          `
            SELECT
              c.TABLE_SCHEMA AS table_schema,
              c.TABLE_NAME AS table_name,
              c.COLUMN_NAME AS column_name,
              c.COLUMN_TYPE AS data_type
            FROM information_schema.COLUMNS c
            WHERE c.COLUMN_NAME LIKE ?
              AND ${scope.clause}
            ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
            LIMIT ?
          `,
          [toLikePattern(pattern), ...scope.params, limit + 1],
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
