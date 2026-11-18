#!/usr/bin/env node
import { config as loadDotenv } from "dotenv";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createAuditLogger } from "./audit/logger.js";
import { loadConfig } from "./config.js";
import { createServerManager } from "./server-manager.js";
import { getPackageRoot, resolvePowerShellExecutable } from "./paths.js";
import type { ToolAnnotationsHint } from "./tools/annotations.js";
import { listManagedServersTool } from "./tools/list-managed-servers.js";
import { testServerConnectionTool } from "./tools/test-server-connection.js";
import {
  getDiskUsageTool,
  getPendingRebootStatusTool,
  getRecentSystemEventsTool,
  getServerHealthTool,
  getServerUptimeTool,
  getServiceStatusTool,
} from "./tools/read-tools.js";
import {
  restartServerTool,
  restartServiceTool,
  runApprovedDiagnosticTool,
  waitForServerOnlineTool,
} from "./tools/mutating-tools.js";

const SERVER_INSTRUCTIONS = `windows-admin-mcp: controlled administration of PRODUCTION Windows servers over PowerShell remoting (SSH or WinRM HTTPS).

Safety model:
- Allowlist: only servers listed in servers.json can be targeted. Call list_managed_servers first and use the exact \`name\` it returns.
- Read-only tools (list_managed_servers, test_server_connection, get_*, run_approved_diagnostic, wait_for_server_online) never change server state.
- Mutating tools (restart_service, restart_server) default to dryRun=true. A real action needs dryRun=false AND the per-server policy flag (allowServiceRestart / allowReboot / allowForceReboot / allowProtectedReboot); protected services and the local host are always refused; only one reboot runs at a time.
- Never set dryRun=false on your own initiative: first run the dry run, show the user the policy checks, and get explicit approval for that exact server and action.
- Every call (including denied ones) is written to a JSONL audit log with a correlation id; secrets are redacted.
- Credentials come from Windows Credential Manager, SecretManagement or env vars referenced in servers.json; they are never returned by any tool.`;

function loadOptionalEnvFile(): void {
  // .env next to package.json (never the process cwd); existing env vars always win.
  const envPath = join(getPackageRoot(), ".env");
  if (existsSync(envPath)) {
    loadDotenv({ path: envPath, override: false, quiet: true });
  }
}

function registerTools(server: McpServer, manager: ReturnType<typeof createServerManager>): void {
  const tools = [
    listManagedServersTool,
    testServerConnectionTool,
    getServerHealthTool,
    getServerUptimeTool,
    getDiskUsageTool,
    getServiceStatusTool,
    getRecentSystemEventsTool,
    getPendingRebootStatusTool,
    restartServiceTool,
    restartServerTool,
    waitForServerOnlineTool,
    runApprovedDiagnosticTool,
  ];

  for (const tool of tools) {
    const definition = tool as typeof tool & { title?: string; annotations?: ToolAnnotationsHint };
    server.registerTool(
      tool.name,
      {
        title: definition.title,
        description: tool.description,
        inputSchema: tool.inputSchema,
        annotations: definition.annotations ? { title: definition.title, ...definition.annotations } : undefined,
      },
      tool.handler(manager),
    );
  }
}

async function main(): Promise<void> {
  loadOptionalEnvFile();

  const audit = createAuditLogger();
  const appConfig = loadConfig();
  const manager = createServerManager(appConfig, {}, audit);

  audit.write({
    correlationId: audit.createCorrelationId(),
    eventType: "server.start",
    tool: "server",
    outcome: "success",
    durationMs: 0,
    parameters: {
      configPath: appConfig.configPath,
      serverCount: appConfig.servers.size,
    },
  });

  const server = new McpServer(
    {
      name: "windows-admin-mcp",
      version: "1.1.0",
    },
    { instructions: SERVER_INSTRUCTIONS },
  );

  registerTools(server, manager);

  const shutdown = async (signal: string): Promise<void> => {
    console.error(`Received ${signal}, shutting down windows-admin-mcp`);
    audit.write({
      correlationId: audit.createCorrelationId(),
      eventType: "server.stop",
      tool: "server",
      outcome: "success",
      durationMs: 0,
      parameters: { signal },
    });
    await manager.close();
    process.exit(0);
  };

  process.on("SIGINT", () => {
    void shutdown("SIGINT");
  });
  process.on("SIGTERM", () => {
    void shutdown("SIGTERM");
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `windows-admin-mcp started via stdio (config=${appConfig.configPath}, servers=${appConfig.servers.size}, powershell=${resolvePowerShellExecutable()})`,
  );
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
