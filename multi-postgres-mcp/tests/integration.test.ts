import { describe, expect, it } from "vitest";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig } from "../src/config.js";
import { createConnectionManager } from "../src/connection-manager.js";

const yaml = `
defaults:
  maxRows: 3
  readonly: true
  port: 5432
  sslmode: prefer
  statementTimeoutMs: 15000
  connectionTimeoutMs: 10000

connections:
  test-postgres:
    host: localhost
    database: postgres
    userEnv: TEST_POSTGRES_USER
    passwordEnv: TEST_POSTGRES_PASSWORD
    maxRows: 3
`;

describe("max rows configuration", () => {
  it("caps requested maxRows to configured maximum", () => {
    const dir = mkdtempSync(join(tmpdir(), "multi-postgres-mcp-rows-"));
    const filePath = join(dir, "databases.yaml");
    writeFileSync(filePath, yaml, "utf8");

    const config = loadConfig(filePath);
    const manager = createConnectionManager(config);
    expect(manager.getMaxRows("test-postgres")).toBe(3);
  });
});

describe("integration (optional)", () => {
  it("connects to test-postgres when integration mode is enabled", async () => {
    const enabled = process.env.RUN_PG_INTEGRATION === "1";
    const required = ["PG_MCP_CONFIG", "TEST_POSTGRES_USER", "TEST_POSTGRES_PASSWORD"];
    const missing = required.filter((name) => !process.env[name]);

    if (!enabled || missing.length > 0) {
      return;
    }

    const config = loadConfig(process.env.PG_MCP_CONFIG);
    const manager = createConnectionManager(config);
    const result = await manager.testConnection("test-postgres");
    expect(result.databaseName).toBeTruthy();
    await manager.close();
  }, 30000);
});
