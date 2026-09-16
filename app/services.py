from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .collectors.base import Collector
from .db import Database


# LSF counts jobs suspended while pending (PSUSP) in queue PEND totals. Treat
# both states as waiting for dashboard filters and aggregate counters, while
# preserving the original status in each job row.
PENDING_STATUSES = frozenset({"PEND", "PSUSP"})


class CollectionService:
    def __init__(
        self,
        db: Database,
        collector: Collector,
        cluster: str,
        retention_days: int = 7,
        max_db_size_mb: int = 1024,
    ):
        self.db = db
        self.collector = collector
        self.cluster = cluster
        self.retention_days = retention_days
        self.max_db_size_mb = max_db_size_mb
        self._run_lock = threading.Lock()

    def run(self) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "error": "collection already in progress", "duration_ms": 0}
        started = time.monotonic()
        collected_at = datetime.now(timezone.utc).isoformat()
        try:
            payload = self.collector.collect()
            # Persist the long LSF report only for failures. DONE/EXIT rows
            # themselves are both retained for seven days, but collecting
            # ``bjobs -l`` for every successful job makes polling expensive.
            terminal_jobs = [
                row for row in payload.get("jobs", [])
                if str(row.get("status", "")).upper() == "EXIT"
            ]
            detail_reader = getattr(self.collector, "job_detail", None)
            if terminal_jobs and callable(detail_reader):
                missing_ids = self.db.terminal_job_ids_needing_detail(
                    self.cluster, (str(row.get("job_id", "")) for row in terminal_jobs)
                )
                for row in terminal_jobs:
                    job_id = str(row.get("job_id", ""))
                    if job_id not in missing_ids:
                        continue
                    try:
                        detail = detail_reader(job_id)
                        if detail.strip():
                            row["detail"] = detail
                    except Exception:
                        # Terminal-detail enrichment must never turn a healthy
                        # scheduler snapshot into a failed collection.  A later
                        # snapshot retries while LSF still lists the job.
                        pass
            duration_ms = int((time.monotonic() - started) * 1000)
            warnings = list(payload.pop("_warnings", []))
            snapshot_id = self.db.save_snapshot(self.cluster, collected_at, payload, duration_ms, warnings)
            cleanup: dict[str, Any]
            try:
                cleanup = self.db.cleanup(
                    self.cluster,
                    retention_days=self.retention_days,
                    max_size_mb=self.max_db_size_mb,
                )
            except Exception as cleanup_error:  # cleanup must not hide a healthy collection
                cleanup = {"deleted_snapshots": 0, "error": str(cleanup_error)}
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "duration_ms": duration_ms,
                "warnings": warnings,
                "cleanup": cleanup,
                "snapshot": {
                    "id": snapshot_id, "cluster": self.cluster, "collected_at": collected_at,
                    "status": "partial" if warnings else "ok", "duration_ms": duration_ms,
                    "error": "; ".join(warnings)[:1000],
                },
                # Internal hand-off to Runtime.  Runtime removes this before
                # returning the result to the HTTP caller.
                "_payload": payload,
            }
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            # Recording an error must not itself kill the collection loop when
            # the database is the failed dependency.
            try:
                self.db.save_failure(self.cluster, collected_at, str(exc), duration_ms)
            except Exception:
                pass
            return {"ok": False, "error": str(exc), "duration_ms": duration_ms}
        finally:
            self._run_lock.release()


