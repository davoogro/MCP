import type { SqlValidationResult } from "./types.js";

// Statements that may start a query. EXPLAIN/DESCRIBE never execute the explained statement in MariaDB.
const READ_STARTERS = ["SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "TABLE", "VALUES"] as const;

// Data-modifying keywords rejected anywhere outside literals/quoted identifiers.
const WRITE_KEYWORDS = [
  "INSERT",
  "UPDATE",
  "DELETE",
  "REPLACE",
  "CREATE",
  "ALTER",
  "DROP",
  "TRUNCATE",
  "GRANT",
  "REVOKE",
  "RENAME",
] as const;

// Write keywords that are also built-in string functions: REPLACE(str, a, b) and INSERT(str, pos, len, new).
const FUNCTION_ALSO_KEYWORDS = ["REPLACE", "INSERT"] as const;

// Known non-read commands, only used to give a precise error when a statement starts with one.
const NON_READ_COMMANDS = [
  ...WRITE_KEYWORDS,
  "MERGE",
  "LOAD",
  "CALL",
  "DO",
  "HANDLER",
  "ANALYZE",
  "OPTIMIZE",
  "REPAIR",
  "CHECK",
  "CHECKSUM",
  "FLUSH",
  "RESET",
  "PURGE",
  "KILL",
  "LOCK",
  "UNLOCK",
  "PREPARE",
  "EXECUTE",
  "DEALLOCATE",
  "BEGIN",
  "COMMIT",
  "ROLLBACK",
  "SAVEPOINT",
  "RELEASE",
  "START",
  "XA",
  "SET",
  "USE",
  "CHANGE",
  "INSTALL",
  "UNINSTALL",
  "SHUTDOWN",
  "BINLOG",
] as const;

// Functions that read server files, sleep/burn CPU, take locks or advance sequences.
const BLOCKED_FUNCTIONS = [
  "load_file",
  "sleep",
  "benchmark",
  "get_lock",
  "release_lock",
  "release_all_locks",
  "master_pos_wait",
  "master_gtid_wait",
  "nextval",
  "setval",
  "sys_exec",
  "sys_eval",
] as const;

const MAX_IDENTIFIER_LENGTH = 64;

export function validateIdentifier(name: string, label: string): void {
  // Identifiers are always passed as bind parameters or quoted with quoteIdentifier, so any
  // printable name is allowed (hyphens, spaces, unicode); only control characters are rejected.
  // eslint-disable-next-line no-control-regex
  if (!name || name.length > MAX_IDENTIFIER_LENGTH || /[\u0000-\u001f]/.test(name)) {
    throw new Error(
      `Invalid ${label}: '${name}'. Identifiers must be 1-${MAX_IDENTIFIER_LENGTH} characters without control characters`,
    );
  }
}

export function quoteIdentifier(identifier: string): string {
  validateIdentifier(identifier, "identifier");
  return `\`${identifier.replace(/`/g, "``")}\``;
}

type SqlTokenKind = "word" | "quoted" | "punct" | "other";

interface SqlToken {
  kind: SqlTokenKind;
  // word: upper-cased keyword/identifier; quoted: identifier content; punct: the character.
  value: string;
}

type LexResult =
  | { tokens: SqlToken[]; backslashInString: boolean; error?: undefined }
  | { tokens?: undefined; backslashInString?: undefined; error: string };

function isIdentifierStart(char: string): boolean {
  return /[A-Za-z_$]/.test(char) || char.charCodeAt(0) >= 0x80;
}

function isIdentifierPart(char: string): boolean {
  return /[A-Za-z0-9_$]/.test(char) || char.charCodeAt(0) >= 0x80;
}

