import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import {
  CURSOR_IDLE_IN_TX_MS,
  generateServerCursorName,
  registerOpenCursor,
} from "../query-cursors.js";
import { resolveMaxRows, resolveTimeoutMs } from "../query-limits.js";
import { validateReadOnlySql, sanitizeErrorMessage, jsonError, jsonText } from "../security.js";
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
      "A single read-only PostgreSQL statement: SELECT, WITH, EXPLAIN [ANALYZE], SHOW, TABLE or VALUES. " +
        "Prefer explicit columns, WHERE filters, aggregates and LIMIT on large tables.",
    ),
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
  // Leading keyword from validateReadOnlySql; EXPLAIN and SHOW cannot run inside a cursor.
  statement?: string;
}

const DIRECT_STATEMENTS = new Set(["EXPLAIN", "SHOW"]);
const CURSOR_STATEMENTS = new Set(["SELECT", "WITH", "VALUES", "TABLE"]);

// Adds PostgreSQL hints/positions to the error so the caller can fix its SQL.
function describeQueryError(error: unknown, positionOffset = 0): string {
  const message = sanitizeErrorMessage(error);
  if (!error || typeof error !== "object") {
    return message;
  }

  const details = error as { hint?: string; position?: string; detail?: string };
  const extras = [
    details.detail ? `detail: ${details.detail}` : undefined,
    details.hint ? `hint: ${details.hint}` : undefined,
    details.position ? `at character ${Math.max(1, Number(details.position) - positionOffset)}` : undefined,
  ].filter(Boolean);

  return extras.length > 0 ? `${message} (${extras.join("; ")})` : message;
}

async function applyLocalStatementTimeout(client: import("pg").PoolClient, timeoutMs: number): Promise<void> {
  await client.query(`SET LOCAL statement_timeout = '${timeoutMs}ms'`);
}

