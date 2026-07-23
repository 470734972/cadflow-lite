from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    snapshot_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    user TEXT NOT NULL,
    status TEXT NOT NULL,
    queue TEXT NOT NULL,
    exec_host TEXT NOT NULL,
    submit_host TEXT NOT NULL DEFAULT '',
    job_name TEXT NOT NULL DEFAULT '',
    submit_time TEXT NOT NULL DEFAULT '',
    slots INTEGER NOT NULL DEFAULT 1,
    requested_mem_mb REAL NOT NULL DEFAULT 0,
    used_mem_mb REAL NOT NULL DEFAULT 0,
    cpu_efficiency REAL NOT NULL DEFAULT 0,
    runtime_seconds INTEGER NOT NULL DEFAULT 0,
    pending_reason TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS queues (
    snapshot_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    max_slots INTEGER NOT NULL DEFAULT 0,
    running INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hosts (
    snapshot_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    max_slots INTEGER NOT NULL DEFAULT 0,
    running_slots INTEGER NOT NULL DEFAULT 0,
    cpu_pct REAL NOT NULL DEFAULT 0,
    mem_pct REAL NOT NULL DEFAULT 0,
    load_1m REAL NOT NULL DEFAULT 0,
    load_15m REAL NOT NULL DEFAULT 0,
    free_mem_mb REAL NOT NULL DEFAULT 0,
    free_tmp_mb REAL NOT NULL DEFAULT 0,
    free_swap_mb REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS licenses (
    snapshot_id INTEGER NOT NULL,
    server TEXT NOT NULL,
    vendor TEXT NOT NULL DEFAULT '',
    feature TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snapshots_cluster_time ON snapshots(cluster, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_snapshot ON jobs(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_queues_snapshot ON queues(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_hosts_snapshot ON hosts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_licenses_snapshot ON licenses(snapshot_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_jobs(conn)
            self._migrate_hosts(conn)

    @staticmethod
    def _migrate_jobs(conn: sqlite3.Connection) -> None:
        """Add new job display fields without invalidating existing snapshots."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        migrations = {
            "submit_host": "TEXT NOT NULL DEFAULT ''",
            "job_name": "TEXT NOT NULL DEFAULT ''",
            "submit_time": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_hosts(conn: sqlite3.Connection) -> None:
        """Add LSF load-index fields while retaining historical snapshots."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(hosts)")}
        migrations = {
            "load_1m": "REAL NOT NULL DEFAULT 0",
            "free_mem_mb": "REAL NOT NULL DEFAULT 0",
            "free_tmp_mb": "REAL NOT NULL DEFAULT 0",
            "free_swap_mb": "REAL NOT NULL DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE hosts ADD COLUMN {name} {definition}")

    def save_snapshot(self, cluster: str, collected_at: str, payload: dict[str, Any], duration_ms: int = 0, warnings: list[str] | None = None) -> int:
        with self._lock, self.connect() as conn:
            warnings = warnings or []
            cursor = conn.execute(
                "INSERT INTO snapshots(cluster, collected_at, status, duration_ms, error) VALUES (?, ?, ?, ?, ?)",
                (cluster, collected_at, "partial" if warnings else "ok", duration_ms, "; ".join(warnings)[:1000]),
            )
            snapshot_id = int(cursor.lastrowid)
            self._insert_many(conn, "jobs", snapshot_id, payload.get("jobs", []))
            self._insert_many(conn, "queues", snapshot_id, payload.get("queues", []))
            self._insert_many(conn, "hosts", snapshot_id, payload.get("hosts", []))
            self._insert_many(conn, "licenses", snapshot_id, payload.get("licenses", []))
            conn.execute(
                "DELETE FROM snapshots WHERE cluster=? AND id NOT IN (SELECT id FROM snapshots WHERE cluster=? ORDER BY id DESC LIMIT 2016)",
                (cluster, cluster),
            )
            return snapshot_id

    def save_failure(self, cluster: str, collected_at: str, error: str, duration_ms: int = 0) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO snapshots(cluster, collected_at, status, duration_ms, error) VALUES (?, ?, 'error', ?, ?)",
                (cluster, collected_at, duration_ms, error[:1000]),
            )

    def load_config(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None

    def save_config(self, key: str, value: dict[str, Any]) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO app_config(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    @staticmethod
    def _insert_many(conn: sqlite3.Connection, table: str, snapshot_id: int, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO {table}(snapshot_id,{','.join(columns)}) VALUES (?,{placeholders})"
        conn.executemany(sql, [[snapshot_id, *[row.get(column) for column in columns]] for row in rows])

    def latest_snapshot_id(self, cluster: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM snapshots WHERE cluster=? AND status IN ('ok', 'partial') ORDER BY id DESC LIMIT 1", (cluster,)
            ).fetchone()
            return int(row["id"]) if row else None

    def latest_rows(self, table: str, cluster: str) -> list[dict[str, Any]]:
        allowed = {"jobs", "queues", "hosts", "licenses"}
        if table not in allowed:
            raise ValueError("invalid table")
        snapshot_id = self.latest_snapshot_id(cluster)
        if snapshot_id is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} WHERE snapshot_id=?", (snapshot_id,)).fetchall()
            return [{key: row[key] for key in row.keys() if key != "snapshot_id"} for row in rows]

    def snapshot_status(self, cluster: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, cluster, collected_at, status, duration_ms, error FROM snapshots WHERE cluster=? ORDER BY id DESC LIMIT 1",
                (cluster,),
            ).fetchone()
            return dict(row) if row else None

    def history(self, cluster: str, limit: int = 48) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.collected_at,
                       COALESCE((SELECT SUM(running) FROM queues q WHERE q.snapshot_id=s.id), 0) AS running,
                       COALESCE((SELECT SUM(pending) FROM queues q WHERE q.snapshot_id=s.id), 0) AS pending,
                       COALESCE((SELECT AVG(cpu_pct) FROM hosts h WHERE h.snapshot_id=s.id), 0) AS cpu_pct,
                       COALESCE((SELECT AVG(mem_pct) FROM hosts h WHERE h.snapshot_id=s.id), 0) AS mem_pct
                FROM snapshots s
                WHERE s.cluster=? AND s.status IN ('ok', 'partial')
                ORDER BY s.id DESC LIMIT ?
                """,
                (cluster, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def sla_snapshots(self, cluster: str, since: str, limit: int = 10000) -> list[dict[str, Any]]:
        """Return raw collection outcomes for availability calculations only."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT collected_at, status, error, duration_ms
                FROM snapshots
                WHERE cluster=? AND collected_at>=?
                ORDER BY id ASC LIMIT ?
                """,
                (cluster, since, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def dump_debug(self, cluster: str) -> str:
        return json.dumps({table: self.latest_rows(table, cluster) for table in ("jobs", "queues", "hosts", "licenses")})
