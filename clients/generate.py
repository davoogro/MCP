#!/usr/bin/env python3
"""Genera la configuración MCP de este repo para varios clientes.

Clientes: cursor, kiro, codex, claude (Claude Code), claude-desktop, vscode.

Ejemplos:
  # Generar todo en clients/out/ con tus rutas
  python clients/generate.py --root C:\\DVB\\mcp --python C:\\DVB\\python\\python.exe

  # Solo algunos servidores y clientes
  python clients/generate.py --only mssql,s3,wsl --client cursor,codex

  # Fusionar directamente en la config de usuario de cada cliente (hace backup .bak-<fecha>)
  python clients/generate.py --only mssql,s3 --client cursor,kiro,codex --install

Solo usa la librería estándar. Las credenciales NO van aquí: cada MCP lee su propio .env
(en su carpeta) o sus config/*.yaml.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENTS = ["cursor", "kiro", "codex", "claude", "claude-desktop", "vscode"]


# --------------------------------------------------------------------------- servidores
def load_servers(root: str, python: str, node: str, only: set[str], exclude: set[str]) -> dict:
    spec = json.loads((HERE / "servers.json").read_text(encoding="utf-8"))["servers"]
    windows = bool(re.match(r"^[A-Za-z]:[\\/]|^\\\\", root))
    sep = "\\" if windows else "/"
    root = root.rstrip("\\/")
    unknown = (only | exclude) - spec.keys()
    if unknown:
        sys.exit(f"Servidores desconocidos: {', '.join(sorted(unknown))}. Disponibles: {', '.join(spec)}")

    out = {}
    for name, s in spec.items():
        if (only and name not in only) or name in exclude:
            continue
        d = f"{root}{sep}{s['folder']}"
        fmt = lambda v: v.format(python=python, node=node, dir=d, sep=sep)  # noqa: E731
        env = {k: fmt(v) for k, v in s.get("env", {}).items()}
        if "python_module" in s:
            command, args = python, ["-m", s["python_module"]]
            env = {"PYTHONPATH": f"{d}{sep}src", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", **env}
        else:
            command, args = node, [d + sep + s["node_script"].replace("/", sep)]
        out[name] = {"command": command, "args": args, "env": env}
    return out


# --------------------------------------------------------------------------- renderers
def as_json_servers(servers: dict, *, typed: bool = False, kiro: bool = False) -> dict:
    res = {}
    for name, s in servers.items():
        e = ({"type": "stdio"} if typed else {}) | {"command": s["command"], "args": s["args"]}
        if s["env"]:
            e["env"] = s["env"]
        if kiro:
            e |= {"disabled": False, "autoApprove": []}
        res[name] = e
    return res


def toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # cadena básica TOML válida


def render_codex(servers: dict) -> str:
    lines = ["# Pegar en ~/.codex/config.toml (o usar --install)", ""]
    for name, s in servers.items():
        lines += [f"[mcp_servers.{name}]",
                  f"command = {toml_str(s['command'])}",
                  "args = [" + ", ".join(toml_str(a) for a in s["args"]) + "]",
                  "startup_timeout_sec = 30",
                  "tool_timeout_sec = 300"]
        if s["env"]:
            lines.append(f"[mcp_servers.{name}.env]")
            lines += [f"{k} = {toml_str(v)}" for k, v in s["env"].items()]
        lines.append("")
    return "\n".join(lines)


def ps_quote(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def render_claude_ps1(servers: dict) -> str:
    lines = ["# Registra los MCP en Claude Code (scope user). Ejecutar en PowerShell.", ""]
    for name, s in servers.items():
        cfg = json.dumps({"type": "stdio", "command": s["command"], "args": s["args"], "env": s["env"]},
                         ensure_ascii=False)
        lines.append(f"claude mcp remove {name} --scope user 2>$null")
        lines.append(f"claude mcp add-json {name} {ps_quote(cfg)} --scope user")
    lines += ["", "claude mcp list", ""]
    return "\n".join(lines)


def render_claude_sh(servers: dict) -> str:
    lines = ["#!/usr/bin/env sh", "# Registra los MCP en Claude Code (scope user).", ""]
    for name, s in servers.items():
        cfg = json.dumps({"type": "stdio", "command": s["command"], "args": s["args"], "env": s["env"]},
                         ensure_ascii=False).replace("'", "'\\''")
        lines.append(f"claude mcp remove {name} --scope user 2>/dev/null")
        lines.append(f"claude mcp add-json {name} '{cfg}' --scope user")
    lines += ["", "claude mcp list", ""]
    return "\n".join(lines)


def render(client: str, servers: dict) -> dict[str, str]:
    """Devuelve {ruta relativa en out/: contenido}."""
    j = lambda o: json.dumps(o, indent=2, ensure_ascii=False) + "\n"  # noqa: E731
    if client == "cursor":
        return {"cursor/mcp.json": j({"mcpServers": as_json_servers(servers)})}
    if client == "kiro":
        return {"kiro/mcp.json": j({"mcpServers": as_json_servers(servers, kiro=True)})}
    if client == "codex":
        return {"codex/config.toml": render_codex(servers)}
    if client == "claude":
        return {"claude/.mcp.json": j({"mcpServers": as_json_servers(servers, typed=True)}),
                "claude/claude-add.ps1": render_claude_ps1(servers),
                "claude/claude-add.sh": render_claude_sh(servers)}
    if client == "claude-desktop":
        return {"claude-desktop/claude_desktop_config.json": j({"mcpServers": as_json_servers(servers)})}
    if client == "vscode":
        return {"vscode/mcp.json": j({"servers": as_json_servers(servers, typed=True)})}
    raise ValueError(client)


# --------------------------------------------------------------------------- install
def user_config_path(client: str) -> Path | None:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    mac = sys.platform == "darwin"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "kiro":
        return home / ".kiro" / "settings" / "mcp.json"
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    if client == "claude-desktop":
        if os.name == "nt":
            return appdata / "Claude" / "claude_desktop_config.json"
        return home / ("Library/Application Support/Claude" if mac else ".config/Claude") / "claude_desktop_config.json"
    if client == "vscode":
        if os.name == "nt":
            return appdata / "Code" / "User" / "mcp.json"
        return home / ("Library/Application Support/Code/User" if mac else ".config/Code/User") / "mcp.json"
    return None  # claude: usar claude-add.ps1/.sh


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + time.strftime(".bak-%Y%m%d-%H%M%S")))


def install_json(path: Path, key: str, entries: dict) -> None:
    data = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault(key, {}).update(entries)
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def install_codex(path: Path, servers: dict) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for name in servers:
        n = re.escape(name)
        hdr = rf'^\[mcp_servers\.(?:{n}|"{n}")(?:\.[^\]]+)?\]\s*$'
        text = re.sub(rf"(?ms){hdr}.*?(?=^\[|\Z)", "", text)
    body = render_codex(servers).split("\n", 2)[2]  # sin el comentario inicial
    text = text.rstrip() + ("\n\n" if text.strip() else "") + body
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(HERE.parent), help="Carpeta donde están los MCP (default: este repo)")
    ap.add_argument("--python", default=sys.executable, help="Python con las dependencias instaladas")
    ap.add_argument("--node", default="node", help="Ejecutable de Node.js")
    ap.add_argument("--only", default="", help="Servidores a incluir, separados por coma")
    ap.add_argument("--exclude", default="", help="Servidores a excluir, separados por coma")
    ap.add_argument("--client", default=",".join(CLIENTS), help=f"Clientes: {','.join(CLIENTS)}")
    ap.add_argument("--out", default=str(HERE / "out"), help="Carpeta de salida (default: clients/out)")
    ap.add_argument("--install", action="store_true", help="Fusionar en la config de usuario de cada cliente")
    ap.add_argument("--list", action="store_true", help="Listar servidores disponibles y salir")
    a = ap.parse_args()

    split = lambda s: {x.strip() for x in s.split(",") if x.strip()}  # noqa: E731
    if a.list:
        for name, s in json.loads((HERE / "servers.json").read_text(encoding="utf-8"))["servers"].items():
            print(f"{name:22} {s['folder']:26} {s.get('build', '(python: pip install -e .)')}")
        return
    clients = split(a.client)
    if bad := clients - set(CLIENTS):
        sys.exit(f"Clientes desconocidos: {', '.join(sorted(bad))}")
    servers = load_servers(a.root, a.python, a.node, split(a.only), split(a.exclude))

    out = Path(a.out)
    for client in [c for c in CLIENTS if c in clients]:
        for rel, content in render(client, servers).items():
            p = out / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")
            print(f"generado  {p}")
        if a.install:
            target = user_config_path(client)
            if client == "claude":
                print(f"claude: ejecuta {out / 'claude' / ('claude-add.ps1' if os.name == 'nt' else 'claude-add.sh')}")
            elif client == "codex":
                install_codex(target, servers)
                print(f"instalado {target}")
            else:
                install_json(target, "servers" if client == "vscode" else "mcpServers",
                             json.loads(render(client, servers)[next(iter(render(client, servers)))])
                             ["servers" if client == "vscode" else "mcpServers"])
                print(f"instalado {target}")
    print(f"{len(servers)} servidores: {', '.join(servers)}")


if __name__ == "__main__":
    main()
