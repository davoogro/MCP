export type SslMode = "disable" | "prefer" | "require" | "verify-ca" | "verify-full";

export type ConnectionStatus = "unknown" | "healthy" | "unhealthy";

export interface ConnectionDefaults {
  port: number;
  sslmode: SslMode;
  statementTimeoutMs: number;
  connectionTimeoutMs: number;
  maxRows: number;
  readonly: boolean;
}

export interface ConnectionConfig {
  alias: string;
  description?: string;
  host: string;
  port: number;
  database: string;
  userEnv: string;
  passwordEnv: string;
  sslmode: SslMode;
  statementTimeoutMs: number;
  connectionTimeoutMs: number;
  maxRows: number;
  readonly: boolean;
}

export interface ConnectionSummary {
  alias: string;
  description?: string;
  host: string;
  port: number;
  database: string;
  readonly: boolean;
  maxRows: number;
  sslmode: SslMode;
  status: ConnectionStatus;
}

export interface TestConnectionResult {
  alias: string;
  databaseName: string;
  serverVersion: string;
  latencyMs: number;
  readonly: boolean;
  // Whether the server confirms the session is READ ONLY (null when the variable is not available).
  sessionReadOnly: boolean | null;
}

export interface SqlValidationResult {
  valid: boolean;
  reason?: string;
  // Leading keyword of an accepted statement (SELECT, WITH, SHOW, DESCRIBE, ...).
  statement?: string;
}

export interface AppConfig {
  defaults: ConnectionDefaults;
  connections: Map<string, ConnectionConfig>;
  configPath: string;
}
