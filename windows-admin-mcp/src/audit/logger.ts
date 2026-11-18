import { appendFileSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { AuditEvent } from "../types.js";
import { sanitizeObject } from "../security.js";
import { defaultAuditDir, resolveFromPackageRoot } from "../paths.js";

export class AuditLogger {
  private readonly auditDir: string;
  private readonly secrets: string[] = [];

  constructor(auditDir?: string) {
    const fromEnv = process.env.WINMCP_AUDIT_DIR?.trim();
    // Default: <package root>/logs/audit (independent of the process cwd).
    this.auditDir = auditDir ?? (fromEnv ? resolveFromPackageRoot(fromEnv) : defaultAuditDir());
    if (!existsSync(this.auditDir)) {
      mkdirSync(this.auditDir, { recursive: true });
    }
  }

  registerSecrets(secrets: string[]): void {
    for (const secret of secrets) {
      if (secret && !this.secrets.includes(secret)) {
        this.secrets.push(secret);
      }
    }
  }

  createCorrelationId(): string {
    return randomUUID();
  }

  write(event: Omit<AuditEvent, "timestamp">): AuditEvent {
    const fullEvent: AuditEvent = {
      ...event,
      timestamp: new Date().toISOString(),
      parameters: event.parameters
        ? (sanitizeObject(event.parameters, this.secrets) as Record<string, unknown>)
        : undefined,
      error: event.error
        ? {
            ...event.error,
            message: sanitizeObject(event.error.message, this.secrets) as string,
          }
        : event.error,
    };

    const date = fullEvent.timestamp.slice(0, 10);
    const filePath = join(this.auditDir, `audit-${date}.jsonl`);
    appendFileSync(filePath, `${JSON.stringify(fullEvent)}\n`, "utf8");
    return fullEvent;
  }
}

export function createAuditLogger(auditDir?: string): AuditLogger {
  return new AuditLogger(auditDir);
}
