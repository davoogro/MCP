import type { SqlValidationResult } from "./types.js";

// Statements that may start a query. EXPLAIN is further checked so only read statements are explained.
const READ_STARTERS = ["SELECT", "WITH", "EXPLAIN", "SHOW", "TABLE", "VALUES"] as const;

// Statements EXPLAIN may wrap (EXPLAIN ANALYZE executes them, so writes must never reach it).
const EXPLAINABLE_STARTERS = ["SELECT", "WITH", "TABLE", "VALUES"] as const;

// Data-modifying keywords rejected anywhere outside literals/quoted identifiers
// (e.g. data-modifying CTEs: WITH x AS (DELETE ... RETURNING *) SELECT ...).
const WRITE_KEYWORDS = [
  "INSERT",
  "UPDATE",
  "DELETE",
  "MERGE",
  "TRUNCATE",
  "CREATE",
  "ALTER",
  "DROP",
  "GRANT",
  "REVOKE",
] as const;

// Known non-read commands, only used to give a precise error when a statement starts with one.
const NON_READ_COMMANDS = [
  ...WRITE_KEYWORDS,
  "UPSERT",
  "COPY",
  "CALL",
  "DO",
  "VACUUM",
  "ANALYZE",
  "REFRESH",
  "REINDEX",
  "CLUSTER",
  "COMMENT",
  "SECURITY",
  "LISTEN",
  "NOTIFY",
  "UNLISTEN",
  "LOAD",
  "DISCARD",
  "RESET",
  "SET",
  "PREPARE",
  "DEALLOCATE",
  "EXECUTE",
  "LOCK",
  "MOVE",
  "FETCH",
  "CLOSE",
  "DECLARE",
  "BEGIN",
  "COMMIT",
  "ROLLBACK",
  "SAVEPOINT",
  "RELEASE",
  "START",
  "ABORT",
  "END",
  "CHECKPOINT",
  "REASSIGN",
  "IMPORT",
  "EXPORT",
] as const;

// Functions with side effects, server file access, session mutation or sleep-based abuse.
const BLOCKED_FUNCTION_PATTERNS: RegExp[] = [
  /^pg_sleep(_for|_until)?$/,
  /^pg_(terminate|cancel)_backend$/,
  /^pg_reload_conf$/,
  /^pg_rotate_logfile/,
  /^pg_promote$/,
  /^pg_switch_(wal|xlog)$/,
  /^pg_create_restore_point$/,
  /^pg_(start|stop)_backup$/,
  /^pg_backup_(start|stop)$/,
  /^pg_read_(binary_)?file$/,
  /^pg_ls_/,
  /^pg_stat_file$/,
  /^pg_file_/,
  /^lo_/,
  /^set_config$/,
  /^(nextval|setval)$/,
  /^pg_(try_)?advisory_/,
  /^pg_notify$/,
  /^dblink/,
  /^(query|cursor)_to_xml/,
  /^query_to_xmlschema$/,
  /^pg_logical_/,
  /^pg_(create|drop)_.*replication_slot$/,
  /^pg_replication_/,
  /^pg_stat_reset/,
  /^pg_stat_statements_reset$/,
  /^pg_log_backend_memory_contexts$/,
];

const MAX_IDENTIFIER_LENGTH = 63;

export function validateIdentifier(name: string, label: string): void {
  // Identifiers are always passed as bind parameters or quoted with quoteIdentifier, so any
  // printable name is allowed (mixed case, hyphens, spaces); only control characters are rejected.
  // eslint-disable-next-line no-control-regex
  if (!name || name.length > MAX_IDENTIFIER_LENGTH || /[\u0000-\u001f]/.test(name)) {
    throw new Error(
      `Invalid ${label}: '${name}'. Identifiers must be 1-${MAX_IDENTIFIER_LENGTH} characters without control characters`,
    );
  }
}

export function quoteIdentifier(identifier: string): string {
  validateIdentifier(identifier, "identifier");
  return `"${identifier.replace(/"/g, '""')}"`;
}

type SqlTokenKind = "word" | "quoted" | "punct" | "other";

interface SqlToken {
  kind: SqlTokenKind;
  // word: upper-cased keyword/identifier; quoted: identifier content; punct: the character.
  value: string;
}

type LexResult = { tokens: SqlToken[]; error?: undefined } | { tokens?: undefined; error: string };

function isIdentifierStart(char: string): boolean {
  return /[A-Za-z_]/.test(char) || char.charCodeAt(0) >= 0x80;
}

function isIdentifierPart(char: string): boolean {
  return /[A-Za-z0-9_$]/.test(char) || char.charCodeAt(0) >= 0x80;
}

