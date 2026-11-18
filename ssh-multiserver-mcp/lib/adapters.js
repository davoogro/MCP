import { runSsh } from "./transport.js";

const WINDOWS_BOOTSTRAP = [
  "$ErrorActionPreference = 'Continue'",
  "$raw = [Console]::In.ReadToEnd()",
  "$script = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($raw -replace '[^A-Za-z0-9+/=]', '')))",
  "$out = ''; $err = ''; $code = 0",
  "try { $all = & ([scriptblock]::Create($script)) 2>&1; $out = ($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | Out-String -Width 512); $err = ($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | Out-String -Width 512); if ($LASTEXITCODE) { $code = $LASTEXITCODE } } catch { $err = ($_ | Out-String -Width 512); $code = 1 }",
  "$json = @{ stdout=$out; stderr=$err; exitCode=$code } | ConvertTo-Json -Compress",
  "[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))",
].join("\n");
const WINDOWS_COMMAND = `powershell.exe -NoProfile -NonInteractive -EncodedCommand ${Buffer.from(WINDOWS_BOOTSTRAP, "utf16le").toString("base64")}`;

function timeoutMs(value) {
  const seconds = Number.isFinite(value) ? Math.trunc(value) : 60;
  if (seconds < 1 || seconds > 600) throw new Error("timeout_seconds must be between 1 and 600");
  return seconds * 1000;
}

export async function execute(server, configPath, command, timeoutSeconds) {
  if (typeof command !== "string" || command.trim() === "") throw new Error("command must be a non-empty string");
  const options = { configPath, alias: server.sshAlias, timeoutMs: timeoutMs(timeoutSeconds) };
  if (server.shell === "posix") return runSsh({ ...options, command });
  const result = await runSsh({ ...options, command: WINDOWS_COMMAND, stdin: Buffer.from(command, "utf8").toString("base64") });
  if (result.timedOut || result.code !== 0) return result;
  try {
    const payload = JSON.parse(Buffer.from(result.stdout.replace(/\s+/g, ""), "base64").toString("utf8"));
    return { ...result, code: payload.exitCode, stdout: payload.stdout, stderr: payload.stderr };
  } catch {
    return { ...result, code: -1, stderr: `${result.stderr}\nInvalid PowerShell response envelope.` };
  }
}
