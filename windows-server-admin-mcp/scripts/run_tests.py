#!/usr/bin/env python3
"""Run unit + security tests and emit JUnit XML + coverage reports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/unit",
        "tests/security",
        "-q",
        "--tb=short",
        f"--junitxml={REPORTS / 'test_results.xml'}",
        "--cov=windows_server_admin_mcp",
        f"--cov-report=xml:{REPORTS / 'coverage.xml'}",
        "--cov-report=term-missing",
    ]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
