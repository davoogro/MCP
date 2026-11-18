const POSIX_READ_ONLY = new Set([
  "awk", "basename", "cat", "cut", "date", "df", "dirname", "du", "echo", "env",
  "file", "find", "free", "grep", "head", "hostname", "id", "ip", "journalctl", "last",
  "ls", "lsblk", "lscpu", "lsof", "netstat", "nproc", "ps", "pwd", "readlink", "sha256sum",
  "sort", "ss", "stat", "systemctl", "tail", "tr", "uname", "uniq", "uptime", "vmstat", "wc", "whoami",
]);
const WINDOWS_DENIED = /\b(remove-item|del|erase|format|clear-disk|stop-service|restart-computer|shutdown|set-item|new-item|copy-item|move-item|rename-item|invoke-webrequest|start-process)\b/i;

/** Guardrail; true authorization remains the remote SSH account and server policy. */
export function assertCommandAllowed(server, command) {
  if (server.access !== "read_only") return;
  if (server.shell === "powershell") {
    if (WINDOWS_DENIED.test(command) || /[;|]|\$\(|`/.test(command)) {
      throw new Error(`Server '${server.id}' is read_only; the PowerShell command was refused by policy.`);
    }
    return;
  }
  if (/[;|]|&&|\|\||>|<|\$\(|`/.test(command)) {
    throw new Error(`Server '${server.id}' is read_only; shell operators and redirection are refused.`);
  }
  const binary = command.trim().split(/\s+/)[0].split("/").at(-1);
  if (!POSIX_READ_ONLY.has(binary)) {
    throw new Error(`Server '${server.id}' is read_only; '${binary}' is not an approved inspection command.`);
  }
}
