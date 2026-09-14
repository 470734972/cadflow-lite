from __future__ import annotations

import asyncio
import hmac
import subprocess
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .collectors import DemoCollector, LsfCollector
from .collectors.base import Collector
from .auth import SESSION_COOKIE, SESSION_TTL_SECONDS, create_session, revoke_session, valid_session, verify_password
from .config import RuntimeConfig, settings
from .db import Database
from .services import (
    PENDING_STATUSES, CollectionService, build_alerts_from_rows, build_sla_from_snapshots,
    build_summary_from_rows,
)


class SetupCollector(Collector):
    def collect(self) -> dict[str, list[dict[str, Any]]]:
        return {"jobs": [], "queues": [], "hosts": [], "licenses": []}


class Runtime:
    """Hot-loadable, persisted runtime settings for the lab setup wizard."""

    def __init__(self, database: Database, initial: RuntimeConfig):
        self.db = database
        self._lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._data_lock = threading.RLock()
        self._last_snapshot: Optional[dict[str, Any]] = None
        self._rows: dict[str, list[dict[str, Any]]] = {name: [] for name in ("jobs", "queues", "hosts", "licenses")}
        self._summary: dict[str, Any] = {}
        self._alerts: list[dict[str, str]] = []
        self._history: list[dict[str, Any]] = []
        self._sla_snapshots: list[dict[str, Any]] = []
        self.config = initial
        self.service = self._make_service(initial)

    def load(self) -> None:
        stored = self.db.load_config("runtime")
        if stored:
            self.config = RuntimeConfig.from_dict(stored)
            self.service = self._make_service(self.config)
        else:
            self.db.save_config("runtime", self.config.to_dict())
        with self._status_lock:
            self._last_snapshot = self.db.snapshot_status(self.config.cluster_name)
        self._load_dashboard_cache()

    def _load_dashboard_cache(self) -> None:
        """Read SQLite once at startup; normal web requests use only memory."""
        rows = {name: self.db.latest_rows(name, self.config.cluster_name) for name in self._rows}
        status = self.snapshot_status()
        now = datetime.now(timezone.utc)
        # Preserve one full day of compact metrics even with a 30-second
        # collection interval (24 * 60 * 2 = 2880 points).
        history = self.db.history(self.config.cluster_name, 3000)
        outcomes = self.db.sla_snapshots(self.config.cluster_name, (now - timedelta(hours=24)).isoformat())
        self._replace_dashboard_cache(rows, status, history, outcomes)

    def _replace_dashboard_cache(
        self, rows: dict[str, list[dict[str, Any]]], status: Optional[dict[str, Any]],
        history: list[dict[str, Any]], outcomes: list[dict[str, Any]],
    ) -> None:
        defaults = {
            "jobs": {"submit_host": "", "job_name": "", "submit_time": "", "slots": 1, "requested_mem_mb": 0,
                     "used_mem_mb": 0, "cpu_efficiency": 0, "runtime_seconds": 0, "pending_reason": "", "project": ""},
            "queues": {"max_slots": 0, "per_user_slots": 0, "per_processor_slots": 0, "per_host_slots": 0,
                       "running": 0, "pending": 0, "suspended": 0, "host_names": ""},
            "hosts": {"max_slots": 0, "running_slots": 0, "cpu_pct": 0, "mem_pct": 0, "total_mem_mb": 0,
                      "load_1m": 0, "load_15m": 0, "free_mem_mb": 0, "free_tmp_mb": 0, "free_swap_mb": 0},
            "licenses": {"vendor": "", "total": 0, "used": 0, "expires_at": "", "status": "ok"},
        }
        normalized = {
            name: [{**defaults[name], **row} for row in rows.get(name, [])]
            for name in self._rows
        }
        summary = build_summary_from_rows(
            self.config.cluster_name, normalized["jobs"], normalized["queues"], normalized["hosts"],
            normalized["licenses"], status,
        )
        with self._data_lock:
            self._rows = normalized
            self._summary = summary
            self._alerts = build_alerts_from_rows(normalized["hosts"], normalized["queues"], normalized["licenses"])
            self._history = list(history)[-3000:]
            self._sla_snapshots = list(outcomes)[-2000:]

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
        return CollectionService(
            self.db,
            collector,
            config.cluster_name,
            retention_days=config.db_retention_days,
            max_db_size_mb=config.db_max_size_mb,
        )

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
        # Keep health state in memory: a DB lock must not make /api/health hang.
        result = self.service.run()
        payload = result.pop("_payload", None)
        snapshot = result.get("snapshot")
        if snapshot:
            with self._status_lock:
                self._last_snapshot = snapshot
            rows = {name: list(payload.get(name, [])) for name in self._rows} if payload else self._rows
            summary = build_summary_from_rows(
                self.config.cluster_name, rows["jobs"], rows["queues"], rows["hosts"], rows["licenses"], snapshot,
            )
            history_row = {
                "collected_at": snapshot["collected_at"], "running": summary["totals"]["running_jobs"],
                "pending": summary["totals"]["pending_jobs"], "cpu_pct": summary["efficiency"]["cpu_pct"],
                "mem_pct": summary["efficiency"]["mem_pct"] or 0,
            }
            outcome = {key: snapshot.get(key, "") for key in ("collected_at", "status", "error", "duration_ms")}
            with self._data_lock:
                history = [*self._history, history_row][-3000:]
                outcomes = [*self._sla_snapshots, outcome][-2000:]
            self._replace_dashboard_cache(rows, snapshot, history, outcomes)
        elif not result.get("ok"):
            with self._status_lock:
                self._last_snapshot = {
                    "cluster": self.config.cluster_name,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "status": "error", "duration_ms": result.get("duration_ms", 0),
                    "error": str(result.get("error", "collection failed")),
                }
        return result

    def snapshot_status(self) -> Optional[dict[str, Any]]:
        with self._status_lock:
            return dict(self._last_snapshot) if self._last_snapshot else None

    def record_failure(self, error: str) -> None:
        with self._status_lock:
            self._last_snapshot = {
                "cluster": self.config.cluster_name,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "status": "error", "duration_ms": 0, "error": error[:1000],
            }

    def rows(self, table: str) -> list[dict[str, Any]]:
        with self._data_lock:
            return list(self._rows.get(table, []))

    def summary(self) -> dict[str, Any]:
        with self._data_lock:
            return dict(self._summary)

    def alerts(self) -> list[dict[str, str]]:
        with self._data_lock:
            return list(self._alerts)

    def history(self, hours: int = 24, limit: int = 3000) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._data_lock:
            return [row for row in self._history if str(row.get("collected_at", "")) >= cutoff][-limit:]

    def sla(self) -> dict[str, Any]:
        with self._data_lock:
            snapshots = list(self._sla_snapshots)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return build_sla_from_snapshots(
            [snapshot for snapshot in snapshots if snapshot["collected_at"] >= cutoff],
            self.config.collect_interval_seconds,
        )


