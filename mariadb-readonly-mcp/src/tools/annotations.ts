import type { ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";

// Tools that only read the local YAML configuration.
export const LOCAL_READ_ONLY: ToolAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};

// Tools that run read-only statements against a remote MariaDB/MySQL server.
export const REMOTE_READ_ONLY: ToolAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: true,
};
