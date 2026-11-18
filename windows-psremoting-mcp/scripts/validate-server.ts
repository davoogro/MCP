import { config as loadDotenv } from "dotenv";
import { resolve } from "node:path";
import { loadConfig } from "../src/config.js";
import { createAuditLogger } from "../src/audit/logger.js";
import { createServerManager } from "../src/server-manager.js";
import { sanitizeString } from "../src/security.js";

loadDotenv({ path: resolve(process.cwd(), ".env"), override: false });

interface ValidationStep {
  name: string;
  success: boolean;
  durationMs: number;
  details: unknown;
}

async function runStep(name: string, fn: () => Promise<unknown> | unknown): Promise<ValidationStep> {
  const started = Date.now();
  try {
    const details = await fn();
    return { name, success: true, durationMs: Date.now() - started, details };
  } catch (error) {
    return {
      name,
      success: false,
      durationMs: Date.now() - started,
      details: { error: error instanceof Error ? error.message : String(error) },
    };
  }
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const jsonOnly = args.includes("--json");
  const skipIntegration = args.includes("--skip-integration");
  const steps: ValidationStep[] = [];

  steps.push(
    await runStep("load_config", () => {
      const config = loadConfig();
      return {
        configPath: config.configPath,
        servers: [...config.servers.keys()],
      };
    }),
  );

  if (!steps[0]?.success) {
    printReport(steps, jsonOnly);
    process.exit(1);
  }

  const audit = createAuditLogger();
  const manager = createServerManager(undefined, {}, audit);

  steps.push(
    await runStep("list_managed_servers", () => ({
      servers: manager.getServers().map((s) => s.name),
    })),
  );

  steps.push(
    await runStep("block_non_allowlisted_host", () => {
      try {
        manager.getServer("NOT-IN-ALLOWLIST");
        throw new Error("Expected allowlist rejection");
      } catch (error) {
        if (error instanceof Error && error.message.includes("Allowlisted")) {
          return { blocked: true };
        }
        throw error;
      }
    }),
  );

  steps.push(
    await runStep("restart_server_dry_run", async () => {
      const tool = (await import("../src/tools/mutating-tools.js")).restartServerTool;
      const handler = tool.handler(manager);
      const response = await handler({
        server: "SERVER-PILOT-01",
        reason: "Validation dry run reboot simulation",
        dryRun: true,
      });
      const payload = JSON.parse(response.content[0]?.text ?? "{}");
      if ("error" in payload && !payload.dryRun) throw new Error(JSON.stringify(payload));
      return payload;
    }),
  );

  steps.push(
    await runStep("audit_redaction_self_check", () => {
      const secret = "SuperSecretPassword123!";
      audit.registerSecrets([secret]);
      audit.write({
        correlationId: audit.createCorrelationId(),
        eventType: "tool.invoke",
        tool: "validate",
        outcome: "success",
        durationMs: 0,
        parameters: { password: secret, note: `password=${secret}` },
      });
      const sanitized = sanitizeString(`password=${secret}`, [secret]);
      if (sanitized.includes(secret)) throw new Error("Secret not redacted");
      return { redacted: sanitized.includes("[REDACTED]") };
    }),
  );

  if (!skipIntegration) {
    steps.push(
      await runStep("mcp_build_exists", async () => {
        const { existsSync } = await import("node:fs");
        const dist = resolve(process.cwd(), "dist/src/index.js");
        if (!existsSync(dist)) throw new Error(`Missing build output: ${dist}`);
        return { dist };
      }),
    );
  }

  printReport(steps, jsonOnly);
  process.exit(steps.some((step) => !step.success) ? 1 : 0);
}

function printReport(steps: ValidationStep[], jsonOnly: boolean): void {
  const report = {
    generatedAt: new Date().toISOString(),
    summary: {
      total: steps.length,
      passed: steps.filter((s) => s.success).length,
      failed: steps.filter((s) => !s.success).length,
    },
    steps,
  };
  const text = JSON.stringify(report, null, 2);
  if (jsonOnly) console.log(text);
  else console.log(text);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
