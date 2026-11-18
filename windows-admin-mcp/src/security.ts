const SECRET_PATTERNS: RegExp[] = [
  /(password|passwd|pwd)\s*[:=]\s*\S+/gi,
  /(secret|token|apikey|api_key)\s*[:=]\s*\S+/gi,
  /Bearer\s+[A-Za-z0-9\-._~+/]+=*/gi,
  /Basic\s+[A-Za-z0-9+/]+=*/gi,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END/g,
  /(Authorization|WWW-Authenticate)\s*:\s*.+/gi,
  /(net\.use|cmdkey)\s+.*(\/user:|\/pass:)\S+/gi,
  /-(?:Credential|Password|Passphrase)\s+\S+/gi,
  /sshpass\s+-p\s+\S+/gi,
];

const REDACT_KEYS = /^(password|passwd|secret|token|apikey|authorization|privatekey|securestring|credential)$/i;

export function sanitizeString(text: string, secrets: string[] = []): string {
  let message = text;
  for (const secret of secrets.sort((a, b) => b.length - a.length)) {
    if (secret.length >= 4) {
      message = message.split(secret).join("[REDACTED]");
    }
  }
  for (const pattern of SECRET_PATTERNS) {
    message = message.replace(pattern, "[REDACTED]");
  }
  return message;
}

export function sanitizeErrorMessage(error: unknown, secrets: string[] = []): string {
  const message = error instanceof Error ? error.message : String(error);
  return sanitizeString(message, secrets);
}

export function sanitizeObject(obj: unknown, secrets: string[] = []): unknown {
  if (obj === null || obj === undefined) return obj;
  if (typeof obj === "string") return sanitizeString(obj, secrets);
  if (typeof obj !== "object") return obj;
  if (Array.isArray(obj)) return obj.map((item) => sanitizeObject(item, secrets));

  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    if (REDACT_KEYS.test(key)) {
      result[key] = "[REDACTED]";
    } else {
      result[key] = sanitizeObject(value, secrets);
    }
  }
  return result;
}

export function jsonText(payload: unknown): { content: Array<{ type: "text"; text: string }> } {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
  };
}

export function jsonError(message: string): {
  content: Array<{ type: "text"; text: string }>;
  isError: true;
} {
  return {
    content: [{ type: "text", text: JSON.stringify({ error: message }, null, 2) }],
    isError: true,
  };
}

export function validateServiceName(name: string): void {
  if (!/^[A-Za-z0-9_.-]{1,256}$/.test(name)) {
    throw new Error(`Invalid service name: '${name}'`);
  }
}

export function validateDriveLetter(drive: string): void {
  if (!/^[A-Z]:$/.test(drive)) {
    throw new Error(`Invalid drive letter: '${drive}'. Expected format like C:`);
  }
}

export function validateReason(reason: string): void {
  if (reason.length < 10 || reason.length > 500) {
    throw new Error("Reason must be between 10 and 500 characters");
  }
  if (/[;&|`$]/.test(reason)) {
    throw new Error("Reason contains forbidden shell metacharacters");
  }
}

export const APPROVED_DIAGNOSTICS = [
  "ping",
  "dns_resolve",
  "tcp_port_check",
  "winrm_listener",
  "disk_smart_status",
  "service_dependencies",
] as const;

export const APPROVED_TCP_PORTS = new Set([22, 445, 5986, 3389]);

export const SERVICE_RESTART_DENYLIST = [
  "NTDS",
  "DNS",
  "ClusSvc",
  "W32Time",
  "Kdc",
  "Iscsi",
];

export function isServiceRestartDenied(serviceName: string): boolean {
  const upper = serviceName.toUpperCase();
  return SERVICE_RESTART_DENYLIST.some(
    (denied) => upper === denied.toUpperCase() || upper.startsWith("MSEXCHANGE"),
  );
}
