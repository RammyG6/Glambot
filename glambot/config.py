"""Load and validate a project's config.json.

Each project subfolder under the inbox has exactly one config.json describing
how every piece of footage in that folder should be processed, plus optional
per-file overrides (currently just `trim`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESOLUTION_RE = re.compile(r"^\d+x\d+$")
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$|^\d+(\.\d+)?$")
_DRIVE_FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")


def extract_drive_folder_id(raw: str) -> str:
    """Accept either a bare Drive folder ID or a full share URL
    (https://drive.google.com/drive/folders/<id>?usp=...) and return just
    the ID either way."""
    raw = raw.strip()
    match = _DRIVE_FOLDER_URL_RE.search(raw)
    return match.group(1) if match else raw

VALID_OVERLAY_POSITIONS = {
    "full",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "custom",
}

VALID_DELIVERY_MODES = {"email", "qr_only"}
VALID_ROTATIONS = {0, 90, -90, 180}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MIN_DB, MAX_DB = -60.0, 12.0

# --- Folders Glambot creates and manages itself ---------------------------
# These live here rather than in processor.py (where they're used) because
# config validation has to reject them as a footage source, and config.py is
# the only module low enough in the import graph for both to share them.
OUTPUT_SUBDIR = "Output_ReadytoSend"
SENT_SUBDIR = "Email Sent File"

# QR-only ("Instant Download" / kiosk) projects use a separate on-disk layout
# from email-mode projects.
QR_OUTPUT_SUBDIR = "Output"
QR_APPROVED_SUBDIR = "Selected Output"
QR_DOWNLOAD_SUBDIR = "Instant Download"

# Where processed originals are moved to, inside their own import folder.
EDITED_FOOTAGES_SUBDIR = "Edited Footages"

# Every folder name above, as one set. Two things key off it: the watcher
# prunes these from its scans, and a project's footage source folder is not
# allowed to sit inside one — pointing a project at, say, "Edited Footages"
# makes it re-process every clip another project has already finished.
MANAGED_SUBDIRS = frozenset({
    OUTPUT_SUBDIR, SENT_SUBDIR, QR_OUTPUT_SUBDIR, QR_APPROVED_SUBDIR,
    QR_DOWNLOAD_SUBDIR, EDITED_FOOTAGES_SUBDIR,
})


def managed_subdir_in(path: Path) -> str | None:
    """The Glambot-managed folder name `path` sits inside (or is), if any."""
    for part in Path(path).parts:
        if part in MANAGED_SUBDIRS:
            return part
    return None


class ConfigError(ValueError):
    """Raised when a project's config.json is missing or invalid."""


@dataclass
class Trim:
    start: str | None = None
    end: str | None = None


@dataclass
class ProjectConfig:
    recipient_email: str
    bitrate: str
    resolution: str
    aspect_ratio: str
    overlay: str | None
    overlay_position: str
    trim: Trim
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_dir: Path | None = None
    fps: int | None = None
    overlay_scale: int | None = None
    overlay_x: float | None = None
    overlay_y: float | None = None
    delivery_mode: str = "email"
    soundtrack: str | None = None
    soundtrack_volume_db: float = 0.0
    original_volume_db: float = 0.0
    soundtrack_trim: Trim | None = None
    auto_deliver: bool = False
    rotation: int = 0
    position_x: int = 0
    position_y: int = 0
    second_resolution: str | None = None
    second_bitrate: str | None = None
    source_dir: Path | None = None
    drive_folder_id: str | None = None
    second_overlay: str | None = None
    second_overlay_position: str = "full"
    second_overlay_scale: int | None = None
    second_overlay_x: float | None = None
    second_overlay_y: float | None = None
    output_dir: Path | None = None

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])

    @property
    def second_width(self) -> int | None:
        return int(self.second_resolution.split("x")[0]) if self.second_resolution else None

    @property
    def second_height(self) -> int | None:
        return int(self.second_resolution.split("x")[1]) if self.second_resolution else None

    def trim_for(self, filename: str) -> Trim:
        """Resolve the effective trim for a specific footage file, applying
        any per-file override on top of the project default."""
        override = self.overrides.get(filename, {})
        override_trim = override.get("trim")
        if override_trim:
            return Trim(
                start=override_trim.get("start", self.trim.start),
                end=override_trim.get("end", self.trim.end),
            )
        return self.trim


