import { describe, expect, it } from "vitest";
import { summarizePostgresExplain } from "../src/explain-summary.js";

describe("summarizePostgresExplain", () => {
  it("extracts plan node fields and nested plans", () => {
    const raw = [
      {
        Plan: {
          "Node Type": "Seq Scan",
          "Relation Name": "users",
          "Total Cost": 10,
          Plans: [{ "Node Type": "Index Scan", "Index Name": "users_pkey" }],
        },
        "Planning Time": 0.1,
        "Execution Time": 1.2,
      },
    ];
    const summary = summarizePostgresExplain(raw) as Record<string, unknown>;
    const plan = summary.Plan as Record<string, unknown>;
    expect(plan["Node Type"]).toBe("Seq Scan");
    expect(summary["Execution Time"]).toBe(1.2);
    expect(Array.isArray(plan.Plans)).toBe(true);
  });
});
