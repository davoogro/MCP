import type {
  ConnectionProbeResult,
  RebootRequest,
  RebootResult,
  RemoteScriptResult,
  RemoteTransport,
} from "../../src/types.js";

export interface MockTransportScenario {
  probe?: ConnectionProbeResult;
  invoke?: RemoteScriptResult;
  reboot?: RebootResult;
  waitOnline?: { online: boolean; latencyMs?: number };
}

export class MockRemoteTransport implements RemoteTransport {
  readonly kind = "mock";
  readonly calls: Array<{ method: string; args: unknown[] }> = [];

  constructor(private scenario: MockTransportScenario) {}

  setScenario(partial: Partial<MockTransportScenario>): void {
    this.scenario = { ...this.scenario, ...partial };
  }

  async probeConnection(serverName: string): Promise<ConnectionProbeResult> {
    this.calls.push({ method: "probeConnection", args: [serverName] });
    return (
      this.scenario.probe ?? {
        reachable: true,
        transportUsed: "mock",
        latencyMs: 10,
        tlsValid: true,
      }
    );
  }

  async invokeOperation(
    serverName: string,
    operationId: string,
    parameters: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<RemoteScriptResult> {
    this.calls.push({ method: "invokeOperation", args: [serverName, operationId, parameters, timeoutMs] });
    return (
      this.scenario.invoke ?? {
        success: true,
        durationMs: 5,
        data: { operationId, parameters },
      }
    );
  }

  async rebootServer(request: RebootRequest): Promise<RebootResult> {
    this.calls.push({ method: "rebootServer", args: [request] });
    if (this.scenario.reboot) return this.scenario.reboot;
    return {
      dryRun: request.dryRun,
      initiated: !request.dryRun,
      rebootCommandSent: !request.dryRun,
      hostWentOffline: !request.dryRun,
      hostRecovered: request.dryRun,
      criticalServicesHealthy: request.dryRun,
      timeline: [{ phase: request.dryRun ? "dry_run" : "reboot", at: new Date().toISOString(), ok: true }],
    };
  }

  async waitForServerOnline(
    serverName: string,
    timeoutMs: number,
    pollIntervalMs?: number,
    validateServices?: boolean,
  ): Promise<{ online: boolean; latencyMs?: number }> {
    this.calls.push({
      method: "waitForServerOnline",
      args: [serverName, timeoutMs, pollIntervalMs, validateServices],
    });
    return this.scenario.waitOnline ?? { online: true, latencyMs: 100 };
  }

  async dispose(): Promise<void> {}
}

export function createMockTransportFactory(
  scenarios: Record<string, MockTransportScenario> = {},
): (serverName: string) => Promise<RemoteTransport> {
  return async (serverName: string) => new MockRemoteTransport(scenarios[serverName] ?? {});
}
