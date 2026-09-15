from datetime import datetime, timedelta, timezone

from app.db import Database


def test_cleanup_by_retention_days_keeps_latest_snapshot(tmp_path):
    db = Database(tmp_path / "cadflow.db")
    db.initialize()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    for age in (3, 2, 1, 0):
        db.save_snapshot("eda", (now - timedelta(days=age)).isoformat(), {"jobs": []})

    result = db.cleanup("eda", retention_days=1, max_size_mb=0, now=now)

    assert result["deleted_snapshots"] == 2
    assert db.latest_snapshot_id("eda") is not None
    assert len(db.latest_rows("jobs", "eda")) == 0
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()["count"]
    assert count == 2


def test_current_details_are_retained_while_history_uses_compact_metrics(tmp_path):
    db = Database(tmp_path / "cadflow.db")
    db.initialize()
    first = "2026-08-25T12:00:00+00:00"
    second = "2026-08-25T12:01:00+00:00"
    db.save_snapshot("eda", first, {
        "jobs": [{"job_id": "1", "user": "u", "status": "RUN", "queue": "q", "exec_host": "h"}],
        "queues": [{"name": "q", "status": "Open:Active", "running": 1, "pending": 0}], "hosts": [], "licenses": [],
    })
    db.save_snapshot("eda", second, {
        "jobs": [{"job_id": "2", "user": "u", "status": "PEND", "queue": "q", "exec_host": "h"}],
        "queues": [{"name": "q", "status": "Open:Active", "running": 0, "pending": 1}], "hosts": [], "licenses": [],
    })

    assert [job["job_id"] for job in db.latest_rows("jobs", "eda")] == ["2"]
    assert [(row["running"], row["pending"]) for row in db.history("eda")] == [(1, 0), (0, 1)]


def test_license_status_history_retains_one_service_row_per_hour_after_detail_trim(tmp_path):
    db = Database(tmp_path / "cadflow.db")
    db.initialize()
    service = lambda status: [{"server": "27000@rd1", "vendor": "snpslmd", "feature": "License Server", "total": 0, "used": 0, "expires_at": "License server UP", "status": status}]
    for when, status in (("2026-09-15T01:01:00+00:00", "ok"), ("2026-09-15T01:59:00+00:00", "critical"), ("2026-09-15T02:01:00+00:00", "ok")):
        db.save_snapshot("eda", when, {"jobs": [], "queues": [], "hosts": [], "licenses": service(status)})
    history = db.license_history("eda", "2026-09-15T01:00:00+00:00")
    assert [(row["collected_at"], row["status"]) for row in history] == [
        ("2026-09-15T01:59:00+00:00", "critical"), ("2026-09-15T02:01:00+00:00", "ok"),
    ]
