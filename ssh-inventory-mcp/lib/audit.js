import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

function auditPath() {
  return process.env.MCP_SSH_AUDIT_LOG || path.join(process.env.LOCALAPPDATA || os.homedir(), "ssh-inventory-mcp", "audit.jsonl");
}

export function redact(value) {
  return String(value)
    .replace(/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g, "[REDACTED PRIVATE KEY]")
    .replace(/\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|token)\s*[:=]\s*("[^"]*"|'[^']*'|\S+)/gi, "$1=[REDACTED]");
}

export async function writeAudit(event) {
  const target = auditPath();
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.appendFile(target, `${JSON.stringify({ ...event, command: redact(event.command), error: event.error ? redact(event.error) : undefined })}\n`, { encoding: "utf8", mode: 0o600 });
}
