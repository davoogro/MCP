import { defineConfig } from "vitest/config";
import { config as loadDotenv } from "dotenv";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

loadDotenv({ path: resolve(root, ".env"), override: false, quiet: true });

export default defineConfig({
  test: {
    globals: false,
    environment: "node",
    include: ["tests/**/*.test.ts"],
    testTimeout: 30000,
    env: {
      // Keep test events out of the production audit log (logs/audit).
      WINMCP_AUDIT_DIR: resolve(root, "logs", "audit-test"),
    },
  },
});
