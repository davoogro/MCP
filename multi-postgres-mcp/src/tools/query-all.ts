import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { sanitizeErrorMessage, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionsField, resolveConnectionList } from "./common.js";
import { executeReadOnlyQuery } from "./execute-query.js";

export const queryAllInputSchema = z.object({
  sql: z
    .string()
    .min(1)
    .describe("Misma sentencia read-only que execute_query; se ejecuta en paralelo en cada alias"),
  connections: connectionsField,
  maxRows: z.number().int().positive().max(10_000).optional(),
  timeoutMs: z.number().int().positive().max(300_000).optional(),
});

export const queryAllTool = {
  name: "query_all",
  title: "Run read-only query on multiple connections",
  description:
    "Ejecuta la misma SQL validada en paralelo en varios aliases; devuelve un resultado por conexión (ok/error, " +
    "filas, truncated, cursor). Si connections se omite, usa todos los aliases configurados.",
  inputSchema: queryAllInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ sql, connections, maxRows, timeoutMs }: z.infer<typeof queryAllInputSchema>) => {
      const aliases = resolveConnectionList(manager, connections);

      const results = await Promise.all(
        aliases.map(async (alias) => {
          const started = Date.now();
          try {
            const response = await executeReadOnlyQuery(manager, alias, sql, maxRows, { timeoutMs });
            const text = response.content[0]?.text ?? "{}";
            const payload = JSON.parse(text) as Record<string, unknown>;
            if (payload.error) {
              return {
                connection: alias,
                ok: false as const,
                error: String(payload.error),
                durationMs: Date.now() - started,
              };
            }
            return {
              connection: alias,
              ok: true as const,
              durationMs: Date.now() - started,
              rowCount: payload.rowCount,
              truncated: payload.truncated,
              cursor: payload.cursor,
              columns: payload.columns,
              rows: payload.rows,
            };
          } catch (error) {
            return {
              connection: alias,
              ok: false as const,
              error: sanitizeErrorMessage(error),
              durationMs: Date.now() - started,
            };
          }
        }),
      );

      return jsonText({
        connectionCount: aliases.length,
        results,
      });
    },
};
