import mysql, { type Pool, type PoolConnection, type RowDataPacket } from "mysql2/promise";
import type { AppConfig, ConnectionConfig, ConnectionStatus, ConnectionSummary, TestConnectionResult } from "./types.js";
import { getConnectionOrThrow, resolveCredentials } from "./config.js";
import { classifyConnectionError, sanitizeErrorMessage } from "./security.js";

interface ManagedPool {
  pool: Pool;
  config: ConnectionConfig;
}

export class ConnectionManager {
  private readonly pools = new Map<string, ManagedPool>();
  private readonly statuses = new Map<string, ConnectionStatus>();
  private readonly secrets = new Set<string>();
  // Physical connections (keyed by the underlying mysql2 connection) whose session guards are applied.
  private readonly configuredConnections = new WeakSet<object>();
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

  async testConnection(alias: string): Promise<TestConnectionResult> {
    const started = Date.now();
    const client = await this.getClient(alias);

    try {
      const [rows] = await client.query<RowDataPacket[]>(`
        SELECT
          DATABASE() AS database_name,
          CURRENT_USER() AS connected_user,
          VERSION() AS server_version
      `);

      const row = rows[0];
      this.statuses.set(alias, "healthy");

      return {
        alias,
        databaseName: String(row.database_name ?? this.getConnectionConfig(alias).database),
        serverVersion: String(row.server_version ?? "unknown"),
        latencyMs: Date.now() - started,
        readonly: this.getConnectionConfig(alias).readonly,
        sessionReadOnly: await this.readSessionReadOnly(client),
      };
    } catch (error) {
      this.statuses.set(alias, "unhealthy");
      throw new Error(classifyConnectionError(error));
    } finally {
      client.release();
    }
  }

  // MariaDB and MySQL 5.7 expose tx_read_only; MySQL 8 renamed it to transaction_read_only.
  private async readSessionReadOnly(client: PoolConnection): Promise<boolean | null> {
    for (const variable of ["tx_read_only", "transaction_read_only"]) {
      try {
        const [rows] = await client.query<RowDataPacket[]>(`SELECT @@SESSION.${variable} AS value`);
        return Number(rows[0]?.value) === 1;
      } catch {
        // Try the next variable name.
      }
    }
    return null;
  }

  async withClient<T>(alias: string, fn: (client: PoolConnection) => Promise<T>): Promise<T> {
    const client = await this.getClient(alias);

    try {
      return await fn(client);
    } finally {
      client.release();
    }
  }

  async query<T extends RowDataPacket>(
    alias: string,
    sql: string,
    params: unknown[] = [],
  ): Promise<{ rows: T[]; rowCount: number; columns: string[] }> {
    return this.withClient(alias, async (client) => {
      const [rows, fields] = await client.query<T[]>(sql, params);
      const resultRows = Array.isArray(rows) ? rows : [];
      return {
        rows: resultRows,
        rowCount: resultRows.length,
        columns: Array.isArray(fields) ? fields.map((field) => field.name) : [],
      };
    });
  }

  private async getClient(alias: string): Promise<PoolConnection> {
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

    let client: PoolConnection;
    try {
      client = await managed.pool.getConnection();
    } catch (error) {
      this.statuses.set(alias, "unhealthy");
      throw new Error(classifyConnectionError(error));
    }

    try {
      // Session settings persist on the physical connection, so they are applied once per connection.
      // User SQL cannot change them afterwards: SET/USE statements are rejected by the validator.
      if (!this.configuredConnections.has(client.connection)) {
        await this.configureSession(client, managed.config);
        this.configuredConnections.add(client.connection);
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

    const pool = mysql.createPool({
      host: config.host,
      port: config.port,
      database: config.database,
      user: credentials.user,
      password: credentials.password,
      ssl: this.buildSslConfig(config.sslmode),
      waitForConnections: true,
      connectionLimit: 5,
      enableKeepAlive: true,
      connectTimeout: config.connectionTimeoutMs,
      charset: "utf8mb4",
      multipleStatements: false,
      // Return DATE/DATETIME/TIMESTAMP exactly as stored instead of JS Dates shifted to the local timezone,
      // and BIGINT/DECIMAL as strings so large values keep their precision.
      dateStrings: true,
      supportBigNumbers: true,
      bigNumberStrings: true,
    });

    const managed: ManagedPool = { pool, config };
    this.pools.set(alias, managed);
    return managed;
  }

  private buildSslConfig(sslmode: ConnectionConfig["sslmode"]): undefined | { rejectUnauthorized: boolean } {
    switch (sslmode) {
      case "disable":
        return undefined;
      case "prefer":
      case "require":
        return { rejectUnauthorized: false };
      case "verify-ca":
      case "verify-full":
        return { rejectUnauthorized: true };
      default:
        return undefined;
    }
  }

  private async configureSession(client: PoolConnection, config: ConnectionConfig): Promise<void> {
    try {
      // MariaDB: seconds (fractions allowed).
      await client.query(`SET SESSION max_statement_time = ${config.statementTimeoutMs / 1000}`);
    } catch {
      try {
        // MySQL: milliseconds, SELECT statements only.
        await client.query(`SET SESSION MAX_EXECUTION_TIME = ${config.statementTimeoutMs}`);
      } catch {
        // Older servers may not expose statement timeouts.
      }
    }

    if (config.readonly) {
      try {
        // Every later transaction on this session is READ ONLY (in addition to START TRANSACTION READ ONLY).
        await client.query("SET SESSION TRANSACTION READ ONLY");
      } catch {
        // Continue; SQL validation still blocks writes.
      }
    }
  }

  async close(): Promise<void> {
    this.closed = true;
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
