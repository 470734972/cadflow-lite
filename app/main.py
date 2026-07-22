from __future__ import annotations

import asyncio
import hmac
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .collectors import DemoCollector, LsfCollector
from .collectors.base import Collector
from .config import RuntimeConfig, settings
from .db import Database
from .services import CollectionService, build_alerts, build_summary


class SetupCollector(Collector):
    def collect(self) -> dict[str, list[dict[str, Any]]]:
        return {"jobs": [], "queues": [], "hosts": [], "licenses": []}


class Runtime:
    """Hot-loadable, persisted runtime settings for the lab setup wizard."""

    def __init__(self, database: Database, initial: RuntimeConfig):
        self.db = database
        self._lock = threading.RLock()
        self.config = initial
        self.service = self._make_service(initial)

    def load(self) -> None:
        stored = self.db.load_config("runtime")
        if stored:
            self.config = RuntimeConfig.from_dict(stored)
            self.service = self._make_service(self.config)
        else:
            self.db.save_config("runtime", self.config.to_dict())

    def _make_service(self, config: RuntimeConfig) -> CollectionService:
        if config.mode == "demo":
            collector: Collector = DemoCollector(config.cluster_name)
        elif config.mode == "lsf":
            collector = LsfCollector(
                config.command_timeout_seconds,
                config.lmstat_path,
                config.license_servers,
                config.lsf_bin_dir,
                config.license_vendor,
                config.lsf_env,
            )
        else:
            collector = SetupCollector()
        return CollectionService(self.db, collector, config.cluster_name)

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = RuntimeConfig.from_dict(payload)
        candidate_service = self._make_service(candidate)
        # A real LSF configuration is validated before becoming the active configuration.
        if candidate.mode == "lsf":
            result = candidate_service.run()
            if not result["ok"]:
                raise ValueError(result["error"])
        with self._lock:
            self.config = candidate
            self.service = candidate_service
            self.db.save_config("runtime", candidate.to_dict())
        return candidate.to_dict()

    def collect(self) -> dict[str, Any]:
        if self.config.mode == "setup":
            return {"ok": False, "error": "complete the LSF configuration first", "duration_ms": 0}
        return self.service.run()


settings.validate()
db = Database(settings.db_path)
runtime = Runtime(db, RuntimeConfig.from_settings(settings))


async def collection_loop() -> None:
    while True:
        await asyncio.sleep(runtime.config.collect_interval_seconds)
        await asyncio.to_thread(runtime.collect)


def snapshot_freshness(status: dict | None) -> dict[str, int | str]:
    if not status:
        return {"freshness": "never", "age_seconds": -1}
    if status["status"] != "ok":
        return {"freshness": "failed", "age_seconds": -1}
    collected_at = datetime.fromisoformat(status["collected_at"].replace("Z", "+00:00"))
    age_seconds = max(0, int((datetime.now(timezone.utc) - collected_at).total_seconds()))
    return {"freshness": "stale" if age_seconds > runtime.config.effective_stale_after_seconds else "fresh", "age_seconds": age_seconds}


