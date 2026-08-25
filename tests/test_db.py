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
