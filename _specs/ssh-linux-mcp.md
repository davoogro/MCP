# Spec: ssh-linux-mcp (SOLO LECTURA)

Construir un MCP server Python en `C:\DVB\mcp\ssh-linux-mcp`, paquete `src/ssh_linux_mcp`, que da acceso
de **solo lectura** a servidores Linux por SSH a través de un proxy SOCKS5 local. Idioma de docstrings,
mensajes e instrucciones: español (como los otros MCP de C:\DVB\mcp).

## Imitar el estilo de `C:\DVB\mcp\ssh-winserver-mcp`
Leer primero `ssh-winserver-mcp/src/ssh_winserver_mcp/{server.py,config.py,__main__.py}`, `pyproject.toml`,
`registrar.ps1` y `README.md` y copiar su estructura: `from mcp.server import MCPServer`,
`mcp.types.ToolAnnotations` (todas las tools con anotación read-only), logging a stderr, `__version__`
en `__init__.py`, `__main__.py`, modos CLI `--test [host]` y `--handshake`, audit log JSONL en
`%LOCALAPPDATA%\ssh-linux-mcp\audit.jsonl`, recorte de respuestas a `SSHLX_MAX_CHARS` (default 60000),
tools síncronas bloqueantes ejecutadas con `asyncio.to_thread`.

## Entorno
- Python: `C:\DVB\python\python.exe` (3.14). Librerías ya instaladas: `paramiko`, `PySocks` (`socks`),
  `python-dotenv`, `mcp`. No instalar nada más.
- Credenciales: variables `LINUX_SSH_USER` y `LINUX_SSH_PASSWORD` del `.env` compartido `C:\DVB\mcp\.env`. Cargar con
  `load_dotenv(override=False)` desde `ssh-linux-mcp/.env` (si existe), luego `C:\DVB\mcp\.env`, y
  `SSHLX_ENV_FILE` si está definida. Nunca loguear ni devolver la contraseña.

## Configuración `config/hosts.json`
```json
{
  "default": "linuxapp01",
  "hosts": {
    "linuxapp01": {
      "hostname": "192.0.2.59",
      "port": 22,
      "user_env": "LINUX_SSH_USER",
      "password_env": "LINUX_SSH_PASSWORD",
      "proxy": "socks5://127.0.0.1:18080",
      "description": "linuxapp01 - RHEL 7.4 (kernel 3.10), usuario appuser, vía SOCKS5 127.0.0.1:18080"
    }
  }
}
```
`proxy` opcional (sin proxy = conexión directa). Ruta sobreescribible con `SSHLX_CONFIG`.

## Transporte (`transport.py`)
- Un `paramiko.SSHClient` persistente por host, protegido con `threading.Lock`; socket creado con
  `socks.socksocket()` + `set_proxy(socks.SOCKS5, host, port, rdns=True)`, timeout de conexión 20 s.
  `allow_agent=False, look_for_keys=False`. Keepalive 15 s (`transport.set_keepalive`).
- Host keys: guardar en `%LOCALAPPDATA%\ssh-linux-mcp\known_hosts`. Primera vez: aceptar y guardar
  (log de advertencia con la huella); luego, si cambia, rechazar con error claro.
- Si la conexión está caída o falla un comando por error de transporte, reconectar una vez y reintentar.
- `run(cmd, timeout_s)` -> exit_code, stdout, stderr, duración. Anteponer internamente `LC_ALL=C ` y
  hacer `exec_command(..., timeout=...)`; decodificar utf-8 con `errors="replace"`. stdin cerrado.
  Matar el canal si excede el timeout.
- SFTP (`client.open_sftp()`) **solo** para operaciones de lectura: `listdir_attr`, `stat`, `open(path, "rb")`,
  `get` hacia local. Nada que escriba en remoto.

## Validador de solo lectura (`readonly.py`) — la parte crítica
`validate(command: str) -> None` lanza `ReadOnlyViolation(msg)` si el comando puede modificar algo.
Tokenizar con `shlex.shlex(command, posix=True, punctuation_chars=True)` (`whitespace_split=True`)
para que los operadores fuera de comillas queden como tokens propios.
- Separadores permitidos entre comandos: `|`, `&&`, `||`, `;`. Rechazar `&` (background).
- Redirecciones: rechazar cualquier token que contenga `>` o `<`, EXCEPTO las secuencias exactas
  `2>/dev/null`, `2>&1`, `>/dev/null`, `&>/dev/null` (reconocerlas tras tokenizar; ojo que shlex las parte).
