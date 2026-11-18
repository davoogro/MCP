import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import type { ResolvedCredential, ServerEntry } from "../types.js";
import { getPackageRoot, powerShellFileArgs, resolvePowerShellExecutable } from "../paths.js";

function getCredentialScript(): string {
  return join(getPackageRoot(), "scripts", "lib", "Get-McpCredential.ps1");
}

export interface CredentialResolution {
  credential: ResolvedCredential;
  secretsForRedaction: string[];
}

function resolveFromEnvironment(server: ServerEntry): CredentialResolution | null {
  if (!server.usernameEnv || !server.passwordEnv) {
    return null;
  }

  const username = process.env[server.usernameEnv];
  const password = process.env[server.passwordEnv];
  if (!username || !password) {
    throw new Error(
      `Missing credential environment variables: ${server.usernameEnv} and/or ${server.passwordEnv}`,
    );
  }

  return {
    credential: {
      username,
      password,
      source: "environment",
      targetRef: `${server.usernameEnv}/${server.passwordEnv}`,
    },
    secretsForRedaction: [password],
  };
}

async function resolveFromPowerShell(server: ServerEntry): Promise<CredentialResolution | null> {
  if (!server.credentialTargetEnv && !server.secretVault) {
    return null;
  }

  const credentialScript = getCredentialScript();
  if (!existsSync(credentialScript)) {
    throw new Error(`Credential helper script not found: ${credentialScript}`);
  }

  const payload = JSON.stringify({
    credentialTarget: server.credentialTargetEnv ? process.env[server.credentialTargetEnv] : undefined,
    secretVault: server.secretVault ?? process.env.WINMCP_SECRET_VAULT,
    usernameSecret: server.usernameSecret,
    passwordSecret: server.passwordSecret,
    usernameEnv: server.usernameEnv,
    passwordEnv: server.passwordEnv,
  });

  return new Promise((resolvePromise, reject) => {
    // The payload only carries references (env var names, vault/secret names), never secret values.
    const child = spawn(
      resolvePowerShellExecutable(),
      [...powerShellFileArgs(credentialScript), "-PayloadJson", payload],
      { windowsHide: true },
    );

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Credential helper failed with exit code ${code}`));
        return;
      }
      try {
        const parsed = JSON.parse(stdout) as {
          username: string;
          password: string;
          source: ResolvedCredential["source"];
          targetRef: string;
        };
        resolvePromise({
          credential: parsed,
          secretsForRedaction: parsed.password ? [parsed.password] : [],
        });
      } catch (error) {
        reject(error);
      }
    });
  });
}

export async function resolveCredentials(server: ServerEntry): Promise<CredentialResolution> {
  try {
    const fromPs = await resolveFromPowerShell(server);
    if (fromPs) return fromPs;
  } catch {
    // Fall through to environment resolution when PS helper unavailable in tests.
  }

  const fromEnv = resolveFromEnvironment(server);
  if (fromEnv) return fromEnv;

  throw new Error(
    `No credential references configured for server '${server.name}'. Set credentialTargetEnv or usernameEnv/passwordEnv.`,
  );
}

export function getOptionalPathFromEnv(envName?: string): string | undefined {
  if (!envName) return undefined;
  const value = process.env[envName];
  return value && value.length > 0 ? value : undefined;
}

export function getProjectRoot(): string {
  return getPackageRoot();
}

export function getRemoteOperationScript(): string {
  return join(getProjectRoot(), "scripts", "lib", "Invoke-RemoteOperation.ps1");
}
