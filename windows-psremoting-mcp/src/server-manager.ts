import type {
  AppConfig,
  ConnectionProbeResult,
  RebootRequest,
  RebootResult,
  RemoteScriptResult,
  RemoteTransport,
  RemoteTransportFactory,
  ServerEntry,
  ServerManagerOptions,
  ToolContext,
  TransportKind,
} from "./types.js";
import { getServerOrThrow, listServers, loadConfig } from "./config.js";
import { createDefaultTransportFactory } from "./transport/factory.js";
import type { AuditLogger } from "./audit/logger.js";

class Semaphore {
  private active = 0;
  private readonly queue: Array<() => void> = [];

  constructor(private readonly max: number) {}

  async acquire(): Promise<() => void> {
    if (this.active < this.max) {
      this.active += 1;
      return () => this.release();
    }

    await new Promise<void>((resolve) => {
      this.queue.push(resolve);
    });
    this.active += 1;
    return () => this.release();
  }

  private release(): void {
    this.active -= 1;
    const next = this.queue.shift();
    if (next) next();
  }
}

export class ServerManager {
  private readonly transportCache = new Map<string, RemoteTransport>();
  private readonly semaphore: Semaphore;
  private rebootInProgress = false;

  constructor(
    readonly config: AppConfig,
    private readonly transportFactory: RemoteTransportFactory,
    readonly audit: AuditLogger,
  ) {
    this.semaphore = new Semaphore(config.defaults.maxConcurrency);
  }

  static create(config?: AppConfig, options: ServerManagerOptions = {}, audit?: AuditLogger): ServerManager {
    const appConfig = config ?? loadConfig();
    const factory = options.transportFactory ?? createDefaultTransportFactory(appConfig);
    return new ServerManager(appConfig, factory, audit!);
  }

  getServers(): ServerEntry[] {
    return listServers(this.config);
  }

  getServer(name: string): ServerEntry {
    return getServerOrThrow(this.config, name);
  }

  private async getTransport(serverName: string): Promise<RemoteTransport> {
    const cached = this.transportCache.get(serverName);
    if (cached) return cached;
    const server = this.getServer(serverName);
    const transport = await this.transportFactory(serverName, server);
    this.transportCache.set(serverName, transport);
    return transport;
  }

  private async withTransport<T>(
    ctx: ToolContext,
    tool: string,
    serverName: string,
    fn: (transport: RemoteTransport, server: ServerEntry) => Promise<T>,
  ): Promise<T> {
    const release = await this.semaphore.acquire();
    const started = Date.now();
    const server = this.getServer(serverName);

    this.audit.write({
      correlationId: ctx.correlationId,
      eventType: "tool.invoke",
      tool,
      serverName: server.name,
      serverAddress: server.address,
      outcome: "success",
      durationMs: 0,
      actor: ctx.actor,
      parameters: { server: serverName },
    });

    try {
      const transport = await this.getTransport(serverName);
      const result = await fn(transport, server);
      this.audit.write({
        correlationId: ctx.correlationId,
        eventType: "tool.result",
        tool,
        serverName: server.name,
        serverAddress: server.address,
        outcome: "success",
        durationMs: Date.now() - started,
        actor: ctx.actor,
      });
      return result;
    } catch (error) {
      this.audit.write({
        correlationId: ctx.correlationId,
        eventType: "transport.error",
        tool,
        serverName: server.name,
        serverAddress: server.address,
        outcome: "failure",
        durationMs: Date.now() - started,
        actor: ctx.actor,
        error: {
          code: "TRANSPORT_ERROR",
          message: error instanceof Error ? error.message : String(error),
          category: "network",
        },
      });
      throw error;
    } finally {
      release();
    }
  }

  async testConnection(ctx: ToolContext, serverName: string, transportOverride?: string): Promise<ConnectionProbeResult> {
    return this.withTransport(ctx, "test_server_connection", serverName, async (transport, server) => {
      const override = transportOverride as TransportKind | undefined;
      if (override && override !== "auto" && override !== server.transport) {
        // One-off probe with the requested transport; not cached so normal calls keep the configured one.
        const oneOff = await this.transportFactory(server.name, { ...server, transport: override });
        try {
          return await oneOff.probeConnection(server.name);
        } finally {
          await oneOff.dispose();
        }
      }
      return transport.probeConnection(server.name);
    });
  }

  async invokeOperation(
    ctx: ToolContext,
    tool: string,
    serverName: string,
    operationId: string,
    parameters: Record<string, unknown> = {},
  ): Promise<RemoteScriptResult> {
    return this.withTransport(ctx, tool, serverName, async (transport) => {
      return transport.invokeOperation(
        serverName,
        operationId,
        parameters,
        this.config.defaults.operationTimeoutSeconds * 1000,
      );
    });
  }

  async rebootServer(ctx: ToolContext, request: RebootRequest): Promise<RebootResult> {
    if (!request.dryRun) {
      if (this.rebootInProgress && !this.config.defaults.allowParallelReboot) {
        throw new Error("Another reboot is in progress and parallel reboots are disabled");
      }
      this.rebootInProgress = true;
    }

    try {
      return await this.withTransport(ctx, "restart_server", request.serverName, async (transport) => {
        return transport.rebootServer(request);
      });
    } finally {
      if (!request.dryRun) {
        this.rebootInProgress = false;
      }
    }
  }

  async waitForOnline(
    ctx: ToolContext,
    serverName: string,
    timeoutSeconds: number,
    pollIntervalSeconds: number,
    validateServices: boolean,
  ): Promise<{ online: boolean; latencyMs?: number }> {
    return this.withTransport(ctx, "wait_for_server_online", serverName, async (transport, server) => {
      return transport.waitForServerOnline(
        server.name,
        timeoutSeconds * 1000,
        pollIntervalSeconds * 1000,
        validateServices,
      );
    });
  }

  async close(): Promise<void> {
    for (const transport of this.transportCache.values()) {
      await transport.dispose();
    }
    this.transportCache.clear();
  }
}

export function createServerManager(
  config?: AppConfig,
  options: ServerManagerOptions = {},
  audit?: AuditLogger,
): ServerManager {
  return ServerManager.create(config, options, audit);
}
