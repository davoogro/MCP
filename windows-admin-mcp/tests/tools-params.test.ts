import { describe, expect, it } from "vitest";
import { restartServerTool, runApprovedDiagnosticTool } from "../src/tools/mutating-tools.js";
import { getServiceStatusTool } from "../src/tools/read-tools.js";

describe("tool parameter schemas", () => {
  it("rejects restart_server reason that is too short", () => {
    expect(() =>
      restartServerTool.inputSchema.parse({
        server: "SERVER-EXAMPLE-01",
        reason: "short",
      }),
    ).toThrow();
  });

  it("accepts valid service status params", () => {
    const parsed = getServiceStatusTool.inputSchema.parse({
      server: "SERVER-EXAMPLE-01",
      serviceNames: ["W32Time"],
    });
    expect(parsed.serviceNames).toEqual(["W32Time"]);
  });

  it("rejects unknown diagnostic id", () => {
    expect(() =>
      runApprovedDiagnosticTool.inputSchema.parse({
        server: "SERVER-EXAMPLE-01",
        diagnostic: "execute_any_powershell",
      }),
    ).toThrow();
  });
});
