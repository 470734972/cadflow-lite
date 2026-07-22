from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta, timezone

from .base import Collector


class DemoCollector(Collector):
    """Deterministic-looking data for evaluation without an LSF installation."""

    def __init__(self, cluster: str):
        self.cluster = cluster
        self._round = 0

    def collect(self) -> dict[str, list[dict]]:
        self._round += 1
        rng = random.Random(20260719 + self._round)
        wave = math.sin(time.time() / 300)
        hosts = []
        for index in range(1, 9):
            max_slots = 64
            running = rng.randint(22, 62)
            status = "closed" if index == 8 and self._round % 5 == 0 else "ok"
            hosts.append({
                "name": f"compute{index:02d}", "status": status, "max_slots": max_slots,
                "running_slots": 0 if status != "ok" else running,
                "cpu_pct": 0 if status != "ok" else round(min(99, running / max_slots * 100 + rng.uniform(-5, 8)), 1),
                "mem_pct": 0 if status != "ok" else round(rng.uniform(48, 91), 1),
                "load_1m": 0 if status != "ok" else round(rng.uniform(12, 62), 2),
                "load_15m": 0 if status != "ok" else round(rng.uniform(18, 68), 2),
                "free_mem_mb": 0 if status != "ok" else round(rng.uniform(8192, 131072)),
                "free_tmp_mb": 0 if status != "ok" else round(rng.uniform(10240, 204800)),
                "free_swap_mb": 0 if status != "ok" else round(rng.uniform(4096, 65536)),
            })
        queues = [
            {"name": "normal", "status": "Open:Active", "max_slots": 256, "running": 218, "pending": 31, "suspended": 2},
            {"name": "large_mem", "status": "Open:Active", "max_slots": 96, "running": 72, "pending": 14, "suspended": 0},
            {"name": "interactive", "status": "Open:Active", "max_slots": 64, "running": 39, "pending": 3, "suspended": 0},
            {"name": "tapeout", "status": "Open:Active", "max_slots": 128, "running": 121, "pending": 22, "suspended": 1},
        ]
        queues[0]["pending"] += max(0, int(wave * 8))
        statuses = ["RUN", "RUN", "RUN", "PEND", "EXIT", "DONE"]
        users = ["alice", "bob", "chen", "david", "emma", "frank"]
        projects = ["orion", "nebula", "phoenix"]
        job_names = ["vcs_compile_top", "innovus_route", "dc_synthesis", "calibre_drc", "simulation_regression"]
        jobs = []
        for index in range(36):
            status = rng.choice(statuses)
            requested = rng.choice([8192, 16384, 32768, 65536, 131072])
            used = round(requested * rng.uniform(0.18, 1.18), 1)
            jobs.append({
                "job_id": str(840100 + index), "user": rng.choice(users), "status": status,
                "queue": rng.choice([q["name"] for q in queues]),
                "submit_host": rng.choice(["login01", "login02", "cad01"]),
                "exec_host": "-" if status == "PEND" else rng.choice(hosts)["name"],
                "job_name": rng.choice(job_names) + f"_{index:02d}",
                "submit_time": (datetime.now(timezone.utc) - timedelta(minutes=rng.randint(1, 2880))).isoformat(),
                "slots": rng.choice([1, 4, 8, 16]), "requested_mem_mb": requested,
                "used_mem_mb": used, "cpu_efficiency": round(rng.uniform(0.12, 0.98), 3),
                "runtime_seconds": rng.randint(180, 172800),
                "pending_reason": rng.choice(["No eligible host", "Queue slot limit", "Waiting for license", ""]) if status == "PEND" else "",
                "project": rng.choice(projects),
            })
        licenses = [
            {"server": "27000@license01", "vendor": "snpslmd", "feature": "VCS", "total": 120, "used": 108, "expires_at": "2027-03-31", "status": "warning"},
            {"server": "27000@license01", "vendor": "snpslmd", "feature": "DC-ULTRA", "total": 80, "used": 54, "expires_at": "2027-03-31", "status": "ok"},
            {"server": "27001@license02", "vendor": "cdslmd", "feature": "Virtuoso", "total": 160, "used": 147, "expires_at": "2026-12-31", "status": "critical"},
            {"server": "27001@license02", "vendor": "cdslmd", "feature": "Innovus", "total": 96, "used": 67, "expires_at": "2026-12-31", "status": "ok"},
        ]
        return {"jobs": jobs, "queues": queues, "hosts": hosts, "licenses": licenses}
