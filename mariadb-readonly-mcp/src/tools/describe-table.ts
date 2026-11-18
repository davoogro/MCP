import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField, tableField } from "./common.js";

export const describeTableInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  table: tableField,
});

export const describeTableTool = {
  name: "describe_table",
  title: "Describe MariaDB table",
  description:
    "Describe a table or view: columns (full type, nullability, default, key, extra such as auto_increment, comment), " +
    "primary key, foreign keys (use them to write JOINs), indexes, engine, comment and approximate row count.",
  inputSchema: describeTableInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, table }: z.infer<typeof describeTableInputSchema>) => {
      try {
        const resolvedSchema = resolveSchema(manager, connection, schema);
        validateIdentifier(table, "table");

        return await manager.withClient(connection, async (client) => {
          const [objectRows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                TABLE_TYPE AS table_type,
                ENGINE AS engine,
                TABLE_ROWS AS estimated_rows,
                NULLIF(TABLE_COMMENT, '') AS comment
              FROM information_schema.TABLES
              WHERE TABLE_SCHEMA = ?
                AND TABLE_NAME = ?
              LIMIT 1
            `,
            [resolvedSchema, table],
          );

          if (objectRows.length === 0) {
            return jsonError(
              `Object '${resolvedSchema}.${table}' was not found or is not accessible. Use list_tables (or search_columns) to find the exact name.`,
            );
          }

          const tableType = String(objectRows[0].table_type ?? "BASE TABLE");
          const objectType = tableType.toUpperCase().includes("VIEW") ? "view" : "table";

          const [columnRows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                COLUMN_NAME AS column_name,
                COLUMN_TYPE AS data_type,
                IS_NULLABLE AS is_nullable,
                COLUMN_DEFAULT AS column_default,
                COLUMN_KEY AS column_key,
                EXTRA AS extra,
                COLUMN_COMMENT AS comment
              FROM information_schema.COLUMNS
              WHERE TABLE_SCHEMA = ?
                AND TABLE_NAME = ?
              ORDER BY ORDINAL_POSITION
            `,
            [resolvedSchema, table],
          );

          const [indexRows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                INDEX_NAME AS index_name,
                NON_UNIQUE AS non_unique,
                INDEX_NAME = 'PRIMARY' AS is_primary,
                GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns
              FROM information_schema.STATISTICS
              WHERE TABLE_SCHEMA = ?
                AND TABLE_NAME = ?
              GROUP BY INDEX_NAME, NON_UNIQUE
              ORDER BY INDEX_NAME
            `,
            [resolvedSchema, table],
          );

          const [foreignKeyRows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                CONSTRAINT_NAME AS name,
                GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) AS columns,
                REFERENCED_TABLE_SCHEMA AS referenced_schema,
                REFERENCED_TABLE_NAME AS referenced_table,
                GROUP_CONCAT(REFERENCED_COLUMN_NAME ORDER BY ORDINAL_POSITION) AS referenced_columns
              FROM information_schema.KEY_COLUMN_USAGE
              WHERE TABLE_SCHEMA = ?
                AND TABLE_NAME = ?
                AND REFERENCED_TABLE_NAME IS NOT NULL
              GROUP BY CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME
              ORDER BY CONSTRAINT_NAME
            `,
            [resolvedSchema, table],
          );

          const primaryIndex = indexRows.find((row) => Boolean(row.is_primary));

          return jsonText({
            connection,
            schema: resolvedSchema,
            table,
            objectType,
            engine: objectRows[0].engine ?? null,
            comment: objectRows[0].comment ?? null,
            estimatedRows: objectRows[0].estimated_rows ?? null,
            columns: columnRows.map((column) => ({
              name: column.column_name,
              dataType: column.data_type,
              isNullable: column.is_nullable === "YES",
              defaultValue: column.column_default,
              ...(column.column_key ? { key: column.column_key } : {}),
              ...(column.extra ? { extra: column.extra } : {}),
              ...(column.comment ? { comment: column.comment } : {}),
            })),
            primaryKey: primaryIndex ? String(primaryIndex.columns ?? "").split(",").filter(Boolean) : [],
            foreignKeys: foreignKeyRows.map((row) => ({
              name: row.name,
              columns: String(row.columns ?? "").split(",").filter(Boolean),
              references: `${row.referenced_schema}.${row.referenced_table}`,
              referencedColumns: String(row.referenced_columns ?? "").split(",").filter(Boolean),
            })),
            indexes: indexRows.map((row) => ({
              name: row.index_name,
              columns: String(row.columns ?? ""),
              isUnique: Number(row.non_unique) === 0,
              isPrimary: Boolean(row.is_primary),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
