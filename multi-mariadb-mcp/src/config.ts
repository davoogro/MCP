import { readFileSync, existsSync } from "node:fs";
import { resolve, isAbsolute } from "node:path";
import { parse as parseYaml } from "yaml";
import { z } from "zod";
import { DEFAULT_CONFIG_PATH, PACKAGE_ROOT } from "./paths.js";
import type { AppConfig, ConnectionConfig, ConnectionDefaults } from "./types.js";

const sslModeSchema = z.enum(["disable", "prefer", "require", "verify-ca", "verify-full"]);

const defaultsSchema = z.object({
  port: z.number().int().positive().default(3306),
  sslmode: sslModeSchema.default("disable"),
  statementTimeoutMs: z.number().int().positive().default(60000),
  connectionTimeoutMs: z.number().int().positive().default(10000),
  maxRows: z.number().int().positive().max(10000).default(2000),
  readonly: z.boolean().default(true),
});

const connectionEntrySchema = z.object({
  description: z.string().optional(),
  host: z.string().min(1),
  port: z.number().int().positive().optional(),
  database: z.string().min(1),
  userEnv: z.string().min(1),
  passwordEnv: z.string().min(1),
  sslmode: sslModeSchema.optional(),
  statementTimeoutMs: z.number().int().positive().optional(),
  connectionTimeoutMs: z.number().int().positive().optional(),
  maxRows: z.number().int().positive().max(10000).optional(),
  readonly: z.boolean().optional(),
});

const configFileSchema = z.object({
  defaults: defaultsSchema.default({}),
  connections: z.record(z.string().min(1), connectionEntrySchema).refine(
    (connections) => Object.keys(connections).length > 0,
    "At least one connection must be defined",
  ),
});

export function resolveConfigPath(configPath?: string): string {
  const requested = configPath ?? process.env.MARIADB_MCP_CONFIG;
  if (!requested) {
    // No explicit path: use the config bundled with the package (works from any cwd).
    return DEFAULT_CONFIG_PATH;
  }

  if (isAbsolute(requested)) {
    return requested;
  }

  // Relative paths (e.g. MARIADB_MCP_CONFIG=./config/databases.yaml in the package .env) resolve against
  // the package root first, then the current working directory for backwards compatibility.
  const fromPackageRoot = resolve(PACKAGE_ROOT, requested);
  if (existsSync(fromPackageRoot)) {
    return fromPackageRoot;
  }

  return resolve(process.cwd(), requested);
}

function mergeConnection(
  alias: string,
  entry: z.infer<typeof connectionEntrySchema>,
  defaults: ConnectionDefaults,
): ConnectionConfig {
  return {
    alias,
    description: entry.description,
    host: entry.host,
    port: entry.port ?? defaults.port,
    database: entry.database,
    userEnv: entry.userEnv,
    passwordEnv: entry.passwordEnv,
    sslmode: entry.sslmode ?? defaults.sslmode,
    statementTimeoutMs: entry.statementTimeoutMs ?? defaults.statementTimeoutMs,
    connectionTimeoutMs: entry.connectionTimeoutMs ?? defaults.connectionTimeoutMs,
    maxRows: entry.maxRows ?? defaults.maxRows,
    readonly: entry.readonly ?? defaults.readonly,
  };
}

export function loadConfig(configPath?: string): AppConfig {
  const resolvedPath = resolveConfigPath(configPath);

  if (!existsSync(resolvedPath)) {
    throw new Error(`Configuration file not found: ${resolvedPath}`);
  }

  const raw = readFileSync(resolvedPath, "utf8");
  const parsed = parseYaml(raw);
  const validated = configFileSchema.parse(parsed);
  const defaults = validated.defaults as ConnectionDefaults;

  const connections = new Map<string, ConnectionConfig>();
  for (const [alias, entry] of Object.entries(validated.connections)) {
    connections.set(alias, mergeConnection(alias, entry, defaults));
  }

  return {
    defaults,
    connections,
    configPath: resolvedPath,
  };
}

export function resolveCredentials(config: ConnectionConfig): { user: string; password: string } {
  const user = process.env[config.userEnv];
  const password = process.env[config.passwordEnv];

  if (!user) {
    throw new Error(`Missing environment variable: ${config.userEnv}`);
  }

  if (!password) {
    throw new Error(`Missing environment variable: ${config.passwordEnv}`);
  }

  return { user, password };
}

export function getConnectionOrThrow(
  config: AppConfig,
  alias: string,
): ConnectionConfig {
  const connection = config.connections.get(alias);
  if (!connection) {
    const available = [...config.connections.keys()].sort().join(", ");
    throw new Error(`Unknown connection alias '${alias}'. Available aliases: ${available}`);
  }

  return connection;
}

export { configFileSchema, connectionEntrySchema, defaultsSchema };
