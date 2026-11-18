import pytest

from ssh_linux_mcp.readonly import ALLOWLIST, ReadOnlyViolation, describe_policy, validate

ALLOWED = [
    # Casos exigidos por la spec
    "ls -la /opt | grep x",
    "ps aux | sort -k3 -rn | head",
    "cat /etc/hosts 2>/dev/null",
    "grep 'a>b' file",
    "awk '{print $1}' f",
    "find /var/log -name '*.log' -mtime -1",
    "systemctl status sshd",
    "journalctl -u sshd -n 50",
    "tar -tzf x.tgz",
    "rpm -qa | grep java",
    "df -h && free -m",
    # Adicionales
    "ps aux 2>&1",
    "cat a >/dev/null",
    "ls /nope &>/dev/null || echo falta",
    "/usr/bin/ls -l /tmp",
    "uname -a; uptime",
    "crontab -l",
    "crontab -u root -l",
    "ip -s link",
    "ip addr show",
    "ip a",
    "ip route",
    "rpm -V bash",
    "rpm -qi kernel",
    "openssl x509 -noout -text",
    "openssl version",
    "systemctl list-units --type=service --all --no-pager --no-legend",
    "systemctl --version",
    "sed -n '1,20p' /etc/passwd",
    "sed 's/warn/x/g' f",
    "sed -n '/success/p' f",
    "sed 's/foo/bar e/' f",
    "sed '/^$/d' f",
    "curl -s -X GET http://x",
    "curl -sI http://x",
    "wget -q -O - http://x",
    "wget --spider http://x",
    "unzip -l a.zip",
    "top -b -n 1",
    "mount -l",
    "mount -t nfs",
    "ulimit -a",
    "date '+%Y-%m-%d'",
    "hostname -f",
    "timedatectl status",
    "hostnamectl",
    "dmesg -T",
    "ifconfig -a",
    "route -n",
    "tail -n 200 -- /var/log/messages | grep -E -- 'err|fail'",
    "du -h --max-depth=1 /var | sort -rh | head -50",
    "ps -eo pid,ppid,user,%cpu,%mem,rss,etime,stat,args --sort=-%cpu",
    "grep -c 'a#b' f",
    "grep -E 'a{1,3}' f",
    "awk '{print $1,$2}' f",
    "grep -F '{\"a\":1,\"b\":2}' f",
    "env",
    "java -version",
    # Comandos que construyen las tools estructuradas (también pasan por validate)
    "hostname; id -un; uname -a",
    "file -b -- '/etc/hosts'; stat -c '%U %G' -- '/etc/hosts'",
    "tail -n 200 -- \"$HOME\"/'app.log' | grep -E -- 'ERROR|WARN'",
    "find \"$HOME\"/'logs' -maxdepth 5 -name '*.log' -type f -mmin -60 -size +1048575c "
    "-printf '%y\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n' 2>/dev/null | head -n 501",
    "grep -n -I -H -Z -E -m 500 -r '--include=*.log' -C 2 -- 'err' /var/log | head -n 3001",
    "du -h --max-depth=1 -- '/opt' 2>/dev/null | sort -rh | head -50",
    "ps -ww -eo pid,ppid,user:32,%cpu,%mem,rss,etime,stat,args --sort=-%mem",
    "ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null",
    "systemctl status sshd --no-pager -l -n 30",
    "journalctl --no-pager -u sshd --since 'yesterday' -p err | grep -E -- 'fail' | tail -n 200",
    "journalctl --no-pager -n 200",
    # Revisión: variantes legítimas que no deben romperse
    "curl -sS -L http://x", "curl -s -X GET http://x", "wget -q -O - http://x", "wget -qO- http://x",
    "sed -n '1,20p' f", "sed -E 's/a/b/g' f", "awk -F'|' '{print $1}' f", "sort -rn f | uniq -c",
    "ip -br a", "ip -s link", "ip link show bat0", "ss -tulpn", "tree -L 2 /opt", "file -b /etc/hosts",
    "jcmd -l", "jcmd 1234 Thread.print", "java -version", "chage -l appuser", "rpm -qa --qf '%{NAME} %{VERSION}'",
    "tar -tzf a.tgz", "openssl x509 -in c.pem -noout -text", "sar -u 1 3", "lastlog -u appuser",
]

