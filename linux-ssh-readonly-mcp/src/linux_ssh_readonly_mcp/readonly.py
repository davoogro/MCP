"""Validador de solo lectura para comandos remotos.

`validate(command)` lanza `ReadOnlyViolation` si el comando puede modificar algo en el servidor.
Estrategia: rechazar expansiones peligrosas en el string crudo, tokenizar con shlex (operadores fuera de
comillas quedan como tokens propios), quitar las redirecciones a /dev/null permitidas, partir en segmentos
por los separadores admitidos y exigir que el primer token de cada segmento esté en la ALLOWLIST, aplicando
además reglas específicas por comando.
"""

from __future__ import annotations

import re
import shlex

MAX_LEN = 2000

# Primer token (sin ruta) admitido en cada segmento.
ALLOWLIST: frozenset[str] = frozenset("""
ls cat head tail grep egrep fgrep zgrep zcat bzcat xzcat find stat file wc du df free uptime uname
hostname id whoami groups w who last lastlog ps pgrep pidof top ss netstat ip ifconfig route arp lsof
systemctl journalctl date cal env printenv echo printf sort uniq cut awk sed tr column nl rev tac
md5sum sha1sum sha256sum cksum readlink realpath basename dirname which type whereis rpm crontab
getent lsblk blkid mount findmnt vmstat iostat mpstat sar pidstat nproc lscpu lsmem dmesg tree
ulimit timedatectl hostnamectl locale strings od hexdump diff cmp test true false sleep java ping
traceroute tracepath nslookup dig host curl wget openssl tar unzip namei getenforce sestatus
chage ls-files jps jstat jcmd
""".split())

# Caracteres que shlex (punctuation_chars=True) devuelve como tokens de operador fuera de comillas.
_OP_CHARS = set("();<>|&")
_SEPARATORS = {"|", "&&", "||", ";"}
# Secuencias de redirección a /dev/null que se toleran (tras tokenizar shlex las parte en varios tokens).
_REDIR3 = {("2", ">", "/dev/null"), ("2", ">&", "1")}
_REDIR2 = {(">", "/dev/null"), ("&>", "/dev/null")}
_RAW_DANGER = ("`", "$(", "<(", ">(", "${")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Expansión de llaves de bash que fabrica opciones (`-{delete,}` / `{x,-delete}` -> `-delete`).
_BRACE_RE = re.compile(r"\{([^\s{}]*(?:,|\.\.)[^\s{}]*)\}")
# Comandos sed w/W/e: heurística de la spec + variantes sin espacio detrás (`sed e`, `s/x/y/e`, `s/x/y/w/f`).
_SED_BAD_RES = (
    re.compile(r"(^|[;}\s/])[wWe]\s"),
    re.compile(r"/[wWe]\s"),
    re.compile(r"(^|[;{}\s/])[wWe]\s*($|[;}])"),
    re.compile(r"s(.)(?:\\.|(?!\1).)*?\1(?:\\.|(?!\1).)*?\1[0-9gpiImM]*[ewW]"),
)


class ReadOnlyViolation(ValueError):
    """El comando no cumple la política de solo lectura."""


def _is_op(token: str) -> bool:
    return bool(token) and all(c in _OP_CHARS for c in token)


def _brace_makes_option(token: str) -> bool:
    if token.startswith("-"):
        return bool(_BRACE_RE.search(token))
    m = _BRACE_RE.match(token)
    return bool(m) and any(p.startswith("-") for p in re.split(r",|\.\.", m.group(1)))


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _first_word(args: list[str]) -> str | None:
    for a in args:
        if not a.startswith("-"):
            return a
    return None


# ---------------------------------------------------------------- reglas por comando


def _rule_find(args: list[str]) -> None:
    bad = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
    for a in args:
        if a in bad:
            raise ReadOnlyViolation(f"find con '{a}' puede ejecutar/escribir")


def _short_bundle(a: str) -> str:
    """Letras de un grupo de opciones cortas (`-sSLo` -> 'sSLo'); '' si no es un grupo corto."""
    return a[1:] if a.startswith("-") and not a.startswith("--") and len(a) > 1 else ""


def _long_name(a: str) -> str:
    return a.split("=", 1)[0] if a.startswith("--") else ""


