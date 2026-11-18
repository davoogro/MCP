import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";
import { connectionField, errorResult } from "./common.js";

export const connectionInputSchema = z.object({
  connection: connectionField,
});

export const testConnectionTool = {
  name: "test_connection",
  title: "Test PostgreSQL connection",
  description:
    "Check that a configured PostgreSQL alias is reachable. Returns the database name, server version, latency, " +
    "whether TLS is in use and whether the alias is read-only. Use it to diagnose connectivity before exploring.",
  inputSchema: connectionInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: (manager: ConnectionManager) => async ({ connection }: z.infer<typeof connectionInputSchema>) => {
    try {
      const result = await manager.testConnection(connection);
      return jsonText(result);
    } catch (error) {
      return errorResult(error);
    }
  },
};