settings.validate()
db = Database(settings.db_path)
runtime = Runtime(db, RuntimeConfig.from_settings(settings))
APP_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = APP_ROOT / "deploy" / "update-and-start.sh"
UPDATE_LOG = APP_ROOT / "logs" / "update-from-web.log"
_update_lock = threading.Lock()


async def collection_loop() -> None:
    while True:
        await asyncio.sleep(runtime.config.collect_interval_seconds)
        try:
            await asyncio.to_thread(runtime.collect)
        except Exception as exc:  # keep one bad collection from killing polling
            runtime.record_failure(f"collection loop failure: {exc}")


def snapshot_freshness(status: Optional[dict]) -> dict[str, Union[int, str]]:
    if not status:
        return {"freshness": "never", "age_seconds": -1}
    if status["status"] == "error":
        return {"freshness": "failed", "age_seconds": -1}
    collected_at = datetime.fromisoformat(status["collected_at"].replace("Z", "+00:00"))
    age_seconds = max(0, int((datetime.now(timezone.utc) - collected_at).total_seconds()))
    if status["status"] == "partial":
        return {"freshness": "partial", "age_seconds": age_seconds}
    return {"freshness": "stale" if age_seconds > runtime.config.effective_stale_after_seconds else "fresh", "age_seconds": age_seconds}


