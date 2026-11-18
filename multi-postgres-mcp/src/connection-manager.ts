import { Pool, TypeOverrides, type PoolClient, type PoolConfig, type QueryResultRow } from "pg";
import type { AppConfig, ConnectionConfig, ConnectionStatus, ConnectionSummary, TestConnectionResult } from "./types.js";
import { getConnectionOrThrow, resolveCredentials } from "./config.js";
import { classifyConnectionError, sanitizeErrorMessage } from "./security.js";

interface ManagedPool {
  pool: Pool;
  config: ConnectionConfig;
  ssl: boolean;
}

// Return date/time values exactly as PostgreSQL prints them. node-pg would otherwise turn
// "timestamp without time zone" into a JS Date in the local timezone and shift it when serialized.
const RAW_TEXT_TYPE_OIDS = [
  1082, // date
  1114, // timestamp
  1184, // timestamptz
  1186, // interval
];

function buildTypeOverrides(): TypeOverrides {
  const overrides = new TypeOverrides();
  for (const oid of RAW_TEXT_TYPE_OIDS) {
    overrides.setTypeParser(oid, "text", (value: string) => value);
  }
  return overrides;
}

function isSslUnsupportedError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.toLowerCase().includes("does not support ssl");
}

export class ConnectionManager {
  private readonly pools = new Map<string, ManagedPool>();
  private readonly statuses = new Map<string, ConnectionStatus>();
  private readonly secrets = new Set<string>();
  // Aliases with sslmode "prefer" whose server refused TLS; they reconnect without it (libpq semantics).
  private readonly plainTextFallback = new Set<string>();
  private readonly typeOverrides = buildTypeOverrides();
  private readonly configuredClients = new WeakSet<PoolClient>();
  private closed = false;

  constructor(private readonly appConfig: AppConfig) {}

  listConnections(): ConnectionSummary[] {
    return [...this.appConfig.connections.values()]
      .map((connection) => ({
        alias: connection.alias,
        description: connection.description,
        host: connection.host,
        port: connection.port,
        database: connection.database,
        readonly: connection.readonly,
        maxRows: connection.maxRows,
        sslmode: connection.sslmode,
        status: this.statuses.get(connection.alias) ?? "unknown",
      }))
      .sort((a, b) => a.alias.localeCompare(b.alias));
  }

  getConnectionConfig(alias: string): ConnectionConfig {
    return getConnectionOrThrow(this.appConfig, alias);
  }

  getMaxRows(alias: string): number {
    return this.getConnectionConfig(alias).maxRows;
  }

  getStatementTimeoutMs(alias: string): number {
    return this.getConnectionConfig(alias).statementTimeoutMs;
  }

  listAliases(): string[] {
    return [...this.appConfig.connections.keys()].sort((a, b) => a.localeCompare(b));
  }

  /** Checkout a pool client; caller must release() unless handed to the cursor store. */
  async checkoutClient(alias: string): Promise<PoolClient> {
    return this.getClient(alias);
  }

  async testConnection(alias: string): Promise<TestConnectionResult> {
    const started = Date.now();
    const client = await this.getClient(alias);

    try {
      const result = await client.query<{
        database_name: string;
        connected_user: string;
        postgres_version: string;
      }>(`
        SELECT
          current_database() AS database_name,
          current_user AS connected_user,
          version() AS postgres_version
      `);

      const row = result.rows[0];
      this.statuses.set(alias, "healthy");

      return {
        alias,
        databaseName: row.database_name,
        postgresVersion: row.postgres_version.split(" on ")[0] ?? row.postgres_version,
        latencyMs: Date.now() - started,
        readonly: this.getConnectionConfig(alias).readonly,
        ssl: this.pools.get(alias)?.ssl ?? false,
      };
    } catch (error) {
      this.statuses.set(alias, "unhealthy");
      throw new Error(classifyConnectionError(error));
    } finally {
      client.release();
    }
  }

  async withClient<T>(alias: string, fn: (client: PoolClient) => Promise<T>): Promise<T> {
    const client = await this.getClient(alias);

    try {
      return await fn(client);
    } finally {
      client.release();
    }
  }

  async query<T extends QueryResultRow>(
    alias: string,
    sql: string,
    params: unknown[] = [],
  ): Promise<{ rows: T[]; rowCount: number }> {
    return this.withClient(alias, async (client) => {
      const result = await client.query<T>(sql, params);
      return {
        rows: result.rows,
        rowCount: result.rowCount ?? result.rows.length,
      };
    });
  }

