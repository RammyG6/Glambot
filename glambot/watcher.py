"""Watches the inbox folder tree for newly saved footage and runs it through
the processor as soon as the file has finished copying and a valid
config.json sits next to it.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import ConfigError, load_config
from .db import JobStore
from .processor import (
    OUTPUT_SUBDIR,
    QR_APPROVED_SUBDIR,
    QR_DOWNLOAD_SUBDIR,
    QR_OUTPUT_SUBDIR,
    SENT_SUBDIR,
    is_footage_file,
    process_job,
)

_EXCLUDED_SUBDIRS = {OUTPUT_SUBDIR, SENT_SUBDIR, QR_OUTPUT_SUBDIR, QR_APPROVED_SUBDIR, QR_DOWNLOAD_SUBDIR}

logger = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "InboxWatcher"):
        self.watcher = watcher

    def on_created(self, event):
        if event.is_directory:
            return
        self.watcher.consider(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        self.watcher.consider(Path(event.dest_path))


class InboxWatcher:
    """Watches `inbox_dir` for new footage in project subfolders and enqueues
    a processing job once each file's size has stopped changing (i.e. the
    copy/save has finished)."""

    def __init__(self, inbox_dir: Path, store: JobStore,
                 settle_seconds: float = 2.0, poll_interval: float = 0.5):
        self.inbox_dir = Path(inbox_dir)
        self.store = store
        self.settle_seconds = settle_seconds
        self.poll_interval = poll_interval
        self._queue: Queue[Path] = Queue()
        self._seen: set[str] = set()
        self._observer = Observer()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self._recover_stuck_jobs()
        self._scan_existing()
        handler = _Handler(self)
        self._observer.schedule(handler, str(self.inbox_dir), recursive=True)
        self._observer.start()
        self._worker_thread.start()
        logger.info("Watching %s for new footage", self.inbox_dir)

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._observer.join()
        self._worker_thread.join(timeout=5)

    def _recover_stuck_jobs(self) -> None:
        """If the process was killed mid-ffmpeg, jobs left in 'processing'
        would otherwise be silently skipped forever (find_by_source matches
        them). Re-run them so a restart always converges."""
        for job in self.store.list_jobs(status="processing"):
            path = Path(job.source_path)
            if not path.exists():
                self.store.mark_error(job.id, "source file disappeared while processing")
                continue
            logger.info("Recovering job %s (%s) left in 'processing'", job.id, job.filename)
            self._seen.add(str(path.resolve()))
            self._queue.put(path)

    def _is_relevant(self, path: Path) -> bool:
        if not is_footage_file(path):
            return False
        try:
            parts = path.relative_to(self.inbox_dir).parts
        except ValueError:
            return False
        if len(parts) < 2:
            return False  # must live inside a project subfolder, not the inbox root
        if _EXCLUDED_SUBDIRS.intersection(parts):
            return False
        return True

    def _scan_existing(self) -> None:
        """Pick up footage that was already sitting in the inbox before this
        process started (e.g. saved while the pipeline was offline)."""
        if not self.inbox_dir.exists():
            return
        for project_dir in sorted(p for p in self.inbox_dir.iterdir() if p.is_dir()):
            for f in sorted(project_dir.iterdir()):
                if f.is_file() and is_footage_file(f):
                    self.consider(f)

    def consider(self, path: Path) -> None:
        if not self._is_relevant(path):
            return
        key = str(path.resolve())
        if key in self._seen or self.store.find_by_source(key):
            return
        self._seen.add(key)
        threading.Thread(target=self._wait_and_enqueue, args=(path,), daemon=True).start()

    def _wait_and_enqueue(self, path: Path) -> None:
        """Poll the file's size until it stops changing for `settle_seconds`,
        so we don't start processing a file that's still being copied."""
        last_size = -1
        stable_since: float | None = None
        while not self._stop.is_set():
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return
            if size == last_size:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= self.settle_seconds:
                    break
            else:
                stable_since = None
                last_size = size
            time.sleep(self.poll_interval)
        else:
            return
        self._queue.put(path)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except Empty:
                continue
            self._process_path(path)

    def _process_path(self, path: Path) -> None:
        project_dir = path.parent
        project = project_dir.name
        source_path = str(path.resolve())

        job = self.store.find_by_source(source_path)
        if job is None:
            job = self.store.create_job(project=project, filename=path.name, source_path=source_path)

        try:
            config = load_config(project_dir)
        except ConfigError as exc:
            logger.error("Config error for %s: %s", project, exc)
            self.store.mark_error(job.id, str(exc))
            return

        updates = {}
        if job.recipient_email is None:
            updates["recipient_email"] = config.recipient_email
        if job.delivery_mode != config.delivery_mode:
            updates["delivery_mode"] = config.delivery_mode
        if updates:
            job = self.store.update_job(job.id, **updates)

        process_job(job, config, self.store)
