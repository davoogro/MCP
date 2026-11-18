import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const VALID_OS = new Set(["linux", "windows"]);
const VALID_SHELL = new Set(["posix", "powershell"]);
const VALID_ACCESS = new Set(["read_only", "managed"]);

export function defaultSshConfigPath() {
  return process.env.MCP_SSH_CONFIG || path.join(os.homedir(), ".ssh", "config");
}

function parseTag(text, line) {
  const metadata = {};
  for (const token of text.trim().split(/\s+/)) {
    const match = /^([A-Za-z][A-Za-z0-9_-]*)=(.+)$/.exec(token);
    if (!match) throw new Error(`Invalid # mcp: token at line ${line}: ${token}`);
    const [, key, value] = match;
    if (metadata[key] !== undefined) throw new Error(`Duplicate # mcp: key '${key}' at line ${line}`);
    metadata[key] = value;
  }
  return metadata;
}

function normalise(alias, tag, line, tagged) {
  if (!ALIAS.test(alias)) throw new Error(`Unsafe SSH alias '${alias}' at line ${line}`);
  const osName = tag.os || "linux";
  const shell = tag.shell || (osName === "windows" ? "powershell" : "posix");
  const access = tag.access || (tagged ? "read_only" : "managed");
  if (!VALID_OS.has(osName)) throw new Error(`Host '${alias}' must declare os=linux or os=windows`);
  if (!VALID_SHELL.has(shell)) throw new Error(`Host '${alias}' has unsupported shell '${shell}'`);
  if (osName === "windows" && shell !== "powershell") throw new Error(`Host '${alias}' must use shell=powershell`);
  if (osName === "linux" && shell !== "posix") throw new Error(`Host '${alias}' must use shell=posix`);
  if (!VALID_ACCESS.has(access)) throw new Error(`Host '${alias}' has unsupported access '${access}'`);
  return {
    id: alias,
    sshAlias: alias,
    os: osName,
    shell,
    environment: tag.env || "unknown",
    access,
    groups: tag.groups ? tag.groups.split(",").filter(Boolean) : [],
    tagged,
    sourceLine: line,
  };
}

/**
 * Reads literal, single-name Host aliases from OpenSSH config.
 * Optional `# mcp:` on the preceding line sets os/access/groups.
 * Untagged hosts are still exposed (linux + managed) so adding a Host
 * to ~/.ssh/config is enough for the MCP to pick it up on the next call.
 * Wildcards, negated patterns and Match blocks stay invisible.
 * OpenSSH itself resolves user, key, port and hostname at execution.
 */
export function parseInventory(configText) {
  const servers = new Map();
  let pendingTag = null;
  const lines = configText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const tag = /^\s*#\s*mcp:\s*(.+?)\s*$/i.exec(line);
    if (tag) {
      pendingTag = { value: parseTag(tag[1], i + 1), line: i + 1 };
      continue;
    }
    if (/^\s*#|^\s*$/.test(line)) continue;
    const host = /^\s*Host\s+(.+?)\s*$/i.exec(line);
    if (host) {
      const names = host[1].trim().split(/\s+/);
      const literal = names.length === 1 && !/[*?!]/.test(names[0]);
      if (pendingTag && !literal) {
        throw new Error(`# mcp: at line ${pendingTag.line} must precede one literal Host alias`);
      }
      if (literal) {
        const tagged = Boolean(pendingTag);
        const server = normalise(names[0], pendingTag ? pendingTag.value : {}, i + 1, tagged);
        if (servers.has(server.id)) throw new Error(`Duplicate MCP server '${server.id}'`);
        servers.set(server.id, server);
      }
      pendingTag = null;
      continue;
    }
    // Metadata must belong to the next Host block, never bleed into a directive.
    pendingTag = null;
  }
  if (pendingTag) throw new Error(`# mcp: at line ${pendingTag.line} is not followed by a Host declaration`);
  return [...servers.values()].sort((a, b) => a.id.localeCompare(b.id));
}

export async function loadInventory(configPath = defaultSshConfigPath()) {
  const text = await fs.readFile(configPath, "utf8");
  return { configPath, servers: parseInventory(text) };
}

export async function getServer(id, configPath) {
  const { servers } = await loadInventory(configPath);
  const server = servers.find((item) => item.id === id);
  if (!server) throw new Error(`Unknown server '${id}'. Use list_servers; only literal Host aliases from the SSH config are allowed.`);
  return server;
}
