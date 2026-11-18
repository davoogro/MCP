import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

const KEY_SETTINGS = [
  "max_connections",
  "shared_buffers",
  "work_mem",
  "maintenance_work_mem",
  "effective_cache_size",
  "random_page_cost",
  "default_transaction_read_only",
  "statement_timeout",
  "idle_in_transaction_session_timeout",
  "timezone",
  "server_version",
  "server_encoding",
] as const;

export const serverInfoInputSchema = z.object({
  connection: connectionField,
});

export const serverInfoTool = {
  name: "server_info",
  title: "PostgreSQL server summary",
  description:
    "Versión, uptime (pg_postmaster_start_time), tamaño de la base actual y de otras bases en el cluster, " +
    "extensiones instaladas y parámetros clave de pg_settings.",
  inputSchema: serverInfoInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection }: z.infer<typeof serverInfoInputSchema>) => {
      try {
        return await manager.withClient(connection, async (client) => {
          const versionResult = await client.query(`
            SELECT
              version() AS full_version,
              current_database() AS current_database,
              current_user AS connected_user,
              pg_postmaster_start_time() AS postmaster_start,
              EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())) AS uptime_seconds
          `);
          const versionRow = versionResult.rows[0];

          const dbSizes = await client.query(`
            SELECT datname AS database_name, pg_database_size(datname) AS size_bytes
            FROM pg_database
            WHERE datallowconn
            ORDER BY pg_database_size(datname) DESC
            LIMIT 50
          `);

          const extensions = await client.query(`
            SELECT extname AS name, extversion AS version
            FROM pg_extension
            ORDER BY extname
          `);

          const placeholders = KEY_SETTINGS.map((_, i) => `$${i + 1}`).join(", ");
          const settingsResult = await client.query(
            `SELECT name, setting, unit FROM pg_settings WHERE name IN (${placeholders}) ORDER BY name`,
            [...KEY_SETTINGS],
          );
          const settings: Record<string, { value: string; unit: string | null }> = {};
          for (const row of settingsResult.rows) {
            settings[String(row.name)] = {
              value: String(row.setting),
              unit: row.unit ? String(row.unit) : null,
            };
          }

          const uptimeSeconds = Number(versionRow?.uptime_seconds ?? 0);

          return jsonText({
            connection,
            version: versionRow?.full_version,
            currentDatabase: versionRow?.current_database,
            connectedUser: versionRow?.connected_user,
            postmasterStart: versionRow?.postmaster_start,
            uptimeSeconds,
            uptimeHuman: uptimeSeconds > 0 ? formatDuration(uptimeSeconds) : null,
            currentDatabaseSizeBytes: dbSizes.rows.find(
              (r) => r.database_name === versionRow?.current_database,
            )?.size_bytes,
            databases: dbSizes.rows.map((row) => ({
              name: row.database_name,
              sizeBytes: row.size_bytes,
            })),
            extensions: extensions.rows.map((row) => ({
              name: row.name,
              version: row.version,
            })),
            settings,
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
