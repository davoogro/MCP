# MCP SSH multi-servidor

Un solo MCP para todos los hosts de `~/.ssh/config`. No guarda IPs, usuarios ni claves: llama al `ssh` del sistema con el alias. Cada `list_servers` / `execute_command` **vuelve a leer** el archivo, así que un `Host` nuevo aparece sin reiniciar Cursor ni clonar carpetas.

## Añadir un servidor

1. Añade el bloque en `%USERPROFILE%\.ssh\config` (Host, HostName, User, IdentityFile, Port).
2. Comprueba `ssh -o BatchMode=yes <alias>`.
3. Listo. En el chat llama a `list_servers`.

Opcional, justo encima del `Host`, para Windows o para limitar comandos:

```sshconfig
# mcp: os=windows env=production access=managed groups=windows,production
Host winsrv01
    HostName ...
```

```sshconfig
# mcp: os=linux env=production access=read_only groups=application
Host db-prod
    HostName ...
```

Sin comentario `# mcp:` el alias se trata como `os=linux` y `access=managed`. Los `Host` con comodines (`*`, `?`) y los bloques `Match` no se exponen.

Campos: `os` (`linux` o `windows`), `env`, `access` (`read_only` o `managed`), `groups`. Windows usa PowerShell; Linux usa POSIX.

`read_only` es un guardrail local, no un límite real: el control de verdad es la cuenta SSH.

La autenticación es la de OpenSSH (`BatchMode=yes`, `StrictHostKeyChecking=accept-new`). Este proceso no lee claves privadas.

Cada ejecución deja una línea JSON en `%LOCALAPPDATA%\ssh-inventory-mcp\audit.jsonl` (o `MCP_SSH_AUDIT_LOG`).

Para otro archivo de config: `MCP_SSH_CONFIG`.

## Ejecución

```powershell
cd D:\Projects\MCP\ssh-inventory-mcp
npm install
npm test
node index.js
```
