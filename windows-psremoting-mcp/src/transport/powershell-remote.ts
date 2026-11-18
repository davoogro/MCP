import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import type {
  ConnectionProbeResult,
  RebootRequest,
  RebootResult,
  RemoteScriptResult,
  RemoteTransport,
  ServerEntry,
} from "../types.js";
import { getOptionalPathFromEnv, getRemoteOperationScript, resolveCredentials } from "../credentials/resolver.js";
import { sanitizeErrorMessage } from "../security.js";
import { powerShellFileArgs, resolvePowerShellExecutable, toAsciiJson } from "../paths.js";

interface OperationRequest {
  operationId: string;
  server: ServerEntry;
  parameters: Record<string, unknown>;
  timeoutMs: number;
  defaults: {
    verifyTls: boolean;
    connectionTimeoutSeconds: number;
  };
  credential: {
    username: string;
    password: string;
    source: string;
    targetRef: string;
  };
  sshKeyPath?: string;
  winrmCertThumbprint?: string;
}

export class PowerShellRemoteTransport implements RemoteTransport {
  readonly kind = "powershell-remote";
  private readonly secrets: string[] = [];

  constructor(
    private readonly server: ServerEntry,
    private readonly defaults: {
      verifyTls: boolean;
      connectionTimeoutSeconds: number;
      operationTimeoutSeconds: number;
      rebootTimeoutSeconds: number;
    },
  ) {}

  private async invoke(
    operationId: string,
    parameters: Record<string, unknown> = {},
    timeoutMs?: number,
  ): Promise<RemoteScriptResult> {
    const scriptPath = getRemoteOperationScript();
    if (!existsSync(scriptPath)) {
      return {
        success: false,
        durationMs: 0,
        errorCode: "INTERNAL_ERROR",
        sanitizedError: `Remote operation script not found: ${scriptPath}`,
      };
    }

    let credentialResolution;
    try {
      credentialResolution = await resolveCredentials(this.server);
      this.secrets.push(...credentialResolution.secretsForRedaction);
    } catch (error) {
      return {
        success: false,
        durationMs: 0,
        errorCode: "MISSING_CREDENTIALS",
        sanitizedError: sanitizeErrorMessage(error, this.secrets),
      };
    }

    const payload: OperationRequest = {
      operationId,
      server: this.server,
      parameters,
      timeoutMs: timeoutMs ?? this.defaults.operationTimeoutSeconds * 1000,
      defaults: {
        verifyTls: this.defaults.verifyTls,
        connectionTimeoutSeconds: this.defaults.connectionTimeoutSeconds,
      },
      credential: {
        username: credentialResolution.credential.username,
        password: credentialResolution.credential.password,
        source: credentialResolution.credential.source,
        targetRef: credentialResolution.credential.targetRef,
      },
      sshKeyPath: getOptionalPathFromEnv(this.server.sshKeyPathEnv),
      winrmCertThumbprint: getOptionalPathFromEnv(this.server.winrmCertThumbprintEnv),
    };

    const shell = resolvePowerShellExecutable();
    const started = Date.now();

    return new Promise((resolvePromise) => {
      // The request (which includes the resolved credential) is piped through stdin,
      // never passed on the command line where other local processes could read it.
      const child = spawn(shell, powerShellFileArgs(scriptPath), {
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      });
      child.stdin.on("error", () => {
        // Process exited before reading stdin; handled by the close/error handlers.
      });
      child.stdin.end(toAsciiJson(payload), "utf8");

      let stdout = "";
      let stderr = "";
      const timer = setTimeout(() => {
        child.kill("SIGTERM");
      }, (timeoutMs ?? this.defaults.operationTimeoutSeconds * 1000) + 5000);

      child.stdout.on("data", (chunk: Buffer) => {
        stdout += chunk.toString("utf8");
      });
      child.stderr.on("data", (chunk: Buffer) => {
        stderr += chunk.toString("utf8");
      });
      child.on("error", (error) => {
        clearTimeout(timer);
        resolvePromise({
          success: false,
          durationMs: Date.now() - started,
          errorCode: "INTERNAL_ERROR",
          sanitizedError: sanitizeErrorMessage(error, this.secrets),
          stderr,
        });
      });
      child.on("close", (code) => {
        clearTimeout(timer);
        const durationMs = Date.now() - started;
        try {
          const parsed = JSON.parse(stdout || "{}") as RemoteScriptResult;
          resolvePromise({
            ...parsed,
            durationMs: parsed.durationMs ?? durationMs,
            stderr: parsed.stderr ?? stderr,
            exitCode: parsed.exitCode ?? code ?? 1,
            sanitizedError: parsed.sanitizedError
              ? sanitizeErrorMessage(parsed.sanitizedError, this.secrets)
              : stderr
                ? sanitizeErrorMessage(stderr, this.secrets)
                : undefined,
          });
        } catch {
          resolvePromise({
            success: false,
            durationMs,
            exitCode: code ?? 1,
            stdout,
            stderr,
            errorCode: "INTERNAL_ERROR",
            sanitizedError: sanitizeErrorMessage(stderr || stdout || "Invalid JSON from remote executor", this.secrets),
          });
        }
      });
    });
  }

