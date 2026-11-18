import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { formatCsvRow } from "../csv-export.js";
import { EXPORT_MAX_ROWS, resolveTimeoutMs } from "../query-limits.js";
import { generateServerCursorName, CURSOR_IDLE_IN_TX_MS } from "../query-cursors.js";
import { POSTGRES_EXPORT_DIR } from "../paths.js";
import { validateReadOnlySql, sanitizeErrorMessage, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField } from "./common.js";

export const exportQueryInputSchema = z.object({
  connection: connectionField,
  sql: z.string().min(1).describe("SELECT o WITH validado; exportación streaming a CSV"),
  format: z.enum(["csv", "parquet"]).default("csv").describe("Solo csv está soportado (sin dependencias parquet)"),
  timeoutMs: z.number().int().positive().max(300_000).optional(),
});

function buildExportPath(connection: string): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const safeAlias = connection.replace(/[^\w.-]+/g, "_");
  return `${POSTGRES_EXPORT_DIR}/${safeAlias}_${stamp}.csv`;
}

export const exportQueryTool = {
  name: "export_query",
  title: "Export read-only query to CSV file",
  description:
    "Ejecuta SELECT/WITH con cursor en streaming y escribe CSV en C:\\DVB\\exports\\postgres\\ sin pasar filas por el LLM. " +
    "Tope 50 millones de filas. Solo lectura con rollback. Parquet no está soportado en Node sin deps nuevas.",
  inputSchema: exportQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, sql, format, timeoutMs }: z.infer<typeof exportQueryInputSchema>) => {
      if (format === "parquet") {
        return jsonError("Parquet export is not supported; use format=csv");
      }

      const validation = validateReadOnlySql(sql);
      if (!validation.valid) {
        return jsonError(validation.reason ?? "Invalid SQL");
      }
      if (validation.statement !== "SELECT" && validation.statement !== "WITH") {
        return jsonError("export_query only supports SELECT or WITH statements");
      }

      const configuredTimeoutMs = manager.getStatementTimeoutMs(connection);
      const appliedTimeout = resolveTimeoutMs(timeoutMs, configuredTimeoutMs);
      const trimmedSql = sql.trim().replace(/;\s*$/, "");
      const filePath = buildExportPath(connection);
      const started = Date.now();

      try {
        await mkdir(dirname(filePath), { recursive: true });

        const summary = await manager.withClient(connection, async (client) => {
          let rowCount = 0;
          let truncated = false;
          let columns: string[] = [];

          await client.query("BEGIN READ ONLY");
          if (appliedTimeout !== configuredTimeoutMs) {
            await client.query(`SET LOCAL statement_timeout = '${appliedTimeout}ms'`);
          }
          await client.query(`SET idle_in_transaction_session_timeout = '${CURSOR_IDLE_IN_TX_MS}ms'`);

          const cursorName = generateServerCursorName();
          await client.query(`DECLARE ${cursorName} NO SCROLL CURSOR FOR ${trimmedSql}`);

          const out = createWriteStream(filePath, { encoding: "utf8" });
          const batchSize = 2000;

          try {
            let headerWritten = false;
            for (;;) {
              const fetchResult = await client.query(`FETCH ${batchSize} FROM ${cursorName}`);
              const batch = fetchResult.rows as Record<string, unknown>[];
              if (batch.length === 0) {
                break;
              }
              if (!headerWritten) {
                columns = fetchResult.fields.map((f) => f.name);
                out.write(`${columns.join(",")}\n`);
                headerWritten = true;
              }
              for (const row of batch) {
                if (rowCount >= EXPORT_MAX_ROWS) {
                  truncated = true;
                  break;
                }
                out.write(`${formatCsvRow(columns, row)}\n`);
                rowCount += 1;
              }
              if (truncated) {
                break;
              }
            }
          } finally {
            await new Promise<void>((resolve, reject) => {
              out.end((err: NodeJS.ErrnoException | null | undefined) => (err ? reject(err) : resolve()));
            });
            try {
              await client.query(`CLOSE ${cursorName}`);
            } catch {
              // ignore
            }
            await client.query("ROLLBACK");
          }

          return { rowCount, truncated, columns };
        });

        return jsonText({
          connection,
          filePath,
          format: "csv",
          rowCount: summary.rowCount,
          truncated: summary.truncated,
          maxRowsCap: EXPORT_MAX_ROWS,
          columnCount: summary.columns.length,
          durationMs: Date.now() - started,
          timeoutMsApplied: appliedTimeout,
        });
      } catch (error) {
        return jsonError(sanitizeErrorMessage(error));
      }
    },
};
