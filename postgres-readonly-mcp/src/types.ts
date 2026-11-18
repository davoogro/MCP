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

export interface ResolvedCredentials {
  user: string;
  password: string;
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
  postgresVersion: string;
  latencyMs: number;
  readonly: boolean;
  ssl: boolean;
}

export interface QueryExecutionResult {
  alias: string;
  rowCount: number;
  columns: string[];
  rows: Record<string, unknown>[];
  durationMs: number;
  truncated: boolean;
}

export interface SqlValidationResult {
  valid: boolean;
  reason?: string;
  // Leading keyword of an accepted statement (SELECT, WITH, EXPLAIN, SHOW, ...).
  statement?: string;
}

export interface TableDescription {
  schema: string;
  table: string;
  objectType: "table" | "partitioned_table" | "view" | "materialized_view" | "foreign_table";
  columns: Array<{
    name: string;
    dataType: string;
    isNullable: boolean;
    defaultValue: string | null;
  }>;
  primaryKey: string[];
  indexes: Array<{
    name: string;
    definition: string;
    isUnique: boolean;
    isPrimary: boolean;
  }>;
}

export interface AppConfig {
  defaults: ConnectionDefaults;
  connections: Map<string, ConnectionConfig>;
  configPath: string;
}
