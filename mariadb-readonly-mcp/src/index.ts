#!/usr/bin/env node
import { config as loadDotenv } from "dotenv";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { loadConfig } from "./config.js";
import { createConnectionManager } from "./connection-manager.js";
import { buildServerInstructions } from "./instructions.js";
import { PACKAGE_ENV_PATH } from "./paths.js";
import { describeTableTool } from "./tools/describe-table.js";
import { executeQueryTool } from "./tools/execute-query.js";
import { explainQueryTool } from "./tools/explain-query.js";
import { exportQueryTool } from "./tools/export-query.js";
import { getActivityTool } from "./tools/get-activity.js";
import { getDefinitionTool } from "./tools/get-definition.js";
import { listConnectionsTool } from "./tools/list-connections.js";
import { listIndexesTool } from "./tools/list-indexes.js";
import { listRoutinesTool } from "./tools/list-routines.js";
import { queryAllTool } from "./tools/query-all.js";
import { searchTablesTool } from "./tools/search-tables.js";
import { serverInfoTool } from "./tools/server-info.js";
import { tableStatsTool } from "./tools/table-stats.js";
import { listDatabasesTool } from "./tools/list-databases.js";
import { listSchemasTool } from "./tools/list-schemas.js";
import { listTablesTool } from "./tools/list-tables.js";
import { sampleDataTool } from "./tools/sample-data.js";
import { searchColumnsTool } from "./tools/search-columns.js";
import { testConnectionTool } from "./tools/test-connection.js";

const SERVER_VERSION = "1.2.0";

function loadOptionalEnvFile(): void {
  // The package .env is resolved from the module location, so credentials load even when the
  // client (e.g. Claude Code) launches the server from another working directory.
  const candidates = [PACKAGE_ENV_PATH, resolve(process.cwd(), ".env")];
  for (const envPath of candidates) {
    if (existsSync(envPath)) {
      // Variables already set by the client (env block) win over the file; quiet keeps stdout clean for JSON-RPC.
      loadDotenv({ path: envPath, override: false, quiet: true });
      return;
    }
  }
}

function registerTools(server: McpServer, manager: ReturnType<typeof createConnectionManager>): void {
  const tools = [
    listConnectionsTool,
    testConnectionTool,
    listDatabasesTool,
    listSchemasTool,
    listTablesTool,
    describeTableTool,
    searchColumnsTool,
    sampleDataTool,
    executeQueryTool,
    explainQueryTool,
    tableStatsTool,
    listIndexesTool,
    getDefinitionTool,
    listRoutinesTool,
    getActivityTool,
    serverInfoTool,
    queryAllTool,
    searchTablesTool,
    exportQueryTool,
  ];

  for (const tool of tools) {
    server.registerTool(
      tool.name,
      {
        title: tool.title,
        description: tool.description,
        inputSchema: tool.inputSchema,
        annotations: tool.annotations,
      },
      // Each handler is typed against its own schema; the SDK validates input before calling it.
      tool.handler(manager) as Parameters<McpServer["registerTool"]>[2],
    );
  }
}

async function main(): Promise<void> {
  loadOptionalEnvFile();

  const appConfig = loadConfig();
  const manager = createConnectionManager(appConfig);

  const server = new McpServer(
    {
      name: "mariadb-readonly-mcp",
      version: SERVER_VERSION,
    },
    {
      instructions: buildServerInstructions(appConfig),
    },
  );

  registerTools(server, manager);

  const shutdown = async (signal: string): Promise<void> => {
    console.error(`Received ${signal}, shutting down mariadb-readonly-mcp`);
    await manager.close();
    process.exit(0);
  };

  process.on("SIGINT", () => {
    void shutdown("SIGINT");
  });

  process.on("SIGTERM", () => {
    void shutdown("SIGTERM");
  });

  // Exit cleanly when the client closes stdin (Claude Code / Cursor shutting the server down).
  process.stdin.on("close", () => {
    void shutdown("stdin close");
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`mariadb-readonly-mcp started via stdio (config: ${appConfig.configPath}, ${appConfig.connections.size} connections)`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
