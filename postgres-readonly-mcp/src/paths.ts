import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Walk up from this module (src/ under tsx/vitest, dist/src/ once built) until package.json is found,
// so config and .env resolve identically no matter which working directory launched the server.
function findPackageRoot(startDir: string): string {
  let current = startDir;
  for (let depth = 0; depth < 5; depth += 1) {
    if (existsSync(resolve(current, "package.json"))) {
      return current;
    }
    const parent = dirname(current);
    if (parent === current) {
      break;
    }
    current = parent;
  }

  return resolve(startDir, "..", "..");
}

export const PACKAGE_ROOT = findPackageRoot(dirname(fileURLToPath(import.meta.url)));
export const DEFAULT_CONFIG_PATH = resolve(PACKAGE_ROOT, "config", "databases.yaml");
export const PACKAGE_ENV_PATH = resolve(PACKAGE_ROOT, ".env");
// Shared .env one level up (C:\DVB\mcp\.env) holds credentials common to several MCP servers.
export const SHARED_ENV_PATH = resolve(PACKAGE_ROOT, "..", ".env");
export const POSTGRES_EXPORT_DIR = "C:\\DVB\\exports\\postgres";