// Minimal PostgreSQL lexer: drops comments and literals, keeps keywords/identifiers and punctuation.
// Handles nested block comments, E'' backslash escapes and dollar-quoted strings so text inside
// literals neither hides statements nor triggers false positives.
function lexSql(sql: string): LexResult {
  const tokens: SqlToken[] = [];
  let i = 0;

  const skipString = (start: number, backslashEscapes: boolean): number => {
    let j = start + 1;
    while (j < sql.length) {
      const char = sql[j];
      if (backslashEscapes && char === "\\") {
        j += 2;
        continue;
      }
      if (char === "'") {
        if (sql[j + 1] === "'") {
          j += 2;
          continue;
        }
        return j + 1;
      }
      j += 1;
    }
    return -1;
  };

  while (i < sql.length) {
    const char = sql[i];
    const next = sql[i + 1];

    if (/\s/.test(char)) {
      i += 1;
      continue;
    }

    if (char === "-" && next === "-") {
      const end = sql.indexOf("\n", i);
      i = end === -1 ? sql.length : end + 1;
      continue;
    }

    if (char === "/" && next === "*") {
      let depth = 1;
      let j = i + 2;
      while (j < sql.length && depth > 0) {
        if (sql[j] === "/" && sql[j + 1] === "*") {
          depth += 1;
          j += 2;
        } else if (sql[j] === "*" && sql[j + 1] === "/") {
          depth -= 1;
          j += 2;
        } else {
          j += 1;
        }
      }
      if (depth > 0) {
        return { error: "Unterminated block comment" };
      }
      i = j;
      continue;
    }

    if (char === "'") {
      const end = skipString(i, false);
      if (end === -1) {
        return { error: "Unterminated string literal" };
      }
      tokens.push({ kind: "other", value: "'" });
      i = end;
      continue;
    }

    if (char === '"') {
      let j = i + 1;
      let content = "";
      let closed = false;
      while (j < sql.length) {
        if (sql[j] === '"') {
          if (sql[j + 1] === '"') {
            content += '"';
            j += 2;
            continue;
          }
          closed = true;
          j += 1;
          break;
        }
        content += sql[j];
        j += 1;
      }
      if (!closed) {
        return { error: "Unterminated quoted identifier" };
      }
      tokens.push({ kind: "quoted", value: content });
      i = j;
      continue;
    }

    if (char === "$") {
      const match = sql.slice(i).match(/^\$([A-Za-z_][A-Za-z0-9_]*)?\$/);
      if (match) {
        const tag = match[0];
        const end = sql.indexOf(tag, i + tag.length);
        if (end === -1) {
          return { error: "Unterminated dollar-quoted string" };
        }
        tokens.push({ kind: "other", value: "$" });
        i = end + tag.length;
        continue;
      }
      // Positional parameter ($1) or operator.
      i += 1;
      while (i < sql.length && /[0-9]/.test(sql[i])) {
        i += 1;
      }
      tokens.push({ kind: "other", value: "$" });
      continue;
    }

    if (isIdentifierStart(char)) {
      let j = i + 1;
      while (j < sql.length && isIdentifierPart(sql[j])) {
        j += 1;
      }
      const word = sql.slice(i, j);
      // E'...' strings honour backslash escapes, so \' does not terminate them.
      if (word.toUpperCase() === "E" && sql[j] === "'") {
        const end = skipString(j, true);
        if (end === -1) {
          return { error: "Unterminated string literal" };
        }
        tokens.push({ kind: "other", value: "'" });
        i = end;
        continue;
      }
      tokens.push({ kind: "word", value: word.toUpperCase() });
      i = j;
      continue;
    }

    if (/[0-9]/.test(char)) {
      let j = i;
      while (j < sql.length && /[0-9._]/.test(sql[j])) {
        j += 1;
      }
      tokens.push({ kind: "other", value: sql.slice(i, j) });
      // Letters glued to a number (e.g. 1INTO) are lexed as a separate word on purpose.
      i = j;
      continue;
    }

    if ("();,.[]".includes(char)) {
      tokens.push({ kind: "punct", value: char });
      i += 1;
      continue;
    }

    tokens.push({ kind: "other", value: char });
    i += 1;
  }

  return { tokens };
}

function isWord(token: SqlToken | undefined, ...values: string[]): boolean {
  return token?.kind === "word" && (values.length === 0 || values.includes(token.value));
}

function isPunct(token: SqlToken | undefined, value: string): boolean {
  return token?.kind === "punct" && token.value === value;
}

// Index of the first token after leading "(" so "(SELECT 1) UNION (SELECT 2)" is accepted.
function skipOpeningParens(tokens: SqlToken[], start: number): number {
  let index = start;
  while (isPunct(tokens[index], "(")) {
    index += 1;
  }
  return index;
}

// Returns the index of the statement wrapped by EXPLAIN [ANALYZE] [VERBOSE] | EXPLAIN (options).
function explainedStatementIndex(tokens: SqlToken[], explainIndex: number): number {
  let index = explainIndex + 1;
  if (isPunct(tokens[index], "(")) {
    let depth = 0;
    for (; index < tokens.length; index += 1) {
      if (isPunct(tokens[index], "(")) {
        depth += 1;
      } else if (isPunct(tokens[index], ")")) {
        depth -= 1;
        if (depth === 0) {
          index += 1;
          break;
        }
      }
    }
  } else {
    while (isWord(tokens[index], "ANALYZE", "ANALYSE", "VERBOSE")) {
      index += 1;
    }
  }
  return skipOpeningParens(tokens, index);
}