REJECTED = [
    # Casos exigidos por la spec
    "rm -rf /",
    "ls > f",
    "echo x >> f",
    "cat a | tee b",
    "find . -delete",
    r"find . -exec rm {} \;",
    "sed -i s/a/b/ f",
    "awk '{print > \"f\"}' x",
    "systemctl restart sshd",
    "sudo ls",
    "ls; rm x",
    "$(rm x)",
    "`rm x`",
    "FOO=1 ls",
    "ls &",
    "crontab -r",
    "rpm -e pkg",
    "curl -o f http://x",
    "wget http://x",
    "tar -xf a.tar",
    "python -c 1",
    "bash -c ls",
    "xargs rm",
    "date -s 2020-01-01",
    "ip link set eth0 down",
    "kill 1",
    # Adicionales
    "cat <(rm x)",
    "echo ${HOME}",
    "ls 2>/tmp/err",
    "ls < /etc/passwd",
    "(ls)",
    "ls |",
    "sed -i.bak s/a/b/ f",
    "sed --in-place s/a/b/ f",
    "sed 's/a/b/ w out' f",
    "echo id | sed e",
    "sed 's/.*/id/e' f",
    "sed 's/a/b/gw/tmp/x' f",
    "sed '/x/{p;e}' f",
    "awk '{print | \"sh\"}' x",
    "awk 'BEGIN{system(\"id\")}'",
    "awk '{getline x < \"f\"}' y",
    "systemctl stop sshd",
    "systemctl",
    "journalctl --vacuum-size=1G",
    "journalctl --rotate",
    "crontab f",
    "crontab",
    "rpm -ivh x.rpm",
    "dmesg -c",
    "date --set=2020-01-01",
    "hostname nuevo",
    "timedatectl set-time 10:00",
    "ip addr add 1.2.3.4/24 dev eth0",
    "ip route del default",
    "ifconfig eth0 down",
    "route add default gw 1.1.1.1",
    "mount /dev/sda1 /mnt",
    "ulimit -n 1024",
    "curl -d x=1 http://x",
    "curl --data-binary @f http://x",
    "curl -X POST http://x",
    "curl -T f http://x",
    "wget -O out http://x",
    "tar -czf a.tgz /etc",
    "tar --extract -f a.tar",
    "unzip a.zip",
    "top",
    "openssl req -new",
    "openssl x509 -in a.pem -out b.pem",
    "/tmp/x/sh -c id",
    "mv a b",
    "chmod 777 f",
    "nohup ls",
    "yum install x",
    "find . -{delete,}",
    "sed -{i,} s/a/b/ f",
    "find . {x,-delete}",
    "ls a#b; rm x",
    "ls\nrm -rf x",
    "ls\rrm x",
    "env rm -rf /tmp/x",
    "env -S 'rm x'",
    "java -jar /tmp/x.jar",
    "java -cp /tmp Evil",
    "",
    "ls " + "x" * 2000,
    # Revisión: bypasses encontrados
    "tar -tf a.tar --checkpoint-action=exec=sh", "tar -tf a.tar --to-command=sh", "tar -tIsh -f a.tar",
    "tar -tf a.tar -I sh", "sort -o /tmp/x f", "sort --output=/tmp/x f", "sort --compress-program=sh f",
    "uniq a /tmp/b", "curl -K cfg http://x", "curl -sO http://x/f", "curl -so /tmp/x http://x",
    "curl --output=/tmp/x http://x", "curl -c jar http://x", "curl -D h http://x", "curl -sXPOST http://x",
    "curl --request=DELETE http://x", "wget -O - -O /tmp/x http://x", "wget -qO/tmp/x http://x",
    "wget -O - -o log http://x", "wget -O - -e output_document=/tmp/x http://x", "wget --output-document=/tmp/x http://x",
    "sed -ni 's/a/b/' f", "sed -nf script f", "sed --file=script f", "awk -f prog.awk f", "awk -lfilefuncs 1",
    "rpm -q --eval '%(id)'", "rpm -qa --pipe sh", "rpm -q --define 'x 1' a", "ip -batch cmds", "ip -force -b f",
    "ip netns exec ns id", "java -XX:OnError=id -version", "java -Xlog:gc:file=/tmp/x -version",
    "jcmd 1234 GC.heap_dump /tmp/h", "jcmd 1234 VM.set_flag X 1", "ss -K dst 1.2.3.4", "ss -tnD /tmp/x",
    "sar -o /tmp/x 1 1", "tree -o /tmp/x", "file -C -m m", "chage -d 0 u", "lastlog -C -u u",
    "openssl s_client -connect a:443 -sess_out /tmp/s", "openssl s_client -keylogfile /tmp/k -connect a:443",
    "hostname -F /tmp/h", "blkid -c /tmp/c",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    validate(cmd)


@pytest.mark.parametrize("cmd", REJECTED)
def test_rejected(cmd):
    with pytest.raises(ReadOnlyViolation):
        validate(cmd)


def test_system_info_command_is_valid():
    from ssh_linux_mcp.server import _SYSINFO_CMD

    validate(_SYSINFO_CMD)


def test_violation_has_reason():
    with pytest.raises(ReadOnlyViolation, match="rm"):
        validate("ls && rm x")


def test_allowlist_excludes_dangerous():
    for name in ("sudo", "su", "rm", "mv", "cp", "tee", "dd", "kill", "chmod", "chown", "touch", "mkdir",
                 "ln", "vi", "nano", "python", "perl", "bash", "sh", "xargs", "nohup", "yum", "pip"):
        assert name not in ALLOWLIST


def test_case_count():
    assert len(ALLOWED) + len(REJECTED) >= 40


def test_describe_policy():
    text = describe_policy()
    assert "lectura" in text and len(text) < 1500
