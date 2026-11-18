import { hostname } from "node:os";
import type { ServerEntry } from "../types.js";

const LOCALHOST_ALIASES = new Set(["localhost", "127.0.0.1", "::1"]);

export function getLocalIdentity(): { hostname: string; fqdn: string } {
  return {
    hostname: (process.env.WINMCP_LOCAL_HOSTNAME ?? hostname()).toUpperCase(),
    fqdn: (process.env.WINMCP_LOCAL_FQDN ?? hostname()).toUpperCase(),
  };
}

export function isLocalHostTarget(server: ServerEntry): boolean {
  const local = getLocalIdentity();
  const address = server.address.trim().toLowerCase();
  const name = server.name.trim().toUpperCase();

  if (LOCALHOST_ALIASES.has(address)) return true;
  if (address === local.hostname.toLowerCase()) return true;
  if (address === local.fqdn.toLowerCase()) return true;
  if (name === local.hostname) return true;
  if (name === local.fqdn) return true;
  return false;
}

export function assertNotLocalHost(server: ServerEntry, operation: string): void {
  if (isLocalHostTarget(server)) {
    throw new Error(`${operation} blocked: target '${server.name}' resolves to the local host running this MCP server`);
  }
}