function findBlockedConstruct(tokens: SqlToken[]): string | undefined {
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    const previous = tokens[i - 1];
    const next = tokens[i + 1];

    if (token.kind === "word" && !isPunct(previous, ".")) {
      if ((WRITE_KEYWORDS as readonly string[]).includes(token.value)) {
        return `Forbidden SQL operation detected: ${token.value}`;
      }

      if (token.value === "INTO") {
        return "SELECT ... INTO is not allowed (it creates tables or writes data)";
      }

      if (token.value === "FOR" && isWord(next, "UPDATE", "SHARE", "NO", "KEY")) {
        return "Row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed";
      }
    }

    if ((token.kind === "word" || token.kind === "quoted") && isPunct(next, "(")) {
      const name = token.value.toLowerCase();
      if (BLOCKED_FUNCTION_PATTERNS.some((pattern) => pattern.test(name))) {
        return `Function not allowed in read-only mode: ${name}`;
      }
    }
  }

  return undefined;
}

export function validateReadOnlySql(sql: string): SqlValidationResult {
  const trimmed = sql.trim();
  if (!trimmed) {
    return { valid: false, reason: "SQL query cannot be empty" };
  }

  const lexed = lexSql(trimmed);
  if (lexed.error !== undefined) {
    return { valid: false, reason: lexed.error };
  }

  const tokens = [...lexed.tokens];
  while (tokens.length > 0 && isPunct(tokens[tokens.length - 1], ";")) {
    tokens.pop();
  }

  if (tokens.length === 0) {
    return { valid: false, reason: "SQL query cannot be empty" };
  }

  if (tokens.some((token) => isPunct(token, ";"))) {
    return { valid: false, reason: "Multiple SQL statements are not allowed" };
  }

  const firstIndex = skipOpeningParens(tokens, 0);
  const first = tokens[firstIndex];
  if (!isWord(first) || !(READ_STARTERS as readonly string[]).includes(first.value)) {
    if (isWord(first) && (NON_READ_COMMANDS as readonly string[]).includes(first.value)) {
      return { valid: false, reason: `Forbidden SQL operation detected: ${first.value}` };
    }
    return {
      valid: false,
      reason: "Only read queries are allowed (SELECT, WITH, EXPLAIN, SHOW, TABLE, VALUES)",
    };
  }

  if (first.value === "EXPLAIN") {
    const explained = tokens[explainedStatementIndex(tokens, firstIndex)];
    if (!isWord(explained) || !(EXPLAINABLE_STARTERS as readonly string[]).includes(explained.value)) {
      return {
        valid: false,
        reason: "EXPLAIN is only allowed for read queries (SELECT, WITH, TABLE, VALUES)",
      };
    }
  }

  const blocked = findBlockedConstruct(tokens);
  if (blocked) {
    return { valid: false, reason: blocked };
  }

  return { valid: true, statement: first.value };
}

const SECRET_PATTERNS: RegExp[] = [
  /password\s*=\s*[^\s'"]+/gi,
  /postgresql:\/\/[^\s]+/gi,
  /postgres:\/\/[^\s]+/gi,
  /(Bearer\s+)[^\s]+/gi,
];

export function sanitizeErrorMessage(error: unknown, secrets: string[] = []): string {
  let message = error instanceof Error ? error.message : String(error);

  for (const secret of secrets) {
    if (secret && secret.length > 0) {
      message = message.split(secret).join("[REDACTED]");
    }
  }

  for (const pattern of SECRET_PATTERNS) {
    message = message.replace(pattern, "[REDACTED]");
  }

  return message;
}

export function classifyConnectionError(error: unknown): string {
  const message = sanitizeErrorMessage(error).toLowerCase();

  if (message.includes("enotfound") || message.includes("getaddrinfo")) {
    return "DNS resolution failed for the configured host";
  }

  if (message.includes("timeout") || message.includes("timed out")) {
    return "Connection or query timed out";
  }

  if (message.includes("econnrefused")) {
    return "Connection refused by the target host/port";
  }

  if (message.includes("password authentication failed") || message.includes("authentication failed")) {
    return "PostgreSQL rejected the credentials";
  }

  if (message.includes("does not support ssl")) {
    return "The server does not support SSL; set sslmode: disable (or prefer) for this alias";
  }

  if (message.includes("ssl") || message.includes("certificate")) {
    return `SSL/TLS negotiation failed: ${sanitizeErrorMessage(error)}`;
  }

  return sanitizeErrorMessage(error);
}

export function jsonText(payload: unknown): { content: Array<{ type: "text"; text: string }> } {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(payload, null, 2),
      },
    ],
  };
}

export function jsonError(message: string): { content: Array<{ type: "text"; text: string }>; isError: true } {
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify({ error: message }, null, 2),
      },
    ],
    isError: true,
  };
}
