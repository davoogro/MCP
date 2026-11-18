import { existsSync, readFileSync } from "node:fs";
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const moduleDir = dirname(fileURLToPath(import.meta.url));

let cachedRoot: string | undefined;

/**
 * Package root (the folder containing this project's package.json), resolved from
 * the module file location so startup never depends on the process cwd.
 * Works from both src/ (tsx) and dist/src/ (compiled).
 */
export function getPackageRoot(): string {
  if (cachedRoot) return cachedRoot;
  let current = moduleDir;
  for (let i = 0; i < 6; i += 1) {
    const candidate = join(current, "package.json");
    if (existsSync(candidate)) {
      try {
        const pkg = JSON.parse(readFileSync(candidate, "utf8")) as { name?: string };
        if (pkg.name === "windows-admin-mcp") {
          cachedRoot = current;
          return current;
        }
      } catch {
        // ignore unreadable package.json and keep walking up
      }
    }
    current = resolve(current, "..");
  }
  cachedRoot = resolve(moduleDir, "..", "..");
  return cachedRoot;
}

/** Resolve a possibly-relative path against the package root (never the cwd). */
export function resolveFromPackageRoot(path: string): string {
  return isAbsolute(path) ? path : resolve(getPackageRoot(), path);
}

export function defaultConfigPath(): string {
  return join(getPackageRoot(), "config", "servers.json");
}

export function defaultAuditDir(): string {
  return join(getPackageRoot(), "logs", "audit");
}

function findOnPath(exe: string): string | undefined {
  const pathVar = process.env.PATH ?? process.env.Path ?? "";
  for (const dir of pathVar.split(delimiter)) {
    if (!dir) continue;
    const candidate = join(dir.replace(/^"|"$/g, ""), exe);
    if (existsSync(candidate)) return candidate;
  }
  return undefined;
}

let cachedShell: string | undefined;

/**
 * PowerShell executable used to run the helper scripts.
 * Order: WINMCP_POWERSHELL env -> pwsh (PowerShell 7) on PATH -> Windows PowerShell 5.1.
 * The helper scripts are compatible with both 5.1 and 7+.
 */
export function resolvePowerShellExecutable(): string {
  const fromEnv = process.env.WINMCP_POWERSHELL;
  if (fromEnv && fromEnv.trim().length > 0) return fromEnv.trim();
  if (cachedShell) return cachedShell;

  if (process.platform === "win32") {
    const pwsh = findOnPath("pwsh.exe");
    if (pwsh) {
      cachedShell = pwsh;
      return pwsh;
    }
    const systemRoot = process.env.SystemRoot ?? process.env.windir ?? "C:\\Windows";
    const winps = join(systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    cachedShell = existsSync(winps) ? winps : "powershell.exe";
    return cachedShell;
  }

  cachedShell = findOnPath("pwsh") ?? "pwsh";
  return cachedShell;
}

/** Common argument prefix for running a local helper script non-interactively. */
export function powerShellFileArgs(scriptPath: string): string[] {
  const args = ["-NoLogo", "-NoProfile", "-NonInteractive"];
  if (process.platform === "win32") {
    // Process-scope only; needed when the machine policy is Restricted (Windows client default).
    args.push("-ExecutionPolicy", "Bypass");
  }
  args.push("-File", scriptPath);
  return args;
}

/**
 * JSON-encode a payload as pure ASCII (non-ASCII escaped as \uXXXX) so it survives
 * any console code page when piped to PowerShell via stdin.
 */
export function toAsciiJson(payload: unknown): string {
  return JSON.stringify(payload).replace(
    /[\u007f-\uffff]/g,
    (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}