// Runs SQL that is already known to be read-only inside a READ ONLY transaction that is always rolled back.
export async function runReadOnlyQuery(
  manager: ConnectionManager,
  connection: string,
  sql: string,
  options: ReadOnlyQueryOptions = {},
): Promise<ReturnType<typeof jsonText> | ReturnType<typeof jsonError>> {
  const started = Date.now();
  let positionOffset = 0;
  const configuredTimeoutMs = manager.getStatementTimeoutMs(connection);
  const configuredIdleInTxMs = configuredTimeoutMs;

  try {
    const maxRows = resolveMaxRows(options.maxRows, manager.getMaxRows(connection));
    const timeoutMs = resolveTimeoutMs(options.timeoutMs, configuredTimeoutMs);
    const maxCellChars = resolveMaxCellChars(options.maxCellChars);
    const usePaginationCursor =
      !options.statement || CURSOR_STATEMENTS.has(options.statement);

    const client = await manager.checkoutClient(connection);
    let cursorKept = false;

    try {
      await client.query("BEGIN READ ONLY");
      if (timeoutMs !== configuredTimeoutMs) {
        await applyLocalStatementTimeout(client, timeoutMs);
      }

      let rows: Record<string, unknown>[];
      let columns: string[];

      if (options.statement && DIRECT_STATEMENTS.has(options.statement)) {
        const result = await client.query(sql);
        rows = result.rows;
        columns = result.fields.map((field) => field.name);
      } else if (usePaginationCursor) {
        const cursorName = generateServerCursorName();
        const declarePrefix = `DECLARE ${cursorName} NO SCROLL CURSOR FOR `;
        positionOffset = declarePrefix.length;
        await client.query(`${declarePrefix}${sql}`);
        positionOffset = 0;
        const fetchResult = await client.query(`FETCH ${maxRows + 1} FROM ${cursorName}`);
        rows = fetchResult.rows;
        columns = fetchResult.fields.map((field) => field.name);

        const truncated = rows.length > maxRows;
        const formatted = formatRows(truncated ? rows.slice(0, maxRows) : rows, maxCellChars);

        if (truncated) {
          await client.query(`SET idle_in_transaction_session_timeout = '${CURSOR_IDLE_IN_TX_MS}ms'`);
          const overflowRow = rows[maxRows] as Record<string, unknown> | undefined;
          const registered = registerOpenCursor({
            connection,
            client,
            cursorName,
            columns,
            maxRows,
            maxCellChars,
            nextOffset: formatted.rows.length,
            pendingRows: overflowRow ? [overflowRow] : [],
            configuredIdleInTxMs,
          });
          cursorKept = true;

          return jsonText({
            connection,
            rowCount: formatted.rows.length,
            columns,
            rows: formatted.rows,
            durationMs: Date.now() - started,
            truncated: true,
            maxRowsApplied: maxRows,
            timeoutMsApplied: timeoutMs,
            truncatedCells: formatted.truncatedCells,
            cursor: registered.id,
            nextOffset: registered.nextOffset,
            note: `Only the first ${maxRows} rows are shown; call fetch_more with cursor to continue.`,
            ...(formatted.truncatedCells > 0
              ? { cellNote: `${formatted.truncatedCells} long cell value(s) were shortened (maxCellChars ${maxCellChars}).` }
              : {}),
          });
        }

        await client.query(`CLOSE ${cursorName}`);
        const formattedFull = formatRows(rows, maxCellChars);
        await client.query("ROLLBACK");

        return jsonText({
          connection,
          rowCount: formattedFull.rows.length,
          columns,
          rows: formattedFull.rows,
          durationMs: Date.now() - started,
          truncated: false,
          maxRowsApplied: maxRows,
          timeoutMsApplied: timeoutMs,
          truncatedCells: formattedFull.truncatedCells,
          cursor: null,
          nextOffset: null,
          ...(formattedFull.truncatedCells > 0
            ? { cellNote: `${formattedFull.truncatedCells} long cell value(s) were shortened (maxCellChars ${maxCellChars}).` }
            : {}),
        });
      } else {
        const cursorName = "mcp_readonly_cursor";
        const declarePrefix = `DECLARE ${cursorName} NO SCROLL CURSOR FOR `;
        positionOffset = declarePrefix.length;
        await client.query(`${declarePrefix}${sql}`);
        positionOffset = 0;
        const fetchResult = await client.query(`FETCH ${maxRows + 1} FROM ${cursorName}`);
        await client.query(`CLOSE ${cursorName}`);
        rows = fetchResult.rows;
        columns = fetchResult.fields.map((field) => field.name);
      }

      const truncated = rows.length > maxRows;
      const formatted = formatRows(truncated ? rows.slice(0, maxRows) : rows, maxCellChars);

      await client.query("ROLLBACK");

      return jsonText({
        connection,
        rowCount: formatted.rows.length,
        columns,
        rows: formatted.rows,
        durationMs: Date.now() - started,
        truncated,
        maxRowsApplied: maxRows,
        timeoutMsApplied: timeoutMs,
        truncatedCells: formatted.truncatedCells,
        cursor: null,
        nextOffset: null,
        ...(truncated
          ? {
              note: `Only the first ${maxRows} rows are shown; more rows exist. Add filters, aggregate, or page with ORDER BY ... LIMIT/OFFSET.`,
            }
          : {}),
        ...(formatted.truncatedCells > 0
          ? { cellNote: `${formatted.truncatedCells} long cell value(s) were shortened (maxCellChars ${maxCellChars}).` }
          : {}),
      });
    } catch (error) {
      if (!cursorKept) {
        try {
          await client.query("ROLLBACK");
        } catch {
          // Ignore rollback failure.
        }
      }
      throw error;
    } finally {
      if (!cursorKept) {
        client.release();
      }
    }
  } catch (error) {
    return jsonError(describeQueryError(error, positionOffset));
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

  return runReadOnlyQuery(manager, connection, sql.trim().replace(/;\s*$/, ""), {
    ...options,
    maxRows: requestedMaxRows,
    statement: validation.statement,
  });
}

export const executeQueryTool = {
  name: "execute_query",
  title: "Run read-only PostgreSQL query",
  description:
    "Run one validated read-only SQL statement (PostgreSQL dialect) inside a READ ONLY transaction that is rolled back. " +
    "Returns columns, rows (objects keyed by column), durationMs, and 'truncated' when more rows exist than maxRows. " +
    "When truncated on SELECT/WITH/VALUES/TABLE, returns cursor and nextOffset; use fetch_more to page (server cursor, TTL 10 min). " +
    "Optional timeoutMs and maxRows override alias defaults (caps 300000 ms and 10000 rows). " +
    "Writes, DDL, multiple statements, SELECT INTO, FOR UPDATE and side-effect functions are rejected. " +
    "Use describe_table first to get exact column names.",
  inputSchema: executeQueryInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, sql, maxRows, timeoutMs, maxCellChars }: z.infer<typeof executeQueryInputSchema>) =>
      executeReadOnlyQuery(manager, connection, sql, maxRows, { timeoutMs, maxCellChars }),
};
