import { describe, expect, it } from "vitest";
import { resolveMaxRows } from "../src/query-limits.js";
import { buildSampleQuery } from "../src/tools/sample-data.js";
import { validateReadOnlySql, sanitizeErrorMessage, quoteIdentifier } from "../src/security.js";

describe("validateReadOnlySql", () => {
  it("accepts SELECT", () => {
    expect(validateReadOnlySql("SELECT 1").valid).toBe(true);
  });

  it("accepts SHOW", () => {
    expect(validateReadOnlySql("SHOW DATABASES").valid).toBe(true);
  });

  it("rejects INSERT", () => {
    expect(validateReadOnlySql("INSERT INTO users(id) VALUES (1)").valid).toBe(false);
  });

  it("rejects multiple statements", () => {
    expect(validateReadOnlySql("SELECT 1; SELECT 2;").valid).toBe(false);
  });
});

describe("validateReadOnlySql false positives", () => {
  const accepted = [
    "SELECT REPLACE(name, 'a', 'b') FROM users",
    "SELECT INSERT('Quadratic', 3, 4, 'What')",
    "SELECT CAST(name AS CHAR CHARACTER SET utf8mb4) FROM users",
    "SELECT * FROM users USE INDEX (idx_name) WHERE name = 'x'",
    "SELECT * FROM logs WHERE message = 'delete; drop table users; insert'",
    "SELECT * FROM logs WHERE message = 'it''s; DELETE'",
    "SELECT 'C:\\temp\\file.txt' AS path",
    "SELECT `update`, `delete`, `check`, `start` FROM `weird-table`",
    "SELECT t.start, t.end, t.check, t.comment FROM tickets t",
    "SELECT CASE WHEN a THEN 1 ELSE 0 END FROM t",
    "SELECT 1--1",
    "SELECT 1 -- trailing comment",
    "SELECT 1 # hash comment",
    "SELECT 1;",
    "(SELECT 1) UNION (SELECT 2)",
    "SHOW DATABASES",
    "SHOW TABLES FROM planning",
    "DESCRIBE users",
    "DESC users",
    "EXPLAIN SELECT * FROM users",
    "EXPLAIN FORMAT=JSON SELECT 1",
    "VALUES (1), (2)",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT updated_at, deleted_flag FROM audit",
    "SELECT * FROM planning.sites LIMIT 10 OFFSET 20",
    "SELECT /* plain comment */ 1",
  ];

  for (const sql of accepted) {
    it(`accepts: ${sql}`, () => {
      const result = validateReadOnlySql(sql);
      expect(result.reason).toBeUndefined();
      expect(result.valid).toBe(true);
    });
  }

  it("reports the leading statement keyword", () => {
    expect(validateReadOnlySql("show tables").statement).toBe("SHOW");
    expect(validateReadOnlySql("desc t").statement).toBe("DESC");
  });
});

describe("validateReadOnlySql bypass attempts", () => {
  const rejected = [
    "SELECT * FROM users INTO OUTFILE '/tmp/users.csv'",
    "SELECT * FROM users INTO DUMPFILE '/tmp/users.bin'",
    "SELECT name INTO @v FROM users LIMIT 1",
    "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
    "SELECT 1 /*M! INTO OUTFILE '/tmp/x' */",
    "SELECT 1 /*!50000 UNION SELECT LOAD_FILE('/etc/passwd') */",
    "SELECT 1 --(1) INTO OUTFILE '/tmp/f'",
    "SELECT 'a\\' ; DELETE FROM users; -- '",
    "SELECT 'a\\' INTO OUTFILE '/tmp/x' -- '",
    "SELECT * FROM logs WHERE message = 'it\\'s'",
    "SELECT * FROM users FOR UPDATE",
    "SELECT * FROM users LOCK IN SHARE MODE",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(100000000, MD5('x'))",
    "SELECT GET_LOCK('x', 10)",
    "SELECT NEXTVAL(seq)",
    "SELECT NEXT VALUE FOR seq",
    "SELECT 1; DELETE FROM users",
    "SELECT 1; SELECT 2;",
    "REPLACE INTO users VALUES (1)",
    "INSERT INTO users VALUES (1)",
    "UPDATE users SET a = 1",
    "DELETE FROM users",
    "DROP TABLE users",
    "TRUNCATE users",
    "CREATE TEMPORARY TABLE t (id INT)",
    "SET SESSION sql_mode = ''",
    "SET STATEMENT max_statement_time = 0 FOR SELECT 1",
    "USE mysql",
    "LOAD DATA INFILE '/tmp/x' INTO TABLE t",
    "DO SLEEP(5)",
    "CALL proc()",
    "HANDLER t OPEN",
    "LOCK TABLES t READ",
    "KILL 123",
    "ANALYZE SELECT * FROM t",
    "EXPLAIN DELETE FROM users",
    "SELECT 1 /* unterminated",
    "SELECT 'unterminated",
    "# only a comment",
  ];

  for (const sql of rejected) {
    it(`rejects: ${sql}`, () => {
      expect(validateReadOnlySql(sql).valid).toBe(false);
    });
  }
});

describe("quoteIdentifier", () => {
  it("quotes and escapes identifiers", () => {
    expect(quoteIdentifier("weird`name")).toBe("`weird``name`");
    expect(quoteIdentifier("my-table")).toBe("`my-table`");
  });

  it("rejects control characters and empty names", () => {
    expect(() => quoteIdentifier("bad\u0000name")).toThrow();
    expect(() => quoteIdentifier("")).toThrow();
  });

  it("builds a safely quoted sample query", () => {
    expect(buildSampleQuery("planning", "my table", 5, ["id", "a`b"])).toBe(
      "SELECT `id`, `a``b` FROM `planning`.`my table` LIMIT 5",
    );
    expect(validateReadOnlySql(buildSampleQuery("db", "delete", 5)).valid).toBe(true);
  });
});

describe("resolveMaxRows", () => {
  it("defaults to configured value and caps at 10000", () => {
    expect(resolveMaxRows(undefined, 2000)).toBe(2000);
    expect(resolveMaxRows(100, 2000)).toBe(100);
    expect(resolveMaxRows(8000, 500)).toBe(8000);
    expect(resolveMaxRows(20000, 500)).toBe(10000);
  });
});

describe("sanitizeErrorMessage", () => {
  it("redacts secrets from error messages", () => {
    const sanitized = sanitizeErrorMessage(new Error("Access denied for user root"), ["super-secret-password"]);
    expect(sanitized).not.toContain("super-secret-password");
  });
});
