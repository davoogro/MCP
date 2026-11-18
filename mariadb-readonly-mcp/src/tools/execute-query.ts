import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { resolveMaxRows, resolveTimeoutMs } from "../query-limits.js";
import { streamQueryRows } from "../query-stream.js";
import { validateReadOnlySql, validateIdentifier, quoteIdentifier, sanitizeErrorMessage, jsonError, jsonText } from "../security.js";
import { formatRows, resolveMaxCellChars, DEFAULT_MAX_CELL_CHARS } from "../result-format.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField } from "./common.js";

export { resolveMaxRows } from "../query-limits.js";

export const executeQueryInputSchema = z.object({
  connection: connectionField,
  sql: z
    .string()
    .min(1)
    .describe(
      "A single read-only MariaDB statement: SELECT, WITH, SHOW, DESCRIBE/DESC, EXPLAIN or VALUES. " +
        "Prefer explicit columns, WHERE filters, aggregates and LIMIT on large tables.",
    ),
  database: z
    .string()
    .min(1)
    .optional()
    .describe("Default database for unqualified table names (same server); defaults to the alias database"),
  maxRows: z
    .number()
    .int()
    .positive()
    .max(10_000)
    .optional()
    .describe("Maximum rows to return (cap 10000); defaults to the alias maxRows from config"),
  timeoutMs: z
    .number()
    .int()
    .positive()
    .max(300_000)
    .optional()
    .describe("Statement timeout in milliseconds (cap 300000); defaults to the alias statementTimeoutMs"),
  maxCellChars: z
    .number()
    .int()
    .positive()
    .optional()
    .describe(`Truncate text/JSON/binary cell values longer than this many characters (default ${DEFAULT_MAX_CELL_CHARS})`),
});

export interface ReadOnlyQueryOptions {
  maxRows?: number;
  timeoutMs?: number;
  maxCellChars?: number;
  // Leading keyword from validateReadOnlySql; only SELECT-like statements honour row streaming limits.
  statement?: string;
  // Default database for the statement (USE), restored afterwards.
  database?: string;
}

const ROW_LIMITED_STATEMENTS = new Set(["SELECT", "WITH", "VALUES", "TABLE"]);

async function applyStatementTimeout(client: import("mysql2/promise").PoolConnection, timeoutMs: number): Promise<void> {
  try {
    await client.query(`SET SESSION max_statement_time = ${timeoutMs / 1000}`);
  } catch {
    try {
      await client.query(`SET SESSION MAX_EXECUTION_TIME = ${timeoutMs}`);
    } catch {
      // Older servers may not expose statement timeouts.
    }
  }
}

async function resetStatementTimeout(client: import("mysql2/promise").PoolConnection, configuredMs: number): Promise<void> {
  await applyStatementTimeout(client, configuredMs);
}