def collection_failure_detail(status: dict | None) -> dict[str, str] | None:
    """Convert a raw collector failure into a concise UI-safe diagnosis."""
    if not status or status["status"] != "error":
        return None
    error = str(status.get("error", "")).strip()
    lowered = error.lower()
    component = "LSF / FlexNet 采集"
    if "lmstat" in lowered:
        component = "FlexNet License"
    elif "bjobs" in lowered:
        component = "LSF 作业"
    elif "bqueues" in lowered:
        component = "LSF 队列"
    elif "bhosts" in lowered or "lsload" in lowered:
        component = "LSF 节点"
    return {"component": component, "message": error[:240] or "采集器未返回错误详情"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    runtime.load()
    if runtime.config.mode != "setup" and db.latest_snapshot_id(runtime.config.cluster_name) is None:
        await asyncio.to_thread(runtime.collect)
    task = asyncio.create_task(collection_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="CADFlow Lite", version="0.2.0", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return runtime.config.to_dict()


@app.put("/api/config")
def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return runtime.update(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/health")
def health() -> dict[str, Any]:
    status = db.snapshot_status(runtime.config.cluster_name)
    freshness = snapshot_freshness(status)
    return {
        "status": "setup" if runtime.config.mode == "setup" else "ok" if freshness["freshness"] == "fresh" else "degraded",
        "mode": runtime.config.mode,
        "cluster": runtime.config.cluster_name,
        "snapshot": status,
        "failure": collection_failure_detail(status),
        **freshness,
    }


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    return build_summary(db, runtime.config.cluster_name)


@app.get("/api/jobs")
def jobs(status: str | None = None, user: str | None = None, queue: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
    rows = db.latest_rows("jobs", runtime.config.cluster_name)
    if status:
        rows = [row for row in rows if row["status"].lower() == status.lower()]
    if user:
        rows = [row for row in rows if row["user"].lower() == user.lower()]
    if queue:
        rows = [row for row in rows if row["queue"].lower() == queue.lower()]
    return rows[:limit]


@app.get("/api/users")
def users() -> list[dict[str, int | str]]:
    """Return one current-snapshot utilization row per LSF user."""
    grouped: dict[str, dict[str, int | str]] = {}
    for job in db.latest_rows("jobs", runtime.config.cluster_name):
        username = job["user"] or "unknown"
        row = grouped.setdefault(username, {
            "user": username, "total_jobs": 0, "running_jobs": 0, "pending_jobs": 0,
            "exit_jobs": 0, "running_slots": 0, "total_slots": 0, "cpu_efficiency_sum": 0,
        })
        status = job["status"].upper()
        slots = int(job["slots"] or 0)
        row["total_jobs"] += 1
        row["total_slots"] += slots
        if status == "RUN":
            row["running_jobs"] += 1
            row["running_slots"] += slots
        elif status == "PEND":
            row["pending_jobs"] += 1
        elif status == "EXIT":
            row["exit_jobs"] += 1
        row["cpu_efficiency_sum"] += round(float(job["cpu_efficiency"] or 0) * 100)

    result = []
    for row in grouped.values():
        total_jobs = int(row["total_jobs"])
        row["cpu_efficiency_pct"] = round(int(row.pop("cpu_efficiency_sum")) / total_jobs) if total_jobs else 0
        result.append(row)
    return sorted(result, key=lambda row: (-int(row["running_slots"]), -int(row["total_jobs"]), str(row["user"])))


@app.get("/api/queues")
def queues() -> list[dict]:
    return db.latest_rows("queues", runtime.config.cluster_name)


@app.get("/api/hosts")
def hosts() -> list[dict]:
    return db.latest_rows("hosts", runtime.config.cluster_name)


@app.get("/api/licenses")
def licenses() -> list[dict]:
    return db.latest_rows("licenses", runtime.config.cluster_name)


@app.get("/api/alerts")
def alerts() -> list[dict]:
    return build_alerts(db, runtime.config.cluster_name)


@app.get("/api/history")
def history(limit: int = Query(48, ge=2, le=500)) -> list[dict]:
    return db.history(runtime.config.cluster_name, limit)


@app.post("/api/collect")
def collect(x_admin_token: str = Header(default="")) -> dict[str, Any]:
    if settings.admin_token and not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=403, detail="invalid admin token")
    result = runtime.collect()
    if result.get("busy"):
        raise HTTPException(status_code=409, detail=result["error"])
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.post("/api/refresh")
def refresh_from_web() -> dict[str, Any]:
    """Run the same fixed read-only collection used by the dashboard refresh button."""
    result = runtime.collect()
    if result.get("busy"):
        raise HTTPException(status_code=409, detail=result["error"])
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    summary_data = build_summary(db, runtime.config.cluster_name)
    totals, efficiency = summary_data["totals"], summary_data["efficiency"]
    lines = [
        "# HELP cadflow_jobs Current jobs by status", "# TYPE cadflow_jobs gauge",
        f'cadflow_jobs{{cluster="{runtime.config.cluster_name}",status="running"}} {totals["running_jobs"]}',
        f'cadflow_jobs{{cluster="{runtime.config.cluster_name}",status="pending"}} {totals["pending_jobs"]}',
        f'cadflow_jobs{{cluster="{runtime.config.cluster_name}",status="exit"}} {totals["exit_jobs"]}',
        "# HELP cadflow_resource_utilization_pct Resource utilization percent", "# TYPE cadflow_resource_utilization_pct gauge",
        f'cadflow_resource_utilization_pct{{cluster="{runtime.config.cluster_name}",resource="cpu"}} {efficiency["cpu_pct"]}',
        f'cadflow_resource_utilization_pct{{cluster="{runtime.config.cluster_name}",resource="memory"}} {efficiency["mem_pct"]}',
        f'cadflow_resource_utilization_pct{{cluster="{runtime.config.cluster_name}",resource="slot"}} {efficiency["slot_pct"]}',
        "# HELP cadflow_collection_fresh Whether the most recent collection is fresh (1) or stale/failed (0)",
        "# TYPE cadflow_collection_fresh gauge",
        f'cadflow_collection_fresh{{cluster="{runtime.config.cluster_name}"}} {1 if snapshot_freshness(db.snapshot_status(runtime.config.cluster_name))["freshness"] == "fresh" else 0}',
    ]
    return "\n".join(lines) + "\n"
