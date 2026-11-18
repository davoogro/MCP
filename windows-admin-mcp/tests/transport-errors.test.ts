import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { createServerManager } from "../src/server-manager.js";
import { createMockTransportFactory } from "./helpers/mock-transport.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("transport error mapping", () => {
  it("surfaces TLS invalid errors from mock transport", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(
      config,
      {
        transportFactory: createMockTransportFactory({
          "SERVER-EXAMPLE-01": {
            probe: {
              reachable: false,
              latencyMs: 100,
              errorCode: "TLS_CERT_INVALID",
              sanitizedError: "Certificate validation failed",
            },
          },
        }),
      },
      audit,
    );
    const result = await manager.testConnection(
      { correlationId: audit.createCorrelationId() },
      "SERVER-EXAMPLE-01",
    );
    expect(result.reachable).toBe(false);
    expect(result.errorCode).toBe("TLS_CERT_INVALID");
  });

  it("surfaces connection timeout from mock transport", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(
      config,
      {
        transportFactory: createMockTransportFactory({
          "SERVER-EXAMPLE-01": {
            invoke: {
              success: false,
              durationMs: 15000,
              errorCode: "CONNECTION_TIMEOUT",
              sanitizedError: "Connection timed out",
            },
          },
        }),
      },
      audit,
    );
    const result = await manager.invokeOperation(
      { correlationId: audit.createCorrelationId() },
      "get_server_health",
      "SERVER-EXAMPLE-01",
      "get_health",
    );
    expect(result.success).toBe(false);
    expect(result.errorCode).toBe("CONNECTION_TIMEOUT");
  });
});
