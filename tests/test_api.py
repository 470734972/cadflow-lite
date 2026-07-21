import os
import tempfile
from pathlib import Path

os.environ["CADFLOW_MODE"] = "demo"
TEST_DB_PATH = Path(tempfile.gettempdir()) / f"cadflow-lite-test-{os.getpid()}.db"
TEST_DB_PATH.unlink(missing_ok=True)
os.environ["CADFLOW_DB_PATH"] = str(TEST_DB_PATH)
os.environ["CADFLOW_ADMIN_TOKEN"] = "test-token"

from fastapi.testclient import TestClient

from app.main import app
from app.services import build_summary


def test_health_and_summary():
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").json()["mode"] == "demo"
        summary = client.get("/api/summary").json()
        assert summary["cluster"] == "eda-lab"
        assert summary["totals"]["hosts"] == 8
        assert client.get("/metrics").status_code == 200


def test_users_are_aggregated_from_current_jobs():
    with TestClient(app) as client:
        response = client.get("/api/users")
        assert response.status_code == 200
        users = response.json()
        assert users
        assert {"user", "total_jobs", "running_slots", "cpu_efficiency_pct"} <= users[0].keys()
        assert sum(user["total_jobs"] for user in users) == len(client.get("/api/jobs").json())


def test_summary_uses_host_capacity_when_lsf_queues_are_unlimited():
    class FakeDb:
        def latest_rows(self, table, cluster):
            rows = {
                "jobs": [],
                "queues": [{"running": 4, "max_slots": 0}],
                "hosts": [
                    {"status": "ok", "running_slots": 2, "max_slots": 2, "cpu_pct": 1, "mem_pct": 0},
                    {"status": "ok", "running_slots": 2, "max_slots": 2, "cpu_pct": 3, "mem_pct": 0},
                ],
                "licenses": [],
            }
            return rows[table]

        def snapshot_status(self, cluster):
            return None

    summary = build_summary(FakeDb(), "eda_cluster")
    assert summary["totals"]["running_slots"] == 4
    assert summary["totals"]["max_slots"] == 4
    assert summary["efficiency"]["slot_pct"] == 100


def test_collect_requires_token():
    with TestClient(app) as client:
        assert client.post("/api/collect").status_code == 403
        assert client.post("/api/collect", headers={"X-Admin-Token": "test-token"}).status_code == 200
