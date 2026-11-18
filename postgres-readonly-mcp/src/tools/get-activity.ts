import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

const MAX_QUERY_CHARS = 500;

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

export const getActivityInputSchema = z.object({
  connection: connectionField,
  includeIdle: z
    .boolean()
    .default(false)
    .describe("Incluir sesiones idle; false deja activas, waiting y otras no idle"),
});

export const getActivityTool = {
  name: "get_activity",
  title: "PostgreSQL session activity",
  description:
    "Resumen de pg_stat_activity en la base actual: pid, usuario, aplicación, estado, wait_event, duración y " +
    "texto de consulta recortado. Útil para cargas activas y consultas lentas.",
  inputSchema: getActivityInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, includeIdle }: z.infer<typeof getActivityInputSchema>) => {
      try {
        return await manager.withClient(connection, async (client) => {
          const result = await client.query(
            `
              SELECT
                pid,
                usename,
                application_name,
                client_addr::text AS client_addr,
                state,
                wait_event_type,
                wait_event,
                backend_type,
                query_start,
                state_change,
                EXTRACT(EPOCH FROM (now() - query_start)) AS duration_seconds,
                LEFT(query, 2000) AS query_text
              FROM pg_stat_activity
              WHERE datname = current_database()
                AND pid <> pg_backend_pid()
                AND ($1::boolean OR state IS DISTINCT FROM 'idle')
              ORDER BY query_start NULLS LAST, pid
              LIMIT 200
            `,
            [includeIdle],
          );

          return jsonText({
            connection,
            sessionCount: result.rows.length,
            sessions: result.rows.map((row) => ({
              pid: row.pid,
              user: row.usename,
              application: row.application_name,
              clientAddr: row.client_addr,
              state: row.state,
              waitEventType: row.wait_event_type,
              waitEvent: row.wait_event,
              backendType: row.backend_type,
              queryStart: row.query_start,
              stateChange: row.state_change,
              durationSeconds: row.duration_seconds,
              query: trimQuery(row.query_text),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};

export const getLocksInputSchema = z.object({
  connection: connectionField,
});

export const getLocksTool = {
  name: "get_locks",
  title: "PostgreSQL lock waits",
  description:
    "Bloqueos desde pg_locks/pg_stat_activity: relaciones bloqueadas, modo, sesiones bloqueante y bloqueada " +
    "con consulta recortada. Solo lectura.",
  inputSchema: getLocksInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection }: z.infer<typeof getLocksInputSchema>) => {
      try {
        return await manager.withClient(connection, async (client) => {
          const result = await client.query(`
            SELECT
              blocked_locks.pid AS blocked_pid,
              blocked_activity.usename AS blocked_user,
              LEFT(blocked_activity.query, 500) AS blocked_query,
              blocking_locks.pid AS blocking_pid,
              blocking_activity.usename AS blocking_user,
              LEFT(blocking_activity.query, 500) AS blocking_query,
              blocked_locks.mode AS blocked_mode,
              blocking_locks.mode AS blocking_mode,
              COALESCE(blocked_class.relname, blocked_locks.locktype) AS locked_object
            FROM pg_catalog.pg_locks blocked_locks
            JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
            JOIN pg_catalog.pg_locks blocking_locks
              ON blocking_locks.locktype = blocked_locks.locktype
              AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
              AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
              AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
              AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
              AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
              AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
              AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
              AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
              AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
              AND blocking_locks.pid <> blocked_locks.pid
            JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
            LEFT JOIN pg_catalog.pg_class blocked_class ON blocked_class.oid = blocked_locks.relation
            WHERE NOT blocked_locks.granted
            LIMIT 100
          `);

          return jsonText({
            connection,
            lockWaitCount: result.rows.length,
            locks: result.rows.map((row) => ({
              blockedPid: row.blocked_pid,
              blockedUser: row.blocked_user,
              blockedQuery: trimQuery(row.blocked_query),
              blockingPid: row.blocking_pid,
              blockingUser: row.blocking_user,
              blockingQuery: trimQuery(row.blocking_query),
              blockedMode: row.blocked_mode,
              blockingMode: row.blocking_mode,
              lockedObject: row.locked_object,
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
