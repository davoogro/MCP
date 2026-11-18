import { createWriteStream } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { streamableConnection } from "../mysql-stream.js";
import { formatCsvRow } from "../csv-export.js";
import { EXPORT_MAX_ROWS, resolveTimeoutMs } from "../query-limits.js";
import { MARIADB_EXPORT_DIR } from "../paths.js";
import { validateReadOnlySql, validateIdentifier, quoteIdentifier, sanitizeErrorMessage, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField } from "./common.js";

export const exportQueryInputSchema = z.object({
  connection: connectionField,
  sql: z.string().min(1).describe("SELECT o WITH validado; se exporta en streaming a CSV"),
  database: z.string().min(1).optional(),
  timeoutMs: z.number().int().positive().max(300_000).optional(),
});

function buildExportPath(connection: string): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const safeAlias = connection.replace(/[^\w.-]+/g, "_");
  return `${MARIADB_EXPORT_DIR}/${safeAlias}_${stamp}.csv`;
}

export const exportQueryTool = {
  name: "export_query",
  title: "Export read-only query to CSV file",
  description:
    "Ejecuta una SELECT/WITH en streaming y escribe CSV en C:\\DVB\\exports\\mariadb\\ (sin pasar filas por el " +
    "contexto del LLM). Tope 50 millones de filas; indica truncated si se alcanza el límite. Solo lectura con rollback.",
  inputSchema: exportQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, sql, database, timeoutMs }: z.infer<typeof exportQueryInputSchema>) => {
      const validation = validateReadOnlySql(sql);
      if (!validation.valid) {
        return jsonError(validation.reason ?? "Invalid SQL");
      }
      if (validation.statement !== "SELECT" && validation.statement !== "WITH") {
        return jsonError("export_query only supports SELECT or WITH statements");
      }

      const configuredTimeoutMs = manager.getStatementTimeoutMs(connection);
      const appliedTimeout = resolveTimeoutMs(timeoutMs, configuredTimeoutMs);
      const defaultDatabase = manager.getConnectionConfig(connection).database;
      const targetDatabase = database ?? defaultDatabase;
      const switchDatabase = targetDatabase !== defaultDatabase;
      if (switchDatabase) {
        validateIdentifier(targetDatabase, "database");
      }

      const filePath = buildExportPath(connection);
      const started = Date.now();

      try {
        await mkdir(dirname(filePath), { recursive: true });

        const summary = await manager.withClient(connection, async (client) => {
          let sessionChanged = false;
          let rowCount = 0;
          let truncated = false;
          let columns: string[] = [];
          let clientDestroyed = false;

          try {
            if (switchDatabase) {
              sessionChanged = true;
              await client.query(`USE ${quoteIdentifier(targetDatabase)}`);
            }
            if (appliedTimeout !== configuredTimeoutMs) {
              sessionChanged = true;
              try {
                await client.query(`SET SESSION max_statement_time = ${appliedTimeout / 1000}`);
              } catch {
                await client.query(`SET SESSION MAX_EXECUTION_TIME = ${appliedTimeout}`);
              }
            }
            await client.query("SET SESSION sql_select_limit = DEFAULT");

            try {
              await client.query("START TRANSACTION READ ONLY");
            } catch {
              await client.query("START TRANSACTION");
            }

            const coreConn = streamableConnection(client);
            await new Promise<void>((resolve, reject) => {
              const out = createWriteStream(filePath, { encoding: "utf8" });
              let headerWritten = false;
              let settled = false;

              const finish = (error?: Error): void => {
                if (settled) {
                  return;
                }
                settled = true;
                out.end(() => {
                  if (error) {
                    reject(error);
                  } else {
                    resolve();
                  }
                });
              };

              const query = coreConn.query(sql);
              query.on("fields", (fields: { name: string }[]) => {
                columns = fields.map((f) => f.name);
                out.write(`${columns.join(",")}\n`);
                headerWritten = true;
              });

              const stream = query.stream();
              stream.on("data", (row: RowDataPacket) => {
                if (truncated) {
                  return;
                }
                if (rowCount >= EXPORT_MAX_ROWS) {
                  truncated = true;
                  stream.destroy();
                  try {
                    coreConn.destroy();
                    clientDestroyed = true;
                  } catch {
                    clientDestroyed = true;
                  }
                  finish();
                  return;
                }
                if (!headerWritten && columns.length === 0) {
                  columns = Object.keys(row);
                  out.write(`${columns.join(",")}\n`);
                  headerWritten = true;
                }
                out.write(`${formatCsvRow(columns, row as Record<string, unknown>)}\n`);
                rowCount += 1;
              });
              stream.on("error", (err: Error) => finish(err));
              stream.on("close", () => finish());
              stream.on("end", () => finish());
            });

            if (!clientDestroyed) {
              await client.query("ROLLBACK");
            }
          } finally {
            if (clientDestroyed) {
              client.destroy();
            } else if (sessionChanged) {
              try {
                if (appliedTimeout !== configuredTimeoutMs) {
                  try {
                    await client.query(`SET SESSION max_statement_time = ${configuredTimeoutMs / 1000}`);
                  } catch {
                    await client.query(`SET SESSION MAX_EXECUTION_TIME = ${configuredTimeoutMs}`);
                  }
                }
                if (switchDatabase) {
                  await client.query(`USE ${quoteIdentifier(defaultDatabase)}`);
                }
              } catch {
                client.destroy();
              }
            }
          }

          return { rowCount, truncated, columns };
        });

        return jsonText({
          connection,
          database: targetDatabase,
          filePath,
          format: "csv",
          rowCount: summary.rowCount,
          truncated: summary.truncated,
          maxRowsCap: EXPORT_MAX_ROWS,
          columnCount: summary.columns.length,
          durationMs: Date.now() - started,
        });
      } catch (error) {
        return jsonError(sanitizeErrorMessage(error));
      }
    },
};
