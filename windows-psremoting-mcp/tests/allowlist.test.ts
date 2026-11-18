import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { createServerManager } from "../src/server-manager.js";
import { createMockTransportFactory } from "./helpers/mock-transport.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("allowlist enforcement", () => {
  it("rejects hosts outside the allowlist", () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(config, { transportFactory: createMockTransportFactory() }, audit);
    expect(() => manager.getServer("UNKNOWN-HOST")).toThrow(/Allowlisted servers/);
  });

  it("returns only configured servers from list_managed_servers data", () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(config, { transportFactory: createMockTransportFactory() }, audit);
    const names = manager.getServers().map((s) => s.name);
    expect(names).toContain("SERVER-EXAMPLE-01");
    expect(names).not.toContain("UNKNOWN-HOST");
  });
});
