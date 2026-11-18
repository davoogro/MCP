"""Mide latencia de ops en vivo (fría + caliente, mismo proceso)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from windows_winrm_fleet_mcp import ops  # noqa: E402
from windows_winrm_fleet_mcp.config import resolve_server_ids  # noqa: E402


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _run(label: str, fn) -> int:
    t0 = time.perf_counter()
    fn()
    elapsed = _ms(t0)
    print(f"{label}: {elapsed} ms")
    return elapsed


def main() -> int:
    servers = resolve_server_ids("all")
    first_server = servers[0] if servers else None
    results: dict[str, dict[str, int]] = {}

    for pass_name in ("cold", "warm"):
        print(f"\n=== {pass_name} ===")
        row: dict[str, int] = {}
        row["test_connection"] = _run("test_connection", lambda: ops.test_connection("all"))
        row["fleet_overview"] = _run("fleet_overview", lambda: ops.fleet_overview("all"))
        row["get_disks_all"] = _run("get_disks", lambda: ops.get_disks("all"))
        row["get_services_problems"] = _run(
            "get_services(problems_only)",
            lambda: ops.get_services("all", problems_only=True),
        )
        row["get_scheduled_tasks_failed"] = _run(
            "get_scheduled_tasks(failed_only)",
            lambda: ops.get_scheduled_tasks("all", failed_only=True),
        )
        if first_server:
            row["get_events_1_server"] = _run(
                f"get_events({first_server})",
                lambda: ops.get_events(first_server, max_events=50),
            )
        results[pass_name] = row

    out_path = ROOT / "scripts" / "measure_live_results.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
