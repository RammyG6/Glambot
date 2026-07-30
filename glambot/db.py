"""SQLite-backed job store.

A "job" is one piece of footage moving through the pipeline:

    processing -> ready -> sent
                       \-> rejected
    (any stage) -> error

`ready` means the compressed/overlaid clip is sitting in Output_ReadytoSend/
waiting for a human to approve or reject it in the review app.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    output_path TEXT,
    status TEXT NOT NULL DEFAULT 'processing',
    recipient_email TEXT,
    drive_link TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
"""

# Columns added after the initial schema. No migration framework exists here
# (just CREATE TABLE IF NOT EXISTS), so new columns are grafted on with a
# guarded ALTER TABLE — SQLite backfills the DEFAULT onto every existing row.
_NEW_COLUMNS = [
    ("delivery_mode", "TEXT NOT NULL DEFAULT 'email'"),
    ("thumbnail_path", "TEXT"),
    ("secondary_output_path", "TEXT"),
    ("secondary_drive_link", "TEXT"),
    ("progress", "INTEGER"),
]


_FOLDER_OWNERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_ownership (
    folder_path TEXT PRIMARY KEY,
    active_project TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_FOLDER_OWNERSHIP_SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in _NEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    project: str
    filename: str
    source_path: str
    output_path: Optional[str]
    status: str
    recipient_email: Optional[str]
    drive_link: Optional[str]
    error: Optional[str]
    created_at: str
    updated_at: str
    sent_at: Optional[str]
    delivery_mode: str = "email"
    thumbnail_path: Optional[str] = None
    secondary_output_path: Optional[str] = None
    secondary_drive_link: Optional[str] = None
    progress: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(**{k: row[k] for k in row.keys()})


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _ensure_schema(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with _lock:
                yield conn
                conn.commit()
        finally:
            conn.close()

    def create_job(self, project: str, filename: str, source_path: str,
                    recipient_email: Optional[str] = None,
                    delivery_mode: str = "email") -> Job:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO jobs (project, filename, source_path, status,
                       recipient_email, delivery_mode, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', ?, ?, ?, ?)""",
                (project, filename, source_path, recipient_email, delivery_mode, now, now),
            )
            job_id = cur.lastrowid
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def find_by_source(self, source_path: str) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE source_path = ?", (source_path,)
            ).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, status: Optional[str] = None, project: Optional[str] = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Job.from_row(r) for r in rows]

    def update_job(self, job_id: int, **fields: Any) -> Job:
        if not fields:
            return self.get_job(job_id)
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {set_clause} WHERE id = ?",
                (*fields.values(), job_id),
            )
        return self.get_job(job_id)

    def mark_ready(self, job_id: int, output_path: str,
                   thumbnail_path: Optional[str] = None,
                   secondary_output_path: Optional[str] = None) -> Job:
        fields: dict[str, Any] = dict(status="ready", output_path=output_path, error=None, progress=None)
        if thumbnail_path is not None:
            fields["thumbnail_path"] = thumbnail_path
        if secondary_output_path is not None:
            fields["secondary_output_path"] = secondary_output_path
        return self.update_job(job_id, **fields)

    def mark_error(self, job_id: int, error: str) -> Job:
        return self.update_job(job_id, status="error", error=error, progress=None)

    def set_progress(self, job_id: int, pct: int) -> None:
        """Lightweight progress write (called many times during a render).
        Doesn't go through update_job so it stays cheap and doesn't churn
        the rest of the row."""
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (pct, job_id))

    def mark_rejected(self, job_id: int) -> Job:
        return self.update_job(job_id, status="rejected")

    def mark_sent(self, job_id: int, drive_link: str, output_path: str,
                   recipient_email: Optional[str] = None,
                   delivery_mode: Optional[str] = None,
                   thumbnail_path: Optional[str] = None,
                   secondary_output_path: Optional[str] = None,
                   secondary_drive_link: Optional[str] = None) -> Job:
        fields: dict[str, Any] = dict(
            status="sent",
            drive_link=drive_link,
            output_path=output_path,
            recipient_email=recipient_email,
            sent_at=_now(),
            error=None,
        )
        if delivery_mode is not None:
            fields["delivery_mode"] = delivery_mode
        if thumbnail_path is not None:
            fields["thumbnail_path"] = thumbnail_path
        if secondary_output_path is not None:
            fields["secondary_output_path"] = secondary_output_path
        if secondary_drive_link is not None:
            fields["secondary_drive_link"] = secondary_drive_link
        return self.update_job(job_id, **fields)

    def get_active_project(self, folder_path: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT active_project FROM folder_ownership WHERE folder_path = ?", (folder_path,)
            ).fetchone()
        return row["active_project"] if row else None

    def set_active_project(self, folder_path: str, project: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO folder_ownership (folder_path, active_project, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(folder_path) DO UPDATE SET
                       active_project = excluded.active_project,
                       updated_at = excluded.updated_at""",
                (folder_path, project, _now()),
            )
