from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .collectors.base import Collector
from .db import Database


class CollectionService:
    def __init__(self, db: Database, collector: Collector, cluster: str):
        self.db = db
        self.collector = collector
        self.cluster = cluster

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        collected_at = datetime.now(timezone.utc).isoformat()
        try:
            payload = self.collector.collect()
            duration_ms = int((time.monotonic() - started) * 1000)
            snapshot_id = self.db.save_snapshot(self.cluster, collected_at, payload, duration_ms)
            return {"ok": True, "snapshot_id": snapshot_id, "duration_ms": duration_ms}
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.db.save_failure(self.cluster, collected_at, str(exc), duration_ms)
            return {"ok": False, "error": str(exc), "duration_ms": duration_ms}


def build_summary(db: Database, cluster: str) -> dict[str, Any]:
    jobs = db.latest_rows("jobs", cluster)
    queues = db.latest_rows("queues", cluster)
    hosts = db.latest_rows("hosts", cluster)
    licenses = db.latest_rows("licenses", cluster)
    running_jobs = [job for job in jobs if job["status"] == "RUN"]
    waste_jobs = [job for job in running_jobs if job["requested_mem_mb"] and job["used_mem_mb"] / job["requested_mem_mb"] < 0.4]
    return {
        "cluster": cluster,
        "snapshot": db.snapshot_status(cluster),
        "totals": {
            "jobs": len(jobs), "running_jobs": len(running_jobs),
            "pending_jobs": sum(1 for job in jobs if job["status"] == "PEND"),
            "exit_jobs": sum(1 for job in jobs if job["status"] == "EXIT"),
            "hosts": len(hosts), "unavailable_hosts": sum(1 for host in hosts if host["status"].lower() not in {"ok", "closed_full"}),
            "running_slots": sum(queue["running"] for queue in queues),
            "max_slots": sum(queue["max_slots"] for queue in queues),
            "memory_waste_jobs": len(waste_jobs),
            "license_risks": sum(1 for item in licenses if item["status"] != "ok"),
        },
        "efficiency": {
            "cpu_pct": round(sum(host["cpu_pct"] for host in hosts) / len(hosts), 1) if hosts else 0,
            "mem_pct": round(sum(host["mem_pct"] for host in hosts) / len(hosts), 1) if hosts else 0,
            "slot_pct": round(sum(queue["running"] for queue in queues) / max(1, sum(queue["max_slots"] for queue in queues)) * 100, 1),
        },
    }


def build_alerts(db: Database, cluster: str) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for host in db.latest_rows("hosts", cluster):
        if host["status"].lower() not in {"ok", "closed_full"}:
            alerts.append({"severity": "critical", "source": host["name"], "message": f"Host status is {host['status']}"})
        elif host["mem_pct"] >= 90:
            alerts.append({"severity": "warning", "source": host["name"], "message": f"Memory usage is {host['mem_pct']}%"})
    for queue in db.latest_rows("queues", cluster):
        if queue["pending"] >= 20:
            alerts.append({"severity": "warning", "source": queue["name"], "message": f"{queue['pending']} jobs are pending"})
    for item in db.latest_rows("licenses", cluster):
        ratio = item["used"] / item["total"] if item["total"] else 0
        if ratio >= 0.95:
            alerts.append({"severity": "critical", "source": item["feature"], "message": f"License usage is {ratio:.0%}"})
        elif ratio >= 0.85:
            alerts.append({"severity": "warning", "source": item["feature"], "message": f"License usage is {ratio:.0%}"})
    return alerts