def _rule_sed(args: list[str]) -> None:
    for a in args:
        if a.startswith("--in-place") or _long_name(a) == "--file" or any(c in _short_bundle(a) for c in "if"):
            raise ReadOnlyViolation("sed -i (edita en sitio) / -f (script desde archivo) no permitidos")
    for a in args:
        if a.startswith("-"):
            continue
        if any(r.search(a) for r in _SED_BAD_RES):
            raise ReadOnlyViolation("el script de sed usa un comando w/W/e (escribe o ejecuta)")


def _rule_awk(args: list[str]) -> None:
    for a in args:
        if _long_name(a) in {"--file", "--include", "--load", "--exec", "--source"} or \
                any(c in _short_bundle(a)[:1] for c in "filE"):
            raise ReadOnlyViolation("awk -f/-i/-l/-E (programa o extensión desde archivo) no permitido")
        if a.startswith("-"):
            continue
        if any(x in a for x in ("system", "getline", "fflush")) or ">" in a or "|" in a:
            raise ReadOnlyViolation("el programa awk usa system/getline/fflush o redirección/pipe")


def _rule_systemctl(args: list[str]) -> None:
    subs = {"status", "list-units", "list-unit-files", "list-timers", "list-sockets", "is-active",
            "is-enabled", "is-failed", "show", "cat", "list-dependencies"}
    if "--version" in args:
        return
    sub = _first_word(args)
    if sub is None or sub not in subs:
        raise ReadOnlyViolation(f"systemctl solo permite subcomandos de consulta ({sub!r} no lo es)")


def _rule_journalctl(args: list[str]) -> None:
    bad = {"--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var",
           "--setup-keys", "--update-catalog"}
    for a in args:
        if a in bad or a.startswith("--vacuum-"):
            raise ReadOnlyViolation(f"journalctl con '{a}' modifica el journal")


def _rule_crontab(args: list[str]) -> None:
    has_list = False
    j = 0
    while j < len(args):
        a = args[j]
        if a == "-l":
            has_list = True
        elif a == "-u":
            j += 1  # salta el usuario
        else:
            raise ReadOnlyViolation("crontab solo se permite como 'crontab -l' (opcionalmente '-u X')")
        j += 1
    if not has_list:
        raise ReadOnlyViolation("crontab solo se permite como 'crontab -l' (opcionalmente '-u X')")


def _rule_rpm(args: list[str]) -> None:
    if not args:
        raise ReadOnlyViolation("rpm solo se permite en modo consulta (-q) o verificación (-V)")
    a = args[0]
    if not (a.startswith("-q") or a == "-V" or a.startswith("--verify")):
        raise ReadOnlyViolation("rpm solo se permite en modo consulta (-q) o verificación (-V)")
    for x in args:
        if "%(" in x or _long_name(x) in {"--eval", "--define", "--pipe", "--macros", "--rcfile", "--load"} \
                or x in {"-E", "-D"}:
            raise ReadOnlyViolation(f"rpm con '{x}' puede ejecutar comandos (macros/pipe)")


def _rule_dmesg(args: list[str]) -> None:
    bad = {"-c", "-C", "--clear", "--read-clear", "-n", "--console-level", "-D", "-E",
           "--console-off", "--console-on"}
    for a in args:
        if a in bad:
            raise ReadOnlyViolation(f"dmesg con '{a}' modifica/limpia el buffer del kernel")


def _rule_date(args: list[str]) -> None:
    for a in args:
        if a == "-s" or a.startswith("--set"):
            raise ReadOnlyViolation("date -s/--set cambia la hora del sistema")


def _rule_hostname(args: list[str]) -> None:
    for a in args:
        if not a.startswith("-") or a in {"-F", "-b"} or _long_name(a) in {"--file", "--boot"}:
            raise ReadOnlyViolation("hostname con un nombre cambiaría el hostname")


def _rule_ctl_status(args: list[str]) -> None:
    non_opt = [a for a in args if not a.startswith("-")]
    if non_opt and non_opt != ["status"]:
        raise ReadOnlyViolation("solo se permite 'status' (o sin argumentos)")


def _rule_ip(args: list[str]) -> None:
    bad = {"add", "del", "delete", "set", "flush", "change", "replace", "exec", "append", "save", "restore"}
    for a in args:
        if a in bad:
            raise ReadOnlyViolation(f"ip ... {a} modifica la configuración de red")
        if a.startswith("-") and (a in {"-b", "--b"} or a.lstrip("-").startswith(("ba", "force"))):
            raise ReadOnlyViolation(f"ip {a} ejecuta comandos desde archivo")


