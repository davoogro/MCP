import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
for (const line of readFileSync(join(root, '.env'), 'utf8').split(/\r?\n/)) {
  const t = line.trim();
  if (!t || t.startsWith('#')) continue;
  const i = t.indexOf('=');
  if (i < 1) continue;
  process.env[t.slice(0, i).trim()] = t.slice(i + 1).trim();
}

const server = join(root, 'node_modules/@zereight/mcp-gitlab/build/index.js');
const child = spawn('C:\\Program Files\\nodejs\\node.exe', [server], {
  cwd: root,
  env: process.env,
  stdio: ['pipe', 'pipe', 'pipe'],
});

let out = '';
let err = '';
child.stdout.on('data', (d) => { out += d.toString(); });
child.stderr.on('data', (d) => { err += d.toString(); });

const send = (obj) => child.stdin.write(JSON.stringify(obj) + '\n');
send({
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'gitlab-mcp-smoke', version: '0.0.1' },
  },
});
send({ jsonrpc: '2.0', method: 'notifications/initialized' });
send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });

setTimeout(() => {
  child.kill();
  const errSafe = err.replace(/glpat-[^\s"']+/gi, 'glpat-[redacted]');
  console.log('--- STDERR ---');
  console.log(errSafe.slice(0, 2000));
  const lines = out.split('\n').filter(Boolean);
  console.log(`--- STDOUT lines=${lines.length} ---`);
  for (const line of lines.slice(0, 4)) {
    try {
      const msg = JSON.parse(line);
      if (msg.id === 1 && msg.result) {
        console.log('INIT_OK', msg.result.serverInfo?.name || 'ok');
      } else if (msg.id === 2 && msg.result?.tools) {
        console.log('TOOLS_OK count=' + msg.result.tools.length);
        console.log('sample=' + msg.result.tools.slice(0, 8).map((t) => t.name).join(', '));
      } else if (msg.error) {
        console.log('RPC_ERROR', JSON.stringify(msg.error).slice(0, 400));
      }
    } catch {
      console.log('NON_JSON', line.slice(0, 120));
    }
  }
  process.exit(lines.length ? 0 : 1);
}, 8000);
