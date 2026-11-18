import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField } from "./common.js";

export const listRoutinesInputSchema = z.object({
  connection: connectionField,
  schema: schemaField.describe("Filtra a un schema; omitido = todos los schemas accesibles"),
  includeSystem: z.boolean().default(false),
});

export const listRoutinesTool = {
  name: "list_routines",
  title: "List MariaDB routines",
  description:
    "Lista procedimientos y funciones almacenados desde information_schema.ROUTINES (tipo, retorno, definer, " +
    "created/modified). Usa get_definition para ver el DDL completo.",
  inputSchema: listRoutinesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, includeSystem }: z.infer<typeof listRoutinesInputSchema>) => {
      try {
        const params: unknown[] = [];
        let where = "1=1";
        if (schema) {
          const resolved = resolveSchema(manager, connection, schema);
          where += " AND ROUTINE_SCHEMA = ?";
          params.push(resolved);
        } else if (!includeSystem) {
          where += " AND ROUTINE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')";
        }

        return await manager.withClient(connection, async (client) => {
          const [rows] = await client.query<RowDataPacket[]>(
            `
              SELECT
                ROUTINE_SCHEMA AS schema_name,
                ROUTINE_NAME AS routine_name,
                ROUTINE_TYPE AS routine_type,
                DTD_IDENTIFIER AS return_type,
                ROUTINE_DEFINITION IS NOT NULL AS has_inline_definition,
                DEFINER AS definer,
                SQL_DATA_ACCESS AS sql_data_access,
                IS_DETERMINISTIC AS is_deterministic,
                CREATED AS created,
                LAST_ALTERED AS last_altered,
                ROUTINE_COMMENT AS comment
              FROM information_schema.ROUTINES
              WHERE ${where}
              ORDER BY ROUTINE_SCHEMA, ROUTINE_TYPE, ROUTINE_NAME
              LIMIT 500
            `,
            params,
          );

          return jsonText({
            connection,
            schema: schema ?? null,
            routineCount: rows.length,
            routines: rows.map((row) => ({
              schema: row.schema_name,
              name: row.routine_name,
              type: row.routine_type,
              returnType: row.return_type,
              hasInlineDefinition: Boolean(row.has_inline_definition),
              definer: row.definer,
              sqlDataAccess: row.sql_data_access,
              isDeterministic: row.is_deterministic === "YES",
              created: row.created,
              lastAltered: row.last_altered,
              ...(row.comment ? { comment: row.comment } : {}),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