- Rechazar sustitución/expansión peligrosa en el string crudo: `` ` ``, `$(`, `<(`, `>(`, `${`.
  Rechazar asignaciones de variables al inicio (`FOO=bar cmd`). Máx 2000 caracteres.
- Primer token de cada segmento (sin ruta: `/usr/bin/ls` -> `ls`) debe estar en ALLOWLIST:
  `ls cat head tail grep egrep fgrep zgrep zcat bzcat xzcat find stat file wc du df free uptime uname
  hostname id whoami groups w who last lastlog ps pgrep pidof top ss netstat ip ifconfig route arp lsof
  systemctl journalctl date cal env printenv echo printf sort uniq cut awk sed tr column nl rev tac
  md5sum sha1sum sha256sum cksum readlink realpath basename dirname which type whereis rpm crontab
  getent lsblk blkid mount findmnt vmstat iostat mpstat sar pidstat nproc lscpu lsmem dmesg tree
  ulimit timedatectl hostnamectl locale strings od hexdump diff cmp test true false sleep java ping
  traceroute tracepath nslookup dig host curl wget openssl tar unzip namei getenforce sestatus
  chage ls-files jps jstat jcmd`
  (`curl`, `wget`, `tar`, `unzip` con restricciones abajo; `sudo`, `su`, `rm`, `mv`, `cp`, `tee`, `dd`,
  `kill*`, `chmod`, `chown`, `touch`, `mkdir`, `ln`, `vi`, `nano`, `python`, `perl`, `bash`, `sh`,
  `xargs`, `nohup`, `yum`, `pip`, editores e intérpretes quedan fuera por no estar en la lista).
- Reglas por comando (rechazar si):
  - `find`: algún arg en `-delete -exec -execdir -ok -okdir -fprint -fprint0 -fprintf -fls`.
  - `sed`: arg `-i`, `-i*`, `--in-place*`; o algún script que contenga comando `w`, `W` o `e`
    (heurística: regex `(^|[;}\s/])[wWe]\s` o `/[wWe]\s` sobre los args no-opción).
  - `awk`: el programa contiene `system`, `getline`, `>`, `|`, `fflush`.
  - `systemctl`: subcomando no en `status list-units list-unit-files list-timers list-sockets
    is-active is-enabled is-failed show cat list-dependencies --version`.
  - `journalctl`: args `--vacuum-size* --vacuum-time* --vacuum-files* --rotate --flush --sync
    --relinquish-var --smart-relinquish-var --setup-keys --update-catalog`.
  - `crontab`: algo distinto de `-l` (opcionalmente con `-u X`).
  - `rpm`: el primer arg no empieza por `-q` (también permitir `-V`/`--verify`).
  - `dmesg`: args `-c -C --clear --read-clear -n --console-level -D -E --console-off --console-on`.
  - `date`: args `-s`, `--set*`. `hostname`: más de una opción sin `-`. `timedatectl`/`hostnamectl`:
    solo `status` o sin args. `ip`: contiene `add del delete set flush change replace link set`
    (permitir `ip a`, `ip addr show`, `ip route`, `ip -s link` etc.). `ifconfig`/`route`/`arp`: solo
    sin args o con `-n`/`-a`/nombre de interfaz sin más palabras como `up down add del`.
  - `mount`: con args distintos de `-l`/`-t X`. `ulimit`: con algún arg que no sea `-a`/`-n`/etc. sin valor.
  - `curl`/`wget`: args `-o -O --output --remote-name -T --upload-file -d --data* -F --form -X`
    salvo `-X GET|HEAD`, `--post*`, `-P`, `--directory-prefix`, `-N`, `-r`; para wget exigir
    `-O -` o `-q -O -`/`--spider`. `tar`: solo `-t`/`t` (listar). `unzip`: solo `-l`/`-v`/`-p`.
  - `top`: exigir `-b`. `test`: ok. `openssl`: solo `x509 s_client version` (x509 sin `-out`).
- Exponer `ALLOWLIST` y una función `describe_policy()` (texto corto) para la instrucción del MCP.
- Además `run_command` debe recordar en su docstring que el usuario remoto no tiene sudo útil y que
  la política es de solo lectura.

## Tools (todas `readOnlyHint=True`, parámetro opcional `host`)
1. `list_hosts` — inventario sin secretos.
2. `test_connection(host)` — conecta, devuelve banner, hostname, usuario, uname, latencia.
3. `get_system_info(host)` — hostname, os-release, kernel, uptime/load, CPU (nproc/lscpu modelo),
   memoria (`free -m` parseado), discos (`df -hP` parseado a lista), SELinux, hora.
4. `run_command(command, host, timeout_s=60 (máx 600), max_chars)` — valida con `readonly.validate`
   y ejecuta. Devuelve exit_code/stdout/stderr/duration/truncated. Si viola la política, error con el motivo.
5. `list_directory(path="~", host, show_hidden=False, limit=500)` — vía SFTP `listdir_attr`;
   nombre, tipo (dir/file/link), tamaño, permisos (`stat.filemode`), mtime ISO, uid/gid. `~` = home.
6. `get_file_info(path, host)` — SFTP stat + `file -b` (vía run validado).
7. `read_file(path, host, offset_lines=0, max_lines=500, max_bytes=2MB)` — SFTP lectura, devuelve
   líneas con número, total aprox, `truncated`. Detectar binario (NUL en primeros 8 KB) y avisar.
8. `tail_file(path, host, lines=200, grep=None)` — `tail -n N -- path [| grep -E -- patt]` con
   `shlex.quote`.
9. `search_files(path, host, name=None, type=None (f|d|l), max_depth=5, newer_than_minutes=None,
   min_size=None, limit=500)` — construye `find` seguro con shlex.quote, `2>/dev/null`, `| head`.
10. `grep_text(pattern, path, host, recursive=True, ignore_case=False, fixed=False, include=None,
    max_matches=500, context=0)` — `grep -n -I -E/-F [-r] [-i] [--include] -m ...`.
11. `disk_usage(path=None, host, depth=1)` — `df -hP` + si path: `du -h --max-depth=depth path | sort -rh | head -50`.
12. `get_processes(host, sort_by="cpu"|"mem", limit=30, filter=None)` — `ps -eo pid,ppid,user,%cpu,%mem,rss,etime,stat,args --sort=-%cpu` parseado.
13. `get_listening_ports(host)` — `ss -tulpn` (fallback `netstat -tulpn`) parseado.
14. `get_services(host, state=None ("running"|"failed"|...), pattern=None)` — `systemctl list-units --type=service --all --no-pager --no-legend` parseado.
15. `service_status(name, host, lines=30)` — `systemctl status <svc> --no-pager -n N` (quote).
16. `get_journal(host, unit=None, since=None, until=None, priority=None, grep=None, lines=200)` —
    `journalctl --no-pager -n N [-u] [--since] [--until] [-p] [-g]` (RHEL7 journalctl no tiene `-g`:
    si grep, usar `| grep -E`). Avisar que sin permisos de grupo `systemd-journal` puede salir vacío.
17. `download_file(remote_path, host, local_dir=None)` — SFTP `get` a local
    (`SSHLX_TRANSFER_DIR`, default `C:\DVB\exports\ssh-linux\<host>\`); límite 500 MB; devuelve ruta local y tamaño.
18. `query_audit_log(limit=50, tool=None)`.

Las tools estructuradas construyen comandos internamente; también deben pasar por `readonly.validate`
(defensa en profundidad). Errores devueltos como `{"ok": false, "error": "..."}` igual que ssh-winserver.

## Instrucciones del MCP
Texto en español: solo lectura, hosts disponibles (desde config), proxy SOCKS 127.0.0.1:18080 debe
estar arriba (si test_connection falla por conexión rechazada, decir que se levante el proxy),
flujo recomendado test_connection -> get_system_info -> tools específicas -> run_command, y
resumen de la política de solo lectura.

## Extras
- `pyproject.toml` (deps: mcp, paramiko, PySocks, python-dotenv), `README.md` breve,
  `registrar.ps1` igual al de ssh-winserver pero con nombre `ssh-linux`, módulo `ssh_linux_mcp`,
  `PYTHONPATH=C:\DVB\mcp\ssh-linux-mcp\src`.
- `tests/test_readonly.py` (pytest) con al menos 40 casos: permitidos (`ls -la /opt | grep x`,
  `ps aux | sort -k3 -rn | head`, `cat /etc/hosts 2>/dev/null`, `grep 'a>b' file`, `awk '{print $1}' f`,
  `find /var/log -name '*.log' -mtime -1`, `systemctl status sshd`, `journalctl -u sshd -n 50`,
  `tar -tzf x.tgz`, `rpm -qa | grep java`, `df -h && free -m`) y rechazados (`rm -rf /`, `ls > f`,
  `echo x >> f`, `cat a | tee b`, `find . -delete`, `find . -exec rm {} \;`, `sed -i s/a/b/ f`,
  `awk '{print > "f"}' x`, `systemctl restart sshd`, `sudo ls`, `ls; rm x`, `$(rm x)`, `` `rm x` ``,
  `FOO=1 ls`, `ls &`, `crontab -r`, `rpm -e pkg`, `curl -o f http://x`, `wget http://x`, `tar -xf a.tar`,
  `python -c 1`, `bash -c ls`, `xargs rm`, `date -s 2020-01-01`, `ip link set eth0 down`, `kill 1`).
- NO tocar `~/.claude.json` ni ejecutar registrar.ps1. NO conectarse al servidor (lo verificaré yo).
- Al terminar, correr `C:\DVB\python\python.exe -m pytest C:\DVB\mcp\ssh-linux-mcp\tests -q` y
  `python -m ssh_linux_mcp --handshake` (con PYTHONPATH) y dejar todo pasando.
