import { describe, expect, it } from "vitest";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { writeTempConfig } from "./helpers/fixtures.js";

const REQUIRED_TOOLS = [
  "list_managed_servers",
  "test_server_connection",
  "get_server_health",
  "get_server_uptime",
  "get_disk_usage",
  "get_service_status",
  "get_recent_system_events",
  "get_pending_reboot_status",
  "restart_service",
  "restart_server",
  "wait_for_server_online",
  "run_approved_diagnostic",
];

const MUTATING_TOOLS = ["restart_service", "restart_server"];

describe("MCP protocol", () => {
  it("starts, handshakes, and enumerates required tools", async () => {
    const distEntry = resolve(process.cwd(), "dist/src/index.js");
    if (!existsSync(distEntry)) {
      throw new Error("Build output missing. Run npm run build before MCP protocol tests.");
    }

    const configPath = writeTempConfig();
    const transport = new StdioClientTransport({
      command: "node",
      args: [distEntry],
      env: {
        ...process.env,
        WINMCP_CONFIG: configPath,
        WINMCP_AUDIT_DIR: resolve(process.cwd(), "logs/audit-test"),
      },
      stderr: "pipe",
    });

    const client = new Client({ name: "windows-admin-mcp-test", version: "1.0.0" });
    await client.connect(transport);

    const tools = await client.listTools();
    const names = tools.tools.map((tool) => tool.name).sort();
    expect(names).toEqual(REQUIRED_TOOLS.sort());

    for (const tool of tools.tools) {
      expect(tool.description?.length ?? 0).toBeGreaterThan(0);
      expect(tool.inputSchema).toBeDefined();
      expect(tool.annotations).toBeDefined();
      if (MUTATING_TOOLS.includes(tool.name)) {
        expect(tool.annotations?.readOnlyHint).toBe(false);
        expect(tool.annotations?.destructiveHint).toBe(true);
      } else {
        expect(tool.annotations?.readOnlyHint).toBe(true);
      }
    }

    expect(client.getInstructions() ?? "").toMatch(/dryRun/);

    await client.close();
  }, 60000);

  it("starts from an unrelated cwd with only absolute command/args/env", async () => {
    const distEntry = resolve(process.cwd(), "dist/src/index.js");
    const configPath = writeTempConfig();
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [distEntry],
      cwd: tmpdir(),
      env: {
        ...process.env,
        WINMCP_CONFIG: configPath,
        WINMCP_AUDIT_DIR: resolve(process.cwd(), "logs/audit-test"),
      } as Record<string, string>,
      stderr: "pipe",
    });
    const client = new Client({ name: "windows-admin-mcp-cwd-test", version: "1.0.0" });
    await client.connect(transport);
    const result = await client.callTool({ name: "list_managed_servers", arguments: {} });
    const content = result.content as Array<{ type: string; text: string }>;
    const payload = JSON.parse(content[0]?.text ?? "{}");
    expect(payload.count).toBe(3);
    await client.close();
  }, 60000);
});

describe("MCP subprocess smoke", () => {
  it.skipIf(process.platform !== "win32")("emits startup message on stderr", async () => {
    const distEntry = resolve(process.cwd(), "dist/src/index.js");
    if (!existsSync(distEntry)) return;

    const configPath = writeTempConfig();
    await new Promise<void>((resolvePromise, reject) => {
      const child = spawn("node", [distEntry], {
        env: {
          ...process.env,
          WINMCP_CONFIG: configPath,
          WINMCP_AUDIT_DIR: resolve(process.cwd(), "logs/audit-test"),
        },
        stdio: ["pipe", "pipe", "pipe"],
      });

      let stderr = "";
      child.stderr.on("data", (chunk: Buffer) => {
        stderr += chunk.toString("utf8");
      });

      setTimeout(() => {
        child.kill("SIGTERM");
      }, 2000);

      child.on("close", () => {
        expect(stderr).toMatch(/windows-psremoting-mcp started via stdio/);
        resolvePromise();
      });
      child.on("error", reject);
    });
  }, 10000);
});