  async probeConnection(serverName: string): Promise<ConnectionProbeResult> {
    void serverName;
    const result = await this.invoke("probe_connection", {}, this.defaults.connectionTimeoutSeconds * 1000);
    const data = (result.data ?? {}) as ConnectionProbeResult;
    return {
      reachable: result.success && data.reachable === true,
      transportUsed: data.transportUsed ?? result.transportUsed,
      latencyMs: data.latencyMs ?? result.durationMs,
      tlsValid: data.tlsValid,
      errorCode: result.errorCode ?? data.errorCode,
      sanitizedError: result.sanitizedError ?? data.sanitizedError,
    };
  }

  async invokeOperation(
    serverName: string,
    operationId: string,
    parameters: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<RemoteScriptResult> {
    void serverName;
    return this.invoke(operationId, parameters, timeoutMs);
  }

  async rebootServer(request: RebootRequest): Promise<RebootResult> {
    const result = await this.invoke(
      "restart_server",
      {
        reason: request.reason,
        dryRun: request.dryRun,
        force: request.force,
        rebootTimeoutSeconds: request.rebootTimeoutSeconds,
        waitForRecovery: request.waitForRecovery,
      },
      request.rebootTimeoutSeconds * 1000 + 30000,
    );
    return (result.data ?? {
      dryRun: request.dryRun,
      initiated: false,
      rebootCommandSent: false,
      hostWentOffline: false,
      hostRecovered: false,
      criticalServicesHealthy: false,
      sanitizedError: result.sanitizedError,
      errorCode: result.errorCode,
      timeline: [],
    }) as RebootResult;
  }

  async waitForServerOnline(
    serverName: string,
    timeoutMs: number,
    pollIntervalMs = 15000,
    validateServices = true,
  ): Promise<{ online: boolean; latencyMs?: number; errorCode?: import("../types.js").TransportErrorCode }> {
    void serverName;
    const result = await this.invoke(
      "wait_for_online",
      { timeoutMs, pollIntervalMs, validateServices },
      timeoutMs + 10000,
    );
    const data = (result.data ?? {}) as { online: boolean; latencyMs?: number; errorCode?: import("../types.js").TransportErrorCode };
    return {
      online: result.success && data.online === true,
      latencyMs: data.latencyMs,
      errorCode: result.errorCode ?? data.errorCode,
    };
  }

  async dispose(): Promise<void> {
    this.secrets.length = 0;
  }
}

export async function createPowerShellTransport(
  server: ServerEntry,
  defaults: {
    verifyTls: boolean;
    connectionTimeoutSeconds: number;
    operationTimeoutSeconds: number;
    rebootTimeoutSeconds: number;
  },
): Promise<RemoteTransport> {
  return new PowerShellRemoteTransport(server, defaults);
}
