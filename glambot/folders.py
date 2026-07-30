"""Shared helpers for figuring out which local folder each project's raw
footage actually comes from, and detecting when two or more projects point
at the same folder. Used by both `watcher.py` (to decide what to watch) and
`app.py` (to render/manage the "shared footage folders" UI) so both compute
the exact same picture instead of duplicating the logic.
"""
from __future__ import annotations

from pathlib import Path

from .config import ConfigError, load_config


def project_watch_dirs(inbox_dir: Path) -> dict[str, Path]:
    """project name -> effective footage-source folder (config.source_dir if
    set, else the project's own inbox_dir/<project> folder), for every
    project that currently has a valid config.json."""
    result: dict[str, Path] = {}
    if not inbox_dir.exists():
        return result
    for project_dir in sorted(p for p in inbox_dir.iterdir() if p.is_dir()):
        try:
            config = load_config(project_dir)
        except ConfigError:
            continue
        result[project_dir.name] = config.source_dir or project_dir
    return result


def group_by_folder(watch_dirs: dict[str, Path]) -> dict[Path, list[str]]:
    """folder -> every project name currently pointing at it."""
    groups: dict[Path, list[str]] = {}
    for project, folder in watch_dirs.items():
        groups.setdefault(folder, []).append(project)
    return groups


def all_project_dirs(inbox_dir: Path) -> list[Path]:
    """Every subfolder of inbox_dir that has a config.json, valid or not —
    for UI surfaces (like the main page's project list) that must list every
    project, including ones whose config is currently broken, unlike
    project_watch_dirs() which silently skips ConfigError projects."""
    if not inbox_dir.exists():
        return []
    return sorted(
        (p for p in inbox_dir.iterdir() if p.is_dir() and (p / "config.json").exists()),
        key=lambda p: p.name,
    )
