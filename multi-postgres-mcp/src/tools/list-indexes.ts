import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, schemaField, tableField } from "./common.js";

export const listIndexesInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  table: tableField,
});

export const listIndexesTool = {
  name: "list_indexes",
  title: "List PostgreSQL table indexes",
  description:
    "Índices de una tabla: columnas, unicidad, definición (pg_get_indexdef), idx_scan desde pg_stat_user_indexes " +
    "y tamaño (pg_relation_size). Complementa describe_table.",
  inputSchema: listIndexesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, table }: z.infer<typeof listIndexesInputSchema>) => {
      try {
        validateIdentifier(schema, "schema");
        validateIdentifier(table, "table");

        return await manager.withClient(connection, async (client) => {
          const result = await client.query(
            `
              SELECT
                i.relname AS index_name,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary,
                pg_get_indexdef(ix.indexrelid) AS definition,
                COALESCE(st.idx_scan, 0) AS idx_scan,
                pg_relation_size(i.oid) AS index_bytes,
                array_agg(a.attname ORDER BY k.ord) AS column_names
              FROM pg_class t
              JOIN pg_namespace n ON n.oid = t.relnamespace
              JOIN pg_index ix ON t.oid = ix.indrelid
              JOIN pg_class i ON i.oid = ix.indexrelid
              JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
              JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
              LEFT JOIN pg_stat_user_indexes st ON st.indexrelid = i.oid
              WHERE n.nspname = $1 AND t.relname = $2
              GROUP BY i.relname, ix.indisunique, ix.indisprimary, ix.indexrelid, i.oid, st.idx_scan
              ORDER BY i.relname
            `,
            [schema, table],
          );

          return jsonText({
            connection,
            schema,
            table,
            indexCount: result.rows.length,
            indexes: result.rows.map((row) => ({
              name: row.index_name,
              isUnique: row.is_unique,
              isPrimary: row.is_primary,
              definition: row.definition,
              idxScan: row.idx_scan,
              indexBytes: row.index_bytes,
              columns: row.column_names,
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
