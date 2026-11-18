import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

const MAX_OUTPUT_BYTES = 200 * 1024;

function sshBinary() {
  if (process.platform !== "win32") return "ssh";
  const root = process.env.SystemRoot || process.env.windir || "C:\\Windows";
  const system = path.join(root, "System32", "OpenSSH", "ssh.exe");
  return existsSync(system) ? system : "ssh";
}

function childEnv() {
  if (process.platform !== "win32") return process.env;
  const env = { ...process.env };
  env.SystemRoot ||= env.windir || "C:\\Windows";
  env.windir ||= env.SystemRoot;
  env.SystemDrive ||= env.SystemRoot.slice(0, 2);
  env.ProgramData ||= `${env.SystemDrive}\\ProgramData`;
  env.ALLUSERSPROFILE ||= env.ProgramData;
  env.ComSpec ||= `${env.SystemRoot}\\System32\\cmd.exe`;
  env.PATHEXT ||= ".COM;.EXE;.BAT;.CMD";
  return env;
}

function killTree(child) {
  try { child.kill("SIGKILL"); } catch {}
  if (process.platform === "win32" && child.pid) {
    spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }).on("error", () => {});
  }
}

function limit(chunks, total) {
  const data = Buffer.concat(chunks);
  const shown = data.subarray(0, MAX_OUTPUT_BYTES).toString("utf8");
  return total > MAX_OUTPUT_BYTES ? `${shown}\n[truncated: ${total} bytes]` : shown;
}

export function runSsh({ configPath, alias, command, stdin, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const args = [
      "-F", configPath,
      "-o", "BatchMode=yes",
      "-o", "StrictHostKeyChecking=accept-new",
      "-o", "LogLevel=ERROR",
      "-o", "ConnectTimeout=15",
      alias,
      command,
    ];
    const child = spawn(sshBinary(), args, { windowsHide: true, env: childEnv() });
    const stdout = []; const stderr = [];
    let stdoutBytes = 0; let stderrBytes = 0; let timedOut = false; let done = false;
    const timer = setTimeout(() => { timedOut = true; killTree(child); }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdoutBytes += chunk.length; if (stdoutBytes <= MAX_OUTPUT_BYTES * 2) stdout.push(chunk); });
    child.stderr.on("data", (chunk) => { stderrBytes += chunk.length; if (stderrBytes <= MAX_OUTPUT_BYTES * 2) stderr.push(chunk); });
    child.on("error", (error) => { if (!done) { done = true; clearTimeout(timer); reject(error); } });
    child.on("close", (code, signal) => {
      if (done) return;
      done = true; clearTimeout(timer);
      resolve({ code: code ?? -1, signal, timedOut, stdout: limit(stdout, stdoutBytes), stderr: limit(stderr, stderrBytes), stdoutBytes, stderrBytes });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(stdin);
  });
}
