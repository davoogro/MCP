"""Static scan: subprocess must never use shell=True."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

# Lines matching shell=True but allowed (comments documenting the prohibition)
ALLOWED_COMMENT_PATTERNS = (
    re.compile(r"never\s+shell\s*=\s*True", re.IGNORECASE),
    re.compile(r"#.*shell\s*=\s*True", re.IGNORECASE),
    re.compile(r'""".*shell\s*=\s*True.*"""', re.DOTALL | re.IGNORECASE),
)


def _is_allowed_line(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    for pat in ALLOWED_COMMENT_PATTERNS:
        if pat.search(line):
            return True
    if '"""' in line or "'''" in line:
        return True
    return False


def _find_shell_true_violations() -> list[str]:
    violations: list[str] = []
    pattern = re.compile(r"shell\s*=\s*True")
    for py_file in SRC_ROOT.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and not _is_allowed_line(line):
                violations.append(f"{py_file.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
    return violations


@pytest.mark.security
def test_no_shell_true_in_source():
    violations = _find_shell_true_violations()
    assert violations == [], f"shell=True found in source:\n" + "\n".join(violations)
