from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .collectors import DemoCollector, LsfCollector
from .config import settings
from .db import Database
from .services import CollectionService, build_alerts, build_summary


settings.validate()
db = Database(settings.db_path)
collector = DemoCollector(settings.cluster_name) if settings.mode == "demo" else LsfCollector(
    settings.command_timeout_seconds,
    settings.lmstat_path,
    settings.license_servers,
    settings.lsf_bin_dir,
    settings.license_vendor,
)
service = CollectionService(db, collector, settings.cluster_name)


async def collection_loop() -> None:
    while True:
        await asyncio.sleep(settings.collect_interval_seconds)
        await asyncio.to_thread(service.run)


def snapshot_freshness(status: dict | None) -> dict[str, int | str]:
    if not status:
        return {"freshness": "never", "age_seconds": -1}
    if status["status"] != "ok":
        return {"freshness": "failed", "age_seconds": -1}
    collected_at = datetime.fromisoformat(status["collected_at"].replace("Z", "+00:00"))
    age_seconds = max(0, int((datetime.now(timezone.utc) - collected_at).total_seconds()))
    return {"freshness": "stale" if age_seconds > settings.effective_stale_after_seconds else "fresh", "age_seconds": age_seconds}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    if db.latest_snapshot_id(settings.cluster_name) is None:
        await asyncio.to_thread(service.run)
    task = asyncio.create_task(collection_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="CADFlow Lite", version="0.1.0", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict:
    status = db.snapshot_status(settings.cluster_name)
    freshness = snapshot_freshness(status)
    return {
        "status": "ok" if freshness["freshness"] == "fresh" else "degraded",
        "mode": settings.mode,
        "cluster": settings.cluster_name,
        "snapshot": status,
        **freshness,
    }


@app.get("/api/summary")
def summary() -> dict:
    return build_summary(db, settings.cluster_name)


@app.get("/api/jobs")
def jobs(status: str | None = None, user: str | None = None, queue: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
    rows = db.latest_rows("jobs", settings.cluster_name)
    if status:
        rows = [row for row in rows if row["status"].lower() == status.lower()]
    if user:
        rows = [row for row in rows if row["user"].lower() == user.lower()]
    if queue:
        rows = [row for row in rows if row["queue"].lower() == queue.lower()]
    return rows[:limit]


@app.get("/api/queues")
def queues() -> list[dict]:
    return db.latest_rows("queues", settings.cluster_name)


@app.get("/api/hosts")
def hosts() -> list[dict]:
    return db.latest_rows("hosts", settings.cluster_name)


@app.get("/api/licenses")
def licenses() -> list[dict]:
    return db.latest_rows("licenses", settings.cluster_name)


@app.get("/api/alerts")
def alerts() -> list[dict]:
    return build_alerts(db, settings.cluster_name)


@app.get("/api/history")
def history(limit: int = Query(48, ge=2, le=500)) -> list[dict]:
    return db.history(settings.cluster_name, limit)


@app.post("/api/collect")
def collect(x_admin_token: str = Header(default="")) -> dict:
    if settings.admin_token and not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=403, detail="invalid admin token")
    result = service.run()
    if result.get("busy"):
        raise HTTPException(status_code=409, detail=result["error"])
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    summary_data = build_summary(db, settings.cluster_name)
    totals, efficiency = summary_data["totals"], summary_data["efficiency"]
    lines = [
        "# HELP cadflow_jobs Current jobs by status", "# TYPE cadflow_jobs gauge",
        f'cadflow_jobs{{cluster="{settings.cluster_name}",status="running"}} {totals["running_jobs"]}',
        f'cadflow_jobs{{cluster="{settings.cluster_name}",status="pending"}} {totals["pending_jobs"]}',
        f'cadflow_jobs{{cluster="{settings.cluster_name}",status="exit"}} {totals["exit_jobs"]}',
        "# HELP cadflow_resource_utilization_pct Resource utilization percent", "# TYPE cadflow_resource_utilization_pct gauge",
        f'cadflow_resource_utilization_pct{{cluster="{settings.cluster_name}",resource="cpu"}} {efficiency["cpu_pct"]}',
        f'cadflow_resource_utilization_pct{{cluster="{settings.cluster_name}",resource="memory"}} {efficiency["mem_pct"]}',
        f'cadflow_resource_utilization_pct{{cluster="{settings.cluster_name}",resource="slot"}} {efficiency["slot_pct"]}',
        "# HELP cadflow_collection_fresh Whether the most recent collection is fresh (1) or stale/failed (0)",
        "# TYPE cadflow_collection_fresh gauge",
        f'cadflow_collection_fresh{{cluster="{settings.cluster_name}"}} {1 if snapshot_freshness(db.snapshot_status(settings.cluster_name))["freshness"] == "fresh" else 0}',
    ]
    return "\n".join(lines) + "\n"
