const PLAN_KEYS = [
  "Node Type",
  "Relation Name",
  "Schema",
  "Alias",
  "Startup Cost",
  "Total Cost",
  "Plan Rows",
  "Plan Width",
  "Actual Startup Time",
  "Actual Total Time",
  "Actual Rows",
  "Actual Loops",
  "Index Name",
  "Join Type",
  "Filter",
  "Index Cond",
  "Recheck Cond",
  "Sort Key",
  "Group Key",
  "Hash Cond",
  "Merge Cond",
  "Strategy",
  "Partial Mode",
] as const;

function pickPlanFields(node: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of PLAN_KEYS) {
    if (node[key] !== undefined) {
      out[key] = node[key];
    }
  }
  const plans = node.Plans;
  if (Array.isArray(plans) && plans.length > 0) {
    out.Plans = plans.map((child) =>
      typeof child === "object" && child !== null ? pickPlanFields(child as Record<string, unknown>) : child,
    );
  }
  return out;
}

export function summarizePostgresExplain(raw: unknown): unknown {
  if (raw === null || raw === undefined) {
    return raw;
  }
  let parsed: unknown = raw;
  if (typeof raw === "string") {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return { raw: raw.slice(0, 8000) };
    }
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    return parsed;
  }
  const first = parsed[0] as Record<string, unknown>;
  const planRoot = first.Plan ?? first;
  if (typeof planRoot !== "object" || planRoot === null) {
    return { plan: planRoot };
  }
  const summary: Record<string, unknown> = {
    Plan: pickPlanFields(planRoot as Record<string, unknown>),
  };
  if (first["Planning Time"] !== undefined) {
    summary["Planning Time"] = first["Planning Time"];
  }
  if (first["Execution Time"] !== undefined) {
    summary["Execution Time"] = first["Execution Time"];
  }
  if (first["Triggers"] !== undefined) {
    summary.Triggers = first.Triggers;
  }
  return summary;
}
