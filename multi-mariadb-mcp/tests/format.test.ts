import { describe, expect, it } from "vitest";
import { formatRows, resolveMaxCellChars } from "../src/result-format.js";
import { buildServerInstructions } from "../src/instructions.js";
import type { AppConfig, ConnectionConfig } from "../src/types.js";

describe("formatRows", () => {
  it("truncates long text and counts truncated cells", () => {
    const { rows, truncatedCells } = formatRows([{ id: 1, body: "x".repeat(50) }], 20);
    expect(truncatedCells).toBe(1);
    expect(String(rows[0].body)).toMatch(/^x{20}\.\.\. \[truncated, 50 chars total\]$/);
    expect(rows[0].id).toBe(1);
  });

  it("serializes binary, bigint, dates and JSON safely", () => {
    const { rows } = formatRows(
      [
        {
          small: Buffer.from([1, 2]),
          big: Buffer.alloc(100),
          n: BigInt("9007199254740993"),
          d: new Date("2024-01-02T03:04:05.000Z"),
          j: { a: [1, 2] },
          nothing: null,
        },
      ],
      1000,
    );
    expect(rows[0].small).toBe("0x0102");
    expect(String(rows[0].big)).toContain("[binary, 100 bytes]");
    expect(rows[0].n).toBe("9007199254740993");
    expect(rows[0].d).toBe("2024-01-02T03:04:05.000Z");
    expect(rows[0].j).toEqual({ a: [1, 2] });
    expect(rows[0].nothing).toBeNull();
  });

  it("clamps maxCellChars", () => {
    expect(resolveMaxCellChars(undefined)).toBe(1000);
    expect(resolveMaxCellChars(1)).toBe(16);
    expect(resolveMaxCellChars(10 ** 9)).toBe(20000);
  });
});

describe("buildServerInstructions", () => {
  it("lists aliases and the recommended flow", () => {
    const connection: ConnectionConfig = {
      alias: "main",
      description: "Main DB",
      host: "db.internal",
      port: 3306,
      database: "app",
      userEnv: "U",
      passwordEnv: "P",
      sslmode: "disable",
      statementTimeoutMs: 1000,
      connectionTimeoutMs: 1000,
      maxRows: 100,
      readonly: true,
    };
    const config: AppConfig = {
      defaults: { ...connection },
      connections: new Map([["main", connection]]),
      configPath: "x",
    };

    const text = buildServerInstructions(config);
    expect(text).toContain("- main: db.internal:3306/app (read-only, maxRows 100) - Main DB");
    expect(text).toContain("list_connections -> list_databases");
    expect(text).toContain("list_tables -> describe_table -> sample_data / execute_query");
    expect(text).toContain("explain_query");
    expect(text).toContain("export_query");
    expect(text).toContain("LIMIT");
    expect(text).not.toMatch(/\bU\b.*\bP\b/);
  });
});
