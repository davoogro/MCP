import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField } from "./common.js";

export const tableStatsInputSchema = z.object({
  connection: connectionField,
  schema: schemaField.describe("Filtra a un schema; omitido = todas las bases accesibles excepto las del sistema opcional"),
  top: z.number().int().positive().max(500).default(50).describe("Máximo de tablas a devolver, ordenadas por tamaño"),
  includeSystem: z.boolean().default(false).describe("Incluir information_schema, mysql, performance_schema y sys"),
});

export const tableStatsTool = {
  name: "table_stats",
  title: "MariaDB table size statistics",
  description:
    "Lista tablas desde information_schema.TABLES con filas estimadas, data_length, index_length, data_free, engine " +
    "y create/update_time, ordenadas por tamaño (data+índices) descendente. Útil para encontrar tablas grandes.",
  inputSchema: tableStatsInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, top, includeSystem }: z.infer<typeof tableStatsInputSchema>) => {
      try {
        const params: unknown[] = [];
        let where = "TABLE_TYPE = 'BASE TABLE'";
        if (schema) {
          const resolved = resolveSchema(manager, connection, schema);
          where += " AND TABLE_SCHEMA = ?";
          params.push(resolved);
        } else if (!includeSystem) {
          where += " AND TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')";
        }
        params.push(top);

        return await manager.withClient(connection, async (client) => {
          const [rows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                TABLE_SCHEMA AS schema_name,
                TABLE_NAME AS table_name,
                ENGINE AS engine,
                TABLE_ROWS AS estimated_rows,
                DATA_LENGTH AS data_length,
                INDEX_LENGTH AS index_length,
                DATA_FREE AS data_free,
                (COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) AS total_bytes,
                CREATE_TIME AS create_time,
                UPDATE_TIME AS update_time,
                NULLIF(TABLE_COMMENT, '') AS comment
              FROM information_schema.TABLES
              WHERE ${where}
              ORDER BY total_bytes DESC, TABLE_SCHEMA, TABLE_NAME
              LIMIT ?
            `,
            params,
          );

          return jsonText({
            connection,
            schema: schema ?? null,
            tableCount: rows.length,
            tables: rows.map((row) => ({
              schema: row.schema_name,
              table: row.table_name,
              engine: row.engine,
              estimatedRows: row.estimated_rows,
              dataLength: row.data_length,
              indexLength: row.index_length,
              dataFree: row.data_free,
              totalBytes: row.total_bytes,
              createTime: row.create_time,
              updateTime: row.update_time,
              ...(row.comment ? { comment: row.comment } : {}),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
