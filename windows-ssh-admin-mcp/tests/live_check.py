"""Prueba en vivo de las tools contra el host por defecto (solo lecturas + escrituras reversibles en %TEMP%)."""

import json
import sys
import time

from windows_ssh_admin_mcp import server as S

TMP = r"C:\Windows\Temp\ssh-winserver-mcp-test"

READS = [
    ("get_system_info", {}),
    ("get_performance", {"top": 3}),
    ("get_disks", {}),
    ("get_events", {"log": "System", "level": "error", "hours": 72, "max_events": 3}),
    ("get_events", {"log": "Application", "hours": 1, "max_events": 2, "contains": "zzzz-no-match"}),
    ("list_event_logs", {"name_filter": "*SSH*"}),
    ("get_services", {"name": "*ssh*", "include_path": True}),
    ("get_processes", {"top": 3, "sort_by": "cpu", "include_path": True}),
    ("list_directory", {"path": "C:\\", "limit": 10}),
    ("read_file", {"path": "%windir%\\System32\\drivers\\etc\\hosts", "tail_lines": 3}),
    ("search_files", {"path": "C:\\ProgramData\\ssh", "name_pattern": "*.conf*", "recurse": False}),
    ("get_file_info", {"path": "C:\\Windows\\System32\\OpenSSH\\sshd.exe", "hash": True}),
    ("get_windows_features", {"installed_only": True}),
    ("get_installed_software", {}),
    ("get_updates", {"limit": 3}),
    ("get_network", {"include_routes": True}),
    ("get_listening_ports", {}),
    ("get_tcp_connections", {"local_port": 22}),
    ("test_network", {"target": "192.0.2.67", "port": 22}),
    ("get_firewall_rules", {"port": 22}),
    ("get_scheduled_tasks", {}),
    ("get_local_users_groups", {}),
    ("get_registry", {"path": "HKLM\\SOFTWARE\\OpenSSH"}),
    ("get_environment", {}),
    ("get_certificates", {}),
    ("get_shares", {}),
    ("list_background_jobs", {}),
]

WRITES = [
    ("file_operation", {"action": "mkdir", "path": TMP}),
    ("write_file", {"path": TMP + "\\a.ini", "content": "[x]\nclave=ñandú\nport=80\n"}),
    ("replace_in_file", {"path": TMP + "\\a.ini", "old": "port=80", "new": "port=8080"}),
    ("read_file", {"path": TMP + "\\a.ini"}),
    ("set_registry", {"path": "HKLM\\SOFTWARE\\ssh-winserver-mcp-test", "name": "v", "value": 5, "type": "DWord"}),
    ("delete_registry", {"path": "HKLM\\SOFTWARE\\ssh-winserver-mcp-test", "confirm": True}),
    ("run_cmd", {"command": "ver & echo %USERNAME%"}),
    ("run_powershell", {"script": "Get-Date; Write-Warning 'aviso'; Out-Json @{n=@(1)}"}),
    ("run_background", {"script": "'hola desde SYSTEM'; whoami", "name": "selftest"}),
]


# replace_in_file debe dejar los bytes igual salvo el texto reemplazado (BOM incluido o excluido).
# (nombre, encoding .NET con que se crea el archivo; su preámbulo es el BOM esperado)
ENCODING_CASES = [
    ("utf8-nobom", "New-Object Text.UTF8Encoding($false)"),
    ("utf8-bom", "New-Object Text.UTF8Encoding($true)"),
    ("utf16le-bom", "New-Object Text.UnicodeEncoding($false, $true)"),
    ("ansi", "[Text.Encoding]::Default"),
]


def check_encodings():
    fails = 0
    for name, enc in ENCODING_CASES:
        path = f"{TMP}\\enc-{name}.conf"
        script = f"""
$enc={enc}; $pre=$enc.GetPreamble()
function Bytes($t) {{ [byte[]]($pre + $enc.GetBytes($t)) }}
[IO.File]::WriteAllBytes('{path}', (Bytes "# clave=ñandú`r`nlisten_addresses = 'localhost'`r`n"))
Out-Json @{{expected=[Convert]::ToBase64String((Bytes "# clave=ñandú`r`nlisten_addresses = '*'`r`n"))}}
"""
        expected = S.run_powershell(script=script).get("data", {}).get("expected")
        res = S.replace_in_file(path=path, old="(?m)^listen_addresses = 'localhost'",
                                new="listen_addresses = '*'", regex=True, count=1, backup=False)
        got = S.run_powershell(script=f"Out-Json @{{b=[Convert]::ToBase64String([IO.File]::ReadAllBytes('{path}'))}}")
        same = bool(expected) and res.get("ok") and got.get("data", {}).get("b") == expected
        fails += not show(f"replace_in_file[{name}]", {}, {**res, "ok": same,
                          "message": "" if same else f"bytes distintos (encoding={res.get('data', {}).get('encoding')})"})
    return fails


def show(name, args, res):
    ok = res.get("ok", True) if isinstance(res, dict) else True
    size = len(json.dumps(res, default=str, ensure_ascii=False))
    detail = res.get("error") or res.get("message") or "" if isinstance(res, dict) else ""
    print(f"{'OK ' if ok else 'ERR'} {name:24} {size:>7}c  {str(detail)[:300]}", flush=True)
    return ok


def main():
    fails = 0
    for name, args in READS + WRITES:
        t = time.time()
        res = getattr(S, name)(**args)
        fails += not show(name, args, res)
        if "-v" in sys.argv:
            print(json.dumps(res, ensure_ascii=False, default=str)[:1500])
        if name == "run_background" and res.get("ok"):
            jid = res["data"]["job_id"]
            time.sleep(8)
            j = S.get_background_job(job_id=jid)
            print("    job:", json.dumps(j.get("data"), ensure_ascii=False)[:400])
            S.remove_background_job(job_id=jid)
    fails += check_encodings()
    import os, tempfile
    loc = os.path.join(tempfile.gettempdir(), "sshwin-up.bin")
    with open(loc, "wb") as f:
        f.write(os.urandom(300_000))
    fails += not show("upload_file", {}, S.upload_file(local_path=loc, remote_path=TMP + "\\sub dir\\up.bin"))
    fails += not show("upload_file", {}, S.upload_file(local_path=loc, remote_path=TMP + "\\up.bin"))
    down = S.download_file(remote_path=TMP + "\\up.bin", local_path=loc + ".back")
    same = down.get("ok") and open(loc, "rb").read() == open(loc + ".back", "rb").read()
    fails += not show("download_file", {}, {**down, "ok": bool(same)})
    print("    remote sha:", S.get_file_info(path=TMP + "\\up.bin", hash=True).get("data", {}).get("size"))
    fails += not show("file_operation", {}, S.file_operation(action="delete", path=TMP, recurse=True))
    fails += not show("file_operation", {}, S.file_operation(action="delete", path=TMP, recurse=True, confirm=True))
    print("fallos:", fails)


if __name__ == "__main__":
    main()
