import { describe, expect, it } from "vitest";
import { resolveMaxRows } from "../src/query-limits.js";
import { buildSampleQuery } from "../src/tools/sample-data.js";
import { validateReadOnlySql, sanitizeErrorMessage, quoteIdentifier } from "../src/security.js";

describe("validateReadOnlySql", () => {
  it("accepts SELECT", () => {
    expect(validateReadOnlySql("SELECT 1").valid).toBe(true);
  });

  it("accepts read-only WITH", () => {
    expect(
      validateReadOnlySql(`
        WITH active_users AS (
          SELECT id FROM users WHERE active = true
        )
        SELECT * FROM active_users
      `).valid,
    ).toBe(true);
  });

  it("rejects INSERT", () => {
    const result = validateReadOnlySql("INSERT INTO users(id) VALUES (1)");
    expect(result.valid).toBe(false);
  });

  it("rejects UPDATE", () => {
    expect(validateReadOnlySql("UPDATE users SET active = false").valid).toBe(false);
  });

  it("rejects DELETE", () => {
    expect(validateReadOnlySql("DELETE FROM users").valid).toBe(false);
  });

  it("rejects DROP", () => {
    expect(validateReadOnlySql("DROP TABLE users").valid).toBe(false);
  });

  it("rejects CREATE TEMP TABLE", () => {
    expect(
      validateReadOnlySql("CREATE TEMP TABLE mcp_should_not_create_anything(id integer)").valid,
    ).toBe(false);
  });

  it("rejects multiple statements", () => {
    expect(validateReadOnlySql("SELECT 1; SELECT 2;").valid).toBe(false);
  });

  it("rejects WITH containing write operations", () => {
    expect(
      validateReadOnlySql(`
        WITH inserted AS (
          INSERT INTO users(id) VALUES (1) RETURNING id
        )
        SELECT * FROM inserted
      `).valid,
    ).toBe(false);
  });

  it("rejects write hidden after comments", () => {
    expect(
      validateReadOnlySql(`
        -- looks innocent
        DELETE FROM users
      `).valid,
    ).toBe(false);
  });
});

describe("validateReadOnlySql false positives", () => {
  const accepted = [
    "SELECT CASE WHEN active THEN 'yes' ELSE 'no' END AS flag FROM users",
    "SELECT * FROM users ORDER BY id FETCH FIRST 10 ROWS ONLY",
    "SELECT * FROM logs WHERE message = 'delete; drop table users; insert'",
    'SELECT "update", "comment", "start", "end" FROM "Weird Table"',
    "SELECT t.comment, t.start, t.release FROM tickets t",
    "SELECT $$ it's a delete; $$ AS literal",
    "SELECT $tag$ DROP TABLE x; $tag$",
    "SELECT substring(name FROM 1 FOR 3) FROM users",
    "SELECT 1;",
    "SELECT 1; -- trailing comment",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SHOW search_path",
    "TABLE users",
    "VALUES (1), (2)",
    "EXPLAIN SELECT * FROM users",
    "EXPLAIN ANALYZE SELECT * FROM users",
    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT 1",
    "SELECT updated_at, deleted_flag, created_by FROM audit",
    "/* header */ SELECT 1 /* nested /* comment */ still comment */",
    "SELECT E'it\\'s' AS escaped",
  ];

  for (const sql of accepted) {
    it(`accepts: ${sql}`, () => {
      const result = validateReadOnlySql(sql);
      expect(result.reason).toBeUndefined();
      expect(result.valid).toBe(true);
    });
  }

  it("reports the leading statement keyword", () => {
    expect(validateReadOnlySql("show all").statement).toBe("SHOW");
    expect(validateReadOnlySql("(select 1)").statement).toBe("SELECT");
  });
});

describe("validateReadOnlySql bypass attempts", () => {
  const rejected = [
    "SELECT * INTO new_table FROM users",
    "SELECT * FROM users FOR UPDATE",
    "SELECT * FROM users FOR NO KEY UPDATE",
    "SELECT * FROM users FOR SHARE",
    "SELECT * FROM users FOR KEY SHARE",
    "EXPLAIN ANALYZE DELETE FROM users",
    "EXPLAIN (ANALYZE) UPDATE users SET a = 1",
    "EXPLAIN ANALYZE EXECUTE prepared_stmt",
    "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
    "WITH x AS (SELECT 1) UPDATE users SET a = 1",
    "COPY users TO '/tmp/users.csv'",
    "COPY (SELECT 1) TO PROGRAM 'calc'",
    "SET ROLE postgres",
    "SET default_transaction_read_only = off",
    "DO $$ BEGIN DELETE FROM users; END $$",
    "CALL do_things()",
    "VACUUM users",
    "LOCK TABLE users",
    "BEGIN",
    "SELECT $$--$$; DELETE FROM users",
    "SELECT E'\\' ', 1; DELETE FROM users; --'",
    "SELECT 1; DELETE FROM users",
    "SELECT 1 /* unterminated",
    "SELECT 'unterminated",
    "SELECT pg_sleep(10)",
    "SELECT pg_catalog.pg_sleep(10)",
    'SELECT "pg_sleep"(10)',
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_export(1234, '/tmp/x')",
    "SELECT nextval('seq')",
    "SELECT pg_advisory_lock(1)",
    "SELECT dblink_exec('dbname=x', 'DROP TABLE y')",
    "SELECT query_to_xml('DELETE FROM users', true, true, '')",
    "-- comment only",
  ];

  for (const sql of rejected) {
    it(`rejects: ${sql}`, () => {
      expect(validateReadOnlySql(sql).valid).toBe(false);
    });
  }
});

describe("quoteIdentifier", () => {
  it("quotes and escapes identifiers", () => {
    expect(quoteIdentifier("Weird \"Name\"")).toBe('"Weird ""Name"""');
    expect(quoteIdentifier("users")).toBe('"users"');
  });

  it("rejects control characters and empty names", () => {
    expect(() => quoteIdentifier("bad\u0000name")).toThrow();
    expect(() => quoteIdentifier("")).toThrow();
  });

  it("builds a safely quoted sample query", () => {
    expect(buildSampleQuery("public", "My Table", 5, ["id", "a\"b"])).toBe(
      'SELECT "id", "a""b" FROM "public"."My Table" LIMIT 5',
    );
    expect(validateReadOnlySql(buildSampleQuery("public", "delete", 5)).valid).toBe(true);
  });
});

describe("resolveMaxRows", () => {
  it("caps at MAX_ROWS_CAP but allows requests above alias default", () => {
    expect(resolveMaxRows(undefined, 2000)).toBe(2000);
    expect(resolveMaxRows(100, 2000)).toBe(100);
    expect(resolveMaxRows(1000, 500)).toBe(1000);
    expect(resolveMaxRows(50_000, 2000)).toBe(10_000);
  });
});

describe("sanitizeErrorMessage", () => {
  it("redacts passwords from error messages", () => {
    const sanitized = sanitizeErrorMessage(new Error("password authentication failed for user postgres"), [
      "super-secret-password",
    ]);
    expect(sanitized).not.toContain("super-secret-password");
  });
});
