export const MAX_ROWS_CAP = 10_000;
export const MAX_TIMEOUT_MS = 300_000;
export const EXPORT_MAX_ROWS = 50_000_000;

export function resolveMaxRows(requestedMaxRows: number | undefined, configuredDefault: number): number {
  const base = requestedMaxRows ?? configuredDefault;
  return Math.min(Math.max(1, base), MAX_ROWS_CAP);
}

export function resolveTimeoutMs(requestedTimeoutMs: number | undefined, configuredDefault: number): number {
  const base = requestedTimeoutMs ?? configuredDefault;
  return Math.min(Math.max(1, base), MAX_TIMEOUT_MS);
}
