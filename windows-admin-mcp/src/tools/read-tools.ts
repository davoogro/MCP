import { z } from "zod";
import type { ServerManager } from "../server-manager.js";
import { jsonError, jsonText } from "../security.js";
import { createToolContext } from "./list-managed-servers.js";
import { REMOTE_READ_ONLY } from "./annotations.js";

export const getServerHealthTool = {
  name: "get_server_health",
  title: "Get server health",
  description:
    "Read-only. OS name/version, last boot time, total/free memory and CPU load of an allowlisted server. " +
    "Set includeServices=true to also check the server's configured critical services.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    includeServices: z.boolean().optional().default(false),
  }),
  handler: (manager: ServerManager) => async (args: { server: string; includeServices: boolean }) => {
    const ctx = createToolContext(manager.audit);
    try {
      const server = manager.getServer(args.server);
      const result = await manager.invokeOperation(ctx, "get_server_health", args.server, "get_health", {
        includeServices: args.includeServices,
        criticalServices: server.criticalServices,
      });
      if (!result.success) {
        return jsonError(result.sanitizedError ?? "Health query failed");
      }
      return jsonText({ server: args.server, ...(result.data as object) });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};

export const getServerUptimeTool = {
  name: "get_server_uptime",
  title: "Get server uptime",
  description:
    "Read-only. Last boot time (UTC) and uptime in seconds of an allowlisted server.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
  }),
  handler: (manager: ServerManager) => async (args: { server: string }) => {
    const ctx = createToolContext(manager.audit);
    try {
      const result = await manager.invokeOperation(ctx, "get_server_uptime", args.server, "get_uptime");
      if (!result.success) return jsonError(result.sanitizedError ?? "Uptime query failed");
      return jsonText({ server: args.server, ...(result.data as object) });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};

export const getDiskUsageTool = {
  name: "get_disk_usage",
  title: "Get disk usage",
  description:
    "Read-only. Size, free space and used percent of fixed disks on an allowlisted server. " +
    "Optionally filter with driveLetters, e.g. [\"C:\", \"D:\"].",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    driveLetters: z.array(z.string()).optional(),
  }),
  handler: (manager: ServerManager) => async (args: { server: string; driveLetters?: string[] }) => {
    const ctx = createToolContext(manager.audit);
    try {
      const result = await manager.invokeOperation(ctx, "get_disk_usage", args.server, "get_disk_usage", {
        driveLetters: args.driveLetters ?? [],
      });
      if (!result.success) return jsonError(result.sanitizedError ?? "Disk usage query failed");
      return jsonText({ server: args.server, ...(result.data as object) });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};

export const getServiceStatusTool = {
  name: "get_service_status",
  title: "Get service status",
  description:
    "Read-only. Status (Running/Stopped/NotFound) and display name of 1-20 Windows services, by service name " +
    "(e.g. W32Time, WinRM), on an allowlisted server.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    serviceNames: z.array(z.string().min(1).max(256)).min(1).max(20),
  }),
  handler: (manager: ServerManager) => async (args: { server: string; serviceNames: string[] }) => {
    const ctx = createToolContext(manager.audit);
    try {
      const result = await manager.invokeOperation(ctx, "get_service_status", args.server, "get_service_status", {
        serviceNames: args.serviceNames,
      });
      if (!result.success) return jsonError(result.sanitizedError ?? "Service status query failed");
      return jsonText({ server: args.server, ...(result.data as object) });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};

export const getRecentSystemEventsTool = {
  name: "get_recent_system_events",
  title: "Get recent event log entries",
  description:
    "Read-only. Recent entries from the System, Application or Security event log of an allowlisted server, " +
    "filtered by level (default Error) and time window in hours (1-72). Messages are truncated to 200 chars.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    logName: z.enum(["System", "Application", "Security"]).default("System"),
    hours: z.number().int().min(1).max(72).default(24),
    level: z.enum(["Critical", "Error", "Warning", "Information"]).default("Error"),
    maxEvents: z.number().int().min(1).max(100).default(50),
  }),
  handler: (manager: ServerManager) =>
    async (args: {
      server: string;
      logName: "System" | "Application" | "Security";
      hours: number;
      level: "Critical" | "Error" | "Warning" | "Information";
      maxEvents: number;
    }) => {
      const ctx = createToolContext(manager.audit);
      try {
        const result = await manager.invokeOperation(
          ctx,
          "get_recent_system_events",
          args.server,
          "get_recent_events",
          args,
        );
        if (!result.success) return jsonError(result.sanitizedError ?? "Event query failed");
        return jsonText({ server: args.server, ...(result.data as object) });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};

export const getPendingRebootStatusTool = {
  name: "get_pending_reboot_status",
  title: "Get pending reboot status",
  description:
    "Read-only. Whether an allowlisted server has a pending reboot (Windows Update / CBS registry flags) and why.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
  }),
  handler: (manager: ServerManager) => async (args: { server: string }) => {
    const ctx = createToolContext(manager.audit);
    try {
      const result = await manager.invokeOperation(
        ctx,
        "get_pending_reboot_status",
        args.server,
        "get_pending_reboot",
      );
      if (!result.success) return jsonError(result.sanitizedError ?? "Pending reboot query failed");
      return jsonText({ server: args.server, ...(result.data as object) });
    } catch (error) {
      return jsonError(error instanceof Error ? error.message : String(error));
    }
  },
};
