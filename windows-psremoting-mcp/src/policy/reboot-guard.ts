import type { AppConfig, RebootRequest, ServerEntry } from "../types.js";
import { assertNotLocalHost, isLocalHostTarget } from "./localhost-guard.js";

export interface RebootPolicyResult {
  allowed: boolean;
  checks: Array<{ check: string; passed: boolean; detail?: string }>;
  errorCode?: string;
  message?: string;
}

export function evaluateRebootPolicy(
  config: AppConfig,
  server: ServerEntry,
  request: RebootRequest,
): RebootPolicyResult {
  const checks: RebootPolicyResult["checks"] = [];

  const allowlistCheck = { check: "allowlist", passed: true };
  checks.push(allowlistCheck);

  const allowRebootPassed = request.dryRun || server.allowReboot === true;
  checks.push({
    check: "allowReboot",
    passed: allowRebootPassed,
    detail: allowRebootPassed ? undefined : "allowReboot=false in servers.json",
  });

  const localBlocked = isLocalHostTarget(server);
  checks.push({
    check: "localhost",
    passed: !localBlocked,
    detail: localBlocked ? "Target matches the local host running this MCP server" : undefined,
  });

  const protectedPassed =
    !server.protectedRole || server.allowProtectedReboot === true;
  checks.push({
    check: "protectedRole",
    passed: protectedPassed,
    detail: protectedPassed ? undefined : "protectedRole=true without allowProtectedReboot",
  });

  const criticalTags = ["domain-controller", "cluster-node", "fsmo"];
  const hasCriticalTag = server.tags.some((tag) => criticalTags.includes(tag.toLowerCase()));
  const criticalPassed = !hasCriticalTag || server.allowProtectedReboot === true;
  checks.push({
    check: "criticalTags",
    passed: criticalPassed,
    detail: hasCriticalTag ? `Tags: ${server.tags.join(", ")}` : undefined,
  });

  if (request.force && !request.dryRun && !server.allowForceReboot) {
    checks.push({
      check: "allowForceReboot",
      passed: false,
      detail: "force=true requires allowForceReboot=true",
    });
  }

  if (!request.dryRun && !config.defaults.allowParallelReboot) {
    checks.push({
      check: "parallelRebootPolicy",
      passed: true,
      detail: "Sequential reboot mode enforced by defaults.allowParallelReboot=false",
    });
  }

  const failed = checks.find((item) => !item.passed);
  if (failed) {
    return {
      allowed: false,
      checks,
      errorCode: failed.check === "allowReboot" ? "POLICY_REBOOT_NOT_ALLOWED" : "POLICY_DENIED",
      message: failed.detail ?? `Reboot policy check failed: ${failed.check}`,
    };
  }

  if (request.dryRun) {
    return { allowed: true, checks };
  }

  assertNotLocalHost(server, "restart_server");
  return { allowed: true, checks };
}

export function evaluateServiceRestartPolicy(
  server: ServerEntry,
  _serviceName: string,
  dryRun: boolean,
): RebootPolicyResult {
  const checks: RebootPolicyResult["checks"] = [
    { check: "allowlist", passed: true },
  ];

  if (isLocalHostTarget(server)) {
    checks.push({ check: "localhost", passed: false, detail: "Localhost service restart blocked" });
  } else {
    checks.push({ check: "localhost", passed: true });
  }

  const allowed = server.allowServiceRestart || dryRun;
  checks.push({
    check: "allowServiceRestart",
    passed: allowed,
    detail: allowed ? undefined : "allowServiceRestart=false",
  });

  const failed = checks.find((item) => !item.passed);
  if (failed) {
    return {
      allowed: false,
      checks,
      errorCode: "POLICY_DENIED",
      message: failed.detail,
    };
  }

  return { allowed: true, checks };
}
