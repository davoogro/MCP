import { z } from "zod";
import type { ConnectionManager } from "../connection-manager.js";
import { jsonError, validateIdentifier } from "../security.js";

export const SYSTEM_SCHEMAS = ["information_schema", "performance_schema", "mysql", "sys"] as const;

export const connectionField = z
  .string()
  .min(1)
  .describe("Connection alias from list_connections (also listed in the server instructions)");

export const schemaField = z
  .string()
  .min(1)
  .optional()
  .describe(
    "Database (= schema in MariaDB) on the same server; defaults to the alias database. Use list_databases to discover others",
  );

export const tableField = z.string().min(1).describe("Table or view name");

// MariaDB treats schema and database as synonyms; omitted values fall back to the alias database.
export function resolveSchema(manager: ConnectionManager, connection: string, schema?: string): string {
  const resolved = schema ?? manager.getConnectionConfig(connection).database;
  validateIdentifier(resolved, "schema");
  return resolved;
}

export function errorResult(error: unknown): ReturnType<typeof jsonError> {
  return jsonError(error instanceof Error ? error.message : String(error));
}

// Turns a user search term into a LIKE pattern: plain text matches anywhere ("cust" -> "%cust%").
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
