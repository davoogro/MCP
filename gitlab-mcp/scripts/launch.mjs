#!/usr/bin/env node
// Stdio launcher for @zereight/mcp-gitlab that does not depend on cwd/envFile.
//
// Claude Code (and other clients) only pass command/args/env, so this script:
//   1. loads ../.env relative to this file (vars already in the environment win),
//   2. resolves relative path settings (certs, token files) against the package dir,
//   3. applies NODE_EXTRA_CA_CERTS from .env (Node only reads it at startup),
//   4. imports the local upstream build/index.js in-process.
// It never writes to stdout: stdout is reserved for JSON-RPC.
//
// Usage: node C:\DVB\mcp\gitlab-mcp\scripts\launch.mjs [upstream CLI flags]

import { existsSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import tls from 'node:tls';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const envFile = process.env.GITLAB_MCP_ENV_FILE || join(root, '.env');

const fail = (msg) => {
  process.stderr.write(`[gitlab-mcp launcher] ${msg}\n`);
  process.exit(1);
};

function loadDotEnv(path) {
  const loaded = new Set();
  if (!existsSync(path)) return loaded;
  for (const rawLine of readFileSync(path, 'utf8').split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice(7).trim();
    const i = line.indexOf('=');
    if (i < 1) continue;
    const name = line.slice(0, i).trim();
    let value = line.slice(i + 1).trim();
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[name] === undefined || process.env[name] === '') {
      process.env[name] = value;
      loaded.add(name);
    }
  }
  return loaded;
}

const fromDotEnv = loadDotEnv(envFile);
if (!existsSync(envFile)) {
  process.stderr.write(`[gitlab-mcp launcher] warning: ${envFile} not found; using process env only\n`);
}

// Same default as scripts/launch.ps1
if (!process.env.GITLAB_API_URL) {
  process.env.GITLAB_API_URL = 'https://gitlab.example.com/api/v4';
}

// Relative paths in .env are relative to the gitlab-mcp folder, not to the client's cwd.
for (const key of [
  'GITLAB_CA_CERT_PATH',
  'NODE_EXTRA_CA_CERTS',
  'GITLAB_OAUTH_TOKEN_PATH',
  'GITLAB_AUTH_COOKIE_PATH',
]) {
  const v = process.env[key];
  if (v && !isAbsolute(v)) process.env[key] = resolve(root, v);
}

for (const key of ['GITLAB_CA_CERT_PATH', 'NODE_EXTRA_CA_CERTS']) {
  const v = process.env[key];
  if (v && !existsSync(v)) fail(`${key} points to a missing file: ${v}`);
}

// NODE_EXTRA_CA_CERTS is only honoured by Node at process start. If it came from .env,
// add it to the default CA store now (Node >= 22.19 / 24.5 have setDefaultCACertificates).
if (fromDotEnv.has('NODE_EXTRA_CA_CERTS') && process.env.NODE_EXTRA_CA_CERTS) {
  if (typeof tls.setDefaultCACertificates === 'function' && typeof tls.getCACertificates === 'function') {
    const extra = readFileSync(process.env.NODE_EXTRA_CA_CERTS, 'utf8');
    tls.setDefaultCACertificates([...tls.getCACertificates('default'), extra]);
  } else {
    process.stderr.write(
      '[gitlab-mcp launcher] warning: NODE_EXTRA_CA_CERTS from .env cannot be applied at runtime ' +
        'on this Node version; use GITLAB_CA_CERT_PATH instead or pass it in the client env\n',
    );
  }
}

const useOAuth = process.env.GITLAB_USE_OAUTH === 'true';
const hasPat = Boolean(process.env.GITLAB_PERSONAL_ACCESS_TOKEN?.trim());
const hasOther = Boolean(process.env.GITLAB_JOB_TOKEN || process.env.GITLAB_AUTH_COOKIE_PATH);
if (!useOAuth && !hasPat && !hasOther) {
  fail(
    `GitLab auth not configured. Set GITLAB_PERSONAL_ACCESS_TOKEN in ${envFile}, ` +
      `or GITLAB_USE_OAUTH=true and run scripts\\auth-oauth.ps1 once (see README.md).`,
  );
}

const server = join(root, 'node_modules', '@zereight', 'mcp-gitlab', 'build', 'index.js');
if (!existsSync(server)) {
  fail(`${server} not found. Run "npm install" in ${root}.`);
}

// The version is pinned in package.json; skip the npm registry lookup on every start.
process.env.GITLAB_DISABLE_VERSION_CHECK ??= 'true';

// Behave as if launched with cwd = package dir (what Cursor's "cwd" did).
process.chdir(root);
await import(pathToFileURL(server).href);
