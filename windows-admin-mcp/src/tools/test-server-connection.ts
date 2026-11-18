import { z } from "zod";
import type { ServerManager } from "../server-manager.js";
import { jsonError, jsonText } from "../security.js";
import { createToolContext } from "./list-managed-servers.js";
import { REMOTE_READ_ONLY } from "./annotations.js";

export const testServerConnectionTool = {
  name: "test_server_connection",
  title: "Test server connection",
  description:
    "Read-only connectivity probe to an allowlisted server over SSH or WinRM HTTPS (TCP port check + WS-Man/SSH handshake). " +
    "Run this first when a server is new or other tools fail. `transport` overrides the configured transport for this probe only.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    transport: z.enum(["auto", "ssh", "winrm"]).optional().default("auto"),
  }),
  handler: (manager: ServerManager) => async (args: { server: string; transport: string }) => {
    const ctx = createToolContext(manager.audit);
    try {
      manager.getServer(args.server);
      const result = await manager.testConnection(ctx, args.server, args.transport);
      return jsonText({ server: args.server, ...result });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};
