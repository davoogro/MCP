import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { createServerManager } from "../src/server-manager.js";
import { restartServerTool } from "../src/tools/mutating-tools.js";
import { createMockTransportFactory } from "./helpers/mock-transport.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("restart_server policy", () => {
  it("simulates reboot with dryRun=true by default", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(config, { transportFactory: createMockTransportFactory() }, audit);
    const handler = restartServerTool.handler(manager);
    const response = await handler({
      server: "SERVER-REBOOT-OK",
      reason: "Planned maintenance simulation test",
      dryRun: true,
    });
    const payload = JSON.parse(response.content[0]?.text ?? "{}");
    expect(payload.dryRun).toBe(true);
    expect(payload.rebootCommandSent).toBe(false);
  });

  it("blocks reboot when allowReboot=false", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(config, { transportFactory: createMockTransportFactory() }, audit);
    const handler = restartServerTool.handler(manager);
    const response = await handler({
      server: "SERVER-EXAMPLE-01",
      reason: "Attempt reboot on protected server",
      dryRun: false,
    });
    expect(response.isError).toBe(true);
    const payload = JSON.parse(response.content[0]?.text ?? "{}");
    expect(payload.error).toMatch(/allowReboot/i);
  });

  it("blocks reboot of localhost target", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(config, { transportFactory: createMockTransportFactory() }, audit);
    const handler = restartServerTool.handler(manager);
    const response = await handler({
      server: "SERVER-LOCALHOST",
      reason: "Attempt reboot localhost blocked",
      dryRun: false,
    });
    expect(response.isError).toBe(true);
    const payload = JSON.parse(response.content[0]?.text ?? "{}");
    expect(payload.error).toMatch(/local|policy/i);
  });

  it("returns recovery failure from mock transport", async () => {
    const configPath = writeTempConfig();
    const config = loadConfig(configPath);
    const audit = createAuditLogger();
    const manager = createServerManager(
      config,
      {
        transportFactory: createMockTransportFactory({
          "SERVER-REBOOT-OK": {
            reboot: {
              dryRun: false,
              initiated: true,
              rebootCommandSent: true,
              hostWentOffline: true,
              hostRecovered: false,
              criticalServicesHealthy: false,
              errorCode: "RECOVERY_FAILED",
              timeline: [{ phase: "recovery", at: new Date().toISOString(), ok: false }],
            },
          },
        }),
      },
      audit,
    );
    const handler = restartServerTool.handler(manager);
    const response = await handler({
      server: "SERVER-REBOOT-OK",
      reason: "Real reboot with failed recovery simulation",
      dryRun: false,
    });
    const payload = JSON.parse(response.content[0]?.text ?? "{}");
    expect(payload.hostRecovered).toBe(false);
  });
});
