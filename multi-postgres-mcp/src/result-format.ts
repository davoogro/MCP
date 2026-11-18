// Keeps tool responses small and JSON-safe: long text, binary and JSON cell values are truncated.

export const DEFAULT_MAX_CELL_CHARS = 1000;
export const MAX_CELL_CHARS_LIMIT = 20000;
const BINARY_PREVIEW_BYTES = 32;

export interface FormattedRows {
  rows: Record<string, unknown>[];
  truncatedCells: number;
}

function truncateText(text: string, maxChars: number, counter: { truncated: number }): string {
  if (text.length <= maxChars) {
    return text;
  }

  counter.truncated += 1;
  return `${text.slice(0, maxChars)}... [truncated, ${text.length} chars total]`;
}

function formatValue(value: unknown, maxChars: number, counter: { truncated: number }): unknown {
  if (value === null || value === undefined) {
    return null;
  }

  if (typeof value === "string") {
    return truncateText(value, maxChars, counter);
  }

  if (typeof value === "bigint") {
    return value.toString();
  }

  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? String(value) : value.toISOString();
  }

  if (value instanceof Uint8Array) {
    const bytes = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    if (bytes.length <= BINARY_PREVIEW_BYTES) {
      return `0x${bytes.toString("hex")}`;
    }
    counter.truncated += 1;
    return `0x${bytes.subarray(0, BINARY_PREVIEW_BYTES).toString("hex")}... [binary, ${bytes.length} bytes]`;
  }

  // JSON/arrays/composite values: keep structure when small, otherwise truncate the serialized form.
  const serialized = JSON.stringify(value, (_key, inner) => (typeof inner === "bigint" ? inner.toString() : inner));
  if (serialized === undefined) {
    return String(value);
  }

  if (serialized.length <= maxChars) {
    return JSON.parse(serialized);
  }

  return truncateText(serialized, maxChars, counter);
}

export function resolveMaxCellChars(requested: number | undefined): number {
  return Math.min(Math.max(requested ?? DEFAULT_MAX_CELL_CHARS, 16), MAX_CELL_CHARS_LIMIT);
}

export function formatRows(rows: Record<string, unknown>[], maxCellChars = DEFAULT_MAX_CELL_CHARS): FormattedRows {
  const counter = { truncated: 0 };
  const formatted = rows.map((row) => {
    const output: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(row)) {
      output[key] = formatValue(value, maxCellChars, counter);
    }
    return output;
  });

  return { rows: formatted, truncatedCells: counter.truncated };
}