def collection_failure_detail(status: Optional[dict]) -> Optional[dict[str, str]]:
    """Convert a raw collector failure into a concise UI-safe diagnosis."""
    if not status or status["status"] not in {"error", "partial"}:
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


app = FastAPI(title="Ncc CAD Flow", version="0.3.42", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


def require_config_session(config_session: str = Cookie(default="", alias=SESSION_COOKIE)) -> None:
    if not settings.config_password_hash:
        raise HTTPException(status_code=503, detail="configuration password is not set")
    if not valid_session(config_session):
        raise HTTPException(status_code=401, detail="configuration authentication required")


@app.post("/api/config/auth")
def authenticate_config(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    password = payload.get("password")
    if not isinstance(password, str) or not password or not settings.config_password_hash:
        raise HTTPException(status_code=401, detail="invalid configuration password")
    if not verify_password(password, settings.config_password_hash):
        raise HTTPException(status_code=401, detail="invalid configuration password")
    response.set_cookie(
        SESSION_COOKIE,
        create_session(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        path="/",
        samesite="lax",
    )
    return {"ok": True, "expires_in_seconds": SESSION_TTL_SECONDS}


@app.post("/api/config/logout")
def logout_config(response: Response, config_session: str = Cookie(default="", alias=SESSION_COOKIE)) -> dict[str, bool]:
    revoke_session(config_session)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/config")
def get_config(_: None = Depends(require_config_session)) -> dict[str, Any]:
    return runtime.config.to_dict()


@app.put("/api/config")
def update_config(payload: dict[str, Any], _: None = Depends(require_config_session)) -> dict[str, Any]:
    try:
        return runtime.update(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _release_update_lock(process: subprocess.Popen) -> None:
    try:
        process.wait()
    finally:
        _update_lock.release()


@app.post("/api/update")
def trigger_update(_: None = Depends(require_config_session)) -> dict[str, Any]:
    """Start the existing fast-forward update script after config auth."""
    if not UPDATE_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="update script is not available")
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="an update is already running")
    try:
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with UPDATE_LOG.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                ["bash", str(UPDATE_SCRIPT)],
                cwd=str(APP_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        _update_lock.release()
        raise HTTPException(status_code=503, detail=f"unable to start update: {exc}") from exc
    threading.Thread(target=_release_update_lock, args=(process,), daemon=True).start()
    return {"ok": True, "message": "update started; CADFlow will restart after the fast-forward update", "log": str(UPDATE_LOG)}


@app.get("/api/health")
def health() -> dict[str, Any]:
    status = runtime.snapshot_status()
    freshness = snapshot_freshness(status)
    return {
        "version": app.version,
        "status": "setup" if runtime.config.mode == "setup" else "ok" if freshness["freshness"] == "fresh" else "degraded",
        "mode": runtime.config.mode,
        "cluster": runtime.config.cluster_name,
        "snapshot": status,
        "failure": collection_failure_detail(status),
        **freshness,
    }


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    return runtime.summary()


@app.get("/api/sla")
def sla() -> dict[str, Any]:
    """Snapshot-based availability for the dashboard's core read-only collectors."""
    return runtime.sla()


@app.get("/api/jobs")
def jobs(
    status: Optional[str] = None,
    user: Optional[str] = None,
    queue: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1),
) -> list[dict]:
    """Return jobs from the current snapshot.

    By default the API returns the complete current snapshot, so the result
    count reflects what LSF reported instead of an application-imposed cap.
    Callers may still provide ``limit`` when they intentionally want a
    smaller response (for example, an external integration or CLI query).
    """
    rows = runtime.rows("jobs")
    if status:
        requested_status = status.upper()
        if requested_status == "PEND":
            rows = [row for row in rows if row["status"].upper() in PENDING_STATUSES]
        else:
            rows = [row for row in rows if row["status"].upper() == requested_status]
    if user:
        rows = [row for row in rows if row["user"].lower() == user.lower()]
    if queue:
        rows = [row for row in rows if row["queue"].lower() == queue.lower()]
    # LSF 10.1 does not expose reliable per-job CPU or memory efficiency
    # values through the read-only snapshot commands. Keep those legacy
    # storage fields internal and do not publish misleading zeroes in the API.
    hidden_metrics = {"requested_mem_mb", "used_mem_mb", "cpu_efficiency"}
    selected = rows if limit is None else rows[:limit]
    return [{key: value for key, value in row.items() if key not in hidden_metrics} for row in selected]


@app.get("/api/users")
def users() -> list[dict[str, Union[int, str]]]:
    """Return one current-snapshot utilization row per LSF user."""
    grouped: dict[str, dict[str, Union[int, str]]] = {}
    for job in runtime.rows("jobs"):
        username = job["user"] or "unknown"
        row = grouped.setdefault(username, {
            "user": username, "total_jobs": 0, "running_jobs": 0, "pending_jobs": 0,
            "exit_jobs": 0, "running_slots": 0, "total_slots": 0,
        })
        status = job["status"].upper()
        slots = int(job["slots"] or 0)
        row["total_jobs"] += 1
        row["total_slots"] += slots
        if status == "RUN":
            row["running_jobs"] += 1
            row["running_slots"] += slots
        elif status in PENDING_STATUSES:
            row["pending_jobs"] += 1
        elif status == "EXIT":
            row["exit_jobs"] += 1
    result = []
    for row in grouped.values():
        result.append(row)
    return sorted(result, key=lambda row: (-int(row["running_slots"]), -int(row["total_jobs"]), str(row["user"])))


@app.get("/api/queues")
def queues() -> list[dict]:
    return runtime.rows("queues")


@app.get("/api/hosts")
def hosts() -> list[dict]:
    return runtime.rows("hosts")


@app.get("/api/licenses")
def licenses() -> list[dict]:
    return runtime.rows("licenses")


@app.get("/api/alerts")
def alerts() -> list[dict]:
    return runtime.alerts()


@app.get("/api/history")
def history(hours: int = Query(24, ge=1, le=168)) -> list[dict]:
    """Compact job metrics sampled during the requested recent time window."""
    return runtime.history(hours=hours)


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
    summary_data = runtime.summary()
    totals, efficiency = summary_data["totals"], summary_data["efficiency"]
    sla_data = runtime.sla()
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
        f'cadflow_collection_fresh{{cluster="{runtime.config.cluster_name}"}} {1 if snapshot_freshness(runtime.snapshot_status())["freshness"] == "fresh" else 0}',
    ]
    lines += ["# HELP cadflow_collector_availability_pct Snapshot-based collector availability over the last 24 hours", "# TYPE cadflow_collector_availability_pct gauge"]
    lines.extend(
        f'cadflow_collector_availability_pct{{cluster="{runtime.config.cluster_name}",component="{item["key"]}"}} {item["availability_pct"] if item["availability_pct"] is not None else 0}'
        for item in sla_data["components"]
    )
    return "\n".join(lines) + "\n"


# Each dashboard area has a stable address while sharing the same lightweight
# browser bundle.  This keeps direct links, browser refresh, and bookmarks on
# the selected area instead of falling back to the overview page.
FRONTEND_VIEWS = {"jobs", "users", "queues", "hosts", "licenses", "config"}


@app.get("/{view_name}", include_in_schema=False)
def frontend_view(view_name: str) -> FileResponse:
    if view_name not in FRONTEND_VIEWS:
        raise HTTPException(status_code=404, detail="page not found")
    return FileResponse(static_dir / "index.html")