// Minimal MariaDB lexer: drops comments and literals, keeps keywords/identifiers and punctuation.
// Follows MariaDB rules that matter for safety: "--" only starts a comment when followed by
// whitespace, strings honour backslash escapes (unless the NO_BACKSLASH_ESCAPES sql_mode is assumed),
// and executable comments are refused outright.
function lexSql(sql: string, backslashEscapes = true): LexResult {
  const tokens: SqlToken[] = [];
  let backslashInString = false;
  let i = 0;

  while (i < sql.length) {
    const char = sql[i];
    const next = sql[i + 1];

    if (/\s/.test(char)) {
      i += 1;
      continue;
    }

    if (char === "#" || (char === "-" && next === "-" && (i + 2 >= sql.length || /\s/.test(sql[i + 2])))) {
      const end = sql.indexOf("\n", i);
      i = end === -1 ? sql.length : end + 1;
      continue;
    }

    if (char === "/" && next === "*") {
      // /*! ... */ and /*M! ... */ are executed by MariaDB, so they could smuggle any statement.
      if (sql[i + 2] === "!" || (sql[i + 2] === "M" && sql[i + 3] === "!")) {
        return { error: "MariaDB executable comments (/*! ... */) are not allowed" };
      }
      const end = sql.indexOf("*/", i + 2);
      if (end === -1) {
        return { error: "Unterminated block comment" };
      }
      i = end + 2;
      continue;
    }

    if (char === "'" || char === '"' || char === "`") {
      const quote = char;
      let j = i + 1;
      let content = "";
      let closed = false;
      while (j < sql.length) {
        if (quote !== "`" && sql[j] === "\\") {
          backslashInString = true;
          if (backslashEscapes) {
            j += 2;
            continue;
          }
        }
        if (sql[j] === quote) {
          if (sql[j + 1] === quote) {
            content += quote;
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
        return { error: quote === "`" ? "Unterminated quoted identifier" : "Unterminated string literal" };
      }
      tokens.push(quote === "`" ? { kind: "quoted", value: content } : { kind: "other", value: "'" });
      i = j;
      continue;
    }

    if (isIdentifierStart(char)) {
      let j = i + 1;
      while (j < sql.length && isIdentifierPart(sql[j])) {
        j += 1;
      }
      tokens.push({ kind: "word", value: sql.slice(i, j).toUpperCase() });
      i = j;
      continue;
    }

    if (/[0-9]/.test(char)) {
      let j = i;
      while (j < sql.length && /[0-9.]/.test(sql[j])) {
        j += 1;
      }
      tokens.push({ kind: "other", value: sql.slice(i, j) });
      // Letters glued to a number (e.g. 1INTO) are lexed as a separate word on purpose.
      i = j;
      continue;
    }

    if ("();,.".includes(char)) {
      tokens.push({ kind: "punct", value: char });
      i += 1;
      continue;
    }

    tokens.push({ kind: "other", value: char });
    i += 1;
  }

  return { tokens, backslashInString };
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

function findBlockedConstruct(tokens: SqlToken[]): string | undefined {
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    const previous = tokens[i - 1];
    const next = tokens[i + 1];

    if (token.kind === "word" && !isPunct(previous, ".")) {
      const isFunctionCall =
        (FUNCTION_ALSO_KEYWORDS as readonly string[]).includes(token.value) && isPunct(next, "(");
      if ((WRITE_KEYWORDS as readonly string[]).includes(token.value) && !isFunctionCall) {
        return `Forbidden SQL operation detected: ${token.value}`;
      }

      if (token.value === "INTO") {
        return "SELECT ... INTO (OUTFILE, DUMPFILE or variables) is not allowed";
      }

      if (token.value === "LOCK" && isWord(next, "IN")) {
        return "Locking reads (LOCK IN SHARE MODE / FOR UPDATE) are not allowed";
      }

      if (token.value === "NEXT" && isWord(next, "VALUE")) {
        return "NEXT VALUE FOR advances a sequence and is not allowed";
      }
    }

    if ((token.kind === "word" || token.kind === "quoted") && isPunct(next, "(")) {
      const name = token.value.toLowerCase();
      if ((BLOCKED_FUNCTIONS as readonly string[]).includes(name)) {
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

  const result = validateTokens(lexed.tokens);
  if (!result.valid || !lexed.backslashInString) {
    return result;
  }

  // A backslash inside a string means different things with and without the NO_BACKSLASH_ESCAPES
  // sql_mode ('a\' INTO OUTFILE ... -- ' hides INTO in one mode only), so both readings must be safe.
  const alternative = lexSql(trimmed, false);
  if (alternative.error !== undefined || !validateTokens(alternative.tokens).valid) {
    return {
      valid: false,
      reason:
        "Ambiguous backslash escape in a string literal (its meaning depends on the server sql_mode); " +
        "escape quotes by doubling them ('') instead of using \\'",
    };
  }

  return result;
}

function validateTokens(lexedTokens: SqlToken[]): SqlValidationResult {
  const tokens = [...lexedTokens];
  while (tokens.length > 0 && isPunct(tokens[tokens.length - 1], ";")) {
    tokens.pop();
  }

  if (tokens.length === 0) {
    return { valid: false, reason: "SQL query cannot be empty" };
  }

  if (tokens.some((token) => isPunct(token, ";"))) {
    return { valid: false, reason: "Multiple SQL statements are not allowed" };
  }

  const first = tokens[skipOpeningParens(tokens, 0)];
  if (!isWord(first) || !(READ_STARTERS as readonly string[]).includes(first.value)) {
    if (isWord(first) && (NON_READ_COMMANDS as readonly string[]).includes(first.value)) {
      return { valid: false, reason: `Forbidden SQL operation detected: ${first.value}` };
    }
    return {
      valid: false,
      reason: "Only read queries are allowed (SELECT, WITH, EXPLAIN, SHOW, DESCRIBE, DESC, TABLE, VALUES)",
    };
  }

  const blocked = findBlockedConstruct(tokens);
  if (blocked) {
    return { valid: false, reason: blocked };
  }

  return { valid: true, statement: first.value };
}

const SECRET_PATTERNS: RegExp[] = [
  /password\s*=\s*[^\s'"]+/gi,
  /mysql:\/\/[^\s]+/gi,
  /mariadb:\/\/[^\s]+/gi,
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

  if (message.includes("timeout") || message.includes("timed out") || message.includes("etimeout")) {
    return "Connection or query timed out";
  }

  if (message.includes("econnrefused")) {
    return "Connection refused by the target host/port";
  }

  if (message.includes("access denied") || message.includes("authentication failed")) {
    return "MariaDB rejected the credentials";
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
