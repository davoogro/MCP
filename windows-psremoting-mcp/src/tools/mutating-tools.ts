import { z } from "zod";
import type { ServerManager } from "../server-manager.js";
import { evaluateRebootPolicy, evaluateServiceRestartPolicy } from "../policy/reboot-guard.js";
import {
  isServiceRestartDenied,
  jsonError,
  jsonText,
  validateReason,
  validateServiceName,
} from "../security.js";
import { createToolContext } from "./list-managed-servers.js";
import { REMOTE_DESTRUCTIVE, REMOTE_READ_ONLY } from "./annotations.js";

export const restartServiceTool = {
  name: "restart_service",
  title: "Restart Windows service (MUTATING)",
  description:
    "MUTATING - restarts a Windows service on a production server. dryRun defaults to true (validates policy and " +
    "connectivity without restarting). A real restart (dryRun=false) requires allowServiceRestart=true for the server, " +
    "is refused for protected services (NTDS, DNS, ClusSvc, W32Time, Kdc, Iscsi, MSExchange*) and for the local host, " +
    "and is written to the audit log with the mandatory `reason` (10-500 chars). Only call with dryRun=false after the " +
    "user has explicitly approved this exact server and service.",
  annotations: REMOTE_DESTRUCTIVE,
  inputSchema: z.object({
    server: z.string().min(1),
    serviceName: z.string().min(1).max(256),
    reason: z.string().min(10).max(500),
    dryRun: z.boolean().optional().default(true),
  }),
  handler: (manager: ServerManager) =>
    async (args: { server: string; serviceName: string; reason: string; dryRun: boolean }) => {
      const ctx = createToolContext(manager.audit);
      try {
        validateReason(args.reason);
        validateServiceName(args.serviceName);
        const server = manager.getServer(args.server);
        const dryRun = args.dryRun ?? true;

        if (isServiceRestartDenied(args.serviceName)) {
          const message = `Service '${args.serviceName}' is in the protected denylist`;
          manager.audit.write({
            correlationId: ctx.correlationId,
            eventType: "policy.denied",
            tool: "restart_service",
            serverName: server.name,
            serverAddress: server.address,
            outcome: "denied",
            durationMs: 0,
            reason: args.reason,
            dryRun,
            parameters: { server: args.server, serviceName: args.serviceName, dryRun },
            policyChecks: [{ check: "serviceDenylist", passed: false, detail: message }],
            error: { code: "POLICY_DENIED", message, category: "policy" },
          });
          return jsonError(message);
        }

        const policy = evaluateServiceRestartPolicy(server, args.serviceName, dryRun);
        manager.audit.write({
          correlationId: ctx.correlationId,
          eventType: policy.allowed ? "tool.invoke" : "policy.denied",
          tool: "restart_service",
          serverName: server.name,
          serverAddress: server.address,
          outcome: policy.allowed ? (dryRun ? "dry_run" : "success") : "denied",
          durationMs: 0,
          reason: args.reason,
          dryRun,
          policyChecks: policy.checks,
          error: policy.allowed
            ? null
            : {
                code: policy.errorCode ?? "POLICY_DENIED",
                message: policy.message ?? "Service restart denied",
                category: "policy",
              },
        });

        if (!policy.allowed) {
          return jsonError(policy.message ?? "Service restart denied by policy");
        }

        const result = await manager.invokeOperation(ctx, "restart_service", args.server, "restart_service", {
          serviceName: args.serviceName,
          reason: args.reason,
          dryRun,
        });

        return jsonText({
          server: args.server,
          serviceName: args.serviceName,
          dryRun,
          policyChecks: policy.checks,
          ...(result.data as object),
        });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};

export const restartServerTool = {
  name: "restart_server",
  title: "Reboot Windows server (MUTATING)",
  description:
    "MUTATING / DESTRUCTIVE - reboots a production Windows server. dryRun defaults to true (policy check only). " +
    "A real reboot (dryRun=false) requires allowReboot=true; protected roles / domain-controller / cluster-node / fsmo " +
    "tags also need allowProtectedReboot=true; force=true needs allowForceReboot=true; the local host is always blocked; " +
    "only one reboot at a time. Every attempt (allowed or denied) is audited with the mandatory `reason`. Never call with " +
    "dryRun=false unless the user has explicitly approved rebooting this exact server now. Follow up with wait_for_server_online.",
  annotations: REMOTE_DESTRUCTIVE,
  inputSchema: z.object({
    server: z.string().min(1),
    reason: z.string().min(10).max(500),
    dryRun: z.boolean().optional().default(true),
    force: z.boolean().optional().default(false),
    waitForRecovery: z.boolean().optional().default(true),
  }),
  handler: (manager: ServerManager) =>
    async (args: {
      server: string;
      reason: string;
      dryRun?: boolean;
      force?: boolean;
      waitForRecovery?: boolean;
    }) => {
      const ctx = createToolContext(manager.audit);
      const started = Date.now();
      try {
        validateReason(args.reason);
        const server = manager.getServer(args.server);
        const dryRun = args.dryRun ?? process.env.WINMCP_DEFAULT_DRY_RUN !== "false";
        const request = {
          serverName: server.name,
          reason: args.reason,
          dryRun,
          force: args.force ?? false,
          waitForRecovery: args.waitForRecovery ?? true,
          rebootTimeoutSeconds: manager.config.defaults.rebootTimeoutSeconds,
        };

        const policy = evaluateRebootPolicy(manager.config, server, request);
        manager.audit.write({
          correlationId: ctx.correlationId,
          eventType: policy.allowed ? "tool.invoke" : "policy.denied",
          tool: "restart_server",
          serverName: server.name,
          serverAddress: server.address,
          outcome: policy.allowed ? (dryRun ? "dry_run" : "success") : "denied",
          durationMs: 0,
          reason: args.reason,
          dryRun,
          parameters: { server: args.server, dryRun, force: request.force, waitForRecovery: request.waitForRecovery },
          policyChecks: policy.checks,
          error: policy.allowed
            ? null
            : {
                code: policy.errorCode ?? "POLICY_DENIED",
                message: policy.message ?? "Reboot denied",
                category: "policy",
              },
        });

        if (!policy.allowed) {
          return jsonError(policy.message ?? "Reboot denied by policy");
        }

        const result = await manager.rebootServer(ctx, request);
        manager.audit.write({
          correlationId: ctx.correlationId,
          eventType: "tool.result",
          tool: "restart_server",
          serverName: server.name,
          serverAddress: server.address,
          outcome: dryRun ? "dry_run" : result.hostRecovered ? "success" : "failure",
          durationMs: Date.now() - started,
          reason: args.reason,
          dryRun,
          policyChecks: policy.checks,
          stateAfter: result,
        });

        return jsonText({
          server: args.server,
          reason: args.reason,
          policyChecks: policy.checks,
          ...result,
        });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};

export const waitForServerOnlineTool = {
  name: "wait_for_server_online",
  title: "Wait for server online",
  description:
    "Read-only polling: waits (up to timeoutSeconds, default rebootTimeoutSeconds) until an allowlisted server answers on " +
    "SSH/WinRM again, typically after a reboot. Does not change the server. Can block for several minutes.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    timeoutSeconds: z.number().int().min(30).max(900).optional(),
    pollIntervalSeconds: z.number().int().min(5).max(60).optional().default(15),
    validateServices: z.boolean().optional().default(true),
  }),
  handler: (manager: ServerManager) =>
    async (args: {
      server: string;
      timeoutSeconds?: number;
      pollIntervalSeconds?: number;
      validateServices?: boolean;
    }) => {
      const ctx = createToolContext(manager.audit);
      try {
        manager.getServer(args.server);
        const timeoutSeconds = args.timeoutSeconds ?? manager.config.defaults.rebootTimeoutSeconds;
        const result = await manager.waitForOnline(
          ctx,
          args.server,
          timeoutSeconds,
          args.pollIntervalSeconds ?? 15,
          args.validateServices ?? true,
        );
        return jsonText({ server: args.server, ...result, timeoutSeconds });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};

export const runApprovedDiagnosticTool = {
  name: "run_approved_diagnostic",
  title: "Run approved diagnostic",
  description:
    "Read-only diagnostics from a closed list (no arbitrary commands): ping {count}, dns_resolve {hostname}, " +
    "tcp_port_check {port: 22|445|3389|5986}, winrm_listener, disk_smart_status, service_dependencies {serviceName}. " +
    "Pass the extra values in `parameters`.",
  annotations: REMOTE_READ_ONLY,
  inputSchema: z.object({
    server: z.string().min(1),
    diagnostic: z.enum([
      "ping",
      "dns_resolve",
      "tcp_port_check",
      "winrm_listener",
      "disk_smart_status",
      "service_dependencies",
    ]),
    parameters: z.record(z.union([z.string(), z.number(), z.boolean()])).optional(),
  }),
  handler: (manager: ServerManager) =>
    async (args: {
      server: string;
      diagnostic: string;
      parameters?: Record<string, string | number | boolean>;
    }) => {
      const ctx = createToolContext(manager.audit);
      try {
        const result = await manager.invokeOperation(ctx, "run_approved_diagnostic", args.server, "diagnostic", {
          diagnostic: args.diagnostic,
          parameters: args.parameters ?? {},
        });
        if (!result.success) return jsonError(result.sanitizedError ?? "Diagnostic failed");
        return jsonText({ server: args.server, diagnostic: args.diagnostic, ...(result.data as object) });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};
