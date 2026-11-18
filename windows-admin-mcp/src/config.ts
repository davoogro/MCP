import { readFileSync, existsSync } from "node:fs";
import { z } from "zod";
import { defaultConfigPath, resolveFromPackageRoot } from "./paths.js";
import type { AppConfig, ServerDefaults, ServerEntry } from "./types.js";

const transportSchema = z.enum(["auto", "ssh", "winrm"]);

const defaultsSchema = z.object({
  connectionTimeoutSeconds: z.number().int().positive().default(15),
  operationTimeoutSeconds: z.number().int().positive().default(120),
  rebootTimeoutSeconds: z.number().int().positive().default(900),
  maxConcurrency: z.number().int().positive().max(20).default(2),
  verifyTls: z.boolean().default(true),
  allowParallelReboot: z.boolean().default(false),
  maxParallelReboot: z.number().int().positive().max(5).default(1),
});

const serverEntrySchema = z.object({
  name: z.string().regex(/^[A-Z0-9][A-Z0-9_-]{0,62}$/),
  address: z.string().min(1),
  transport: transportSchema.default("auto"),
  port: z.number().int().positive().max(65535).nullable().default(null),
  allowReboot: z.boolean().default(false),
  protectedRole: z.boolean().default(false),
  allowProtectedReboot: z.boolean().default(false),
  allowServiceRestart: z.boolean().default(false),
  allowForceReboot: z.boolean().default(false),
  criticalServices: z.array(z.string().regex(/^[A-Za-z0-9_.-]{1,256}$/)).default([]),
  tags: z.array(z.string()).default([]),
  credentialTargetEnv: z.string().optional(),
  usernameEnv: z.string().optional(),
  passwordEnv: z.string().optional(),
  secretVault: z.string().optional(),
  usernameSecret: z.string().optional(),
  passwordSecret: z.string().optional(),
  sshKeyPathEnv: z.string().optional(),
  winrmCertThumbprintEnv: z.string().optional(),
});

const configFileSchema = z.object({
  defaults: defaultsSchema.default({}),
  servers: z.array(serverEntrySchema).min(1),
});

function resolveConfigPath(configPath?: string): string {
  const explicit = configPath ?? process.env.WINMCP_CONFIG;
  if (!explicit || explicit.trim().length === 0) {
    // Default: <package root>/config/servers.json (independent of the process cwd).
    return defaultConfigPath();
  }
  // Relative paths resolve against the package root, never the cwd.
  return resolveFromPackageRoot(explicit.trim());
}

function mergeServer(entry: z.infer<typeof serverEntrySchema>): ServerEntry {
  return {
    name: entry.name,
    address: entry.address,
    transport: entry.transport,
    port: entry.port,
    allowReboot: entry.allowReboot,
    protectedRole: entry.protectedRole,
    allowProtectedReboot: entry.allowProtectedReboot,
    allowServiceRestart: entry.allowServiceRestart,
    allowForceReboot: entry.allowForceReboot,
    criticalServices: entry.criticalServices,
    tags: entry.tags,
    credentialTargetEnv: entry.credentialTargetEnv,
    usernameEnv: entry.usernameEnv,
    passwordEnv: entry.passwordEnv,
    secretVault: entry.secretVault,
    usernameSecret: entry.usernameSecret,
    passwordSecret: entry.passwordSecret,
    sshKeyPathEnv: entry.sshKeyPathEnv,
    winrmCertThumbprintEnv: entry.winrmCertThumbprintEnv,
  };
}

export function loadConfig(configPath?: string): AppConfig {
  const resolvedPath = resolveConfigPath(configPath);
  if (!existsSync(resolvedPath)) {
    throw new Error(`Configuration file not found: ${resolvedPath}`);
  }

  const raw = readFileSync(resolvedPath, "utf8");
  const parsed = JSON.parse(raw) as unknown;
  const validated = configFileSchema.parse(parsed);
  const defaults = validated.defaults as ServerDefaults;

  const servers = new Map<string, ServerEntry>();
  const names = new Set<string>();
  for (const entry of validated.servers) {
    if (names.has(entry.name)) {
      throw new Error(`Duplicate server name in allowlist: ${entry.name}`);
    }
    names.add(entry.name);
    servers.set(entry.name, mergeServer(entry));
  }

  return { defaults, servers, configPath: resolvedPath };
}

export function getServerOrThrow(config: AppConfig, name: string): ServerEntry {
  const server = config.servers.get(name);
  if (!server) {
    const available = [...config.servers.keys()].sort().join(", ");
    throw new Error(`Unknown server '${name}'. Allowlisted servers: ${available}`);
  }
  return server;
}

export function listServers(config: AppConfig): ServerEntry[] {
  return [...config.servers.values()].sort((a, b) => a.name.localeCompare(b.name));
}

export { configFileSchema, serverEntrySchema, defaultsSchema };
