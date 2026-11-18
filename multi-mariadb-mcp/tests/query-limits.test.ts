import { describe, expect, it } from "vitest";
import { resolveTimeoutMs } from "../src/query-limits.js";

describe("resolveTimeoutMs", () => {
  it("defaults and caps at 300000", () => {
    expect(resolveTimeoutMs(undefined, 60_000)).toBe(60_000);
    expect(resolveTimeoutMs(5000, 60_000)).toBe(5000);
    expect(resolveTimeoutMs(400_000, 60_000)).toBe(300_000);
  });
});
