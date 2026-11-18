import { describe, expect, it } from "vitest";
import { resolveMaxRows, resolveTimeoutMs } from "../src/query-limits.js";

describe("query limits", () => {
  it("resolveMaxRows applies alias default and global cap", () => {
    expect(resolveMaxRows(undefined, 2000)).toBe(2000);
    expect(resolveMaxRows(8000, 2000)).toBe(8000);
    expect(resolveMaxRows(20_000, 2000)).toBe(10_000);
  });

  it("resolveTimeoutMs applies alias default and cap", () => {
    expect(resolveTimeoutMs(undefined, 60_000)).toBe(60_000);
    expect(resolveTimeoutMs(120_000, 60_000)).toBe(120_000);
    expect(resolveTimeoutMs(400_000, 60_000)).toBe(300_000);
  });
});
