import test from "node:test";
import assert from "node:assert/strict";
import { parseInventory } from "../lib/inventory.js";
import { assertCommandAllowed } from "../lib/policy.js";

test("includes tagged and untagged literal Host aliases", () => {
  const servers = parseInventory(`# mcp: os=linux env=production access=read_only groups=db,production\nHost db-prod\n HostName db.example\n\nHost ignored\n HostName ignored.example\n\nHost *\n ServerAliveInterval 60\n\n# mcp: os=windows env=production access=managed groups=windows\nHost win-ops\n HostName win.example\n`);
  assert.deepEqual(servers.map((server) => server.id), ["db-prod", "ignored", "win-ops"]);
  assert.equal(servers[1].tagged, false);
  assert.equal(servers[1].os, "linux");
  assert.equal(servers[1].access, "managed");
  assert.equal(servers[2].shell, "powershell");
  assert.equal(servers[0].tagged, true);
});

test("rejects a tag on wildcard Hosts", () => {
  assert.throws(() => parseInventory(`# mcp: os=linux\nHost *\n`), /literal Host alias/);
});

test("rejects invalid metadata", () => {
  assert.throws(() => parseInventory(`# mcp: os=linux access=admin\nHost db\n`), /unsupported access/);
});

test("read-only Linux policy refuses shell composition and writes", () => {
  const server = { id: "db", shell: "posix", access: "read_only" };
  assert.doesNotThrow(() => assertCommandAllowed(server, "df -h"));
  assert.throws(() => assertCommandAllowed(server, "cat /etc/passwd > /tmp/x"));
  assert.throws(() => assertCommandAllowed(server, "rm -rf /tmp/x"));
});
