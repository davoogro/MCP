import { describe, expect, it, vi } from "vitest";
import {
  closeAllCursors,
  fetchMorePage,
  registerOpenCursor,
  _openCursorCount,
} from "../src/query-cursors.js";
import type { PoolClient } from "pg";

function mockClient(fetchBatches: Record<string, unknown>[][]): PoolClient {
  let fetchIndex = 0;
  return {
    query: vi.fn(async (sql: string) => {
      if (sql.startsWith("FETCH")) {
        const batch = fetchBatches[fetchIndex] ?? [];
        fetchIndex += 1;
        return {
          rows: batch,
          fields: batch.length > 0 ? Object.keys(batch[0]!).map((name) => ({ name })) : [{ name: "id" }],
        };
      }
      return { rows: [], fields: [] };
    }),
    release: vi.fn(),
  } as unknown as PoolClient;
}

describe("query cursors", () => {
  it("registers and pages until exhausted", async () => {
    await closeAllCursors();
    const client = mockClient([[{ id: 3 }], []]);
    const registered = registerOpenCursor({
      connection: "test",
      client,
      cursorName: "mcp_test",
      columns: ["id"],
      maxRows: 2,
      maxCellChars: 500,
      nextOffset: 2,
      configuredIdleInTxMs: 60_000,
    });

    expect(_openCursorCount()).toBe(1);

    const page = await fetchMorePage(registered.id);
    expect(page.rowCount).toBe(1);
    expect(page.truncated).toBe(false);
    expect(page.cursor).toBeNull();
    expect(_openCursorCount()).toBe(0);
    expect(client.release).toHaveBeenCalled();
  });

  it("returns truncated page when more rows remain", async () => {
    await closeAllCursors();
    const client = mockClient([[{ id: 1 }, { id: 2 }, { id: 3 }]]);
    const registered = registerOpenCursor({
      connection: "test",
      client,
      cursorName: "mcp_test2",
      columns: ["id"],
      maxRows: 2,
      maxCellChars: 500,
      nextOffset: 2,
      configuredIdleInTxMs: 60_000,
    });

    const page = await fetchMorePage(registered.id);
    expect(page.truncated).toBe(true);
    expect(page.cursor).toBe(registered.id);
    expect(page.rowCount).toBe(2);
    await closeAllCursors();
  });
});
