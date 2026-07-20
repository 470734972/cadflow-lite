from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Sequence

from .base import Collector


class CommandError(RuntimeError):
    pass


class SafeRunner:
    ALLOWED = {"bjobs", "bqueues", "bhosts", "lsload", "lmstat"}

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def run(self, argv: Sequence[str]) -> str:
        if not argv or Path(argv[0]).name not in self.ALLOWED:
            raise CommandError("command is not in the read-only allowlist")
        try:
            result = subprocess.run(list(argv), capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandError(str(exc)) from exc
        if result.returncode != 0:
            raise CommandError(result.stderr.strip() or f"command exited {result.returncode}")
        return result.stdout


def _number(value: str, default: float = 0) -> float:
    value = value.strip().rstrip("%")
    match = re.match(r"^([0-9.]+)([KMGT]?)$", value, re.I)
    if not match:
        return default
    number = float(match.group(1))
    factor = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[match.group(2).upper()]
    return number * factor


def parse_pipe_table(text: str, columns: list[str]) -> list[dict]:
    rows = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("No unfinished job"):
            continue
        values = [value.strip() for value in raw.split("|")]
        if len(values) == len(columns):
            rows.append(dict(zip(columns, values)))
    return rows


def parse_lmstat(text: str, server: str) -> list[dict]:
    features = []
    pattern = re.compile(r"Users of ([^:]+):.*?Total of (\d+) licenses issued;.*?Total of (\d+) licenses in use", re.I)
    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            total, used = int(match.group(2)), int(match.group(3))
            utilization = used / total if total else 0
            features.append({
                "server": server, "vendor": "", "feature": match.group(1), "total": total, "used": used,
                "expires_at": "", "status": "critical" if utilization >= 0.95 else "warning" if utilization >= 0.85 else "ok",
            })
    return features


class LsfCollector(Collector):
    def __init__(self, timeout: int, lmstat_path: str, license_servers: tuple[str, ...]):
        self.runner = SafeRunner(timeout)
        self.lmstat_path = lmstat_path
        self.license_servers = license_servers

    def collect(self) -> dict[str, list[dict]]:
        return {
            "jobs": self._jobs(), "queues": self._queues(), "hosts": self._hosts(), "licenses": self._licenses()
        }

    def _jobs(self) -> list[dict]:
        output = self.runner.run(["bjobs", "-u", "all", "-a", "-noheader", "-o", "jobid|user|stat|queue|exec_host|nreq_slot|mem|max_mem|run_time|project_name delimiter='|'"])
        parsed = parse_pipe_table(output, ["job_id", "user", "status", "queue", "exec_host", "slots", "used_mem", "max_mem", "runtime", "project"])
        return [{
            "job_id": row["job_id"], "user": row["user"], "status": row["status"], "queue": row["queue"],
            "exec_host": row["exec_host"], "slots": int(_number(row["slots"], 1)), "requested_mem_mb": 0,
            "used_mem_mb": _number(row["max_mem"] or row["used_mem"]), "cpu_efficiency": 0,
            "runtime_seconds": int(_number(row["runtime"])), "pending_reason": "", "project": row["project"],
        } for row in parsed]

    def _queues(self) -> list[dict]:
        output = self.runner.run(["bqueues", "-noheader", "-o", "queue_name|status|max|run|pend|susp delimiter='|'"])
        parsed = parse_pipe_table(output, ["name", "status", "max_slots", "running", "pending", "suspended"])
        return [{**row, **{key: int(_number(row[key])) for key in ("max_slots", "running", "pending", "suspended")}} for row in parsed]

    def _hosts(self) -> list[dict]:
        output = self.runner.run(["bhosts", "-noheader", "-o", "host_name|status|max|njobs delimiter='|'"])
        parsed = parse_pipe_table(output, ["name", "status", "max_slots", "running_slots"])
        return [{
            **row, "max_slots": int(_number(row["max_slots"])), "running_slots": int(_number(row["running_slots"])),
            "cpu_pct": 0, "mem_pct": 0, "load_15m": 0,
        } for row in parsed]

    def _licenses(self) -> list[dict]:
        rows = []
        for server in self.license_servers:
            output = self.runner.run([self.lmstat_path, "-a", "-c", server])
            rows.extend(parse_lmstat(output, server))
        return rows

