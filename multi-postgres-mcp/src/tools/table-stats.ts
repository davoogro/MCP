import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

export const tableStatsInputSchema = z.object({
  connection: connectionField,
  schema: z.string().min(1).optional().describe("Filtra a un schema; omitido = todos los schemas de usuario"),
  top: z.number().int().positive().max(500).default(50).describe("Máximo de tablas, ordenadas por tamaño total"),
});

export const tableStatsTool = {
  name: "table_stats",
  title: "PostgreSQL table statistics",
  description:
    "Estadísticas desde pg_stat_user_tables: pg_total_relation_size, tamaño tabla/índices, n_live_tup, n_dead_tup, " +
    "last_vacuum/autovacuum/analyze y seq_scan vs idx_scan. Ordenado por tamaño descendente.",
  inputSchema: tableStatsInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, top }: z.infer<typeof tableStatsInputSchema>) => {
      try {
        const params: unknown[] = [];
        let where = "1=1";
        if (schema) {
          validateIdentifier(schema, "schema");
          where += " AND schemaname = $1";
          params.push(schema);
        }
        params.push(top);
        const limitParam = `$${params.length}`;

        return await manager.withClient(connection, async (client) => {
          const result = await client.query(
            `
              SELECT
                schemaname AS schema_name,
                relname AS table_name,
                pg_total_relation_size(relid) AS total_bytes,
                pg_relation_size(relid) AS table_bytes,
                pg_indexes_size(relid) AS index_bytes,
                n_live_tup,
                n_dead_tup,
                seq_scan,
                idx_scan,
                last_vacuum,
                last_autovacuum,
                last_analyze,
                last_autoanalyze
              FROM pg_stat_user_tables
              WHERE ${where}
              ORDER BY pg_total_relation_size(relid) DESC, schemaname, relname
              LIMIT ${limitParam}
            `,
            params,
          );

          return jsonText({
            connection,
            schema: schema ?? null,
            tableCount: result.rows.length,
            tables: result.rows.map((row) => ({
              schema: row.schema_name,
              table: row.table_name,
              totalBytes: row.total_bytes,
              tableBytes: row.table_bytes,
              indexBytes: row.index_bytes,
              liveRows: row.n_live_tup,
              deadRows: row.n_dead_tup,
              seqScan: row.seq_scan,
              idxScan: row.idx_scan,
              lastVacuum: row.last_vacuum,
              lastAutovacuum: row.last_autovacuum,
              lastAnalyze: row.last_analyze,
              lastAutoanalyze: row.last_autoanalyze,
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
