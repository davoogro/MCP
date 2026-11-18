import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { connectionInputSchema } from "./test-connection.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { errorResult } from "./common.js";

export const listSchemasTool = {
  name: "list_schemas",
  title: "List PostgreSQL schemas",
  description:
    "List the schemas the connected user can use in the alias database (system schemas excluded), with owner. " +
    "Next step: list_tables for a schema.",
  inputSchema: connectionInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) => async ({ connection }: z.infer<typeof connectionInputSchema>) => {
    try {
      const { rows } = await manager.query<{
        schema_name: string;
        schema_owner: string;
      }>(
        connection,
        `
          SELECT
            n.nspname AS schema_name,
            pg_get_userbyid(n.nspowner) AS schema_owner
          FROM pg_catalog.pg_namespace n
          WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            AND n.nspname NOT LIKE 'pg_temp_%'
            AND n.nspname NOT LIKE 'pg_toast_temp_%'
            AND has_schema_privilege(current_user, n.oid, 'USAGE')
          ORDER BY n.nspname
        `,
      );

      return jsonText({ connection, schemas: rows });
    } catch (error) {
      return errorResult(error);
    }
  },
};

export const listSchemasInputSchema = connectionInputSchema;
