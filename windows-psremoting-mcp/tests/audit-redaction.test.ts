import { describe, expect, it } from "vitest";
import { sanitizeObject, sanitizeString } from "../src/security.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

describe("audit redaction", () => {
  it("redacts password patterns from strings", () => {
    const input = "Connection failed password=SecretValue123 token=abc";
    const output = sanitizeString(input, ["SecretValue123"]);
    expect(output).not.toContain("SecretValue123");
    expect(output).toContain("[REDACTED]");
  });

  it("redacts sensitive object keys", () => {
    const output = sanitizeObject({ username: "admin", password: "SecretValue123" }, ["SecretValue123"]);
    expect((output as Record<string, unknown>).password).toBe("[REDACTED]");
  });

  it("does not write raw secrets to audit jsonl", () => {
    const dir = mkdtempSync(join(tmpdir(), "winmcp-audit-"));
    const secret = "AuditSecret987!";
    const audit = createAuditLogger(dir);
    audit.registerSecrets([secret]);
    audit.write({
      correlationId: audit.createCorrelationId(),
      eventType: "tool.invoke",
      tool: "test",
      outcome: "success",
      durationMs: 0,
      parameters: { password: secret, note: `password=${secret}` },
    });
    const auditFiles = readdirSync(dir).filter((name) => name.startsWith("audit-") && name.endsWith(".jsonl"));
    expect(auditFiles.length).toBeGreaterThan(0);
    const files = readFileSync(join(dir, auditFiles[0]!), "utf8");
    expect(files).not.toContain(secret);
    expect(files).toContain("[REDACTED]");
  });
});
