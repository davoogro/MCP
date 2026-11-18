import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { summarizePostgresExplain } from "../explain-summary.js";
import { validateReadOnlySql, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";
import { runReadOnlyQuery } from "./execute-query.js";

export const explainQueryInputSchema = z.object({
  connection: connectionField,
  sql: z
    .string()
    .min(1)
    .describe("Una sola sentencia SELECT o WITH (no EXPLAIN); se valida como solo lectura"),
  analyze: z
    .boolean()
    .default(false)
    .describe("Si true, usa EXPLAIN (ANALYZE, FORMAT JSON) dentro de READ ONLY con rollback"),
});

const EXPLAIN_SELECT_ONLY = new Set(["SELECT", "WITH"]);

export const explainQueryTool = {
  name: "explain_query",
  title: "EXPLAIN PostgreSQL query plan",
  description:
    "Devuelve un plan EXPLAIN (FORMAT JSON) resumido para SELECT o WITH validado. Con analyze=true ejecuta " +
    "EXPLAIN (ANALYZE, FORMAT JSON) en transacción READ ONLY con rollback. Útil para costes, filas estimadas e índices.",
  inputSchema: explainQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, sql, analyze }: z.infer<typeof explainQueryInputSchema>) => {
      try {
        const validation = validateReadOnlySql(sql);
        if (!validation.valid || !validation.statement || !EXPLAIN_SELECT_ONLY.has(validation.statement)) {
          return jsonError("explain_query only accepts a single SELECT or WITH statement");
        }

        const explainSql = analyze
          ? `EXPLAIN (ANALYZE, FORMAT JSON) ${sql}`
          : `EXPLAIN (FORMAT JSON) ${sql}`;

        const result = await runReadOnlyQuery(manager, connection, explainSql, {
          statement: "EXPLAIN",
          maxRows: 1,
        });

        const text = result.content[0]?.text;
        if (!text) {
          return result;
        }
        const payload = JSON.parse(text) as { error?: string; rows?: Record<string, unknown>[] };
        if (payload.error) {
          return jsonError(payload.error);
        }
        const firstRow = payload.rows?.[0];
        const rawPlan =
          firstRow &&
          (Object.values(firstRow).find((v) => typeof v === "string" || typeof v === "object") ??
            firstRow[Object.keys(firstRow)[0] ?? ""]);

        return jsonText({
          connection,
          analyze,
          plan: summarizePostgresExplain(rawPlan),
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