def _require(data: dict, key: str, project_dir: Path) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{project_dir.name}/config.json: missing required field '{key}'")
    return data[key]


def _validate_timestamp(value: str, field_name: str, project_dir: Path) -> None:
    if not isinstance(value, str) or not TIMESTAMP_RE.match(value.strip()):
        raise ConfigError(
            f"{project_dir.name}/config.json: '{field_name}' must look like HH:MM:SS "
            f"or a number of seconds, got {value!r}"
        )


def _parse_trim(trim_data: Any, label: str, project_dir: Path) -> Trim | None:
    """Parse an optional {"start": ..., "end": ...} block. Either side left
    empty/absent means "don't trim that side" — returns None entirely if
    neither side is set."""
    if trim_data in (None, {}):
        return None
    if not isinstance(trim_data, dict):
        raise ConfigError(f"{project_dir.name}/config.json: '{label}' must be an object with 'start'/'end'")
    start = trim_data.get("start")
    end = trim_data.get("end")
    if start not in (None, ""):
        _validate_timestamp(start, f"{label}.start", project_dir)
        start = str(start)
    else:
        start = None
    if end not in (None, ""):
        _validate_timestamp(end, f"{label}.end", project_dir)
        end = str(end)
    else:
        end = None
    if start is None and end is None:
        return None
    return Trim(start=start, end=end)


def _validate_db(value: Any, name: str, project_dir: Path) -> float:
    if value is None:
        return 0.0
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not (MIN_DB <= value <= MAX_DB):
        raise ConfigError(
            f"{project_dir.name}/config.json: '{name}' must be a number between {MIN_DB} and "
            f"{MAX_DB} dB, got {value!r}"
        )
    return float(value)


def _validate_int(value: Any, name: str, project_dir: Path, default: int = 0) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{project_dir.name}/config.json: '{name}' must be an integer, got {value!r}")
    return value


def _parse_overlay_group(data: dict, prefix: str, project_dir: Path):
    """Validate one overlay's fields (`<prefix>overlay`, `<prefix>overlay_position`,
    `<prefix>overlay_scale`, `<prefix>overlay_x/y`). The overlay itself is
    optional — a missing/empty path means "no overlay". Returns
    (path_or_None, position, scale_or_None, x_or_None, y_or_None)."""
    overlay = data.get(f"{prefix}overlay")
    overlay_path = None
    if overlay:
        p = Path(overlay)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            raise ConfigError(f"{project_dir.name}/config.json: overlay file not found: {overlay}")
        overlay_path = str(p)

    position = data.get(f"{prefix}overlay_position", "full")
    if position not in VALID_OVERLAY_POSITIONS:
        raise ConfigError(
            f"{project_dir.name}/config.json: {prefix}overlay_position must be one of "
            f"{sorted(VALID_OVERLAY_POSITIONS)}, got {position!r}"
        )

    scale = data.get(f"{prefix}overlay_scale")
    if scale is not None:
        if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not (1 <= scale <= 100):
            raise ConfigError(
                f"{project_dir.name}/config.json: '{prefix}overlay_scale' must be a number between 1 and 100, got {scale!r}"
            )
        scale = int(scale)

    x = data.get(f"{prefix}overlay_x")
    y = data.get(f"{prefix}overlay_y")
    if position == "custom":
        for name, value in ((f"{prefix}overlay_x", x), (f"{prefix}overlay_y", y)):
            if value is None or not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 <= value <= 100):
                raise ConfigError(
                    f"{project_dir.name}/config.json: '{name}' must be a number between 0 and 100 "
                    f"when {prefix}overlay_position is 'custom', got {value!r}"
                )
    x = float(x) if x is not None else None
    y = float(y) if y is not None else None
    return overlay_path, position, scale, x, y


