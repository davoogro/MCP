"""Static scan for hardcoded secrets and insecure credential patterns."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL_DIR = PROJECT_ROOT / "powershell"
SRC_DIR = PROJECT_ROOT / "src"

HARDCODED_PASSWORD_PATTERNS = [
    re.compile(r'password\s*=\s*["\'][^"\']{3,}["\']', re.IGNORECASE),
    re.compile(r'Password\s*=\s*["\'][^"\']+["\']'),
    re.compile(r"ConvertTo-SecureString\s+['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*=\s*['\"][^'\"]{8,}['\"]"),
]


def _scan_file(path: Path, patterns: list[re.Pattern[str]]) -> list[str]:
    hits: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat in patterns:
            if pat.search(line):
                rel = path.relative_to(PROJECT_ROOT)
                hits.append(f"{rel}:{lineno}: {stripped[:120]}")
    return hits


@pytest.mark.security
def test_no_hardcoded_passwords_in_python():
    hits: list[str] = []
    for py_file in SRC_DIR.rglob("*.py"):
        hits.extend(_scan_file(py_file, HARDCODED_PASSWORD_PATTERNS))
    assert hits == [], "Hardcoded credential patterns found:\n" + "\n".join(hits)


@pytest.mark.security
def test_no_hardcoded_passwords_in_powershell():
    hits: list[str] = []
    for ps1 in POWERSHELL_DIR.rglob("*.ps1"):
        hits.extend(_scan_file(ps1, HARDCODED_PASSWORD_PATTERNS))
    assert hits == [], "Hardcoded credential patterns in PS1:\n" + "\n".join(hits)


@pytest.mark.security
def test_no_asplaintext_securestring_in_powershell():
    """No ConvertTo-SecureString -AsPlainText in dispatcher scripts."""
    pattern = re.compile(r"ConvertTo-SecureString.*-AsPlainText", re.IGNORECASE)
    violations: list[str] = []
    for ps1 in POWERSHELL_DIR.rglob("*.ps1"):
        text = ps1.read_text(encoding="utf-8")
        if pattern.search(text):
            violations.append(str(ps1.relative_to(PROJECT_ROOT)))
    assert violations == [], (
        "ConvertTo-SecureString -AsPlainText found:\n" + "\n".join(violations)
    )


@pytest.mark.security
def test_winrm_transport_uses_integrated_auth_only():
    """WinRM transport must not resolve or embed credentials."""
    winrm = SRC_DIR / "windows_guarded_admin_mcp" / "execution" / "winrm_transport.py"
    text = winrm.read_text(encoding="utf-8")
    assert "credential" not in text.lower()
    assert "password" not in text.lower()
    assert "authentication" in text
    assert "conn.authentication" in text
