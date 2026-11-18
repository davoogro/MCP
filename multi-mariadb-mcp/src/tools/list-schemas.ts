import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { connectionInputSchema } from "./test-connection.js";
import { jsonText } from "../security.js";
import type { RowDataPacket } from "mysql2";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { errorResult } from "./common.js";

export const listSchemasTool = {
  name: "list_schemas",
  title: "List MariaDB schemas",
  description:
    "List every database (schema) visible on the server behind an alias, including system schemas, with default " +
    "charset. In MariaDB schema = database; list_databases returns the same with more detail. Next step: list_tables.",
  inputSchema: connectionInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) => async ({ connection }: z.infer<typeof connectionInputSchema>) => {
    try {
      const { rows } = await manager.query<RowDataPacket>(
        connection,
        `
          SELECT
            SCHEMA_NAME AS schema_name,
            DEFAULT_CHARACTER_SET_NAME AS default_charset
          FROM information_schema.SCHEMATA
          ORDER BY SCHEMA_NAME
        `,
      );

      return jsonText({ connection, schemas: rows });
    } catch (error) {
      return errorResult(error);
    }
  },
};

export const listSchemasInputSchema = connectionInputSchema;
