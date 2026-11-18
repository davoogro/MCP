import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { LOCAL_READ_ONLY } from "./annotations.js";

export const listConnectionsTool = {
  name: "list_connections",
  title: "List PostgreSQL connections",
  description:
    "List the configured PostgreSQL connection aliases (host, port, database, read-only flag, maxRows, last known status). " +
    "No credentials are returned and no database is contacted. Start here to pick the 'connection' for other tools.",
  inputSchema: z.object({}),
  annotations: LOCAL_READ_ONLY,
  handler: (manager: ConnectionManager) => async () => jsonText({ connections: manager.listConnections() }),
};
