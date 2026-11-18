import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { LOCAL_READ_ONLY } from "./annotations.js";

export const listConnectionsTool = {
  name: "list_connections",
  title: "List MariaDB connections",
  description:
    "List the configured MariaDB/MySQL connection aliases (host, port, default database, read-only flag, maxRows, last " +
    "known status). No credentials are returned and no server is contacted. Start here to pick the 'connection'.",
  inputSchema: z.object({}),
  annotations: LOCAL_READ_ONLY,
  handler: (manager: ConnectionManager) => async () => jsonText({ connections: manager.listConnections() }),
};
