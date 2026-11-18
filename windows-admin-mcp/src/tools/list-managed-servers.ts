import { z } from "zod";
import type { ServerManager } from "../server-manager.js";
import { jsonText } from "../security.js";
import type { ToolContext } from "../types.js";
import { LOCAL_READ_ONLY } from "./annotations.js";

export const listManagedServersTool = {
  name: "list_managed_servers",
  title: "List managed Windows servers",
  description:
    "List the allowlisted Windows servers from servers.json (local config only, no remote calls, no credentials). " +
    "Use the returned `name` as the `server` argument of every other tool. Also shows each server's policy flags " +
    "(allowReboot, allowServiceRestart, protectedRole, ...) so you can tell in advance whether a mutating action would be permitted.",
  annotations: LOCAL_READ_ONLY,
  inputSchema: z.object({}),
  handler: (manager: ServerManager) => async () => {
    const servers = manager.getServers().map((server) => ({
      name: server.name,
      address: server.address,
      transport: server.transport,
      port: server.port,
      allowReboot: server.allowReboot,
      allowForceReboot: server.allowForceReboot,
      allowServiceRestart: server.allowServiceRestart,
      protectedRole: server.protectedRole,
      allowProtectedReboot: server.allowProtectedReboot,
      criticalServices: server.criticalServices,
      tags: server.tags,
    }));
    return jsonText({ servers, count: servers.length });
  },
};

export function createToolContext(audit: { createCorrelationId(): string }): ToolContext {
  return { correlationId: audit.createCorrelationId() };
}
