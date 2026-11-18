import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { quoteIdentifier, validateIdentifier } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField, tableField } from "./common.js";
import { runReadOnlyQuery } from "./execute-query.js";

const DEFAULT_SAMPLE_CELL_CHARS = 300;

export const sampleDataInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  table: tableField,
  limit: z.number().int().positive().max(100).default(10).describe("Number of rows to return (1-100, default 10)"),
  columns: z
    .array(z.string().min(1))
    .max(200)
    .optional()
    .describe("Optional subset of column names (default: all columns)"),
  maxCellChars: z
    .number()
    .int()
    .positive()
    .optional()
    .describe(`Truncate long cell values to this many characters (default ${DEFAULT_SAMPLE_CELL_CHARS})`),
});

export function buildSampleQuery(schema: string, table: string, limit: number, columns?: string[]): string {
  validateIdentifier(schema, "schema");
  validateIdentifier(table, "table");
  const selectList = columns && columns.length > 0 ? columns.map(quoteIdentifier).join(", ") : "*";
  return `SELECT ${selectList} FROM ${quoteIdentifier(schema)}.${quoteIdentifier(table)} LIMIT ${Math.floor(limit)}`;
}

export const sampleDataTool = {
  name: "sample_data",
  title: "Sample MariaDB table rows",
  description:
    "Return the first N rows (no particular order) of a table or view to see what the data looks like. Identifiers " +
    "are safely quoted, so unusual names work. Long values are shortened. For filtered or ordered results use " +
    "execute_query.",
  inputSchema: sampleDataInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, table, limit, columns, maxCellChars }: z.infer<typeof sampleDataInputSchema>) => {
      try {
        const sql = buildSampleQuery(resolveSchema(manager, connection, schema), table, limit, columns);
        return await runReadOnlyQuery(manager, connection, sql, {
          maxRows: limit,
          maxCellChars: maxCellChars ?? DEFAULT_SAMPLE_CELL_CHARS,
          statement: "SELECT",
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