def _rule_ifconfig(args: list[str]) -> None:
    bad = {"up", "down", "add", "del", "delete"}
    for a in args:
        if a in bad:
            raise ReadOnlyViolation(f"'{a}' modificaría la interfaz/ruta")


def _rule_mount(args: list[str]) -> None:
    j = 0
    while j < len(args):
        a = args[j]
        if a == "-l":
            pass
        elif a == "-t":
            j += 1  # salta el tipo
        else:
            raise ReadOnlyViolation("mount solo se permite para listar (sin args, -l o -t X)")
        j += 1


def _rule_ulimit(args: list[str]) -> None:
    for a in args:
        if not re.match(r"^-[A-Za-z]+$", a):
            raise ReadOnlyViolation("ulimit solo se permite para consultar (sin asignar valores)")


def _rule_curl(args: list[str]) -> None:
    bad_long = {"--output", "--remote-name", "--remote-name-all", "--upload-file", "--form", "--form-string",
                "--config", "--dump-header", "--cookie-jar", "--trace", "--trace-ascii", "--stderr", "--libcurl",
                "--output-dir", "--create-dirs", "--etag-save", "--hsts", "--alt-svc", "--json", "--request"}
    bad_short = set("oOTdFKDc")
    for i, a in enumerate(args):
        name = _long_name(a)
        if name in bad_long or name.startswith("--data") or name.startswith("--upload"):
            if name == "--request":
                val = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
                if val.upper() in ("GET", "HEAD"):
                    continue
            raise ReadOnlyViolation(f"curl con '{a}' sube/escribe datos")
        bundle = _short_bundle(a)
        for j, c in enumerate(bundle):
            if c in bad_short:
                raise ReadOnlyViolation(f"curl con '-{c}' sube/escribe datos")
            if c == "X":
                val = bundle[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                if val.upper() not in ("GET", "HEAD"):
                    raise ReadOnlyViolation("curl -X solo se permite con GET o HEAD")
                break


def _rule_wget(args: list[str]) -> None:
    bad_long = {"--output-file", "--append-output", "--execute", "--save-cookies", "--directory-prefix",
                "--recursive", "--mirror", "--page-requisites", "--convert-links", "--backups", "--post-data",
                "--post-file", "--body-data", "--body-file", "--method", "--timestamping", "--force-directories",
                "--warc-file", "--input-file"}
    ok = False
    for i, a in enumerate(args):
        name = _long_name(a)
        if a == "--spider":
            ok = True
        elif name == "--output-document":
            val = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if val != "-":
                raise ReadOnlyViolation("wget solo puede escribir a stdout (-O -)")
            ok = True
        elif name in bad_long:
            raise ReadOnlyViolation(f"wget con '{a}' escribe archivos o ejecuta comandos")
        bundle = _short_bundle(a)
        for j, c in enumerate(bundle):
            if c in "oaePrmpkxNi":
                raise ReadOnlyViolation(f"wget con '-{c}' escribe archivos o ejecuta comandos")
            if c == "O":
                val = bundle[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                if val != "-":
                    raise ReadOnlyViolation("wget solo puede escribir a stdout (-O -)")
                ok = True
                break
    if not ok:
        raise ReadOnlyViolation("wget solo se permite con '-O -' (a stdout) o '--spider'")


def _rule_tar(args: list[str]) -> None:
    longbad = {"--extract", "--create", "--append", "--update", "--delete", "--concatenate",
               "--catenate", "--diff", "--compare"}
    execbad = {"--checkpoint-action", "--to-command", "--use-compress-program", "--rsh-command",
               "--info-script", "--new-volume-script", "--rmt-command", "--index-file"}
    for i, a in enumerate(args):
        if a in longbad:
            raise ReadOnlyViolation(f"tar {a} no es solo lectura (solo se permite listar: -t)")
        if _long_name(a) in execbad or (i > 0 and any(c in _short_bundle(a) for c in "IF")) or \
                (i == 0 and any(c in a.lstrip("-") for c in "IF")):
            raise ReadOnlyViolation(f"tar con '{a}' ejecuta programas externos")
    if not args:
        raise ReadOnlyViolation("tar solo se permite para listar (-t)")
    mode = args[0]
    if mode.startswith("--"):
        if mode != "--list":
            raise ReadOnlyViolation("tar solo se permite para listar (-t / --list)")
        return
    m = mode.lstrip("-")
    if "t" not in m or any(c in m for c in "xcruAd"):
        raise ReadOnlyViolation("tar solo se permite para listar (-t)")


def _rule_unzip(args: list[str]) -> None:
    if not any(a in {"-l", "-v", "-p"} for a in args):
        raise ReadOnlyViolation("unzip solo se permite para listar/ver (-l, -v o -p)")


def _rule_top(args: list[str]) -> None:
    if "-b" not in args:
        raise ReadOnlyViolation("top solo se permite en modo batch (-b)")


def _rule_openssl(args: list[str]) -> None:
    sub = _first_word(args)
    if sub not in {"x509", "s_client", "version"}:
        raise ReadOnlyViolation("openssl solo se permite con x509, s_client o version")
    for a in args:
        if a in {"-out", "-sess_out", "-keylogfile", "-msgfile", "-writerand", "-CAcreateserial", "-CAserial"}:
            raise ReadOnlyViolation(f"openssl {sub} {a} escribe un archivo")


def _rule_env(args: list[str]) -> None:
    # `env CMD` / `env -S CMD` lanzan otro programa: solo se admite para imprimir el entorno.
    for a in args:
        if a not in {"-0", "--null"}:
            raise ReadOnlyViolation("env solo se permite para listar el entorno (env lanzaría otro programa)")


def _rule_java(args: list[str]) -> None:
    ok = {"-version", "--version", "-showversion", "--show-version", "-fullversion", "--full-version",
          "--list-modules", "-help", "--help", "-X"}
    for a in args:
        if a not in ok and not a.startswith("-XshowSettings"):
            raise ReadOnlyViolation("java solo se permite con opciones informativas (p.ej. -version)")


def _rule_jcmd(args: list[str]) -> None:
    safe = {"help", "VM.version", "VM.uptime", "VM.flags", "VM.system_properties", "VM.command_line",
            "VM.info", "Thread.print", "GC.heap_info", "PerfCounter.print", "VM.dynlibs", "Compiler.queue"}
    if args in (["-l"], ["-h"], ["--help"], []):
        return
    cmds = [a for a in args[1:] if not a.startswith("-")]
    if len(args) < 2 or not cmds or cmds[0] not in safe:
        raise ReadOnlyViolation("jcmd solo se permite con -l o '<pid> <comando informativo>' "
                                f"({', '.join(sorted(safe))})")


def _deny_opts(cmd: str, short: str, long: set[str]):
    """Regla genérica: rechaza opciones cortas (también agrupadas) y largas que escriben/ejecutan."""
    def rule(args: list[str]) -> None:
        for a in args:
            if any(c in _short_bundle(a) for c in short) or _long_name(a) in long:
                raise ReadOnlyViolation(f"{cmd} con '{a}' escribe archivos o modifica el sistema")
    return rule


def _rule_uniq(args: list[str]) -> None:
    if len([a for a in args if not a.startswith("-")]) > 1:
        raise ReadOnlyViolation("uniq con dos archivos escribe la salida en el segundo")


def _rule_chage(args: list[str]) -> None:
    if not args or args[0] != "-l":
        raise ReadOnlyViolation("chage solo se permite como 'chage -l usuario'")


_RULES = {
    "env": _rule_env, "java": _rule_java, "jcmd": _rule_jcmd, "uniq": _rule_uniq, "chage": _rule_chage,
    "sort": _deny_opts("sort", "o", {"--output", "--compress-program"}),
    "ss": _deny_opts("ss", "KD", {"--kill", "--diag"}),
    "sar": _deny_opts("sar", "o", set()),
    "tree": _deny_opts("tree", "o", set()),
    "file": _deny_opts("file", "C", {"--compile"}),
    "blkid": _deny_opts("blkid", "c", {"--cache-file"}),
    "lastlog": _deny_opts("lastlog", "CS", {"--clear", "--set"}),
    "find": _rule_find, "sed": _rule_sed, "awk": _rule_awk, "systemctl": _rule_systemctl,
    "journalctl": _rule_journalctl, "crontab": _rule_crontab, "rpm": _rule_rpm, "dmesg": _rule_dmesg,
    "date": _rule_date, "hostname": _rule_hostname, "timedatectl": _rule_ctl_status,
    "hostnamectl": _rule_ctl_status, "ip": _rule_ip, "ifconfig": _rule_ifconfig, "route": _rule_ifconfig,
    "arp": _rule_ifconfig, "mount": _rule_mount, "ulimit": _rule_ulimit, "curl": _rule_curl,
    "wget": _rule_wget, "tar": _rule_tar, "unzip": _rule_unzip, "top": _rule_top, "openssl": _rule_openssl,
}


# ---------------------------------------------------------------- núcleo


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # '#' no es comentario: en el shell remoto 'a#b' no corta el comando
    try:
        return list(lexer)
    except ValueError as exc:
        raise ReadOnlyViolation(f"No se pudo tokenizar el comando (¿comillas sin cerrar?): {exc}") from exc


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Quita las redirecciones a /dev/null permitidas y rechaza cualquier otra redirección/operador raro."""
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        trio = tuple(tokens[i:i + 3])
        duo = tuple(tokens[i:i + 2])
        if trio in _REDIR3:
            i += 3
            continue
        if duo in _REDIR2:
            i += 2
            continue
        if _is_op(t):
            if t in _SEPARATORS:
                out.append(t)
            elif t == "&":
                raise ReadOnlyViolation("no se permite ejecutar en segundo plano (&)")
            elif ">" in t or "<" in t:
                raise ReadOnlyViolation(f"redirección no permitida ('{t}'); solo se toleran "
                                        "2>/dev/null, 2>&1, >/dev/null y &>/dev/null")
            else:
                raise ReadOnlyViolation(f"operador de shell no permitido ('{t}')")
        else:
            out.append(t)
        i += 1
    return out


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for t in tokens:
        if t in _SEPARATORS:
            segments.append(current)
            current = []
        else:
            current.append(t)
    segments.append(current)
    return segments


def validate(command: str) -> None:
    """Lanza ReadOnlyViolation si `command` no respeta la política de solo lectura."""
    if not command or not command.strip():
        raise ReadOnlyViolation("comando vacío")
    if len(command) > MAX_LEN:
        raise ReadOnlyViolation(f"comando demasiado largo ({len(command)} > {MAX_LEN} caracteres)")
    if any(ord(c) < 32 and c != "\t" for c in command):
        raise ReadOnlyViolation("saltos de línea/caracteres de control no permitidos (usa ; para encadenar)")
    for pat in _RAW_DANGER:
        if pat in command:
            raise ReadOnlyViolation(f"sustitución/expansión no permitida ('{pat}')")

    tokens = _tokenize(command)
    if not tokens:
        raise ReadOnlyViolation("comando vacío")
    tokens = _strip_redirections(tokens)

    for segment in _split_segments(tokens):
        if not segment:
            raise ReadOnlyViolation("segmento de comando vacío (revisa los separadores | && || ;)")
        first = segment[0]
        for t in segment:
            if _brace_makes_option(t):
                raise ReadOnlyViolation(f"expansión de llaves no permitida en opciones ('{t}')")
        if _ASSIGN_RE.match(first):
            raise ReadOnlyViolation(f"asignación de variable al inicio no permitida ('{first}')")
        cmd = _basename(first)
        if cmd not in ALLOWLIST:
            raise ReadOnlyViolation(f"comando no permitido: '{cmd}' (no está en la allowlist de solo lectura)")
        rule = _RULES.get(cmd)
        if rule is not None:
            rule(segment[1:])


def describe_policy() -> str:
    """Resumen corto de la política para la instrucción del MCP."""
    return ("Solo lectura: se permiten comandos encadenados con | && || ; (nunca &) cuyo ejecutable esté en "
            "una allowlist de ~110 utilidades de diagnóstico (ls, cat, grep, find, ps, ss, systemctl status, "
            "journalctl, rpm -q, df, free, etc.). Se rechazan redirecciones salvo 2>/dev/null, 2>&1, "
            ">/dev/null y &>/dev/null; sustitución de comandos ($(...), backticks, ${...}, <(...)); "
            "asignaciones de variables; y las variantes de escritura de cada comando (sed -i, find -delete/"
            "-exec, systemctl restart, ip ... set, curl -o, tar -x, etc.). sudo/su, rm/mv/cp, tee, dd, "
            "kill, chmod, editores e intérpretes (bash, python...) no están permitidos.")
