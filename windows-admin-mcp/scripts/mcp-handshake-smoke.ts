import { resolve } from "node:path";
import { existsSync } from "node:fs";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const node = process.env.NODE_PATH ?? "C:\\Users\\youruser\\AppData\\Local\\Programs\\cursor\\resources\\app\\resources\\helpers\\node.exe";
const distEntry = resolve("dist/src/index.js");
const configPath = resolve("config/servers.json");

async function main(): Promise<void> {
  if (!existsSync(distEntry)) {
    throw new Error(`Missing ${distEntry}`);
  }

  const transport = new StdioClientTransport({
    command: node,
    args: [distEntry],
    env: {
      ...process.env,
      WINMCP_CONFIG: configPath,
      WINMCP_AUDIT_DIR: resolve("logs/audit"),
    },
    stderr: "pipe",
  });

  const client = new Client({ name: "handshake-smoke", version: "1.0.0" });
  await client.connect(transport);
  const init = await client.getServerVersion();
  const tools = await client.listTools();
  console.log(JSON.stringify({ init, toolCount: tools.tools.length, tools: tools.tools.map((t) => t.name).sort() }, null, 2));
  await client.close();
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
