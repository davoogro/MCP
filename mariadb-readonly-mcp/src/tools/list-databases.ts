import { z } from "zod";
import type { RowDataPacket } from "mysql2";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { SYSTEM_SCHEMAS, connectionField, errorResult } from "./common.js";

export const listDatabasesInputSchema = z.object({
  connection: connectionField,
  includeSystem: z
    .boolean()
    .default(false)
    .describe("Include system databases (information_schema, performance_schema, mysql, sys); default false"),
});

export const listDatabasesTool = {
  name: "list_databases",
  title: "List MariaDB databases",
  description:
    "List the databases (SHOW DATABASES equivalent) on the server behind an alias, with charset/collation and which " +
    "one is the alias default. Any of them can be explored with the same alias: pass it as 'schema' to list_tables, " +
    "describe_table, sample_data and search_columns, or as 'database' to execute_query (or qualify names as db.table).",
  inputSchema: listDatabasesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) =>
    async ({ connection, includeSystem }: z.infer<typeof listDatabasesInputSchema>) => {
      try {
        const defaultDatabase = manager.getConnectionConfig(connection).database;
        const { rows } = await manager.query<RowDataPacket>(
          connection,
          `
            SELECT
              SCHEMA_NAME AS database_name,
              DEFAULT_CHARACTER_SET_NAME AS default_charset,
              DEFAULT_COLLATION_NAME AS default_collation
            FROM information_schema.SCHEMATA
            ORDER BY SCHEMA_NAME
          `,
        );

        const databases = rows
          .map((row) => {
            const name = String(row.database_name);
            return {
              database_name: name,
              default_charset: row.default_charset,
              default_collation: row.default_collation,
              isSystem: (SYSTEM_SCHEMAS as readonly string[]).includes(name.toLowerCase()),
              isDefault: name === defaultDatabase,
            };
          })
          .filter((row) => includeSystem || !row.isSystem || row.isDefault);

        return jsonText({ connection, defaultDatabase, databases });
      } catch (error) {
        return errorResult(error);
      }
    },
};
