import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const exampleServersJson = {
  defaults: {
    connectionTimeoutSeconds: 15,
    operationTimeoutSeconds: 120,
    rebootTimeoutSeconds: 900,
    maxConcurrency: 2,
    verifyTls: true,
    allowParallelReboot: false,
    maxParallelReboot: 1,
  },
  servers: [
    {
      name: "SERVER-EXAMPLE-01",
      address: "server-example-01.example.local",
      transport: "auto",
      port: null,
      allowReboot: false,
      protectedRole: true,
      allowProtectedReboot: false,
      allowServiceRestart: false,
      allowForceReboot: false,
      criticalServices: ["W32Time"],
      tags: ["example"],
      usernameEnv: "TEST_SERVER_USER",
      passwordEnv: "TEST_SERVER_PASSWORD",
    },
    {
      name: "SERVER-LOCALHOST",
      address: "127.0.0.1",
      transport: "auto",
      port: null,
      allowReboot: true,
      protectedRole: false,
      allowProtectedReboot: false,
      allowServiceRestart: true,
      allowForceReboot: false,
      criticalServices: [],
      tags: [],
      usernameEnv: "TEST_SERVER_USER",
      passwordEnv: "TEST_SERVER_PASSWORD",
    },
    {
      name: "SERVER-REBOOT-OK",
      address: "server-reboot-ok.example.local",
      transport: "auto",
      port: null,
      allowReboot: true,
      protectedRole: false,
      allowProtectedReboot: false,
      allowServiceRestart: true,
      allowForceReboot: false,
      criticalServices: ["W32Time"],
      tags: [],
      usernameEnv: "TEST_SERVER_USER",
      passwordEnv: "TEST_SERVER_PASSWORD",
    },
  ],
};

export function writeTempConfig(content: unknown = exampleServersJson): string {
  const dir = mkdtempSync(join(tmpdir(), "windows-admin-mcp-"));
  const filePath = join(dir, "servers.json");
  writeFileSync(filePath, JSON.stringify(content, null, 2), "utf8");
  return filePath;
}
