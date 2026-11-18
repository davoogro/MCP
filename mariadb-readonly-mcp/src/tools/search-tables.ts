import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionsField, resolveConnectionList, toLikePattern } from "./common.js";

export const searchTablesInputSchema = z.object({
  search: z.string().min(1).describe("Texto o patrón LIKE (%/_); busca en TABLE_NAME"),
  connections: connectionsField,
  limitPerConnection: z.number().int().positive().max(200).default(50),
  includeSystem: z.boolean().default(false),
});

export const searchTablesTool = {
  name: "search_tables",
  title: "Search tables across MariaDB connections",
  description:
    "Busca tablas y vistas por nombre en information_schema.TABLES en uno o todos los aliases (en paralelo). " +
    "Devuelve schema, tabla, tipo y filas estimadas por coincidencia.",
  inputSchema: searchTablesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ search, connections, limitPerConnection, includeSystem }: z.infer<typeof searchTablesInputSchema>) => {
      const aliases = resolveConnectionList(manager, connections);
      const pattern = toLikePattern(search);

      const results = await Promise.all(
        aliases.map(async (alias) => {
          try {
            const params: unknown[] = [pattern];
            let systemFilter = "";
            if (!includeSystem) {
              systemFilter = " AND TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')";
            }
            params.push(limitPerConnection);

            const data = await manager.withClient(alias, async (client) => {
              const [rows] = await client.query<RowDataPacket[]>(
                `
                  SELECT
                    TABLE_SCHEMA AS schema_name,
                    TABLE_NAME AS table_name,
                    TABLE_TYPE AS table_type,
                    ENGINE AS engine,
                    TABLE_ROWS AS estimated_rows
                  FROM information_schema.TABLES
                  WHERE TABLE_NAME LIKE ?
                    ${systemFilter}
                  ORDER BY TABLE_SCHEMA, TABLE_NAME
                  LIMIT ?
                `,
                params,
              );
              return rows.map((row) => ({
                schema: row.schema_name,
                table: row.table_name,
                tableType: row.table_type,
                engine: row.engine,
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
