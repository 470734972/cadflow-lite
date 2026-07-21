from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .base import Collector


class CommandError(RuntimeError):
    pass


class ParseError(ValueError):
    pass


class SafeRunner:
    """Run only the fixed, read-only commands used by the LSF collector."""

    ALLOWED = {"bjobs", "bqueues", "bhosts", "lsload", "lmstat"}

    def __init__(self, timeout: int = 20, lsf_bin_dir: Path | None = None, lmstat_path: Path | None = None, extra_env: Mapping[str, str] | None = None):
        self.timeout = timeout
        self.lsf_bin_dir = lsf_bin_dir.resolve() if lsf_bin_dir else None
        self.lmstat_path = lmstat_path.resolve() if lmstat_path else None
        self.extra_env = dict(extra_env or {})

    def _resolve(self, command: str) -> str:
        name = Path(command).name
        if name not in self.ALLOWED:
            raise CommandError("command is not in the read-only allowlist")
        if name == "lmstat" and self.lmstat_path:
            candidate = self.lmstat_path
        elif self.lsf_bin_dir:
            candidate = self.lsf_bin_dir / name
        else:
            discovered = shutil.which(name)
            if not discovered:
                raise CommandError(f"required command is not available: {name}")
            candidate = Path(discovered)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise CommandError(f"required command is not executable: {candidate}")
        return str(candidate)

    def check_available(self, commands: Sequence[str]) -> dict[str, str]:
        return {name: self._resolve(name) for name in commands}

    def run(self, argv: Sequence[str]) -> str:
        if not argv:
            raise CommandError("empty command")
        executable = self._resolve(argv[0])
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env.update(self.extra_env)
        try:
            result = subprocess.run(
                [executable, *argv[1:]], capture_output=True, text=True, timeout=self.timeout, check=False, env=env
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandError(str(exc)) from exc
        if result.returncode != 0:
            raise CommandError(result.stderr.strip() or f"command exited {result.returncode}")
        if len(result.stdout) > 10_000_000:
            raise CommandError(f"command output exceeds 10 MB: {Path(argv[0]).name}")
        return result.stdout


def _number(value: str, default: float = 0) -> float:
    value = value.strip().rstrip("%")
    if value.lower() in {"", "-", "n/a", "unknown", "unlimited"}:
        return default
    match = re.match(r"^([0-9.]+)([KMGT]?)$", value, re.I)
    if not match:
        return default
    number = float(match.group(1))
    factor = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[match.group(2).upper()]
    return number * factor


def parse_duration_seconds(value: str) -> int:
    value = value.strip()
    if value in {"", "-", "N/A"}:
        return 0
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    if len(parts) not in {2, 3, 4} or not all(part.isdigit() for part in parts):
        return 0
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    days, hours, minutes, seconds = numbers
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_pipe_table(text: str, columns: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith(("No unfinished job", "No matching job", "No job found")):
            continue
        values = [value.strip() for value in raw.split("|")]
        if len(values) != len(columns):
            raise ParseError(f"expected {len(columns)} columns, received {len(values)} in: {raw[:200]}")
        rows.append(dict(zip(columns, values)))
    return rows


def parse_lsload(text: str) -> dict[str, dict[str, float]]:
    """Return LSF load-index values keyed by host from `lsload -w` output."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    header_index = next((index for index, values in enumerate(lines) if "HOST_NAME" in values), None)
    if header_index is None:
        raise ParseError("lsload output does not contain HOST_NAME header")
    headers = [value.lower() for value in lines[header_index]]
    required = {"host_name", "r15m", "ut"}
    if not required.issubset(headers):
        raise ParseError(f"lsload header is missing required columns: {sorted(required - set(headers))}")
    result: dict[str, dict[str, float]] = {}
    for values in lines[header_index + 1 :]:
        if len(values) < len(headers):
            continue
        row = dict(zip(headers, values))
        host = row["host_name"]
        if host.lower() in {"host_name", "-"}:
            continue
        result[host] = {"cpu_pct": _number(row["ut"]), "load_15m": _number(row["r15m"])}
    return result


def parse_lmstat(text: str, server: str, vendor: str = "") -> list[dict[str, str | int]]:
    features: list[dict[str, str | int]] = []
    pattern = re.compile(
        r"Users of (?P<feature>[^:]+):.*?Total of (?P<total>\d+) licenses issued;.*?Total of (?P<used>\d+) licenses in use",
        re.I | re.S,
    )
    for match in pattern.finditer(text):
        total, used = int(match.group("total")), int(match.group("used"))
        utilization = used / total if total else 0
        features.append({
            "server": server,
            "vendor": vendor,
            "feature": match.group("feature").strip(),
            "total": total,
            "used": used,
            "expires_at": "",
            "status": "critical" if utilization >= 0.95 else "warning" if utilization >= 0.85 else "ok",
        })
    return features


class LsfCollector(Collector):
    """Collector for an existing, authorised IBM Spectrum LSF client installation."""

    def __init__(
        self,
        timeout: int,
        lmstat_path: str,
        license_servers: tuple[str, ...],
        lsf_bin_dir: str = "",
        license_vendor: str = "",
        lsf_env: Mapping[str, str] | None = None,
        runner: SafeRunner | None = None,
    ):
        self.runner = runner or SafeRunner(
            timeout,
            Path(lsf_bin_dir) if lsf_bin_dir else None,
            Path(lmstat_path) if lmstat_path else None,
            lsf_env,
        )
        self.license_servers = license_servers
        self.license_vendor = license_vendor

    def preflight(self) -> dict[str, str]:
        commands = ["bjobs", "bqueues", "bhosts", "lsload"]
        if self.license_servers:
            commands.append("lmstat")
        return self.runner.check_available(commands)

    def collect(self) -> dict[str, list[dict]]:
        self.preflight()
        return {"jobs": self._jobs(), "queues": self._queues(), "hosts": self._hosts(), "licenses": self._licenses()}

    def _jobs(self) -> list[dict]:
        output = self.runner.run([
            "bjobs", "-u", "all", "-a", "-noheader", "-o",
            "jobid user stat queue exec_host nreq_slot max_mem run_time project_name delimiter='|'",
        ])
        parsed = parse_pipe_table(output, ["job_id", "user", "status", "queue", "exec_host", "slots", "max_mem", "runtime", "project"])
        return [{
            "job_id": row["job_id"], "user": row["user"], "status": row["status"], "queue": row["queue"],
            "exec_host": row["exec_host"] or "-", "slots": max(1, int(_number(row["slots"], 1))),
            # LSF max_mem is measured usage, not an rusage[mem] request. Keep request unknown rather than invent it.
            "requested_mem_mb": 0, "used_mem_mb": _number(row["max_mem"]), "cpu_efficiency": 0,
            "runtime_seconds": parse_duration_seconds(row["runtime"]), "pending_reason": "", "project": row["project"],
        } for row in parsed]

    def _queues(self) -> list[dict]:
        output = self.runner.run(["bqueues", "-noheader", "-o", "queue_name status max run pend susp delimiter='|'"])
        parsed = parse_pipe_table(output, ["name", "status", "max_slots", "running", "pending", "suspended"])
        return [{**row, **{key: int(_number(row[key])) for key in ("max_slots", "running", "pending", "suspended")}} for row in parsed]

    def _hosts(self) -> list[dict]:
        hosts_output = self.runner.run(["bhosts", "-noheader", "-o", "host_name status max njobs delimiter='|'"])
        load_output = self.runner.run(["lsload", "-w"])
        loads = parse_lsload(load_output)
        parsed = parse_pipe_table(hosts_output, ["name", "status", "max_slots", "running_slots"])
        return [{
            **row,
            "max_slots": int(_number(row["max_slots"])),
            "running_slots": int(_number(row["running_slots"])),
            "cpu_pct": loads.get(row["name"], {}).get("cpu_pct", 0),
            # lsload reports free memory, not total memory. Do not derive a fake percentage.
            "mem_pct": 0,
            "load_15m": loads.get(row["name"], {}).get("load_15m", 0),
        } for row in parsed]

    def _licenses(self) -> list[dict]:
        rows: list[dict] = []
        for server in self.license_servers:
            output = self.runner.run(["lmstat", "-a", "-c", server])
            rows.extend(parse_lmstat(output, server, self.license_vendor))
        return rows
