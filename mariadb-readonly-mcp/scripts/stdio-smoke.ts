// End-to-end MCP stdio check, launched the way Claude Code does it: `node <abs>/dist/src/index.js`
// from an unrelated working directory, with no config env vars and no cwd hint.
// Usage: npm run build && node dist/scripts/stdio-smoke.js [alias] [--cwd <dir>] [--query "<sql>"]
import { parse as parsePath, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport, getDefaultEnvironment } from "@modelcontextprotocol/sdk/client/stdio.js";
import { PACKAGE_ROOT } from "../src/paths.js";

function argValue(flag: string): string | undefined {
  const index = process.argv.indexOf(flag);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

function firstText(result: unknown): string {
  const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
  return content.find((item) => item.type === "text")?.text ?? "";
}

async function main(): Promise<void> {
  const positional = process.argv.slice(2).filter((arg, index, all) => !arg.startsWith("--") && !all[index - 1]?.startsWith("--"));
  const requestedAlias = positional[0];
  const cwd = argValue("--cwd") ?? parsePath(process.cwd()).root;
  const query = argValue("--query") ?? "SELECT DATABASE() AS database_name, CURRENT_USER() AS connected_user";
  const serverPath = resolve(PACKAGE_ROOT, "dist", "src", "index.js");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    cwd,
    env: getDefaultEnvironment(),
    stderr: "pipe",
  });
  const stderr: string[] = [];
  transport.stderr?.on("data", (chunk) => stderr.push(String(chunk)));

  const client = new Client({ name: "stdio-smoke", version: "1.0.0" });
  await client.connect(transport);

  const serverInfo = client.getServerVersion();
  const instructions = client.getInstructions() ?? "";
  console.log(`cwd: ${cwd}`);
  console.log(`initialize: ${serverInfo?.name} ${serverInfo?.version}`);
  console.log(`instructions (${instructions.length} chars):\n${instructions.split("\n").slice(0, 4).join("\n")}\n...`);

  const { tools } = await client.listTools();
  console.log(`tools/list: ${tools.length} tools`);
  for (const tool of tools) {
    const hints = tool.annotations ?? {};
    console.log(`  - ${tool.name} readOnly=${hints.readOnlyHint} openWorld=${hints.openWorldHint}`);
  }

  const listResult = await client.callTool({ name: "list_connections", arguments: {} });
  const connections = (JSON.parse(firstText(listResult)) as { connections: Array<{ alias: string }> }).connections;
  console.log(`list_connections: ${connections.map((item) => item.alias).join(", ")}`);

  const alias = requestedAlias ?? connections[0]?.alias;
  let failed = Boolean(listResult.isError);
  if (alias) {
    const steps: Array<[string, Record<string, unknown>]> = [
      ["test_connection", { connection: alias }],
      ["list_schemas", { connection: alias }],
      ["execute_query", { connection: alias, sql: query, maxRows: 5 }],
      ["execute_query", { connection: alias, sql: "DELETE FROM mcp_smoke_should_be_blocked" }],
    ];
    for (const [name, args] of steps) {
      const result = await client.callTool({ name, arguments: args });
      const text = firstText(result);
      const expectError = String(args.sql ?? "").startsWith("DELETE");
      const ok = expectError ? Boolean(result.isError) : !result.isError;
      failed ||= !ok;
      console.log(`${name}${expectError ? " (write must be rejected)" : ""}: ${ok ? "OK" : "FAILED"}\n${text.slice(0, 600)}`);
    }
  }

  await client.close();
  if (failed && stderr.length > 0) {
    console.log(`server stderr:\n${stderr.join("").slice(0, 2000)}`);
  }
  process.exit(failed ? 1 : 0);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
