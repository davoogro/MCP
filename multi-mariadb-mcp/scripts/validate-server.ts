import { config as loadDotenv } from "dotenv";
import { PACKAGE_ENV_PATH } from "../src/paths.js";
import { loadConfig } from "../src/config.js";
import { createConnectionManager } from "../src/connection-manager.js";
import { executeReadOnlyQuery } from "../src/tools/execute-query.js";
import { validateReadOnlySql } from "../src/security.js";

loadDotenv({ path: PACKAGE_ENV_PATH, override: false, quiet: true });

interface ValidationStep {
  name: string;
  success: boolean;
  details: unknown;
}

async function main(): Promise<void> {
  const steps: ValidationStep[] = [];

  try {
    const appConfig = loadConfig();
    steps.push({
      name: "load_config",
      success: true,
      details: {
        configPath: appConfig.configPath,
        connections: [...appConfig.connections.keys()],
      },
    });
  } catch (error) {
    steps.push({
      name: "load_config",
      success: false,
      details: { error: error instanceof Error ? error.message : String(error) },
    });
    printReport(steps);
    process.exit(1);
  }

  const appConfig = loadConfig();
  const manager = createConnectionManager(appConfig);

  steps.push({
    name: "list_connections",
    success: true,
    details: { connections: manager.listConnections() },
  });

  for (const alias of [...appConfig.connections.keys()].sort()) {
    try {
      const testResult = await manager.testConnection(alias);
      steps.push({
        name: `test_connection_${alias}`,
        success: true,
        details: testResult,
      });
    } catch (error) {
      steps.push({
        name: `test_connection_${alias}`,
        success: false,
        details: { error: error instanceof Error ? error.message : String(error) },
      });
    }
  }

  const blockedValidation = validateReadOnlySql("CREATE TEMP TABLE mcp_should_not_create_anything(id integer);");
  steps.push({
    name: "block_create_temp_table",
    success: !blockedValidation.valid,
    details: blockedValidation,
  });

  const firstAlias = [...appConfig.connections.keys()][0];
  if (firstAlias) {
    const queryResult = await executeReadOnlyQuery(
      manager,
      firstAlias,
      "SELECT DATABASE() AS database_name, CURRENT_USER() AS connected_user, NOW() AS server_time",
      10,
    );
    const queryPayload = JSON.parse(queryResult.content[0]?.text ?? "{}");
    steps.push({
      name: "execute_valid_select",
      success: !("error" in queryPayload),
      details: queryPayload,
    });
  }

  await manager.close();
  printReport(steps);

  const failed = steps.some((step) => !step.success);
  process.exit(failed ? 1 : 0);
}

function printReport(steps: ValidationStep[]): void {
  console.log(JSON.stringify({ steps }, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