def load_config(project_dir: Path) -> ProjectConfig:
    """Load and validate config.json from a project folder.

    Raises ConfigError with a human-readable message on any problem so it can
    be surfaced directly in the review app / logs.
    """
    config_path = project_dir / "config.json"
    if not config_path.exists():
        raise ConfigError(f"{project_dir.name}: no config.json found")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{project_dir.name}/config.json: invalid JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{project_dir.name}/config.json: top-level value must be an object")

    delivery_mode = data.get("delivery_mode", "email")
    if delivery_mode not in VALID_DELIVERY_MODES:
        raise ConfigError(
            f"{project_dir.name}/config.json: delivery_mode must be one of "
            f"{sorted(VALID_DELIVERY_MODES)}, got {delivery_mode!r}"
        )

    recipient_email = str(data.get("recipient_email") or "").strip()
    if delivery_mode == "email":
        if not recipient_email or not EMAIL_RE.match(recipient_email):
            raise ConfigError(
                f"{project_dir.name}/config.json: recipient_email is required and must be a "
                f"valid address when delivery_mode is 'email'"
            )
    elif recipient_email and not EMAIL_RE.match(recipient_email):
        raise ConfigError(
            f"{project_dir.name}/config.json: recipient_email {recipient_email!r} is not a valid address"
        )

    bitrate = _require(data, "bitrate", project_dir)
    resolution = _require(data, "resolution", project_dir)
    if not RESOLUTION_RE.match(resolution):
        raise ConfigError(
            f"{project_dir.name}/config.json: resolution must look like WIDTHxHEIGHT, got {resolution!r}"
        )

    aspect_ratio = _require(data, "aspect_ratio", project_dir)

    # Overlay is optional now — no file means the clip is processed without a logo.
    overlay_path, overlay_position, overlay_scale, overlay_x, overlay_y = _parse_overlay_group(
        data, "", project_dir
    )

    fps = data.get("fps")
    if fps is not None:
        if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
            raise ConfigError(f"{project_dir.name}/config.json: 'fps' must be a positive integer, got {fps!r}")

    # Second-resolution overlay (its own optional overlay for the dual-res export).
    (second_overlay, second_overlay_position, second_overlay_scale,
     second_overlay_x, second_overlay_y) = _parse_overlay_group(data, "second_", project_dir)

    # trim is fully optional now: leaving start/end empty means "don't trim
    # that side" — a bare {} or missing key means "don't trim at all".
    trim = _parse_trim(data.get("trim"), "trim", project_dir) or Trim(start=None, end=None)

    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigError(f"{project_dir.name}/config.json: 'overrides' must be an object")
    for filename, override in overrides.items():
        if "trim" in override:
            ot = override["trim"]
            if "start" in ot:
                _validate_timestamp(ot["start"], f"overrides.{filename}.trim.start", project_dir)
            if "end" in ot:
                _validate_timestamp(ot["end"], f"overrides.{filename}.trim.end", project_dir)

    # --- Soundtrack ---------------------------------------------------
    soundtrack = data.get("soundtrack")
    if soundtrack:
        soundtrack_path = Path(soundtrack)
        if not soundtrack_path.is_absolute():
            soundtrack_path = Path.cwd() / soundtrack_path
        if not soundtrack_path.exists():
            raise ConfigError(f"{project_dir.name}/config.json: soundtrack file not found: {soundtrack}")
        soundtrack = str(soundtrack_path)
    else:
        soundtrack = None

    soundtrack_volume_db = _validate_db(data.get("soundtrack_volume_db"), "soundtrack_volume_db", project_dir)
    original_volume_db = _validate_db(data.get("original_volume_db"), "original_volume_db", project_dir)
    soundtrack_trim = _parse_trim(data.get("soundtrack_trim"), "soundtrack_trim", project_dir)

    # --- Full automation ------------------------------------------------
    auto_deliver = bool(data.get("auto_deliver", False))

    # --- Rotation / repositioning ---------------------------------------
    rotation = data.get("rotation", 0)
    if rotation not in VALID_ROTATIONS:
        raise ConfigError(
            f"{project_dir.name}/config.json: 'rotation' must be one of {sorted(VALID_ROTATIONS)}, got {rotation!r}"
        )
    position_x = _validate_int(data.get("position_x"), "position_x", project_dir)
    position_y = _validate_int(data.get("position_y"), "position_y", project_dir)

    # --- Dual-resolution export ------------------------------------------
    second_resolution = data.get("second_resolution") or None
    if second_resolution is not None and not RESOLUTION_RE.match(second_resolution):
        raise ConfigError(
            f"{project_dir.name}/config.json: second_resolution must look like WIDTHxHEIGHT, "
            f"got {second_resolution!r}"
        )
    second_bitrate = data.get("second_bitrate") or None
    if second_bitrate is not None:
        second_bitrate = str(second_bitrate)

    # --- Custom footage source folder ------------------------------------
    source_dir = data.get("source_dir")
    if source_dir:
        source_dir_path = Path(source_dir).expanduser()
        if not source_dir_path.is_absolute():
            source_dir_path = Path.cwd() / source_dir_path
        if not source_dir_path.is_dir():
            raise ConfigError(
                f"{project_dir.name}/config.json: source_dir not found or not a directory: {source_dir}"
            )
        source_dir = source_dir_path.resolve()
        managed = managed_subdir_in(source_dir)
        if managed:
            # Watching a folder Glambot writes into re-processes clips that
            # are already finished — an "Edited Footages" source turns every
            # archived original back into new footage, forever.
            raise ConfigError(
                f"{project_dir.name}/config.json: source_dir is inside Glambot's own "
                f"'{managed}' folder ({source_dir}). That folder holds clips Glambot has "
                f"already processed - point source_dir at the import folder instead."
            )
    else:
        source_dir = None

    # --- Per-project Google Drive destination folder ---------------------
    drive_folder_id = data.get("drive_folder_id")
    if drive_folder_id:
        drive_folder_id = extract_drive_folder_id(str(drive_folder_id))
    else:
        drive_folder_id = None

    # --- Custom output location (parent dir; "<project>_Output" goes inside it) ---
    output_dir = data.get("output_dir")
    if output_dir:
        output_dir_path = Path(output_dir).expanduser()
        if not output_dir_path.is_absolute():
            output_dir_path = Path.cwd() / output_dir_path
        try:
            output_dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"{project_dir.name}/config.json: output_dir not accessible: {output_dir} ({exc})"
            )
        output_dir = output_dir_path.resolve()
    else:
        output_dir = None

    return ProjectConfig(
        recipient_email=recipient_email,
        bitrate=str(bitrate),
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        overlay=overlay_path,
        overlay_position=overlay_position,
        trim=trim,
        overrides=overrides,
        project_dir=project_dir,
        fps=fps,
        overlay_scale=overlay_scale,
        overlay_x=overlay_x,
        overlay_y=overlay_y,
        delivery_mode=delivery_mode,
        soundtrack=soundtrack,
        soundtrack_volume_db=soundtrack_volume_db,
        original_volume_db=original_volume_db,
        soundtrack_trim=soundtrack_trim,
        auto_deliver=auto_deliver,
        rotation=rotation,
        position_x=position_x,
        position_y=position_y,
        second_resolution=second_resolution,
        second_bitrate=second_bitrate,
        source_dir=source_dir,
        drive_folder_id=drive_folder_id,
        second_overlay=second_overlay,
        second_overlay_position=second_overlay_position,
        second_overlay_scale=second_overlay_scale,
        second_overlay_x=second_overlay_x,
        second_overlay_y=second_overlay_y,
        output_dir=output_dir,
    )


def save_config(project_dir: Path, updates: dict, *, merge: bool = True) -> Path:
    """Write project_dir/config.json.

    When merge=True (the default, used by project edits), any existing
    config.json is read first and `updates` is merged on top of it, so keys
    not present in `updates` — overrides, or an overlay/soundtrack left
    unchanged on an edit — are preserved rather than clobbered. merge=False
    (used by project creation) writes `updates` as-is.
    """
    config_path = project_dir / "config.json"
    data = dict(updates)
    if merge and config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if isinstance(existing, dict):
            existing.update(updates)
            data = existing
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return config_path
