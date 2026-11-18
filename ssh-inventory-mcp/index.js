#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { execute } from "./lib/adapters.js";
import { writeAudit } from "./lib/audit.js";
import { defaultSshConfigPath, getServer, loadInventory } from "./lib/inventory.js";
import { assertCommandAllowed } from "./lib/policy.js";
import { runSsh } from "./lib/transport.js";

const text = (value) => ({ content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] });
const fail = (value) => ({ content: [{ type: "text", text: value }], isError: true });
const configPath = defaultSshConfigPath();

function shQuote(value) {
  return `'${String(value).replaceAll("'", `'\\''`)}'`;
}

function psQuote(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function publicServer(item) {
  const { sourceLine, ...safe } = item;
  return safe;
}

async function runAudited(target, command, timeoutSeconds) {
  const startedAt = new Date().toISOString();
  const started = performance.now();
  try {
    assertCommandAllowed(target, command);
    const result = await execute(target, configPath, command, timeoutSeconds);
    const durationMs = Math.round(performance.now() - started);
    await writeAudit({
      started_at: startedAt,
      server: target.id,
      environment: target.environment,
      access: target.access,
      command,
      duration_ms: durationMs,
      exit_code: result.code,
      timed_out: result.timedOut,
      error: result.stderr || undefined,
    });
    return { startedAt, durationMs, result };
  } catch (error) {
    await writeAudit({
      started_at: startedAt,
      server: target.id,
      environment: target.environment,
      access: target.access,
      command,
      duration_ms: Math.round(performance.now() - started),
      error: error?.message || String(error),
    });
    throw error;
  }
}

const tools = [
  {
    name: "list_servers",
    description:
      "List literal Host aliases from ~/.ssh/config (or MCP_SSH_CONFIG). Re-reads the file on every call, so a new Host appears without restarting. Optional # mcp: metadata sets os/access/groups; untagged hosts default to linux + managed. Never returns hostname, user or key path.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "get_server_info",
    description: "Safe inventory metadata for one SSH alias from the config. Does not reveal HostName, user or IdentityFile.",
    inputSchema: { type: "object", properties: { server: { type: "string", description: "Host alias from ~/.ssh/config" } }, required: ["server"] },
  },
  {
    name: "test_connection",
    description: "SSH into an alias from ~/.ssh/config and report remote identity (hostname, whoami, uptime).",
    inputSchema: { type: "object", properties: { server: { type: "string" } }, required: ["server"] },
  },
  {
    name: "execute_command",
    description: "Run a command on an explicitly selected Host alias. Auth, user, port and key come only from ~/.ssh/config. Linux uses POSIX; Windows uses PowerShell.",
    inputSchema: {
      type: "object",
      properties: {
        server: { type: "string" },
        command: { type: "string" },
        timeout_seconds: { type: "integer", minimum: 1, maximum: 600 },
      },
      required: ["server", "command"],
    },
  },
  {
    name: "read_remote_file",
    description: "Read a text file from a Host alias. Output is truncated by the SSH transport cap.",
    inputSchema: {
      type: "object",
      properties: {
        server: { type: "string" },
        path: { type: "string", description: "Absolute or home-relative remote path" },
      },
      required: ["server", "path"],
    },
  },
  {
    name: "list_dir",
    description: "List a remote directory on a Host alias.",
    inputSchema: {
      type: "object",
      properties: {
        server: { type: "string" },
        path: { type: "string", description: "Remote directory. Defaults to home/current directory." },
      },
      required: ["server"],
    },
  },
  {
    name: "write_remote_file",
    description: "Write a UTF-8 text file on a managed Host alias. Parent directories are not created.",
    inputSchema: {
      type: "object",
      properties: {
        server: { type: "string" },
        path: { type: "string" },
        content: { type: "string" },
      },
      required: ["server", "path", "content"],
    },
  },
];

const server = new Server({ name: "ssh-inventory-mcp", version: "0.2.0" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  try {
    const args = request.params.arguments || {};
    if (request.params.name === "list_servers") {
      const { servers, configPath: used } = await loadInventory(configPath);
      return text({
        configPath: used,
        count: servers.length,
        servers: servers.map(publicServer),
      });
    }

    const target = await getServer(args.server, configPath);
    if (request.params.name === "get_server_info") return text(publicServer(target));

    if (request.params.name === "test_connection") {
      const probe = target.shell === "powershell"
        ? "hostname; whoami; Get-Date"
        : "echo CONNECTED; hostname; whoami; uptime";
      const { startedAt, durationMs, result } = await runAudited(target, probe, 30);
      return text({
        server: target.id,
        os: target.os,
        started_at: startedAt,
        duration_ms: durationMs,
        exit_code: result.code,
        timed_out: result.timedOut,
        stdout: result.stdout,
        stderr: result.stderr,
      });
    }

    if (request.params.name === "execute_command") {
      const { startedAt, durationMs, result } = await runAudited(target, args.command, args.timeout_seconds);
      return text({
        server: target.id,
        os: target.os,
        started_at: startedAt,
        duration_ms: durationMs,
        exit_code: result.code,
        timed_out: result.timedOut,
        stdout: result.stdout,
        stderr: result.stderr,
      });
    }

    if (request.params.name === "read_remote_file") {
      if (typeof args.path !== "string" || args.path.trim() === "") throw new Error("path is required");
      const command = target.shell === "powershell"
        ? `Get-Content -Raw -LiteralPath ${psQuote(args.path)}`
        : `base64 -- ${shQuote(args.path)}`;
      const { result } = await runAudited(target, command, 60);
      if (result.code !== 0 || result.timedOut) {
        return fail(`read_remote_file failed on ${target.id} (exit ${result.code})\n${result.stderr || result.stdout}`);
      }
      if (target.shell === "powershell") return text(result.stdout);
      const buf = Buffer.from(result.stdout.replace(/\s+/g, ""), "base64");
      return text(buf.toString("utf8"));
    }

    if (request.params.name === "list_dir") {
      const remotePath = typeof args.path === "string" && args.path.trim() !== "" ? args.path : ".";
      const command = target.shell === "powershell"
        ? `Get-ChildItem -Force -LiteralPath ${psQuote(remotePath)} | Select-Object Mode, Length, LastWriteTime, Name | Format-Table -AutoSize | Out-String -Width 200`
        : `ls -lA --time-style=long-iso -- ${shQuote(remotePath)}`;
      const { result } = await runAudited(target, command, 60);
      if (result.code !== 0 || result.timedOut) {
        return fail(`list_dir failed on ${target.id} (exit ${result.code})\n${result.stderr || result.stdout}`);
      }
      return text({ server: target.id, path: remotePath, listing: result.stdout });
    }

    if (request.params.name === "write_remote_file") {
      if (target.access === "read_only") throw new Error(`Server '${target.id}' is read_only; write_remote_file is refused.`);
      if (typeof args.path !== "string" || args.path.trim() === "") throw new Error("path is required");
      if (typeof args.content !== "string") throw new Error("content must be a string");
      const b64 = Buffer.from(args.content, "utf8").toString("base64");
      const startedAt = new Date().toISOString();
      const started = performance.now();
      const command = target.shell === "powershell"
        ? `$bytes = [Convert]::FromBase64String('${b64}'); [IO.File]::WriteAllBytes(${psQuote(args.path)}, $bytes)`
        : `base64 -d > ${shQuote(args.path)}`;
      try {
        const result = target.shell === "powershell"
          ? (await runAudited(target, command, 60)).result
          : await runSsh({
            configPath,
            alias: target.sshAlias,
            command,
            stdin: b64,
            timeoutMs: 60_000,
          });
        if (target.shell !== "powershell") {
          await writeAudit({
            started_at: startedAt,
            server: target.id,
            environment: target.environment,
            access: target.access,
            command: `write_remote_file ${args.path}`,
            duration_ms: Math.round(performance.now() - started),
            exit_code: result.code,
            timed_out: result.timedOut,
            error: result.stderr || undefined,
          });
        }
        if (result.code !== 0 || result.timedOut) {
          return fail(`write_remote_file failed on ${target.id} (exit ${result.code})\n${result.stderr || result.stdout}`);
        }
        return text(`Wrote ${args.path} on ${target.id}.`);
      } catch (error) {
        if (target.shell !== "powershell") {
          await writeAudit({
            started_at: startedAt,
            server: target.id,
            environment: target.environment,
            access: target.access,
            command: `write_remote_file ${args.path}`,
            duration_ms: Math.round(performance.now() - started),
            error: error?.message || String(error),
          });
        }
        throw error;
      }
    }

    return fail(`Unknown tool: ${request.params.name}`);
  } catch (error) {
    return fail(error?.message || String(error));
  }
});
await server.connect(new StdioServerTransport());
