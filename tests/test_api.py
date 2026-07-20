import os
from pathlib import Path

os.environ["CADFLOW_MODE"] = "demo"
os.environ["CADFLOW_DB_PATH"] = "/tmp/cadflow-lite-test.db"
os.environ["CADFLOW_ADMIN_TOKEN"] = "test-token"

from fastapi.testclient import TestClient

from app.main import app


def setup_module():
    path = Path("/tmp/cadflow-lite-test.db")
    if path.exists():
        path.unlink()


def test_health_and_summary():
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        summary = client.get("/api/summary").json()
        assert summary["cluster"] == "eda-lab"
        assert summary["totals"]["hosts"] == 8
        assert client.get("/metrics").status_code == 200


def test_collect_requires_token():
    with TestClient(app) as client:
        assert client.post("/api/collect").status_code == 403
        assert client.post("/api/collect", headers={"X-Admin-Token": "test-token"}).status_code == 200

