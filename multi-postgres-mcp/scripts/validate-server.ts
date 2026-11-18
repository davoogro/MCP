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

  try {
    const connections = manager.listConnections();
    steps.push({
      name: "list_connections",
      success: true,
      details: { connections },
    });
  } catch (error) {
    steps.push({
      name: "list_connections",
      success: false,
      details: { error: error instanceof Error ? error.message : String(error) },
    });
  }

  try {
    const testResult = await manager.testConnection("test-postgres");
    steps.push({
      name: "test_connection",
      success: true,
      details: testResult,
    });
  } catch (error) {
    steps.push({
      name: "test_connection",
      success: false,
      details: { error: error instanceof Error ? error.message : String(error) },
    });
  }

  const blockedSql = "CREATE TEMP TABLE mcp_should_not_create_anything(id integer);";
  const blockedValidation = validateReadOnlySql(blockedSql);
  steps.push({
    name: "block_create_temp_table",
    success: !blockedValidation.valid,
    details: blockedValidation,
  });

  const multiStatementSql = "SELECT 1; SELECT 2;";
  const multiValidation = validateReadOnlySql(multiStatementSql);
  steps.push({
    name: "block_multiple_statements",
    success: !multiValidation.valid,
    details: multiValidation,
  });

  const validSql = `
    SELECT
      current_database() AS database_name,
      current_user AS connected_user,
      now() AS server_time
  `;

  const queryResult = await executeReadOnlyQuery(manager, "test-postgres", validSql, 10);
  const queryPayload = JSON.parse(queryResult.content[0]?.text ?? "{}");
  steps.push({
    name: "execute_valid_select",
    success: !("error" in queryPayload),
    details: queryPayload,
  });

  try {
    const schemas = await manager.query("test-postgres", `
      SELECT n.nspname AS schema_name
      FROM pg_catalog.pg_namespace n
      WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        AND has_schema_privilege(current_user, n.oid, 'USAGE')
      ORDER BY n.nspname
      LIMIT 5
    `);
    steps.push({
      name: "list_schemas_sample",
      success: true,
      details: { schemas: schemas.rows },
    });
  } catch (error) {
    steps.push({
      name: "list_schemas_sample",
      success: false,
      details: { error: error instanceof Error ? error.message : String(error) },
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
