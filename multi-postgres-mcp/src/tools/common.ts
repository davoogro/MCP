import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonError } from "../security.js";

export const connectionField = z
  .string()
  .min(1)
  .describe("Connection alias from list_connections (also listed in the server instructions)");

export const schemaField = z
  .string()
  .min(1)
  .default("public")
  .describe("PostgreSQL schema name (default 'public'); use list_schemas to discover others");

export const tableField = z.string().min(1).describe("Table, view or materialized view name (case-sensitive as stored)");

export function errorResult(error: unknown): ReturnType<typeof jsonError> {
  return jsonError(error instanceof Error ? error.message : String(error));
}

// Turns a user search term into an ILIKE pattern: plain text matches anywhere ("cust" -> "%cust%").
export function toLikePattern(pattern: string): string {
  return pattern.includes("%") ? pattern : `%${pattern}%`;
}

export const connectionsField = z
  .array(z.string().min(1))
  .optional()
  .describe("Connection aliases to include; defaults to all configured aliases");

export function resolveConnectionList(manager: ConnectionManager, connections?: string[]): string[] {
  if (connections && connections.length > 0) {
    for (const alias of connections) {
      manager.getConnectionConfig(alias);
    }
    return [...connections].sort((a, b) => a.localeCompare(b));
  }
  return manager.listAliases();
}
