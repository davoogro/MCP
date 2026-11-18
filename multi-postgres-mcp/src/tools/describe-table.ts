import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, schemaField, tableField } from "./common.js";

export const describeTableInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  table: tableField,
});

const OBJECT_TYPES: Record<string, string> = {
  r: "table",
  p: "partitioned_table",
  v: "view",
  m: "materialized_view",
  f: "foreign_table",
};

export const describeTableTool = {
  name: "describe_table",
  title: "Describe PostgreSQL table",
  description:
    "Describe a table, view or materialized view: columns (full type incl. length/precision, nullability, default, " +
    "comment), primary key, foreign keys (use them to write JOINs), indexes, comment and row estimate.",
  inputSchema: describeTableInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, table }: z.infer<typeof describeTableInputSchema>) => {
      try {
        validateIdentifier(schema, "schema");
        validateIdentifier(table, "table");

        return await manager.withClient(connection, async (client) => {
          const objectResult = await client.query<{
            oid: number;
            relkind: string;
            comment: string | null;
            estimated_rows: number | null;
          }>(
            `
              SELECT
                c.oid,
                c.relkind,
                obj_description(c.oid, 'pg_class') AS comment,
                CASE WHEN c.relkind IN ('r', 'm') AND c.reltuples >= 0 THEN round(c.reltuples)::float8 END AS estimated_rows
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = $1
                AND c.relname = $2
                AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              LIMIT 1
            `,
            [schema, table],
          );

          if (objectResult.rows.length === 0) {
            return jsonError(
              `Object '${schema}.${table}' was not found or is not accessible. Use list_tables (or search_columns) to find the exact name.`,
            );
          }

          const { oid, relkind, comment, estimated_rows: estimatedRows } = objectResult.rows[0];

          const columnsResult = await client.query<{
            column_name: string;
            data_type: string;
            is_nullable: boolean;
            column_default: string | null;
            comment: string | null;
          }>(
            `
              SELECT
                a.attname AS column_name,
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                NOT a.attnotnull AS is_nullable,
                pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default,
                pg_catalog.col_description(a.attrelid, a.attnum) AS comment
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              WHERE a.attrelid = $1
                AND a.attnum > 0
                AND NOT a.attisdropped
              ORDER BY a.attnum
            `,
            [oid],
          );

          const constraintResult = await client.query<{
            name: string;
            type: string;
            columns: string[] | null;
            definition: string;
          }>(
            `
              SELECT
                con.conname AS name,
                con.contype AS type,
                ARRAY(
                  SELECT a.attname::text
                  FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                  ORDER BY k.ord
                ) AS columns,
                pg_catalog.pg_get_constraintdef(con.oid) AS definition
              FROM pg_catalog.pg_constraint con
              WHERE con.conrelid = $1
                AND con.contype IN ('p', 'f')
              ORDER BY con.contype DESC, con.conname
            `,
            [oid],
          );

          const indexResult = await client.query<{
            index_name: string;
            index_definition: string;
            is_unique: boolean;
            is_primary: boolean;
          }>(
            `
              SELECT
                i.relname AS index_name,
                pg_catalog.pg_get_indexdef(i.oid) AS index_definition,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary
              FROM pg_catalog.pg_index ix
              JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
              WHERE ix.indrelid = $1
              ORDER BY i.relname
            `,
            [oid],
          );

          const primaryKey = constraintResult.rows.find((row) => row.type === "p");

          return jsonText({
            connection,
            schema,
            table,
            objectType: OBJECT_TYPES[relkind] ?? "table",
            comment,
            estimatedRows,
            columns: columnsResult.rows.map((column) => ({
              name: column.column_name,
              dataType: column.data_type,
              isNullable: column.is_nullable,
              defaultValue: column.column_default,
              ...(column.comment ? { comment: column.comment } : {}),
            })),
            primaryKey: primaryKey?.columns ?? [],
            foreignKeys: constraintResult.rows
              .filter((row) => row.type === "f")
              .map((row) => ({ name: row.name, columns: row.columns ?? [], definition: row.definition })),
            indexes: indexResult.rows.map((row) => ({
              name: row.index_name,
              definition: row.index_definition,
              isUnique: row.is_unique,
              isPrimary: row.is_primary,
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
