import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

const KEY_VARIABLES = [
  "version",
  "version_comment",
  "max_connections",
  "innodb_buffer_pool_size",
  "character_set_server",
  "collation_server",
  "sql_mode",
  "read_only",
  "super_read_only",
] as const;

export const serverInfoInputSchema = z.object({
  connection: connectionField,
  schemaTop: z.number().int().positive().max(100).default(30).describe("Cuántos schemas listar por tamaño"),
});

export const serverInfoTool = {
  name: "server_info",
  title: "MariaDB server summary",
  description:
    "Resumen del servidor: versión, uptime, variables globales clave y tamaño agregado por schema " +
    "(information_schema.TABLES). Complementa test_connection para diagnóstico de capacidad.",
  inputSchema: serverInfoInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schemaTop }: z.infer<typeof serverInfoInputSchema>) => {
      try {
        return await manager.withClient(connection, async (client) => {
          const [[versionRow]] = await client.query<RowDataPacket[]>(
            "SELECT VERSION() AS version, DATABASE() AS current_database, CURRENT_USER() AS connected_user",
          );

          const [statusRows] = await client.query<RowDataPacket[]>(
            "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Threads_connected', 'Threads_running', 'Queries', 'Slow_queries')",
          );
          const status: Record<string, string> = {};
          for (const row of statusRows) {
            status[String(row.Variable_name)] = String(row.Value);
          }

          const placeholders = KEY_VARIABLES.map(() => "?").join(", ");
          const [varRows] = await client.query<RowDataPacket[]>(
            `SHOW GLOBAL VARIABLES WHERE Variable_name IN (${placeholders})`,
            [...KEY_VARIABLES],
          );
          const variables: Record<string, string> = {};
          for (const row of varRows) {
            variables[String(row.Variable_name)] = String(row.Value);
          }

          const [schemaRows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                TABLE_SCHEMA AS schema_name,
                COUNT(*) AS table_count,
                SUM(COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) AS total_bytes,
                SUM(TABLE_ROWS) AS estimated_rows
              FROM information_schema.TABLES
              WHERE TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')
              GROUP BY TABLE_SCHEMA
              ORDER BY total_bytes DESC
              LIMIT ?
            `,
            [schemaTop],
          );

          const uptimeSeconds = Number(status.Uptime ?? 0);

          return jsonText({
            connection,
            version: versionRow?.version,
            currentDatabase: versionRow?.current_database,
            connectedUser: versionRow?.connected_user,
            uptimeSeconds,
            uptimeHuman: uptimeSeconds > 0 ? formatDuration(uptimeSeconds) : null,
            globalStatus: status,
            variables,
            schemasBySize: schemaRows.map((row) => ({
              schema: row.schema_name,
              tableCount: row.table_count,
              totalBytes: row.total_bytes,
              estimatedRows: row.estimated_rows,
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};

function formatDuration(totalSeconds: number): string {
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const parts: string[] = [];
  if (days > 0) {
    parts.push(`${days}d`);
  }
  if (hours > 0) {
    parts.push(`${hours}h`);
  }
  if (minutes > 0 || parts.length === 0) {
    parts.push(`${minutes}m`);
  }
  return parts.join(" ");
}
