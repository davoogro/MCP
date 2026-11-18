import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

export const listDatabasesInputSchema = z.object({
  connection: connectionField,
  includeSize: z
    .boolean()
    .default(false)
    .describe("Also compute each database size (can be slow on servers with many or large databases)"),
});

export const listDatabasesTool = {
  name: "list_databases",
  title: "List PostgreSQL databases",
  description:
    "List the databases on the server behind an alias (name, owner, encoding, optional size) and which " +
    "configured aliases point at each one. PostgreSQL cannot query across databases, so to explore another database " +
    "use (or ask the user to add) an alias for it.",
  inputSchema: listDatabasesInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) => async ({ connection, includeSize }: z.infer<typeof listDatabasesInputSchema>) => {
    try {
      const current = manager.getConnectionConfig(connection);
      const { rows } = await manager.query<{
        database_name: string;
        owner: string;
        encoding: string;
        allow_connections: boolean;
        can_connect: boolean;
        size: string | null;
      }>(
        connection,
        `
          SELECT
            d.datname AS database_name,
            pg_catalog.pg_get_userbyid(d.datdba) AS owner,
            pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
            d.datallowconn AS allow_connections,
            has_database_privilege(d.datname, 'CONNECT') AS can_connect,
            CASE
              WHEN $1::boolean AND d.datallowconn AND has_database_privilege(d.datname, 'CONNECT')
                THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname))
            END AS size
          FROM pg_catalog.pg_database d
          WHERE NOT d.datistemplate
          ORDER BY d.datname
        `,
        [includeSize],
      );

      const sameServer = manager
        .listConnections()
        .filter((item) => item.host.toLowerCase() === current.host.toLowerCase() && item.port === current.port);

      return jsonText({
        connection,
        server: `${current.host}:${current.port}`,
        databases: rows.map((row) => ({
          ...row,
          isCurrent: row.database_name === current.database,
          aliases: sameServer.filter((item) => item.database === row.database_name).map((item) => item.alias),
        })),
      });
    } catch (error) {
      return errorResult(error);
    }
  },
};
