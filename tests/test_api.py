import os
import tempfile
from pathlib import Path

os.environ["CADFLOW_MODE"] = "demo"
TEST_DB_PATH = Path(tempfile.gettempdir()) / f"cadflow-lite-test-{os.getpid()}.db"
TEST_DB_PATH.unlink(missing_ok=True)
os.environ["CADFLOW_DB_PATH"] = str(TEST_DB_PATH)
os.environ["CADFLOW_ADMIN_TOKEN"] = "test-token"

from fastapi.testclient import TestClient

from app.auth import hash_password

os.environ["CADFLOW_CONFIG_PASSWORD_HASH"] = hash_password("config-pass")

from app.main import app
from app.services import build_sla, build_summary
import app.main as main_module


def test_health_and_summary():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["version"] == "0.3.27"
        assert client.get("/api/config").status_code == 401
        assert client.post("/api/config/auth", json={"password": "config-pass"}).status_code == 200
        config = client.get("/api/config").json()
        assert config["mode"] == "demo"
        assert config["db_retention_days"] == 7
        assert config["db_max_size_mb"] == 1024
        summary = client.get("/api/summary").json()
        assert summary["cluster"] == "demo-cluster"
        assert "memory_waste_jobs" not in summary["totals"]
        jobs = client.get("/api/jobs").json()
        assert jobs
        assert len(client.get("/api/jobs?limit=1").json()) == 1
        assert len(client.get("/api/jobs?limit=1001").json()) == len(jobs)
        assert client.get("/api/jobs?limit=0").status_code == 422
        assert not {"requested_mem_mb", "used_mem_mb", "cpu_efficiency"} & jobs[0].keys()
        assert summary["totals"]["hosts"] == 8
        queues = client.get("/api/queues").json()
        assert queues
        assert {"per_user_slots", "per_processor_slots", "per_host_slots"} <= queues[0].keys()
        sla = client.get("/api/sla").json()
        assert sla["window_hours"] == 24
        assert {"collection", "jobs", "queues", "hosts", "licenses"} == {item["key"] for item in sla["components"]}
        assert client.get("/metrics").status_code == 200


def test_users_are_aggregated_from_current_jobs():
    with TestClient(app) as client:
        response = client.get("/api/users")
        assert response.status_code == 200
        users = response.json()
        assert users
        assert {"user", "total_jobs", "running_slots"} <= users[0].keys()
        assert "cpu_efficiency_pct" not in users[0]
        assert sum(user["total_jobs"] for user in users) == len(client.get("/api/jobs").json())


def test_summary_uses_host_capacity_when_lsf_queues_are_unlimited():
    class FakeDb:
        def latest_rows(self, table, cluster):
            rows = {
                "jobs": [],
                "queues": [{"running": 4, "max_slots": 0}],
                "hosts": [
                    {"status": "ok", "running_slots": 2, "max_slots": 2, "cpu_pct": 1, "mem_pct": 0, "free_mem_mb": 1024},
                    {"status": "ok", "running_slots": 2, "max_slots": 2, "cpu_pct": 3, "mem_pct": 0, "free_mem_mb": 2048},
                ],
                "licenses": [],
            }
            return rows[table]

        def snapshot_status(self, cluster):
            return None

    summary = build_summary(FakeDb(), "eda_cluster")
    assert summary["totals"]["running_slots"] == 4
    assert summary["totals"]["max_slots"] == 4
    assert summary["totals"]["free_mem_mb"] == 3072
    assert summary["efficiency"]["slot_pct"] == 100


def test_pending_summary_includes_pending_suspended_jobs():
    class FakeDb:
        def latest_rows(self, table, cluster):
            rows = {
                "jobs": [
                    {"status": "PEND", "slots": 1},
                    {"status": "PSUSP", "slots": 1},
                ],
                "queues": [],
                "hosts": [],
                "licenses": [],
            }
            return rows[table]

        def snapshot_status(self, cluster):
            return None

    summary = build_summary(FakeDb(), "eda_cluster")
    assert summary["totals"]["pending_jobs"] == 2


def test_sla_keeps_lsf_available_when_only_flexnet_is_partial():
    class FakeDb:
        def sla_snapshots(self, cluster, since):
            return [
                {"collected_at": "2026-07-22T00:00:00+00:00", "status": "ok", "error": "", "duration_ms": 10},
                {"collected_at": "2026-07-22T00:01:00+00:00", "status": "partial", "error": "lmstat timed out", "duration_ms": 20},
                {"collected_at": "2026-07-22T00:02:00+00:00", "status": "error", "error": "bjobs timed out", "duration_ms": 30},
            ]

    sla = build_sla(FakeDb(), "eda_cluster", 60)
    components = {item["key"]: item for item in sla["components"]}
    assert components["licenses"]["availability_pct"] == round(1 / 3 * 100, 2)
    assert components["jobs"]["availability_pct"] == round(2 / 3 * 100, 2)
    assert components["queues"]["availability_pct"] == round(2 / 3 * 100, 2)


def test_collect_requires_token():
    with TestClient(app) as client:
        assert client.post("/api/collect").status_code == 403
        assert client.post("/api/collect", headers={"X-Admin-Token": "test-token"}).status_code == 200


def test_config_auth_rejects_wrong_password_and_supports_logout():
    with TestClient(app) as client:
        assert client.post("/api/config/auth", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/config/auth", json={"password": "config-pass"}).status_code == 200
        assert client.get("/api/config").status_code == 200
        assert client.post("/api/config/logout").status_code == 200
        assert client.get("/api/config").status_code == 401


def test_web_update_requires_config_auth_and_starts_fixed_script(monkeypatch, tmp_path):
    calls = {}

    class FakeProcess:
        pid = 1234

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "UPDATE_LOG", tmp_path / "update.log")
    with TestClient(app) as client:
        assert client.post("/api/update").status_code == 401
        assert client.post("/api/config/auth", json={"password": "config-pass"}).status_code == 200
        response = client.post("/api/update")
        assert response.status_code == 200
        assert calls["argv"][0] == "bash"
        assert calls["argv"][-1].endswith("deploy\\update-and-start.sh") or calls["argv"][-1].endswith("deploy/update-and-start.sh")


def test_web_refresh_runs_collection_without_exposing_admin_token():
    with TestClient(app) as client:
        response = client.post("/api/refresh")
        assert response.status_code == 200
        assert response.json()["ok"] is True
