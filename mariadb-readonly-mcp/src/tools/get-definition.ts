import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { validateIdentifier, quoteIdentifier, jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult, resolveSchema, schemaField } from "./common.js";

const kindSchema = z.enum(["view", "procedure", "function", "trigger", "event"]);

export const getDefinitionInputSchema = z.object({
  connection: connectionField,
  schema: schemaField,
  name: z.string().min(1).describe("Nombre del objeto (vista, procedimiento, función, trigger o evento)"),
  kind: kindSchema.default("view").describe("Tipo de objeto"),
});

function buildShowCreate(kind: z.infer<typeof kindSchema>, qualified: string): string {
  switch (kind) {
    case "view":
      return `SHOW CREATE VIEW ${qualified}`;
    case "procedure":
      return `SHOW CREATE PROCEDURE ${qualified}`;
    case "function":
      return `SHOW CREATE FUNCTION ${qualified}`;
    case "trigger":
      return `SHOW CREATE TRIGGER ${qualified}`;
    case "event":
      return `SHOW CREATE EVENT ${qualified}`;
  }
}

function extractCreateStatement(row: RowDataPacket): string | null {
  for (const value of Object.values(row)) {
    if (typeof value === "string" && /CREATE\s/i.test(value)) {
      return value;
    }
  }
  const createView = row["Create View"] ?? row["Create View".toLowerCase()];
  if (typeof createView === "string") {
    return createView;
  }
  return null;
}

export const getDefinitionTool = {
  name: "get_definition",
  title: "MariaDB object DDL definition",
  description:
    "Obtiene el DDL con SHOW CREATE para una vista, procedimiento, función, trigger o evento en un schema. " +
    "Solo lectura (no ejecuta el objeto).",
  inputSchema: getDefinitionInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, schema, name, kind }: z.infer<typeof getDefinitionInputSchema>) => {
      try {
        const resolvedSchema = resolveSchema(manager, connection, schema);
        validateIdentifier(name, "name");
        const qualified = `${quoteIdentifier(resolvedSchema)}.${quoteIdentifier(name)}`;
        const sql = buildShowCreate(kind, qualified);

        return await manager.withClient(connection, async (client) => {
          const [rows] = await client.query<RowDataPacket[]>(sql);
          if (rows.length === 0) {
            return jsonError(`Object '${resolvedSchema}.${name}' (${kind}) was not found`);
          }
          const ddl = extractCreateStatement(rows[0]!);
          if (!ddl) {
            return jsonError("Could not read CREATE statement from server response");
          }
          return jsonText({
            connection,
            schema: resolvedSchema,
            name,
            kind,
            definition: ddl,
          });
        });
      } catch (error) {
        return errorResult(error);
      }
    },
};
