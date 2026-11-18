import { describe, expect, it } from "vitest";
import { escapeCsvCell, formatCsvRow } from "../src/csv-export.js";

describe("csv export", () => {
  it("escapes commas, quotes and newlines", () => {
    expect(escapeCsvCell('a"b')).toBe('"a""b"');
    expect(escapeCsvCell("line1\nline2")).toBe('"line1\nline2"');
    expect(formatCsvRow(["a", "b"], { a: "x,y", b: 1 })).toBe('"x,y",1');
  });
});
