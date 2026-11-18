import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { validateReadOnlySql, validateIdentifier, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";
import { runReadOnlyQuery } from "./execute-query.js";

export const explainQueryInputSchema = z.object({
  connection: connectionField,
  sql: z
    .string()
    .min(1)
    .describe("A single SELECT or WITH query to explain (not EXPLAIN itself); validated as read-only"),
  database: z.string().min(1).optional().describe("Default database for unqualified names"),
  format: z.enum(["json"]).default("json").describe("EXPLAIN output format (only json is supported)"),
  analyze: z
    .boolean()
    .default(false)
    .describe("When true, runs EXPLAIN ANALYZE FORMAT=JSON (executes the SELECT; still read-only and rolled back)"),
});

const EXPLAIN_SELECT_ONLY = new Set(["SELECT", "WITH"]);

function summarizeExplainJson(raw: unknown): unknown {
  if (raw === null || raw === undefined) {
    return raw;
  }
  let parsed: unknown = raw;
  if (typeof raw === "string") {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return { raw: String(raw).slice(0, 8000) };
    }
  }
  if (typeof parsed !== "object" || parsed === null) {
    return parsed;
  }
  const root = parsed as Record<string, unknown>;
  const queryBlock = root.query_block ?? root;
  if (typeof queryBlock !== "object" || queryBlock === null) {
    return { query_block: queryBlock };
  }
  const block = queryBlock as Record<string, unknown>;
  const summary: Record<string, unknown> = {};
  if (block.select_id !== undefined) {
    summary.select_id = block.select_id;
  }
  if (block.cost_info) {
    summary.cost_info = block.cost_info;
  }
  if (block.nested_loop) {
    summary.nested_loop = summarizeNestedLoop(block.nested_loop);
  } else if (block.table) {
    summary.table = summarizeTableAccess(block.table);
  }
  if (block.ordering_operation) {
    summary.ordering_operation = block.ordering_operation;
  }
  if (block.grouping_operation) {
    summary.grouping_operation = block.grouping_operation;
  }
  return { query_block: summary };
}

function summarizeNestedLoop(nested: unknown): unknown {
  if (!Array.isArray(nested)) {
    return nested;
  }
  return nested.map((item) => {
    if (typeof item === "object" && item !== null && "table" in item) {
      return { table: summarizeTableAccess((item as { table: unknown }).table) };
    }
    return item;
  });
}

function summarizeTableAccess(table: unknown): unknown {
  if (typeof table !== "object" || table === null) {
    return table;
  }
  const t = table as Record<string, unknown>;
  const pick = [
    "table_name",
    "access_type",
    "possible_keys",
    "key",
    "key_length",
    "ref",
    "rows_examined_per_scan",
    "rows_produced_per_join",
    "filtered",
    "cost_info",
    "attached_condition",
  ];
  const out: Record<string, unknown> = {};
  for (const key of pick) {
    if (t[key] !== undefined) {
      out[key] = t[key];
    }
  }
  return out;
}

export const explainQueryTool = {
  name: "explain_query",
  title: "EXPLAIN MariaDB query plan",
  description:
    "Devuelve un plan EXPLAIN FORMAT=JSON resumido para una sentencia SELECT o WITH (validada como solo lectura). " +
    "Con analyze=true usa EXPLAIN ANALYZE FORMAT=JSON, que ejecuta la consulta dentro de una transacción READ ONLY " +
    "con rollback. Útil para diagnosticar índices, filas estimadas y coste.",
  inputSchema: explainQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, sql, database, format, analyze }: z.infer<typeof explainQueryInputSchema>) => {
      try {
        if (format !== "json") {
          return jsonError("Only format=json is supported");
        }

        const validation = validateReadOnlySql(sql);
        if (!validation.valid || !validation.statement || !EXPLAIN_SELECT_ONLY.has(validation.statement)) {
          return jsonError("explain_query only accepts a single SELECT or WITH statement");
        }

        if (database) {
          validateIdentifier(database, "database");
        }

        const explainSql = analyze ? `EXPLAIN ANALYZE FORMAT=JSON ${sql}` : `EXPLAIN FORMAT=JSON ${sql}`;

        const result = await runReadOnlyQuery(manager, connection, explainSql, {
          database,
          statement: "EXPLAIN",
          maxRows: 1,
        });

        const text = result.content[0]?.text;
        if (!text) {
          return result;
        }
        const payload = JSON.parse(text) as { error?: string; rows?: RowDataPacket[] };
        if (payload.error) {
          return jsonError(payload.error);
        }
        const firstRow = payload.rows?.[0];
        const explainColumn =
          firstRow &&
          (Object.values(firstRow).find((v) => typeof v === "string" || typeof v === "object") ??
            firstRow[Object.keys(firstRow)[0] ?? ""]);

        return jsonText({
          connection,
          database: database ?? manager.getConnectionConfig(connection).database,
          analyze,
          plan: summarizeExplainJson(explainColumn),
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
