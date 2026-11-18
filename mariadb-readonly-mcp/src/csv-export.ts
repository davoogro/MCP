export function escapeCsvCell(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  let text: string;
  if (typeof value === "bigint") {
    text = value.toString();
  } else if (value instanceof Date) {
    text = value.toISOString();
  } else if (Buffer.isBuffer(value)) {
    text = `0x${value.toString("hex")}`;
  } else if (typeof value === "object") {
    text = JSON.stringify(value);
  } else {
    text = String(value);
  }
  if (/[",\r\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

export function formatCsvRow(columns: string[], row: Record<string, unknown>): string {
  return columns.map((col) => escapeCsvCell(row[col])).join(",");
}
