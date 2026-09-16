from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import signal
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

from .base import Collector


class CommandError(RuntimeError):
    pass


class ParseError(ValueError):
    pass


class SafeRunner:
    """Run only the fixed, read-only commands used by the LSF collector."""

    ALLOWED = {"bjobs", "bqueues", "bhosts", "bmgroup", "lshosts", "lsload", "lmstat"}

    def __init__(self, timeout: int = 20, lsf_bin_dir: Optional[Path] = None, lmstat_path: Optional[Path] = None, extra_env: Optional[Mapping[str, str]] = None):
        self.timeout = timeout
        self.lsf_bin_dir = lsf_bin_dir.resolve() if lsf_bin_dir else None
        # FlexNet commonly exposes ``lmstat`` as a symbolic link to ``lmutil``.
        # lmutil dispatches from argv[0], so resolving this link changes the
        # invoked program name to ``lmutil`` and prints usage text instead of
        # running the lmstat subcommand.  Preserve the configured link.
        self.lmstat_path = lmstat_path.absolute() if lmstat_path else None
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
        name = Path(argv[0]).name
        executable = self._resolve(argv[0])
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        if name == "lmstat":
            # LSF and FlexNet can ship incompatible shared libraries.  The
            # saved LSF environment is needed for bjobs/bhosts, but must not
            # leak into a separately configured lmstat binary.
            env = {key: value for key, value in env.items() if not key.startswith("LSF_") and key != "LD_LIBRARY_PATH"}
            env["LC_ALL"] = "C"
            env["LANG"] = "C"
        else:
            env.update(self.extra_env)
        try:
            process = subprocess.Popen(
                [executable, *argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=env, start_new_session=(os.name == "posix"),
            )
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - production collector runs on Linux
                    process.kill()
                stdout, stderr = process.communicate()
                raise CommandError(f"command timed out after {self.timeout}s: {Path(argv[0]).name}")
        except OSError as exc:
            raise CommandError(str(exc)) from exc
        if process.returncode != 0:
            raise CommandError(stderr.strip() or f"command exited {process.returncode}")
        # FlexNet builds differ in where they write the status report.  The
        # interactive shell shows both streams, while the collector used to
        # parse stdout only; preserve both for lmstat so a valid server report
        # written to stderr is not mistaken for an empty/unknown result.
        if name == "lmstat" and stderr.strip():
            stdout = f"{stdout}\n{stderr}" if stdout.strip() else stderr
        if len(stdout) > 10_000_000:
            raise CommandError(f"command output exceeds 10 MB: {Path(argv[0]).name}")
        return stdout


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


def parse_pipe_table(text: str, columns: list[str], embedded_delimiter_index: Optional[int] = None) -> list[dict[str, str]]:
    """Parse a pipe table, optionally allowing delimiters in one field.

    LSF job names are user-controlled shell commands and may contain literal
    pipes (for example ``cmd 2>&1 | tee build.log``).  When the caller knows
    which column carries that value, split the fixed prefix from the fixed
    suffix so the embedded delimiters stay inside the field.
    """
    rows: list[dict[str, str]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith(("No unfinished job", "No matching job", "No job found")):
            continue
        if embedded_delimiter_index is None:
            values = [value.strip() for value in raw.split("|")]
        else:
            suffix_count = len(columns) - embedded_delimiter_index - 1
            if not 0 <= embedded_delimiter_index < len(columns) or suffix_count < 0:
                raise ParseError("embedded delimiter column is outside the table")
            prefix = raw.split("|", embedded_delimiter_index)
            if len(prefix) != embedded_delimiter_index + 1:
                raise ParseError(f"expected {len(columns)} columns, received {len(prefix)} in: {raw[:200]}")
            suffix = prefix.pop().rsplit("|", suffix_count)
            values = prefix + suffix
            values = [value.strip() for value in values]
        if len(values) != len(columns):
            raise ParseError(f"expected {len(columns)} columns, received {len(values)} in: {raw[:200]}")
        rows.append(dict(zip(columns, values)))
    return rows


def parse_pending_reasons(text: str) -> dict[str, str]:
    """Extract PEND/PSUSP explanations from the portable ``bjobs -p`` output.

    Legacy LSF prints a pending job row followed by one or more indented reason
    lines (for example, a per-user slot limit).  Newer clients may use the
    detailed ``bjobs -l`` spelling instead, so labelled ``PENDING REASONS``
    blocks are accepted as well.  Unknown or unrelated lines are ignored.
    """
    reasons: dict[str, list[str]] = {}
    current: Optional[str] = None
    detailed = False
    capture_detail = False

    def add_reason(job_id: str, value: str) -> None:
        value = re.sub(r"^\s*(?:pending\s+reasons?|reason)\s*:\s*", "", value, flags=re.I).strip()
        if not value or value in {"-", "N/A"}:
            return
        bucket = reasons.setdefault(job_id, [])
        if value not in bucket:
            bucket.append(value)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            current = None
            detailed = False
            capture_detail = False
            continue
        if line.upper().startswith(("JOBID ", "NO UNFINISHED JOB", "NO MATCHING JOB", "NO JOB FOUND")):
            continue

        detail_match = re.match(r"^Job\s+<(?P<job_id>\d+)>", line, re.I)
        if detail_match:
            current = detail_match.group("job_id")
            detailed = True
            capture_detail = False
            continue

        row_match = re.match(r"^(?P<job_id>\d+)\s+\S+\s+(?P<status>PEND|PSUSP)\b", line, re.I)
        if row_match:
            current = row_match.group("job_id")
            detailed = False
            capture_detail = True
            continue
        if not current:
            continue

        if detailed:
            if re.search(r"pending\s+reasons?\s*:", line, re.I):
                capture_detail = True
                add_reason(current, line)
            elif capture_detail and (line.startswith(">") or re.match(r"^(?:User has reached|Job was suspended|Not enough|Waiting for|No available|The queue|The host|License)", line, re.I)):
                add_reason(current, line.lstrip("> "))
        else:
            add_reason(current, line)

    return {job_id: " ".join(values) for job_id, values in reasons.items() if values}


def parse_whitespace_table(text: str, required: set[str]) -> list[dict[str, str]]:
    """Parse the standard, whitespace-delimited LSF table formats used by 10.1.0.0."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    header_index = next((index for index, values in enumerate(lines) if required.issubset(set(values))), None)
    if header_index is None:
        raise ParseError(f"LSF table is missing required columns: {sorted(required)}")
    headers = lines[header_index]
    rows: list[dict[str, str]] = []
    for values in lines[header_index + 1 :]:
        if len(values) < len(headers):
            continue
        rows.append(dict(zip(headers, values)))
    return rows


def parse_bqueues_hosts(text: str) -> dict[str, str]:
    """Return each queue's configured LSF host expression from ``bqueues -l``."""
    result: dict[str, str] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        queue_match = re.match(r"^QUEUE\s*:\s*(\S+)", line, re.I)
        if queue_match:
            current = queue_match.group(1)
            continue
        hosts_match = re.match(r"^HOSTS?\s*:\s*(.*?)\s*$", line, re.I)
        if current and hosts_match:
            result[current] = hosts_match.group(1).strip()
    return result


def parse_bmgroup_hosts(text: str) -> dict[str, list[str]]:
    """Parse recursively expanded host groups from ``bmgroup -r -w``.

    LSF prints host groups as a two-column whitespace table.  Host groups are
    commonly written with a leading slash (``/gpu``); the normalizer in the
    collector handles both that form and the trailing slash shown by some
    ``bqueues -l`` versions.
    """
    result: dict[str, list[str]] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if re.match(r"^GROUP_NAME\s+HOSTS(?:\s|$)", line, re.I):
            continue
        match = re.match(r"^(\S+)\s+(.*?)\s*$", line)
        if not match:
            continue
        group = match.group(1).strip().strip("/")
        if not group:
            continue
        members = []
        for token in re.split(r"[\s,]+", match.group(2).strip("() ")):
            token = token.strip("{},()")
            if token and token not in {"-", "all", "allremote"}:
                members.append(token.lstrip("+-"))
        if members:
            result[group] = list(dict.fromkeys(members))
    return result


def parse_lsload(text: str) -> dict[str, dict[str, float]]:
    """Return actual LSF load-index values keyed by host.

    The collector normally requests a delimiter-separated field list.  This
    avoids a legacy ``lsload -w`` quirk where a host with an unavailable
    trailing index can produce one fewer whitespace-delimited value.  The
    whitespace parser remains as a compatibility fallback for older LSF
    installations and pads a single missing trailing field instead of
    silently dropping the host.
    """
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    delimiter = "|" if any("|" in line for line in raw_lines) else None
    lines = [line.split(delimiter) if delimiter else line.split() for line in raw_lines]
    lines = [[value.strip() for value in values] for values in lines]
    header_index = next((index for index, values in enumerate(lines) if "HOST_NAME" in values), None)
    if header_index is None:
        raise ParseError("lsload output does not contain HOST_NAME header")
    headers = [value.lower() for value in lines[header_index]]
    required = {"host_name", "r1m", "r15m", "ut", "mem", "tmp", "swp"}
    if not required.issubset(headers):
        raise ParseError(f"lsload header is missing required columns: {sorted(required - set(headers))}")
    result: dict[str, dict[str, float]] = {}
    for values in lines[header_index + 1 :]:
        if len(values) < len(headers):
            # Some older ``lsload -w`` builds omit an unavailable final
            # index instead of printing ``-``.  A second legacy quirk is that
            # the fixed-width ``it`` column can overflow into ``tmp`` (for
            # example ``274810648G`` means ``it=2748`` and ``tmp=10648G``).
            # Repair that known adjacent pair before falling back to an
            # unknown trailing metric, so the later swp/mem values remain
            # aligned.
            if delimiter is None and len(values) == len(headers) - 1:
                values = _repair_lsload_row(values, headers)
            if len(values) < len(headers):
                values = [*values, "-"]
            if len(values) != len(headers):
                continue
        if len(values) > len(headers):
            continue
        row = dict(zip(headers, values))
        host = row["host_name"]
        if host.lower() in {"host_name", "-"}:
            continue
        result[host] = {
            "cpu_pct": _number(row["ut"]),
            "load_1m": _number(row["r1m"]),
            "load_15m": _number(row["r15m"]),
            # LSF reports these as available capacity, in MB after _number conversion.
            "free_mem_mb": _number(row["mem"]),
            "free_tmp_mb": _number(row["tmp"]),
            "free_swap_mb": _number(row["swp"]),
        }
    return result


def _repair_lsload_row(values: list[str], headers: list[str]) -> list[str]:
    """Repair one missing field in a whitespace-delimited ``lsload`` row.

    Legacy LSF clients format ``it`` with a narrow fixed-width column.  When
    the following ``tmp`` value is also wide, the separator disappears and
    the two values become one token (``274810648G``).  The header allocates
    four characters to ``it``; split that token at the legacy boundary only
    when the suffix is a valid storage quantity.  Other short rows are left
    untouched and will expose the missing trailing field as unknown.
    """
    try:
        idle_index = headers.index("it")
        tmp_index = headers.index("tmp")
    except ValueError:
        return values
    if tmp_index != idle_index + 1 or idle_index >= len(values):
        return values
    token = values[idle_index]
    match = re.fullmatch(r"(\d{4})(\d+(?:\.\d+)?[KMGT])", token, re.IGNORECASE)
    if not match:
        return values
    return [*values[:idle_index], match.group(1), match.group(2), *values[idle_index + 1 :]]


def parse_lshosts(text: str) -> dict[str, dict[str, float]]:
    """Return physical host capacity values from ``lshosts -w``.

    ``lshosts`` reports ``maxmem`` as the host's total physical memory while
    ``lsload`` reports the currently available memory.  Header spelling has
    varied slightly between LSF releases, so this parser matches columns
    case-insensitively and ignores extra resource columns.
    """
    lines = [line.split() for line in text.splitlines() if line.strip()]
    header_index = next((index for index, values in enumerate(lines) if {value.lower() for value in values} >= {"host_name", "maxmem"}), None)
    if header_index is None:
        raise ParseError("lshosts output is missing HOST_NAME or maxmem columns")
    headers = [value.lower() for value in lines[header_index]]
    result: dict[str, dict[str, float]] = {}
    for values in lines[header_index + 1 :]:
        if len(values) < len(headers):
            continue
        row = dict(zip(headers, values))
        host = row.get("host_name", "")
        if host.lower() in {"", "host_name", "-"}:
            continue
        result[host] = {"total_mem_mb": _number(row.get("maxmem", ""))}
    return result


def host_data(records: dict[str, dict[str, float]], name: str) -> dict[str, float]:
    """Match LSF records by exact name, then by a unique short/FQDN name."""
    if name in records:
        return records[name]
    normalize = lambda value: value.strip().rstrip(".").lower().split(".", 1)[0]
    target = normalize(name)
    matches = [record for key, record in records.items() if normalize(key) == target]
    return matches[0] if len(matches) == 1 else {}


def parse_lmstat(text: str, server: str, vendor: str = "") -> list[dict[str, Union[str, int]]]:
    features: list[dict[str, Union[str, int]]] = []
    counted_pattern = re.compile(
        r"Users of (?P<feature>[^:]+):.*?Total of (?P<total>\d+) licenses issued;.*?Total of (?P<used>\d+) licenses in use",
        re.I | re.S,
    )
    for match in counted_pattern.finditer(text):
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
    # Node-locked licenses intentionally have no floating pool total. They are
    # still useful inventory data and must not be mistaken for an empty result.
    node_locked_pattern = re.compile(r"Users of (?P<feature>[^:]+):\s*\(Uncounted,\s*node-locked\)", re.I)
    for match in node_locked_pattern.finditer(text):
        features.append({
            "server": server,
            "vendor": vendor,
            "feature": match.group("feature").strip(),
            "total": 0,
            "used": 0,
            "expires_at": "",
            "status": "node_locked",
        })
    return features


def parse_lmstat_server_status(text: str, server: str, vendor: str = "") -> dict[str, Union[str, int]]:
    """Parse only the service header of an ``lmstat`` report.

    Some older FlexNet clients do not include the server line in ``lmstat -s``.
    When that happens the caller retries ``lmstat -a`` but passes this function
    only the text before ``Feature usage info:``, so Feature inventory is never
    parsed or persisted.
    """
    # FlexNet 11.x builds may add terminal color/control bytes when invoked
    # from a service wrapper.  Strip those before matching the human report.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text.replace("\x00", " "))
    text = text.split("Feature usage info:", 1)[0]
    server_up = bool(re.search(r"\blicense\s+server\s+(?:UP|is\s+UP)\b", text, re.I))
    vendor_states = {
        name.lower(): state.upper()
        for name, state in re.findall(r"^\s*([A-Za-z0-9_.-]+):\s*(UP|DOWN)\b", text, re.I | re.M)
    }
    configured_vendors = [name.strip().lower() for name in vendor.split(",") if name.strip()]
    down_vendors = [name for name in configured_vendors if vendor_states.get(name) == "DOWN"]
    status_header = bool(re.search(r"license\s+server(?:\s+status)?\s*(?:status\s*)?[:=]\s*", text, re.I))
    reachable_evidence = bool(re.search(r"license\s+file\s*\(s\)\s+on|vendor\s+daemon\s+status", text, re.I))
    explicit_server_down = bool(re.search(
        r"(?:license\s+server[^\n]*(?:DOWN|not\s+(?:responding|available)|cannot|failed))|"
        r"(?:cannot|unable|failed|error)\s+(?:connect\s+to\s+)?[^\n]{0,80}license\s+server",
        text, re.I,
    ))
    if (server_up or ((status_header or reachable_evidence) and not explicit_server_down)) and not down_vendors:
        detail = "License server UP"
        status = "ok"
    elif down_vendors:
        detail = f"Vendor daemon DOWN: {', '.join(down_vendors)}"
        status = "critical"
    else:
        detail = "lmstat did not report license server UP"
        status = "critical"
    return {
        "server": server,
        "vendor": vendor,
        "feature": "License Server",
        "total": 0,
        "used": 0,
        "expires_at": detail,
        "status": status,
    }


def parse_lmstat_feature_summary(text: str) -> dict[str, Union[str, int]]:
    """Return only aggregate Feature metadata from a full ``lmstat`` report.

    The report itself can contain tens of thousands of Feature entries.  It is
    deliberately discarded after this parser runs: CADFlow retains just the
    count and one expiry string that lmstat explicitly reports.
    """
    feature_names = {
        name.strip()
        for name in re.findall(r"^\s*Users of\s+([^:]+):", text, re.I | re.M)
        if name.strip()
    }
    expiry_values: list[str] = []
    for value in re.findall(
        r"\b(?:expires?|expiration(?:\s+date)?)\s*(?:date)?\s*[:=]\s*([^\r\n,;]+)",
        text,
        re.I,
    ):
        normalized = " ".join(value.strip().split())
        if normalized and normalized.lower() not in {"none", "n/a", "unknown"}:
            expiry_values.append(normalized)
    # lmstat commonly omits expiry information entirely.  Say so explicitly
    # rather than guessing from a license-file path or expanding Feature rows.
    expiry_summary = expiry_values[0] if expiry_values else "lmstat 未报告到期时间"
    return {"feature_count": len(feature_names), "expiry_summary": expiry_summary}


class LsfCollector(Collector):
    """Collector for an existing, authorised IBM Spectrum LSF client installation."""

    def __init__(
        self,
        timeout: int,
        lmstat_path: str,
        license_servers: tuple[str, ...],
        lsf_bin_dir: str = "",
        license_vendor: str = "",
        lsf_env: Optional[Mapping[str, str]] = None,
        runner: Optional[SafeRunner] = None,
        license_sources: Optional[Sequence[Mapping[str, str]]] = None,
        license_sample_interval_seconds: int = 3600,
    ):
        self.runner = runner or SafeRunner(
            timeout,
            Path(lsf_bin_dir) if lsf_bin_dir else None,
            Path(lmstat_path) if lmstat_path else None,
            lsf_env,
        )
        self.license_servers = license_servers
        self.license_vendor = license_vendor
        self.license_sources = tuple(license_sources or (
            {"server": server, "vendor": license_vendor} for server in license_servers
        ))
        self.license_sample_interval_seconds = license_sample_interval_seconds
        self._license_cache: tuple[list[dict], list[str]] = ([], [])
        self._last_license_sample = 0.0

    def preflight(self) -> dict[str, str]:
        return self.runner.check_available(["bjobs", "bqueues", "bhosts", "lsload"])

    def collect(self) -> dict[str, object]:
        self.preflight()
        hosts = self._hosts()
        payload: dict[str, object] = {"jobs": self._jobs(), "queues": self._queues([host["name"] for host in hosts]), "hosts": hosts, "licenses": []}
        if self.license_servers:
            try:
                self.runner.check_available(["lmstat"])
                license_rows, license_warnings = self._licenses()
                payload["licenses"] = license_rows
                if license_warnings:
                    payload["_warnings"] = license_warnings
            except CommandError as exc:
                # License availability must not hide otherwise healthy LSF capacity data.
                payload["_warnings"] = [f"FlexNet License: {exc}"]
        return payload

    def _jobs(self) -> list[dict]:
        format_args = [
            "-noheader", "-o",
            "jobid user stat queue from_host exec_host job_name submit_time slots max_mem run_time proj_name delimiter='|'",
        ]
        columns = ["job_id", "user", "status", "queue", "submit_host", "exec_host", "job_name", "submit_time", "slots", "max_mem", "runtime", "project"]
        output = self.runner.run(["bjobs", "-u", "all", "-a", *format_args])

        def parse_output(text: str) -> list[dict[str, str]]:
            return parse_pipe_table(text, columns, embedded_delimiter_index=6)

        parsed_rows = parse_output(output)
        # Some older LSF installations do not include pending jobs in the
        # custom ``bjobs -a -o`` result even though ``bjobs -p -u all`` does.
        # Only issue the extra read-only query when the full result has no
        # PEND/PSUSP row, and merge by Job ID so a job is never shown twice.
        if not any(row["status"].upper() in {"PEND", "PSUSP"} for row in parsed_rows):
            try:
                # Use the portable ``-p`` form.  Some LSF 10.x clients accept
                # ``-p0`` as a different option and silently omit pending jobs.
                pending_output = self.runner.run(["bjobs", "-p", "-u", "all", *format_args])
                parsed_rows.extend(parse_output(pending_output))
            except (CommandError, ParseError):
                pass

        pending_reasons: dict[str, str] = {}
        if any(row["status"].upper() in {"PEND", "PSUSP"} for row in parsed_rows):
            try:
                # The plain portable form is supported by legacy LSF 10.1 and
                # includes the human-readable reason lines after each row.
                pending_reasons = parse_pending_reasons(self.runner.run(["bjobs", "-p", "-u", "all"]))
            except (CommandError, ParseError):
                # Reasons are enrichment only; a transient/unsupported reason
                # query must not make an otherwise valid collection fail.
                pass

        jobs: dict[str, dict] = {}
        for row in parsed_rows:
            jobs[row["job_id"]] = {
                "job_id": row["job_id"], "user": row["user"], "status": row["status"], "queue": row["queue"],
                "submit_host": row["submit_host"] or "-", "exec_host": row["exec_host"] or "-",
                "job_name": row["job_name"], "submit_time": row["submit_time"], "slots": max(1, int(_number(row["slots"], 1))),
                # LSF max_mem is measured usage, not an rusage[mem] request. Keep request unknown rather than invent it.
                "requested_mem_mb": 0, "used_mem_mb": _number(row["max_mem"]), "cpu_efficiency": 0,
                "runtime_seconds": parse_duration_seconds(row["runtime"]),
                "pending_reason": pending_reasons.get(row["job_id"], "") if row["status"].upper() in {"PEND", "PSUSP"} else "",
                "project": row["project"],
            }
        return list(jobs.values())

    def _queues(self, host_names: Optional[Sequence[str]] = None) -> list[dict]:
        # LSF 10.1.0.0 does not support bqueues -o or -noheader. The standard
        # wide table is stable across the legacy and current command variants.
        parsed = parse_whitespace_table(self.runner.run(["bqueues", "-w"]), {"QUEUE_NAME", "STATUS", "MAX", "PEND", "RUN", "SUSP"})
        try:
            host_specs = parse_bqueues_hosts(self.runner.run(["bqueues", "-l"]))
        except (CommandError, ParseError):
            # Queue host membership is optional enrichment. Older/site-specific
            # clients may not expose the long queue description.
            host_specs = {}
        try:
            group_hosts = parse_bmgroup_hosts(self.runner.run(["bmgroup", "-r", "-w"]))
        except (CommandError, ParseError):
            # Host-group expansion is optional. Keep the configured group name
            # visible when bmgroup is unavailable instead of inventing hosts.
            group_hosts = {}

        def queue_hosts(name: str) -> list[str]:
            spec = host_specs.get(name, "").strip()
            if not spec:
                return []
            tokens = [token.strip("{},") for token in re.split(r"[\s,]+", spec) if token.strip("{},")]
            if any(token.lower() in {"all", "allhosts"} for token in tokens):
                return list(host_names or [])
            expanded: list[str] = []
            for token in tokens:
                group_key = token.strip("/@")
                members = group_hosts.get(group_key)
                if members:
                    expanded.extend(members)
                else:
                    expanded.append(token)
            return list(dict.fromkeys(expanded))

        return [{
            "name": row["QUEUE_NAME"], "status": row["STATUS"], "max_slots": int(_number(row["MAX"])),
            # JL/U is the maximum number of job slots one user can consume
            # in this queue; a dash means no per-user limit.
            "per_user_slots": int(_number(row.get("JL/U", ""))),
            "per_processor_slots": _number(row.get("JL/P", "")),
            "per_host_slots": _number(row.get("JL/H", "")),
            "running": int(_number(row["RUN"])), "pending": int(_number(row["PEND"])), "suspended": int(_number(row["SUSP"])),
            "host_names": json.dumps(queue_hosts(row["QUEUE_NAME"]), ensure_ascii=False),
        } for row in parsed]

    def _hosts(self) -> list[dict]:
        # Use the legacy-compatible standard table for the same reason as bqueues.
        hosts_output = self.runner.run(["bhosts", "-w"])
        # Request only the fields used by CADFlow with an explicit delimiter.
        # Unlike ``-w``, this preserves empty/unknown trailing fields and
        # avoids variable-width rows on hosts such as lg12.  Fall back to the
        # legacy wide table for older LSF clients that do not support ``-o``.
        try:
            load_output = self.runner.run(["lsload", "-o", "HOST_NAME status r1m r15m ut tmp swp mem delimiter='|'"])
        except CommandError:
            load_output = self.runner.run(["lsload", "-w"])
        loads = parse_lsload(load_output)
        try:
            capacities = parse_lshosts(self.runner.run(["lshosts", "-w"]))
        except (CommandError, KeyError, ParseError):
            # Keep LSF collection usable on installations that do not expose
            # lshosts; the UI will explicitly show that total memory is unknown.
            capacities = {}
        parsed = parse_whitespace_table(hosts_output, {"HOST_NAME", "STATUS", "MAX", "RUN"})
        hosts = []
        for row in parsed:
            name = row["HOST_NAME"]
            load = host_data(loads, name)
            total_mem_mb = host_data(capacities, name).get("total_mem_mb", 0)
            free_mem_mb = load.get("free_mem_mb", 0)
            hosts.append({
                "name": name, "status": row["STATUS"], "max_slots": int(_number(row["MAX"])),
                "running_slots": int(_number(row["RUN"])), "cpu_pct": load.get("cpu_pct", 0),
                # lshosts reports total memory; lsload reports currently available memory.
                "total_mem_mb": total_mem_mb,
                "mem_pct": round((1 - free_mem_mb / total_mem_mb) * 100, 1) if total_mem_mb > 0 else -1,
                "load_1m": load.get("load_1m", 0), "load_15m": load.get("load_15m", 0),
                "free_mem_mb": free_mem_mb, "free_tmp_mb": load.get("free_tmp_mb", 0),
                "free_swap_mb": load.get("free_swap_mb", 0),
            })
        return hosts

    def _licenses(self) -> tuple[list[dict], list[str]]:
        """Collect one bounded service-health row per configured license server."""
        now = __import__("time").monotonic()
        if self._license_cache[0] and now - self._last_license_sample < self.license_sample_interval_seconds:
            return self._license_cache
        rows: list[dict] = []
        warnings: list[str] = []
        for source in self.license_sources:
            server = str(source.get("server", "")).strip()
            vendor = str(source.get("vendor", "")).strip()
            try:
                # Service health remains the source of truth.  The additional
                # report is parsed into two aggregate values only, then thrown
                # away; no Feature detail is stored in SQLite or sent to UI.
                output = self.runner.run(["lmstat", "-s", "-c", server])
                row = parse_lmstat_server_status(output, server, vendor)
                if row["status"] == "ok":
                    try:
                        row.update(parse_lmstat_feature_summary(self.runner.run(["lmstat", "-a", "-c", server])))
                    except CommandError:
                        row.update({"feature_count": -1, "expiry_summary": "Feature 摘要未获取"})
                else:
                    row.update({"feature_count": -1, "expiry_summary": "Feature 摘要未获取"})
            except CommandError as exc:
                row = {
                    "server": server, "vendor": vendor, "feature": "License Server",
                    "total": 0, "used": 0, "expires_at": str(exc), "status": "critical",
                    "feature_count": -1, "expiry_summary": "Feature 摘要未获取",
                }
            rows.append(row)
            if row["status"] != "ok":
                warnings.append(f"FlexNet License: lmstat status check for {server}: {row['expires_at']}")
        self._license_cache = (rows, warnings)
        self._last_license_sample = now
        return self._license_cache