def build_summary_from_rows(
    cluster: str, jobs: list[dict[str, Any]], queues: list[dict[str, Any]],
    hosts: list[dict[str, Any]], licenses: list[dict[str, Any]], snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    running_jobs = [job for job in jobs if job["status"] == "RUN"]
    queue_running_slots = sum(queue["running"] for queue in queues)
    queue_max_slots = sum(queue["max_slots"] for queue in queues)
    host_running_slots = sum(host["running_slots"] for host in hosts)
    host_max_slots = sum(host["max_slots"] for host in hosts)
    # Login and utility hosts (for example lg*) are reported by LSF but have
    # no schedulable slots.  Exclude them from cluster resource utilization so
    # their idle CPU and memory do not dilute the compute-node figures.
    compute_hosts = [host for host in hosts if float(host.get("max_slots", 0) or 0) > 0]
    total_mem_mb = sum(max(0, float(host.get("total_mem_mb", 0) or 0)) for host in compute_hosts)
    free_mem_mb = sum(max(0, float(host.get("free_mem_mb", 0) or 0)) for host in compute_hosts)
    memory_pct = round(max(0, min(100, (total_mem_mb - free_mem_mb) / total_mem_mb * 100)), 1) if total_mem_mb else (
        round(sum(host["mem_pct"] for host in compute_hosts if host["mem_pct"] >= 0) / len([host for host in compute_hosts if host["mem_pct"] >= 0]), 1)
        if any(host["mem_pct"] >= 0 for host in compute_hosts) else None
    )
    # An LSF queue MAX of "-" means unlimited and is stored as zero. In that
    # case, hosts are the authoritative physical capacity for the dashboard.
    running_slots = queue_running_slots if queues else host_running_slots
    max_slots = queue_max_slots or host_max_slots
    return {
        "cluster": cluster,
        "snapshot": snapshot,
        "totals": {
            "jobs": len(jobs), "running_jobs": len(running_jobs),
            "pending_jobs": sum(1 for job in jobs if job["status"].upper() in PENDING_STATUSES),
            "exit_jobs": sum(1 for job in jobs if job["status"] == "EXIT"),
            "hosts": len(hosts), "unavailable_hosts": sum(1 for host in hosts if host["status"].lower() not in {"ok", "closed_full"}),
            "running_slots": running_slots,
            "max_slots": max_slots,
            "total_mem_mb": round(total_mem_mb, 1),
            "free_mem_mb": round(free_mem_mb, 1),
            "license_risks": sum(1 for item in licenses if item["status"] in {"warning", "critical"}),
        },
        "efficiency": {
            "cpu_pct": round(sum(host["cpu_pct"] for host in compute_hosts) / len(compute_hosts), 1) if compute_hosts else 0,
            "mem_pct": memory_pct,
            "slot_pct": round(running_slots / max(1, max_slots) * 100, 1),
        },
    }


def build_summary(db: Database, cluster: str) -> dict[str, Any]:
    return build_summary_from_rows(
        cluster,
        db.latest_rows("jobs", cluster), db.latest_rows("queues", cluster),
        db.latest_rows("hosts", cluster), db.latest_rows("licenses", cluster),
        db.snapshot_status(cluster),
    )


def build_alerts_from_rows(
    hosts: list[dict[str, Any]], queues: list[dict[str, Any]], licenses: list[dict[str, Any]],
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for host in hosts:
        if host["status"].lower() not in {"ok", "closed_full"}:
            alerts.append({"severity": "critical", "source": host["name"], "message": f"Host status is {host['status']}"})
        elif host["mem_pct"] >= 90:
            alerts.append({"severity": "warning", "source": host["name"], "message": f"Memory usage is {host['mem_pct']}%"})
    for queue in queues:
        if queue["pending"] >= 20:
            alerts.append({"severity": "warning", "source": queue["name"], "message": f"{queue['pending']} jobs are pending"})
    for item in licenses:
        if item.get("feature") == "License Server":
            if item.get("status") != "ok":
                alerts.append({
                    "severity": "critical",
                    "source": item.get("server", "License Server"),
                    "message": item.get("expires_at", "License server status is unavailable"),
                })
            continue
        ratio = item["used"] / item["total"] if item["total"] else 0
        if ratio >= 0.95:
            alerts.append({"severity": "critical", "source": item["feature"], "message": f"License usage is {ratio:.0%}"})
        elif ratio >= 0.85:
            alerts.append({"severity": "warning", "source": item["feature"], "message": f"License usage is {ratio:.0%}"})
    return alerts


def build_alerts(db: Database, cluster: str) -> list[dict[str, str]]:
    return build_alerts_from_rows(
        db.latest_rows("hosts", cluster), db.latest_rows("queues", cluster), db.latest_rows("licenses", cluster),
    )


SLA_COMPONENTS = (
    ("collection", "采集链路", ()),
    ("jobs", "LSF 作业", ("bjobs",)),
    ("queues", "LSF 队列", ("bqueues",)),
    ("hosts", "LSF 节点", ("bhosts", "lsload")),
    ("licenses", "FlexNet License", ("lmstat",)),
)


def _component_available(snapshot: dict[str, Any], component: str, markers: tuple[str, ...]) -> bool:
    """Determine availability from the saved collector outcome without inventing probes."""
    status = str(snapshot.get("status", "")).lower()
    error = str(snapshot.get("error", "")).lower()
    # Versions before the lmstat symlink fix invoked lmutil itself and wrote
    # this synthetic warning. Preserve it in SQLite for audit, but do not let
    # it count as a real License outage in the dashboard SLA.
    if component == "licenses" and "lmstat did not report license server up" in error:
        return True
    if component == "collection":
        return status == "ok"
    if status == "ok":
        return True
    if markers and any(marker in error for marker in markers):
        return False
    # A partial snapshot preserves healthy LSF data when only another collector failed.
    return status == "partial" and component != "licenses"


def build_sla_from_snapshots(
    snapshots: list[dict[str, Any]], collect_interval_seconds: int,
    window_hours: int = 24,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a transparent snapshot-based SLA view for the dashboard."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    expected = max(1, int(window_hours * 3600 / max(1, collect_interval_seconds)))
    observed = len(snapshots)
    coverage_pct = round(min(100, observed / expected * 100), 1)
    components = []
    for key, title, markers in SLA_COMPONENTS:
        good = sum(_component_available(snapshot, key, markers) for snapshot in snapshots)
        availability = round(good / observed * 100, 2) if observed else None
        components.append({
            "key": key,
            "title": title,
            "availability_pct": availability,
            "good_samples": good,
            "observed_samples": observed,
            "status": "unknown" if not observed else "ok" if availability == 100 else "degraded",
        })
    return {
        "window_hours": window_hours,
        "expected_samples": expected,
        "observed_samples": observed,
        "coverage_pct": coverage_pct,
        "partial_samples": sum(snapshot["status"] == "partial" for snapshot in snapshots),
        "failed_samples": sum(snapshot["status"] == "error" for snapshot in snapshots),
        "components": components,
        "timeline": [
            {"collected_at": snapshot["collected_at"], "status": snapshot["status"], "error": snapshot["error"][:120]}
            for snapshot in snapshots[-24:]
        ],
    }


def build_sla(
    db: Database, cluster: str, collect_interval_seconds: int,
    window_hours: int = 24, now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return build_sla_from_snapshots(
        db.sla_snapshots(cluster, (now - timedelta(hours=window_hours)).isoformat()),
        collect_interval_seconds, window_hours=window_hours, now=now,
    )
