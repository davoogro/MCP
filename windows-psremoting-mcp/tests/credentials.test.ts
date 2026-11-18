import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { resolveCredentials } from "../src/credentials/resolver.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("credential resolver", () => {
  it("throws when required env vars are missing", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const server = config.servers.get("SERVER-EXAMPLE-01");
    expect(server).toBeDefined();
    delete process.env.TEST_SERVER_USER;
    delete process.env.TEST_SERVER_PASSWORD;
    await expect(resolveCredentials(server!)).rejects.toThrow(/TEST_SERVER_USER/);
  });

  it("resolves credentials from environment variables", async () => {
    process.env.TEST_SERVER_USER = "EXAMPLE\\svc-winadmin";
    process.env.TEST_SERVER_PASSWORD = "test-password-value";
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const server = config.servers.get("SERVER-EXAMPLE-01")!;
    const result = await resolveCredentials(server);
    expect(result.credential.username).toBe("EXAMPLE\\svc-winadmin");
    expect(result.credential.source).toBe("environment");
    delete process.env.TEST_SERVER_USER;
    delete process.env.TEST_SERVER_PASSWORD;
  });
});
