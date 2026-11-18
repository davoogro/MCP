import { describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig } from "../src/config.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { createServerManager } from "../src/server-manager.js";
import { restartServiceTool } from "../src/tools/mutating-tools.js";
import {
  defaultAuditDir,
  defaultConfigPath,
  getPackageRoot,
  resolvePowerShellExecutable,
  toAsciiJson,
} from "../src/paths.js";
import type { RemoteTransport, ServerEntry } from "../src/types.js";
import { MockRemoteTransport } from "./helpers/mock-transport.js";
import { writeTempConfig } from "./helpers/fixtures.js";

describe("cwd-independent paths", () => {
  it("resolves package root from the module location", () => {
    const root = getPackageRoot();
    expect(existsSync(join(root, "package.json"))).toBe(true);
    expect(defaultConfigPath()).toBe(join(root, "config", "servers.json"));
    expect(defaultAuditDir()).toBe(join(root, "logs", "audit"));
  });

  it("honours WINMCP_POWERSHELL override", () => {
    const previous = process.env.WINMCP_POWERSHELL;
    process.env.WINMCP_POWERSHELL = "C:\\custom\\pwsh.exe";
    try {
      expect(resolvePowerShellExecutable()).toBe("C:\\custom\\pwsh.exe");
    } finally {
      if (previous === undefined) delete process.env.WINMCP_POWERSHELL;
      else process.env.WINMCP_POWERSHELL = previous;
    }
  });

  it("encodes stdin payloads as pure ASCII JSON", () => {
    const encoded = toAsciiJson({ reason: "mantenimiento día ñ" });
    expect(/^[\x00-\x7e]*$/.test(encoded)).toBe(true);
    expect(JSON.parse(encoded).reason).toBe("mantenimiento día ñ");
  });
});

describe("test_server_connection transport override", () => {
  it("probes with a one-off transport using the requested kind", async () => {
    const config = loadConfig(writeTempConfig());
    const audit = createAuditLogger(mkdtempSync(join(tmpdir(), "winmcp-audit-")));
    const created: ServerEntry[] = [];
    const manager = createServerManager(
      config,
      {
        transportFactory: async (_name: string, server: ServerEntry): Promise<RemoteTransport> => {
          created.push(server);
          return new MockRemoteTransport({});
        },
      },
      audit,
    );
    await manager.testConnection({ correlationId: "t1" }, "SERVER-EXAMPLE-01", "winrm");
    expect(created.map((s) => s.transport)).toEqual(["auto", "winrm"]);
  });
});

describe("restart_service denylist", () => {
  it("audits denylist refusals", async () => {
    const dir = mkdtempSync(join(tmpdir(), "winmcp-audit-"));
    const config = loadConfig(writeTempConfig());
    const audit = createAuditLogger(dir);
    const manager = createServerManager(
      config,
      { transportFactory: async () => new MockRemoteTransport({}) },
      audit,
    );
    const response = await restartServiceTool.handler(manager)({
      server: "SERVER-REBOOT-OK",
      serviceName: "NTDS",
      reason: "Testing protected service refusal",
      dryRun: false,
    });
    expect(response.isError).toBe(true);
    const lines = readdirSync(dir).flatMap((f) =>
      readFileSync(join(dir, f), "utf8").trim().split("\n"),
    );
    const denied = lines.map((l) => JSON.parse(l)).find((e) => e.eventType === "policy.denied");
    expect(denied?.tool).toBe("restart_service");
    expect(denied?.outcome).toBe("denied");
  });
});
