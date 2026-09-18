r"""SQLite-backed job store.

A "job" is one piece of footage moving through the pipeline:

    processing -> ready -> sent
                       \-> rejected
    (any stage) -> error

`ready` means the compressed/overlaid clip is sitting in Footage/ (with its
thumbnail in Thumbnail/) waiting for a human to approve or reject it in the
review app.
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
    # Identifies the footage itself rather than where it happens to sit, so
    # the same clip arriving at a second path (re-uploaded by FTP, renamed,
    # moved) isn't processed twice. See content_hash() in processor.py.
    ("content_hash", "TEXT"),
    # Operator-controlled visibility on the kiosk screen only - orthogonal to
    # `status` (a lifecycle field many call sites filter on exactly). Hiding
    # never deletes or moves anything; see JobStore.set_hidden().
    ("hidden_from_kiosk", "INTEGER NOT NULL DEFAULT 0"),
    # Rendered clip length in seconds, probed via ffprobe right before
    # mark_ready() (see processor.py). NULL for jobs rendered before this
    # column existed - the review UI shows "-" for those rather than
    # backfilling.
    ("duration_seconds", "REAL"),
    # Unguessable token for the guest LAN download URL (/d/<token>). NULL until
    # a project with lan_delivery on delivers the clip. See glambot/lan.py.
    ("download_token", "TEXT"),
    ("lan_download_count", "INTEGER NOT NULL DEFAULT 0"),
]


_FOLDER_OWNERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_ownership (
    folder_path TEXT PRIMARY KEY,
    active_project TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Which clip the kiosk screen should pin to, and whether playback is paused -
# both operator-controlled from the /remote touch page. Orthogonal to job
# `status`; never moves or deletes anything.
_KIOSK_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kiosk_state (
    project TEXT PRIMARY KEY,
    live_job_id INTEGER,
    paused INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

# One-shot markers dropped by the "Import existing footage" page so the watcher
# renders that exact file even when identical footage was already processed for
# the project. Consumed (deleted) by watcher._process_path() the first time it
# sees the file; see JobStore.take_forced_import().
_FORCED_IMPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS forced_imports (
    source_path TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_FOLDER_OWNERSHIP_SCHEMA)
    conn.execute(_KIOSK_STATE_SCHEMA)
    conn.execute(_FORCED_IMPORTS_SCHEMA)
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
    content_hash: Optional[str] = None
    hidden_from_kiosk: int = 0
    duration_seconds: Optional[float] = None
    download_token: Optional[str] = None
    lan_download_count: int = 0

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
                    delivery_mode: str = "email",
                    content_hash: Optional[str] = None) -> Job:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO jobs (project, filename, source_path, status,
                       recipient_email, delivery_mode, content_hash, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', ?, ?, ?, ?, ?)""",
                (project, filename, source_path, recipient_email, delivery_mode,
                 content_hash, now, now),
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

    def find_by_hash(self, content_hash: str, project: str) -> Optional[Job]:
        """An existing job for this exact footage under this same project.

        Deliberately scoped per-project rather than globally: pointing a
        second project at one import folder to export a different format is
        a supported setup, and that project must still get its own job for
        the same clip. What this stops is one project rendering the same
        footage twice because it turned up at a new path."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs WHERE content_hash = ? AND project = ?
                   ORDER BY id LIMIT 1""",
                (content_hash, project),
            ).fetchone()
        return Job.from_row(row) if row else None

    def get_job_by_token(self, token: str) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE download_token = ?", (token,)
            ).fetchone()
        return Job.from_row(row) if row else None

    def bump_lan_download(self, job_id: int) -> None:
        """Cheap counter increment (like set_progress) - not worth churning the
        whole row / updated_at for a download tally."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET lan_download_count = lan_download_count + 1 WHERE id = ?",
                (job_id,),
            )

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
                   secondary_output_path: Optional[str] = None,
                   duration_seconds: Optional[float] = None) -> Job:
        fields: dict[str, Any] = dict(status="ready", output_path=output_path, error=None, progress=None)
        if thumbnail_path is not None:
            fields["thumbnail_path"] = thumbnail_path
        if secondary_output_path is not None:
            fields["secondary_output_path"] = secondary_output_path
        if duration_seconds is not None:
            fields["duration_seconds"] = duration_seconds
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

    def set_hidden(self, job_id: int, hidden: bool) -> Job:
        """Toggle a clip's visibility on the kiosk screen. DB-only and fully
        reversible - never touches status or any file on disk."""
        return self.update_job(job_id, hidden_from_kiosk=1 if hidden else 0)

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

    def delete_job(self, job_id: int) -> None:
        """Drop a single job row. Files on disk are the caller's responsibility."""
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def delete_jobs_for_project(self, project: str) -> int:
        """Drop this project's job history. Returns how many rows went.

        Only touches the DB — every rendered file stays on disk. Clearing
        history also un-remembers which clips were already processed, so
        footage still sitting in the import folder will be picked up again."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE project = ?", (project,))
            return cur.rowcount

    def forget_project(self, project: str) -> None:
        """Remove every trace of a project from the DB: its jobs, plus any
        folder it was the active owner of (otherwise a deleted project keeps
        a shared folder pinned to a project that no longer exists)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE project = ?", (project,))
            conn.execute("DELETE FROM folder_ownership WHERE active_project = ?", (project,))

    def mark_forced_import(self, source_path: str) -> None:
        """Record that `source_path` was hand-picked on the Import page, so the
        watcher renders it even if identical footage was already processed for
        the project. One-shot: cleared by take_forced_import()."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO forced_imports (source_path, created_at) VALUES (?, ?)",
                (source_path, _now()),
            )

    def has_forced_import(self, source_path: str) -> bool:
        """Whether a force-render marker is pending for this path. A peek - does
        NOT consume it; take_forced_import() does the one-shot consume when the
        watcher actually renders."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM forced_imports WHERE source_path = ?", (source_path,)
            ).fetchone() is not None

    def take_forced_import(self, source_path: str) -> bool:
        """Return whether `source_path` has a pending force-render marker,
        consuming it in the same step so it only ever applies once."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM forced_imports WHERE source_path = ?", (source_path,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM forced_imports WHERE source_path = ?", (source_path,))
            return True

    def prune_forced_imports(self, max_age_hours: int = 24) -> None:
        """Drop markers whose file never landed, so they can't accumulate or
        force-render a much-later, unrelated file that happens to reuse the path."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        with self._connect() as conn:
            rows = conn.execute("SELECT source_path, created_at FROM forced_imports").fetchall()
            stale = [
                r["source_path"] for r in rows
                if datetime.fromisoformat(r["created_at"]).timestamp() < cutoff
            ]
            for path in stale:
                conn.execute("DELETE FROM forced_imports WHERE source_path = ?", (path,))

    def get_kiosk_state(self, project: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT live_job_id, paused FROM kiosk_state WHERE project = ?", (project,)
            ).fetchone()
        if row is None:
            return {"live_job_id": None, "paused": False}
        return {"live_job_id": row["live_job_id"], "paused": bool(row["paused"])}

    def set_kiosk_state(self, project: str, live_job_id: Optional[int], paused: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO kiosk_state (project, live_job_id, paused, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(project) DO UPDATE SET
                       live_job_id = excluded.live_job_id,
                       paused = excluded.paused,
                       updated_at = excluded.updated_at""",
                (project, live_job_id, 1 if paused else 0, _now()),
            )

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
