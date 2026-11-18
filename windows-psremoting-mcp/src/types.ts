export type TransportKind = "auto" | "ssh" | "winrm";

export interface ServerDefaults {
  connectionTimeoutSeconds: number;
  operationTimeoutSeconds: number;
  rebootTimeoutSeconds: number;
  maxConcurrency: number;
  verifyTls: boolean;
  allowParallelReboot: boolean;
  maxParallelReboot: number;
}

export interface ServerEntry {
  name: string;
  address: string;
  transport: TransportKind;
  port: number | null;
  allowReboot: boolean;
  protectedRole: boolean;
  allowProtectedReboot: boolean;
  allowServiceRestart: boolean;
  allowForceReboot: boolean;
  criticalServices: string[];
  tags: string[];
  credentialTargetEnv?: string;
  usernameEnv?: string;
  passwordEnv?: string;
  secretVault?: string;
  usernameSecret?: string;
  passwordSecret?: string;
  sshKeyPathEnv?: string;
  winrmCertThumbprintEnv?: string;
}

export interface AppConfig {
  defaults: ServerDefaults;
  servers: Map<string, ServerEntry>;
  configPath: string;
}

export interface ResolvedCredential {
  username: string;
  password: string;
  source: "secretmanagement" | "credentialmanager" | "environment";
  targetRef: string;
}

export type TransportErrorCode =
  | "ALLOWLIST_REJECTED"
  | "MISSING_CREDENTIALS"
  | "TLS_CERT_INVALID"
  | "CONNECTION_TIMEOUT"
  | "HOST_UNREACHABLE"
  | "AUTH_FAILED"
  | "OPERATION_TIMEOUT"
  | "LOCALHOST_BLOCKED"
  | "REBOOT_NOT_ALLOWED"
  | "PROTECTED_ROLE"
  | "RECOVERY_FAILED"
  | "POLICY_DENIED"
  | "VALIDATION_ERROR"
  | "INTERNAL_ERROR";

export interface RemoteScriptResult {
  success: boolean;
  data?: unknown;
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  durationMs: number;
  errorCode?: TransportErrorCode;
  sanitizedError?: string;
  transportUsed?: string;
}

export interface ConnectionProbeResult {
  reachable: boolean;
  transportUsed?: string;
  latencyMs: number;
  tlsValid?: boolean;
  errorCode?: TransportErrorCode;
  sanitizedError?: string;
}

export interface RebootRequest {
  serverName: string;
  reason: string;
  dryRun: boolean;
  force: boolean;
  rebootTimeoutSeconds: number;
  waitForRecovery: boolean;
}

export interface RebootResult {
  dryRun: boolean;
  initiated: boolean;
  rebootCommandSent: boolean;
  hostWentOffline: boolean;
  hostRecovered: boolean;
  criticalServicesHealthy: boolean;
  errorCode?: TransportErrorCode;
  sanitizedError?: string;
  timeline: Array<{ phase: string; at: string; ok: boolean; detail?: string }>;
}

export interface RemoteTransport {
  readonly kind: string;
  probeConnection(serverName: string): Promise<ConnectionProbeResult>;
  invokeOperation(
    serverName: string,
    operationId: string,
    parameters: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<RemoteScriptResult>;
  rebootServer(request: RebootRequest): Promise<RebootResult>;
  waitForServerOnline(
    serverName: string,
    timeoutMs: number,
    pollIntervalMs?: number,
    validateServices?: boolean,
  ): Promise<{ online: boolean; latencyMs?: number; errorCode?: TransportErrorCode }>;
  dispose(): Promise<void>;
}

export type RemoteTransportFactory = (
  serverName: string,
  server: ServerEntry,
) => Promise<RemoteTransport>;

export interface AuditEvent {
  timestamp: string;
  correlationId: string;
  eventType: "tool.invoke" | "tool.result" | "policy.denied" | "transport.error" | "server.start" | "server.stop";
  tool: string;
  serverName?: string | null;
  serverAddress?: string | null;
  transport?: string | null;
  outcome: "success" | "failure" | "denied" | "timeout" | "dry_run";
  durationMs: number;
  reason?: string | null;
  dryRun?: boolean;
  parameters?: Record<string, unknown>;
  policyChecks?: Array<{ check: string; passed: boolean; detail?: string }>;
  stateBefore?: unknown;
  stateAfter?: unknown;
  error?: { code: string; message: string; category: string } | null;
  actor?: { windowsUser?: string; mcpSessionId?: string };
  credentialRef?: string | null;
  credentialSource?: string | null;
}

export type ApprovedDiagnosticId =
  | "ping"
  | "dns_resolve"
  | "tcp_port_check"
  | "winrm_listener"
  | "disk_smart_status"
  | "service_dependencies";

export interface ServerManagerOptions {
  transportFactory?: RemoteTransportFactory;
}

export interface ToolContext {
  correlationId: string;
  actor?: { windowsUser?: string; mcpSessionId?: string };
}