// Runs SQL that is already known to be read-only inside a READ ONLY transaction that is always rolled back.
export async function runReadOnlyQuery(
  manager: ConnectionManager,
  connection: string,
  sql: string,
  options: ReadOnlyQueryOptions = {},
): Promise<ReturnType<typeof jsonText> | ReturnType<typeof jsonError>> {
  const started = Date.now();
  const configuredTimeoutMs = manager.getStatementTimeoutMs(connection);

  try {
    const maxRows = resolveMaxRows(options.maxRows, manager.getMaxRows(connection));
    const timeoutMs = resolveTimeoutMs(options.timeoutMs, configuredTimeoutMs);
    const maxCellChars = resolveMaxCellChars(options.maxCellChars);
    const defaultDatabase = manager.getConnectionConfig(connection).database;
    const database = options.database ?? defaultDatabase;
    const switchDatabase = database !== defaultDatabase;
    const streamRows = !options.statement || ROW_LIMITED_STATEMENTS.has(options.statement);
    if (switchDatabase) {
      validateIdentifier(database, "database");
    }

    return await manager.withClient(connection, async (client) => {
      // Session state changed here is restored in finally; if that fails the connection is discarded.
      let sessionChanged = false;
      let clientDestroyed = false;
      try {
        if (switchDatabase) {
          sessionChanged = true;
          await client.query(`USE ${quoteIdentifier(database)}`);
        }
        if (timeoutMs !== configuredTimeoutMs) {
          sessionChanged = true;
          await applyStatementTimeout(client, timeoutMs);
        }
        if (streamRows) {
          // sql_select_limit only caps the outermost SELECT; streaming enforces maxRows on the full result.
          sessionChanged = true;
          await client.query("SET SESSION sql_select_limit = DEFAULT");
        }

        try {
          await client.query("START TRANSACTION READ ONLY");
        } catch {
          await client.query("START TRANSACTION");
        }

        let resultRows: RowDataPacket[];
        let columns: string[];
        let truncated = false;
        try {
          if (streamRows) {
            const streamed = await streamQueryRows(client, sql, maxRows + 1);
            clientDestroyed = streamed.connectionDestroyed;
            resultRows = streamed.rows;
            columns = streamed.columns;
            truncated = streamed.truncated;
          } else {
            const [rows, fields] = await client.query<RowDataPacket[]>(sql);
            resultRows = Array.isArray(rows) ? rows : [];
            columns = Array.isArray(fields) ? fields.map((field) => field.name) : [];
            truncated = resultRows.length > maxRows;
            if (truncated) {
              resultRows = resultRows.slice(0, maxRows);
            }
          }
        } finally {
          if (!clientDestroyed) {
            await client.query("ROLLBACK");
          }
        }

        const formatted = formatRows(truncated ? resultRows.slice(0, maxRows) : resultRows, maxCellChars);

        return jsonText({
          connection,
          database,
          rowCount: formatted.rows.length,
          columns,
          rows: formatted.rows,
          durationMs: Date.now() - started,
          truncated,
          maxRowsApplied: maxRows,
          timeoutMsApplied: timeoutMs,
          truncatedCells: formatted.truncatedCells,
          ...(truncated
            ? {
                note: `Only the first ${maxRows} rows are shown; more rows exist. Add filters, aggregate, or page with ORDER BY ... LIMIT n OFFSET m.`,
              }
            : {}),
          ...(formatted.truncatedCells > 0
            ? { cellNote: `${formatted.truncatedCells} long cell value(s) were shortened (maxCellChars ${maxCellChars}).` }
            : {}),
        });
      } finally {
        if (clientDestroyed) {
          client.destroy();
        } else if (sessionChanged) {
          try {
            if (streamRows) {
              await client.query("SET SESSION sql_select_limit = DEFAULT");
            }
            if (timeoutMs !== configuredTimeoutMs) {
              await resetStatementTimeout(client, configuredTimeoutMs);
            }
            if (switchDatabase) {
              await client.query(`USE ${quoteIdentifier(defaultDatabase)}`);
            }
          } catch {
            client.destroy();
          }
        }
      }
    });
  } catch (error) {
    return jsonError(sanitizeErrorMessage(error));
  }
}

export async function executeReadOnlyQuery(
  manager: ConnectionManager,
  connection: string,
  sql: string,
  requestedMaxRows?: number,
  options: Omit<ReadOnlyQueryOptions, "maxRows" | "statement"> = {},
): Promise<ReturnType<typeof jsonText> | ReturnType<typeof jsonError>> {
  const validation = validateReadOnlySql(sql);
  if (!validation.valid) {
    return jsonError(validation.reason ?? "Invalid SQL");
  }

  return runReadOnlyQuery(manager, connection, sql, {
    ...options,
    maxRows: requestedMaxRows,
    statement: validation.statement,
  });
}

export const executeQueryTool = {
  name: "execute_query",
  title: "Run read-only MariaDB query",
  description:
    "Run one validated read-only SQL statement (MariaDB/MySQL dialect) inside a READ ONLY transaction that is rolled " +
    "back. Returns columns, rows (objects keyed by column), durationMs, and 'truncated' when more rows exist than " +
    "maxRows (streaming stops after maxRows+1 for SELECT/WITH/VALUES/TABLE). Optional timeoutMs and maxRows override " +
    "the alias defaults (caps 300000 ms and 10000 rows). Use 'database' or db.table names to query other databases on " +
    "the same server. Writes, DDL, multiple statements, SELECT ... INTO, locking reads, LOAD_FILE/SLEEP/BENCHMARK and " +
    "/*! */ comments are rejected.",
  inputSchema: executeQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({
      connection,
      sql,
      database,
      maxRows,
      timeoutMs,
      maxCellChars,
    }: z.infer<typeof executeQueryInputSchema>) =>
      executeReadOnlyQuery(manager, connection, sql, maxRows, { database, timeoutMs, maxCellChars }),
};
