import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField, tableField } from "./common.js";

export const listIndexesInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  table: tableField,
});

export const listIndexesTool = {
  name: "list_indexes",
  title: "List MariaDB table indexes",
  description:
    "Lista índices de una tabla desde information_schema.STATISTICS: nombre, columnas, unicidad, cardinalidad " +
    "y tipo (BTREE/HASH). Complementa describe_table cuando solo necesitas índices.",
  inputSchema: listIndexesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, table }: z.infer<typeof listIndexesInputSchema>) => {
      try {
        const resolvedSchema = resolveSchema(manager, connection, schema);
        validateIdentifier(table, "table");

        return await manager.withClient(connection, async (client) => {
          const [rows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                INDEX_NAME AS index_name,
                NON_UNIQUE AS non_unique,
                INDEX_TYPE AS index_type,
                SEQ_IN_INDEX AS seq_in_index,
                COLUMN_NAME AS column_name,
                COLLATION AS collation,
                CARDINALITY AS cardinality,
                NULLABLE AS nullable,
                INDEX_COMMENT AS comment
              FROM information_schema.STATISTICS
              WHERE TABLE_SCHEMA = ?
                AND TABLE_NAME = ?
              ORDER BY INDEX_NAME, SEQ_IN_INDEX
            `,
            [resolvedSchema, table],
          );

          const grouped = new Map<
            string,
            {
              name: string;
              isUnique: boolean;
              indexType: string;
              columns: string[];
              cardinalities: (number | null)[];
              comment: string | null;
            }
          >();

          for (const row of rows) {
            const name = String(row.index_name);
            let entry = grouped.get(name);
            if (!entry) {
              entry = {
                name,
                isUnique: Number(row.non_unique) === 0,
                indexType: String(row.index_type ?? "BTREE"),
                columns: [],
                cardinalities: [],
                comment: row.comment ? String(row.comment) : null,
              };
              grouped.set(name, entry);
            }
            entry.columns.push(String(row.column_name));
            entry.cardinalities.push(row.cardinality === null ? null : Number(row.cardinality));
          }

          return jsonText({
            connection,
            schema: resolvedSchema,
            table,
            indexCount: grouped.size,
            indexes: [...grouped.values()],
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
