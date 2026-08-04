"""Watches each project's footage folder for newly saved footage and runs it
through the processor as soon as the file has finished copying and a valid
config.json sits next to it.

Each project's `config.json` always lives at `inbox_dir/<project>/`, but the
folder the *raw footage* is watched from is configurable per project (see
`ProjectConfig.source_dir`) — it may be an arbitrary folder elsewhere on
disk (e.g. an SD-card import folder), not necessarily `inbox_dir/<project>`
itself. A periodic sync pass keeps the set of watched folders in step with
each project's current config, so new projects/changed source folders are
picked up without a restart.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import MANAGED_SUBDIRS, ConfigError, load_config, managed_subdir_in
from .db import JobStore
from .folders import group_by_folder, project_watch_dirs
from .processor import is_footage_file, process_job

_EXCLUDED_SUBDIRS = MANAGED_SUBDIRS

logger = logging.getLogger(__name__)


def _is_locked(path: Path) -> bool:
    """Windows-only: True if another process (e.g. an FTP server) still has
    `path` open. Renaming a file onto itself fails with a sharing violation
    while the writer holds it without FILE_SHARE_DELETE — a stronger
    completion signal than size-stability alone, since a stalled (but still
    open) transfer can otherwise look byte-for-byte stable."""
    if os.name != "nt":
        return False
    try:
        os.rename(path, path)
    except OSError:
        return True
    return False


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
    """Watches every project's effective footage folder and enqueues a
    processing job once each new file's size has stopped changing (i.e. the
    copy/save has finished)."""

    def __init__(self, inbox_dir: Path, store: JobStore,
                 settle_seconds: float = 2.0, poll_interval: float = 0.5,
                 watch_sync_interval: float = 10.0):
        self.inbox_dir = Path(inbox_dir)
        self.store = store
        self.settle_seconds = settle_seconds
        self.poll_interval = poll_interval
        self.watch_sync_interval = watch_sync_interval
        self._queue: Queue[Path] = Queue()
        # Dedup key is (resolved_path, active_project): a file is processed
        # once per project that's active for its folder, so switching the
        # active project (or a stale job left by a since-deleted project)
        # correctly reprocesses the file under the now-active project.
        self._seen: set[tuple[str, str]] = set()
        self._observer = Observer()
        self._handler = _Handler(self)
        self._project_watch_dirs: dict[str, Path] = {}
        # watchdog keeps ONE underlying emitter per filesystem path, so watches
        # are tracked by resolved path string (not by project) — otherwise
        # unscheduling one project's watch on a shared folder tears down the
        # emitter the newly-active project still depends on.
        self._watched_paths: dict[str, object] = {}
        # Projects already warned about pointing at a Glambot-managed folder,
        # so the every-10s sync doesn't spam the log with the same line.
        self._warned_managed: set[str] = set()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self._recover_stuck_jobs()
        self._sync_watches()
        self._observer.start()
        self._worker_thread.start()
        logger.info("Watching %s for new footage", self.inbox_dir)

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._observer.join()
        self._worker_thread.join(timeout=5)

    def _drop_managed_watch_dirs(self, candidates: dict[str, Path]) -> dict[str, Path]:
        """Refuse to watch any folder inside one Glambot manages itself.

        load_config() rejects these too, so normally nothing reaches here —
        but a config.json written before that validation existed would
        otherwise start an endless loop: `_archive_original` moves each
        finished original into "Edited Footages", and a project watching that
        folder sees every one of them as brand-new footage."""
        kept: dict[str, Path] = {}
        for project, folder in candidates.items():
            managed = managed_subdir_in(folder)
            if managed is None:
                kept[project] = folder
                continue
            if project not in self._warned_managed:
                self._warned_managed.add(project)
                logger.warning(
                    "Not watching %s for project %s: it is inside Glambot's own '%s' "
                    "folder, which holds already-processed clips. Point the project's "
                    "footage source folder at the import folder instead.",
                    folder, project, managed,
                )
        return kept

    def _sync_watches(self) -> None:
        """Re-read every project's config.json, keep the set of watched
        folders in step with it, and scan every active folder for footage.

        When two or more projects point at the same folder, only one — the
        "active" one, persisted in `folder_ownership` so it stays stable
        across restarts/syncs — receives new footage (see the "Shared footage
        folders" UI in app.py). watchdog watches are managed by *path*, not by
        project: a shared folder keeps its single live watch no matter which
        project is active, so switching the active project never leaves the
        folder "deaf".

        Runs on startup and every ~watch_sync_interval seconds. The
        per-folder scan at the end is the reliable detection mechanism —
        idempotent (consider() dedups via `_seen`/`find_by_source`) and cheap,
        so it also self-heals any live FS event that was missed."""
        candidates = project_watch_dirs(self.inbox_dir)
        candidates = self._drop_managed_watch_dirs(candidates)
        groups = group_by_folder(candidates)

        current: dict[str, Path] = {}
        for folder, projects in groups.items():
            if len(projects) == 1:
                current[projects[0]] = folder
                continue
            folder_key = str(folder)
            active = self.store.get_active_project(folder_key)
            if active not in projects:
                # First time this collision is seen, or the previously
                # active project no longer claims this folder — pick a
                # stable default and persist it so it doesn't flip on
                # every sync.
                active = projects[0]
                self.store.set_active_project(folder_key, active)
            current[active] = folder

        # Publish the mapping BEFORE scanning: consider() -> _project_for_path()
        # reads self._project_watch_dirs, so it must already reflect the active
        # projects or the scan finds no owning project and drops every file.
        self._project_watch_dirs = current

        # Reconcile watchdog watches by resolved path (deduped by watchdog to
        # one emitter per path). Only schedule paths not yet watched; only
        # unschedule paths no longer wanted by any active project.
        desired = {str(folder.resolve()): folder for folder in current.values()}
        for path_str, folder in desired.items():
            if path_str in self._watched_paths:
                continue
            try:
                self._watched_paths[path_str] = self._observer.schedule(
                    self._handler, path_str, recursive=True
                )
                logger.info("Watching %s", path_str)
            except OSError:
                logger.warning("Could not watch %s (missing/inaccessible?)", path_str)
        for path_str in list(self._watched_paths):
            if path_str not in desired:
                self._observer.unschedule(self._watched_paths.pop(path_str))
                logger.info("Stopped watching %s", path_str)

        for folder in current.values():
            self._scan_folder(folder)

    def _scan_folder(self, folder: Path) -> None:
        """Consider every footage file in an active folder, pruning Glambot's
        own generated output subfolders so it never walks those big trees."""
        if not folder.is_dir():
            return
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_SUBDIRS]
            for name in files:
                path = Path(root) / name
                if is_footage_file(path):
                    self.consider(path)

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
            self._seen.add((str(path.resolve()), job.project))
            self._queue.put(path)

    def _project_for_path(self, path: Path) -> str | None:
        """Which project's watch folder (if any) this file lives under,
        excluding Glambot's own output subfolders."""
        resolved = path.resolve()
        for project, watch_dir in self._project_watch_dirs.items():
            try:
                rel = resolved.relative_to(watch_dir.resolve())
            except ValueError:
                continue
            if _EXCLUDED_SUBDIRS.intersection(rel.parts):
                continue
            return project
        return None

    def consider(self, path: Path) -> None:
        if not is_footage_file(path):
            return
        project = self._project_for_path(path)
        if project is None:
            return
        resolved = str(path.resolve())
        key = (resolved, project)
        if key in self._seen:
            return
        existing = self.store.find_by_source(resolved)
        if existing is not None and (self.inbox_dir / existing.project / "config.json").exists():
            # This file already has a job under a project that STILL EXISTS —
            # it's handled, so don't reprocess or reassign it. This is what
            # keeps switching the active project on a shared folder from
            # churning every already-processed clip: only files with no job,
            # or an orphaned job left by a since-DELETED project, fall through
            # to be processed under the now-active project.
            self._seen.add(key)
            return
        self._seen.add(key)
        threading.Thread(target=self._wait_and_enqueue, args=(path,), daemon=True).start()

    def _wait_and_enqueue(self, path: Path) -> None:
        """Poll the file's size until it stops changing for `settle_seconds`
        AND it's no longer held open by another process, so we don't start
        processing a file that's still being copied — or one whose FTP
        transfer has merely stalled long enough to look size-stable."""
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
                    if not _is_locked(path):
                        break
                    # Still open by its writer: restart the settle timer and
                    # keep waiting rather than grabbing a partial file.
                    stable_since = None
            else:
                stable_since = None
                last_size = size
            time.sleep(self.poll_interval)
        else:
            return
        self._queue.put(path)

    def _worker_loop(self) -> None:
        last_sync = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_sync >= self.watch_sync_interval:
                # Never let a bad sync (e.g. an unreadable folder) kill the
                # worker thread — that would silently stop ALL detection and
                # processing until the app is restarted.
                try:
                    self._sync_watches()
                except Exception:
                    logger.exception("watch sync failed")
                last_sync = time.monotonic()
            try:
                path = self._queue.get(timeout=0.5)
            except Empty:
                continue
            # Likewise, one clip that blows up must not take the whole
            # pipeline down with it.
            try:
                self._process_path(path)
            except Exception:
                logger.exception("processing %s failed", path)

    def _process_path(self, path: Path) -> None:
        project = self._project_for_path(path)
        if project is None:
            # Watch config changed between enqueue and now (e.g. source_dir
            # edited or the project removed) — nothing sane to do with it.
            return
        project_dir = self.inbox_dir / project
        source_path = str(path.resolve())

        job = self.store.find_by_source(source_path)
        if job is None:
            job = self.store.create_job(project=project, filename=path.name, source_path=source_path)
        elif job.project != project:
            # The folder's active project changed since this file was last
            # processed (or the old project was deleted). Re-attribute the
            # single source_path row to the now-active project and clear ALL
            # of the previous run's outputs/attribution — otherwise stale
            # paths from the old project (thumbnail, secondary render, Drive
            # links) leak into the fresh delivery.
            job = self.store.update_job(
                job.id, project=project, recipient_email=None,
                output_path=None, thumbnail_path=None, secondary_output_path=None,
                drive_link=None, secondary_drive_link=None, error=None,
            )

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
