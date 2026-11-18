import { describe, expect, it } from "vitest";
import { loadConfig, getServerOrThrow } from "../src/config.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("config loader", () => {
  it("loads a valid servers.json configuration", () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    expect(config.servers.size).toBe(3);
    expect(config.defaults.maxConcurrency).toBe(2);
    expect(config.servers.get("SERVER-EXAMPLE-01")?.allowReboot).toBe(false);
  });

  it("rejects invalid configuration", () => {
    const configPath = writeTempConfig({ defaults: {}, servers: [] });
    expect(() => loadConfig(configPath)).toThrow();
  });

  it("throws for unknown server lookup", () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    expect(() => getServerOrThrow(config, "NOT-LISTED")).toThrow(/Allowlisted servers/);
  });
});
