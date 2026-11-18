import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, toLikePattern } from "./common.js";

export const listFunctionsInputSchema = z.object({
  connection: connectionField,
  schema: z.string().min(1).optional().describe("Filtra a un schema; omitido = todos los schemas accesibles"),
  search: z.string().min(1).optional().describe("Filtra por nombre (ILIKE); texto libre se envuelve con %"),
});

export const listFunctionsTool = {
  name: "list_functions",
  title: "List PostgreSQL functions",
  description:
    "Lista funciones (pg_proc, prokind f) con argumentos, tipo de retorno, lenguaje y volatilidad. " +
    "Usa get_definition para el DDL completo.",
  inputSchema: listFunctionsInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, search }: z.infer<typeof listFunctionsInputSchema>) => {
      try {
        const params: unknown[] = [];
        let where = "p.prokind = 'f' AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'";
        if (schema) {
          validateIdentifier(schema, "schema");
          params.push(schema);
          where += ` AND n.nspname = $${params.length}`;
        }
        if (search) {
          params.push(toLikePattern(search));
          where += ` AND p.proname ILIKE $${params.length}`;
        }

        return await manager.withClient(connection, async (client) => {
          const result = await client.query(
            `
              SELECT
                n.nspname AS schema_name,
                p.proname AS function_name,
                pg_get_function_identity_arguments(p.oid) AS arguments,
                pg_get_function_result(p.oid) AS return_type,
                l.lanname AS language,
                p.provolatile AS volatility,
                p.prosecdef AS security_definer,
                obj_description(p.oid, 'pg_proc') AS comment
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_language l ON l.oid = p.prolang
              WHERE ${where}
              ORDER BY n.nspname, p.proname, arguments
              LIMIT 500
            `,
            params,
          );

          return jsonText({
            connection,
            schema: schema ?? null,
            search: search ?? null,
            functionCount: result.rows.length,
            functions: result.rows.map((row) => ({
              schema: row.schema_name,
              name: row.function_name,
              arguments: row.arguments,
              returnType: row.return_type,
              language: row.language,
              volatility: row.volatility,
              securityDefiner: row.security_definer,
              ...(row.comment ? { comment: row.comment } : {}),
            })),
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
