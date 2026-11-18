import { randomUUID } from "node:crypto";
import type { PoolClient } from "pg";
import { formatRows } from "./result-format.js";
import { sanitizeErrorMessage } from "./security.js";

export const CURSOR_TTL_MS = 10 * 60 * 1000;
export const MAX_OPEN_CURSORS = 5;
/** Held transactions need longer than default statement idle timeout while paginating. */
export const CURSOR_IDLE_IN_TX_MS = 660_000;

export interface RegisteredCursor {
  id: string;
  connection: string;
  columns: string[];
  maxRows: number;
  maxCellChars: number;
  nextOffset: number;
}

interface OpenCursor {
  id: string;
  connection: string;
  client: PoolClient;
  cursorName: string;
  columns: string[];
  maxRows: number;
  maxCellChars: number;
  nextOffset: number;
  /** Row already read from the server cursor to detect truncation without losing it. */
  pendingRows: Record<string, unknown>[];
  lastUsedAt: number;
  configuredIdleInTxMs: number;
}

const openCursors = new Map<string, OpenCursor>();

function makeCursorName(): string {
  return `mcp_${randomUUID().replace(/-/g, "")}`;
}

export function generateServerCursorName(): string {
  return makeCursorName();
}

async function closeEntry(entry: OpenCursor): Promise<void> {
  try {
    await entry.client.query(`CLOSE ${entry.cursorName}`);
  } catch {
    // Cursor may already be closed.
  }
  try {
    await entry.client.query("ROLLBACK");
  } catch {
    // Ignore rollback errors during cleanup.
  }
  try {
    await entry.client.query(`SET idle_in_transaction_session_timeout = '${entry.configuredIdleInTxMs}ms'`);
  } catch {
    // Best effort restore.
  }
  entry.client.release();
}

export async function closeCursor(cursorId: string): Promise<boolean> {
  const entry = openCursors.get(cursorId);
  if (!entry) {
    return false;
  }
  openCursors.delete(cursorId);
  await closeEntry(entry);
  return true;
}

export async function closeAllCursors(): Promise<void> {
  const ids = [...openCursors.keys()];
  await Promise.all(ids.map((id) => closeCursor(id)));
}

function evictIfNeeded(): void {
  const now = Date.now();
  for (const [id, entry] of openCursors) {
    if (now - entry.lastUsedAt > CURSOR_TTL_MS) {
      void closeCursor(id);
    }
  }
  while (openCursors.size >= MAX_OPEN_CURSORS) {
    const oldest = [...openCursors.entries()].sort((a, b) => a[1].lastUsedAt - b[1].lastUsedAt)[0];
    if (!oldest) {
      break;
    }
    void closeCursor(oldest[0]);
  }
}

export function registerOpenCursor(params: {
  connection: string;
  client: PoolClient;
  cursorName: string;
  columns: string[];
  maxRows: number;
  maxCellChars: number;
  nextOffset: number;
  pendingRows?: Record<string, unknown>[];
  configuredIdleInTxMs: number;
}): RegisteredCursor {
  evictIfNeeded();
  const id = randomUUID();
  openCursors.set(id, {
    id,
    connection: params.connection,
    client: params.client,
    cursorName: params.cursorName,
    columns: params.columns,
    maxRows: params.maxRows,
    maxCellChars: params.maxCellChars,
    nextOffset: params.nextOffset,
    pendingRows: params.pendingRows ? [...params.pendingRows] : [],
    lastUsedAt: Date.now(),
    configuredIdleInTxMs: params.configuredIdleInTxMs,
  });
  return {
    id,
    connection: params.connection,
    columns: params.columns,
    maxRows: params.maxRows,
    maxCellChars: params.maxCellChars,
    nextOffset: params.nextOffset,
  };
}

export interface FetchMoreResult {
  connection: string;
  columns: string[];
  rows: Record<string, unknown>[];
  rowCount: number;
  truncated: boolean;
  cursor: string | null;
  nextOffset: number | null;
  maxRowsApplied: number;
  truncatedCells: number;
}

export async function fetchMorePage(
  cursorId: string,
  requestedMaxRows?: number,
): Promise<FetchMoreResult> {
  evictIfNeeded();
  const entry = openCursors.get(cursorId);
  if (!entry) {
    throw new Error(`Cursor '${cursorId}' does not exist or expired (TTL ${CURSOR_TTL_MS / 1000}s)`);
  }

  entry.lastUsedAt = Date.now();
  const maxRows = requestedMaxRows ?? entry.maxRows;
  const fetchSize = maxRows + 1;

  try {
    const rawRows: Record<string, unknown>[] = [...entry.pendingRows];
    entry.pendingRows = [];
    if (rawRows.length <= maxRows) {
      const need = fetchSize - rawRows.length;
      if (need > 0) {
        const fetchResult = await entry.client.query(`FETCH ${need} FROM ${entry.cursorName}`);
        rawRows.push(...(fetchResult.rows as Record<string, unknown>[]));
      }
    }
    const truncated = rawRows.length > maxRows;
    if (truncated) {
      entry.pendingRows = rawRows.slice(maxRows);
    }
    const slice = truncated ? rawRows.slice(0, maxRows) : rawRows;
    const formatted = formatRows(slice, entry.maxCellChars);
    entry.nextOffset += formatted.rows.length;

    if (!truncated && entry.pendingRows.length === 0) {
      openCursors.delete(cursorId);
      await closeEntry(entry);
      return {
        connection: entry.connection,
        columns: entry.columns,
        rows: formatted.rows,
        rowCount: formatted.rows.length,
        truncated: false,
        cursor: null,
        nextOffset: null,
        maxRowsApplied: maxRows,
        truncatedCells: formatted.truncatedCells,
      };
    }

    return {
      connection: entry.connection,
      columns: entry.columns,
      rows: formatted.rows,
      rowCount: formatted.rows.length,
      truncated: true,
      cursor: cursorId,
      nextOffset: entry.nextOffset,
      maxRowsApplied: maxRows,
      truncatedCells: formatted.truncatedCells,
    };
  } catch (error) {
    openCursors.delete(cursorId);
    try {
      await closeEntry(entry);
    } catch {
      entry.client.release();
    }
    throw new Error(sanitizeErrorMessage(error));
  }
}

/** @internal test hook */
export function _openCursorCount(): number {
  return openCursors.size;
}
