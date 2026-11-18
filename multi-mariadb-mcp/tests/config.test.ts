import { describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig, resolveConfigPath, resolveCredentials, getConnectionOrThrow } from "../src/config.js";
import { DEFAULT_CONFIG_PATH, PACKAGE_ENV_PATH, PACKAGE_ROOT } from "../src/paths.js";
import type { ConnectionConfig } from "../src/types.js";

const validYaml = `
defaults:
  port: 3306
  sslmode: disable
  statementTimeoutMs: 15000
  connectionTimeoutMs: 10000
  maxRows: 500
  readonly: true

connections:
  test-mariadb:
    description: Test server
    host: example.internal
    port: 3306
    database: mysql
    userEnv: MARIADB_USER
    passwordEnv: MARIADB_PASSWORD
`;

function writeTempConfig(content: string): string {
  const dir = mkdtempSync(join(tmpdir(), "multi-mariadb-mcp-"));
  const filePath = join(dir, "databases.yaml");
  writeFileSync(filePath, content, "utf8");
  return filePath;
}

describe("config loader", () => {
  it("loads a valid YAML configuration", () => {
    const configPath = writeTempConfig(validYaml);
    const config = loadConfig(configPath);
    expect(config.connections.size).toBe(1);
    expect(config.connections.get("test-mariadb")).toMatchObject({
      alias: "test-mariadb",
      host: "example.internal",
      port: 3306,
      database: "mysql",
      readonly: true,
    });
  });

  it("throws for unknown alias lookup via getConnectionOrThrow", () => {
    const config = loadConfig(writeTempConfig(validYaml));
    expect(() => getConnectionOrThrow(config, "missing")).toThrow(/Unknown connection alias/);
  });

  it("defaults to the package config when no path or env var is given", () => {
    const previous = process.env.MARIADB_MCP_CONFIG;
    delete process.env.MARIADB_MCP_CONFIG;
    try {
      expect(resolveConfigPath()).toBe(DEFAULT_CONFIG_PATH);
      expect(DEFAULT_CONFIG_PATH).toBe(join(PACKAGE_ROOT, "config", "databases.yaml"));
    } finally {
      if (previous !== undefined) {
        process.env.MARIADB_MCP_CONFIG = previous;
      }
    }
  });

  it("resolves relative paths against the package root regardless of cwd", () => {
    const previousCwd = process.cwd();
    process.chdir(tmpdir());
    try {
      expect(resolveConfigPath("./package.json")).toBe(join(PACKAGE_ROOT, "package.json"));
      expect(resolveConfigPath("./does-not-exist.yaml")).toBe(join(tmpdir(), "does-not-exist.yaml"));
    } finally {
      process.chdir(previousCwd);
    }
  });

  it("locates the package root and .env next to package.json", () => {
    expect(existsSync(join(PACKAGE_ROOT, "package.json"))).toBe(true);
    expect(PACKAGE_ENV_PATH).toBe(join(PACKAGE_ROOT, ".env"));
  });

  it("throws when required env var is missing", () => {
    const previous = process.env.MISSING_USER;
    delete process.env.MISSING_USER;
    const connection: ConnectionConfig = {
      alias: "x",
      host: "localhost",
      port: 3306,
      database: "mysql",
      userEnv: "MISSING_USER",
      passwordEnv: "MISSING_PASSWORD",
      sslmode: "disable",
      statementTimeoutMs: 1000,
      connectionTimeoutMs: 1000,
      maxRows: 100,
      readonly: true,
    };
    expect(() => resolveCredentials(connection)).toThrow(/MISSING_USER/);
    if (previous !== undefined) {
      process.env.MISSING_USER = previous;
    }
  });
});
