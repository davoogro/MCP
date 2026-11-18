import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

const MAX_QUERY_CHARS = 500;

export const getActivityInputSchema = z.object({
  connection: connectionField,
  includeSleep: z
    .boolean()
    .default(true)
    .describe("Incluir sesiones en estado Sleep; false deja solo consultas activas o bloqueadas"),
});

function trimQuery(text: unknown): string | null {
  if (text === null || text === undefined) {
    return null;
  }
  const s = String(text);
  if (s.length <= MAX_QUERY_CHARS) {
    return s;
  }
  return `${s.slice(0, MAX_QUERY_CHARS)}... [truncated, ${s.length} chars total]`;
}

export const getActivityTool = {
  name: "get_activity",
  title: "MariaDB session activity",
  description:
    "Resumen de SHOW FULL PROCESSLIST (o equivalente): id, usuario, host, db, comando, tiempo, estado y texto " +
    "de consulta recortado. Útil para ver cargas activas y consultas lentas en la instancia.",
  inputSchema: getActivityInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, includeSleep }: z.infer<typeof getActivityInputSchema>) => {
      try {
        return await manager.withClient(connection, async (client) => {
          let rows: RowDataPacket[];
          try {
            const [result] = await client.query<RowDataPacket[]>("SHOW FULL PROCESSLIST");
            rows = result;
          } catch {
            const [result] = await client.query<RowDataPacket[]>(
              `
                SELECT
                  ID AS Id,
                  USER AS User,
                  HOST AS Host,
                  DB AS db,
                  COMMAND AS Command,
                  TIME AS Time,
                  STATE AS State,
                  INFO AS Info
                FROM information_schema.PROCESSLIST
              `,
            );
            rows = result;
          }

          const filtered = includeSleep ? rows : rows.filter((row) => String(row.Command ?? row.COMMAND) !== "Sleep");

          return jsonText({
            connection,
            sessionCount: filtered.length,
            sessions: filtered.map((row) => ({
              id: row.Id ?? row.ID,
              user: row.User ?? row.USER,
              host: row.Host ?? row.HOST,
              database: row.db ?? row.DB ?? null,
              command: row.Command ?? row.COMMAND,
              timeSeconds: row.Time ?? row.TIME,
              state: row.State ?? row.STATE ?? null,
              query: trimQuery(row.Info ?? row.INFO),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
