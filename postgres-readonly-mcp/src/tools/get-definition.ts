import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, schemaField } from "./common.js";

const kindSchema = z.enum(["view", "function", "matview", "trigger"]);

export const getDefinitionInputSchema = z.object({
  connection: connectionField,
  name: z.string().min(1).describe("Nombre del objeto (vista, función, vista materializada o trigger)"),
  schema: schemaField.describe("Schema del objeto (default public)"),
  kind: kindSchema.default("view").describe("Tipo de objeto"),
});

export const getDefinitionTool = {
  name: "get_definition",
  title: "PostgreSQL object definition",
  description:
    "DDL o definición de una vista, vista materializada, función o trigger (pg_get_viewdef, pg_get_functiondef, " +
    "pg_get_triggerdef). Solo lectura.",
  inputSchema: getDefinitionInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, name, schema, kind }: z.infer<typeof getDefinitionInputSchema>) => {
      try {
        validateIdentifier(schema, "schema");
        validateIdentifier(name, "name");

        return await manager.withClient(connection, async (client) => {
          let definition: string | null = null;

          if (kind === "view") {
            const result = await client.query(
              `
                SELECT pg_get_viewdef(c.oid, true) AS definition
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'v'
              `,
              [schema, name],
            );
            definition = result.rows[0]?.definition ?? null;
          } else if (kind === "matview") {
            const result = await client.query(
              `
                SELECT pg_get_viewdef(c.oid, true) AS definition
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'm'
              `,
              [schema, name],
            );
            definition = result.rows[0]?.definition ?? null;
          } else if (kind === "function") {
            const result = await client.query(
              `
                SELECT pg_get_functiondef(p.oid) AS definition
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = $1 AND p.proname = $2
                ORDER BY pg_get_function_identity_arguments(p.oid)
                LIMIT 1
              `,
              [schema, name],
            );
            definition = result.rows[0]?.definition ?? null;
          } else {
            const result = await client.query(
              `
                SELECT pg_get_triggerdef(t.oid, true) AS definition
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1 AND t.tgname = $2 AND NOT t.tgisinternal
              `,
              [schema, name],
            );
            definition = result.rows[0]?.definition ?? null;
          }

          if (!definition) {
            return jsonError(`Object '${schema}.${name}' (${kind}) was not found`);
          }

          return jsonText({
            connection,
            schema,
            name,
            kind,
            definition,
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
