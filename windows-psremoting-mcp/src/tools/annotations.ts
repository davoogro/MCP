/**
 * MCP tool annotations (hints for clients such as Claude Code / Cursor).
 * They do not enforce anything: the real guardrails are the allowlist, per-server
 * policy flags, dry-run defaults, the service denylist and the audit log.
 */
export interface ToolAnnotationsHint {
  title?: string;
  readOnlyHint?: boolean;
  destructiveHint?: boolean;
  idempotentHint?: boolean;
  openWorldHint?: boolean;
}

/** Reads local configuration only; never contacts a remote host. */
export const LOCAL_READ_ONLY: ToolAnnotationsHint = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};

/** Contacts an allowlisted remote host but does not change its state. */
export const REMOTE_READ_ONLY: ToolAnnotationsHint = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: true,
};

/** Changes the state of a remote production host (service restart, reboot). */
export const REMOTE_DESTRUCTIVE: ToolAnnotationsHint = {
  readOnlyHint: false,
  destructiveHint: true,
  idempotentHint: false,
  openWorldHint: true,
};