  private async getClient(alias: string): Promise<PoolClient> {
    if (this.closed) {
      throw new Error("Connection manager is closed");
    }

    let managed: ManagedPool;
    try {
      managed = await this.getOrCreatePool(alias);
    } catch (error) {
      this.statuses.set(alias, "unhealthy");
      throw new Error(classifyConnectionError(error));
    }

    let client: PoolClient;
    try {
      client = await managed.pool.connect();
    } catch (error) {
      if (managed.config.sslmode === "prefer" && managed.ssl && isSslUnsupportedError(error)) {
        // sslmode "prefer": the server does not offer TLS, so retry once in plain text like libpq does.
        this.plainTextFallback.add(alias);
        await this.discardPool(alias);
        return this.getClient(alias);
      }
      this.statuses.set(alias, "unhealthy");
      throw new Error(classifyConnectionError(error));
    }

    try {
      // pg pools hand back the same Client object for a physical connection, so session settings are
      // applied once per connection. They cannot drift afterwards: SET/set_config are rejected by the
      // validator and user SQL always runs inside a transaction that is rolled back.
      if (!this.configuredClients.has(client)) {
        await this.configureSession(client, managed.config);
        this.configuredClients.add(client);
      }
      return client;
    } catch (error) {
      client.release();
      this.statuses.set(alias, "unhealthy");
      throw new Error(sanitizeErrorMessage(error, [...this.secrets]));
    }
  }

  private async getOrCreatePool(alias: string): Promise<ManagedPool> {
    const existing = this.pools.get(alias);
    if (existing) {
      return existing;
    }

    const config = getConnectionOrThrow(this.appConfig, alias);
    const credentials = resolveCredentials(config);
    this.secrets.add(credentials.password);
    this.secrets.add(credentials.user);

    const ssl = this.plainTextFallback.has(alias) ? false : this.buildSslConfig(config.sslmode);
    const poolConfig: PoolConfig = {
      host: config.host,
      port: config.port,
      database: config.database,
      user: credentials.user,
      password: credentials.password,
      ssl,
      types: this.typeOverrides,
      max: 5,
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: config.connectionTimeoutMs,
      application_name: "multi-postgres-mcp",
    };

    const pool = new Pool(poolConfig);
    pool.on("error", () => {
      this.statuses.set(alias, "unhealthy");
    });

    const managed: ManagedPool = { pool, config, ssl: ssl !== false };
    this.pools.set(alias, managed);
    return managed;
  }

  private async discardPool(alias: string): Promise<void> {
    const managed = this.pools.get(alias);
    this.pools.delete(alias);
    try {
      await managed?.pool.end();
    } catch {
      // Ignore errors from a pool that never connected.
    }
  }

  private buildSslConfig(sslmode: ConnectionConfig["sslmode"]): PoolConfig["ssl"] {
    switch (sslmode) {
      case "disable":
        return false;
      case "prefer":
        return { rejectUnauthorized: false };
      case "require":
        return { rejectUnauthorized: false };
      case "verify-ca":
        return { rejectUnauthorized: true };
      case "verify-full":
        return { rejectUnauthorized: true };
      default:
        return { rejectUnauthorized: false };
    }
  }

  private async configureSession(client: PoolClient, config: ConnectionConfig): Promise<void> {
    const statementTimeout = `${config.statementTimeoutMs}ms`;
    const statements: string[] = [];

    if (config.readonly) {
      // Session-level guardrails in addition to SQL validation and read-only transactions.
      statements.push("SET default_transaction_read_only = on");
    }

    statements.push(`SET statement_timeout = '${statementTimeout}'`);
    statements.push("SET lock_timeout = '5000ms'");
    statements.push(`SET idle_in_transaction_session_timeout = '${statementTimeout}'`);

    // One round trip (simple query protocol accepts several statements when no parameters are bound).
    await client.query(statements.join("; "));
  }

  async close(): Promise<void> {
    this.closed = true;
    const { closeAllCursors } = await import("./query-cursors.js");
    await closeAllCursors();
    const pools = [...this.pools.values()];

    await Promise.all(
      pools.map(async ({ pool }) => {
        try {
          await pool.end();
        } catch {
          // Ignore shutdown errors.
        }
      }),
    );

    this.pools.clear();
  }
}

export function createConnectionManager(appConfig: AppConfig): ConnectionManager {
  return new ConnectionManager(appConfig);
}
