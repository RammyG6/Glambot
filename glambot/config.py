"""Load and validate a project's config.json.

Each project subfolder under the inbox has exactly one config.json describing
how every piece of footage in that folder should be processed, plus optional
per-file overrides (currently just `trim`).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .effects import Grade, SpeedRamp

logger = logging.getLogger(__name__)

# (project, asset-path) pairs already warned about, so a missing file logs once
# per process instead of on every load_config() call (the review page loads
# every project's config repeatedly).
_warned_missing: set[tuple[str, str]] = set()


def _warn_missing(project: str, kind: str, ref: str) -> None:
    key = (project, ref)
    if key not in _warned_missing:
        _warned_missing.add(key)
        logger.warning("%s/config.json: %s not found, ignoring: %s", project, kind, ref)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESOLUTION_RE = re.compile(r"^\d+x\d+$")
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$|^\d+(\.\d+)?$")
DOWNLOAD_PIN_RE = re.compile(r"^\d{4,8}$")
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
#
# Two folders, split by file type rather than by delivery lifecycle stage:
# every video artifact (rendered output, secondary-resolution output, the
# archived original source) goes in Footage/; every image artifact (the
# thumbnail, the thumbnail+QR "download photo") goes in Thumbnail/. Delivery
# lifecycle (ready/sent/approved) lives only in the job's DB `status` column
# now — files never move between folders as a job progresses.
FOOTAGE_SUBDIR = "Footage"
THUMBNAIL_SUBDIR = "Thumbnail"

# Both folder names, as one set. Two things key off it: the watcher prunes
# these from its scans, and a project's footage source folder is not allowed
# to sit inside one — pointing a project at, say, "Footage" makes it
# re-process every clip another project has already finished.
MANAGED_SUBDIRS = frozenset({FOOTAGE_SUBDIR, THUMBNAIL_SUBDIR})


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
    # Playback frame rate to reinterpret raw footage at (e.g. a Phantom .cine
    # whose header stores its high-speed capture rate). None = trust the file.
    source_fps: float | None = None
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
    # Orientation-keyed overlays: the renderer picks vertical or horizontal to
    # match each output's aspect ratio (see processor._overlay_spec_for). These
    # supersede second_overlay; a legacy `overlay` / `second_overlay` is used as
    # a fallback (resolved in load_config).
    vertical_overlay: str | None = None
    vertical_overlay_position: str = "full"
    vertical_overlay_scale: int | None = None
    vertical_overlay_x: float | None = None
    vertical_overlay_y: float | None = None
    horizontal_overlay: str | None = None
    horizontal_overlay_position: str = "full"
    horizontal_overlay_scale: int | None = None
    horizontal_overlay_x: float | None = None
    horizontal_overlay_y: float | None = None
    output_dir: Path | None = None
    playback_background: str | None = None
    playback_background_opacity: int = 50
    lan_delivery: bool = False
    download_pin: str | None = None
    offline_mode: bool = False
    grade: Grade | None = None
    speed_ramp: SpeedRamp | None = None
    email_subject: str | None = None
    email_body: str | None = None
    # Referenced asset files that were missing on disk (see load_config). Each
    # entry is a short "<kind>: <path>" string for the UI to surface.
    missing_assets: list[str] = field(default_factory=list)

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

    def grade_for(self, filename: str) -> Grade | None:
        override = self.overrides.get(filename, {})
        if "grade" in override:
            return _parse_grade(override["grade"], f"overrides.{filename}.grade", self.project_dir or Path("."))
        return self.grade

    def speed_ramp_for(self, filename: str) -> SpeedRamp | None:
        override = self.overrides.get(filename, {})
        if "speed_ramp" in override:
            return _parse_speed_ramp(override["speed_ramp"], f"overrides.{filename}.speed_ramp",
                                     self.project_dir or Path("."))
        return self.speed_ramp

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


def _parse_grade(data: Any, label: str, project_dir: Path) -> Grade | None:
    """Parse an optional {"exposure","contrast","white_balance"} block.
    Returns None when absent or fully neutral."""
    if data in (None, {}):
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{project_dir.name}/config.json: '{label}' must be an object")

    def _num(key, lo, hi, default):
        v = data.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.{key}' must be a number")
        if not (lo <= v <= hi):
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.{key}' must be between {lo} and {hi}")
        return v

    grade = Grade(
        exposure=float(_num("exposure", -2.0, 2.0, 0.0)),
        contrast=float(_num("contrast", 0.5, 2.0, 1.0)),
        white_balance=int(_num("white_balance", -100, 100, 0)),
    )
    return None if grade.is_neutral() else grade


def _parse_speed_ramp(data: Any, label: str, project_dir: Path) -> SpeedRamp | None:
    """Parse an optional speed-ramp block. Returns None when absent or disabled."""
    if data in (None, {}):
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{project_dir.name}/config.json: '{label}' must be an object")
    if not data.get("enabled", False):
        return None

    from .effects import DEFAULT_MAX_SPEED, HARD_MAX_SPEED
    ms = data.get("max_speed", DEFAULT_MAX_SPEED)
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        raise ConfigError(f"{project_dir.name}/config.json: '{label}.max_speed' must be a number")
    if not (2.0 <= ms <= HARD_MAX_SPEED):
        raise ConfigError(f"{project_dir.name}/config.json: '{label}.max_speed' must be between 2 and {HARD_MAX_SPEED:g}")
    max_speed = float(ms)

    raw_points = data.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ConfigError(f"{project_dir.name}/config.json: '{label}.points' must be a list of at least 2 points")
    points: list[dict] = []
    last_t = -1.0
    for idx, p in enumerate(raw_points):
        if not isinstance(p, dict):
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.points[{idx}]' must be an object")
        t, speed = p.get("t"), p.get("speed")
        for nm, v in (("t", t), ("speed", speed)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ConfigError(f"{project_dir.name}/config.json: '{label}.points[{idx}].{nm}' must be a number")
        if not (0.0 <= t <= 1.0):
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.points[{idx}].t' must be between 0 and 1")
        if not (0.1 <= speed <= max_speed):
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.points[{idx}].speed' must be between 0.1 and {max_speed:g}")
        if t <= last_t:
            raise ConfigError(f"{project_dir.name}/config.json: '{label}.points' must be sorted by strictly increasing t")
        last_t = t
        pt: dict = {"t": float(t), "speed": float(speed)}
        # Optional bezier tangent handles [dt, dv] in normalised editor space;
        # clamp rather than reject so a wild drag can't break the config.
        for key, dt_lo, dt_hi in (("hl", -1.0, 0.0), ("hr", 0.0, 1.0)):
            h = p.get(key)
            if isinstance(h, (list, tuple)) and len(h) == 2:
                try:
                    dt = min(dt_hi, max(dt_lo, float(h[0])))
                    dv = min(1.0, max(-1.0, float(h[1])))
                    pt[key] = [round(dt, 4), round(dv, 4)]
                except (TypeError, ValueError):
                    pass
        points.append(pt)
    points[0]["t"] = 0.0
    points[-1]["t"] = 1.0

    interpolation = data.get("interpolation", "smooth")
    if interpolation not in {"smooth", "linear"}:
        raise ConfigError(f"{project_dir.name}/config.json: '{label}.interpolation' must be 'smooth' or 'linear'")

    return SpeedRamp(points=points, interpolation=interpolation,
                     smooth_frames=bool(data.get("smooth_frames", False)),
                     max_speed=max_speed)


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
        if p.exists():
            overlay_path = str(p)
        else:
            # A missing overlay file must not brick the whole project - render
            # without it and let the UI flag the missing reference.
            _warn_missing(project_dir.name, f"{prefix}overlay file", overlay)

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

    # Asset files (overlays, soundtrack, playback background) that are
    # referenced but missing on disk are collected here rather than raising -
    # the project still loads and renders, and the UI surfaces the list.
    missing_assets: list[str] = []

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

    source_fps = data.get("source_fps")
    if source_fps is not None:
        if isinstance(source_fps, bool) or not isinstance(source_fps, (int, float)) or not (0 < source_fps <= 1000):
            raise ConfigError(
                f"{project_dir.name}/config.json: 'source_fps' must be a number between 0 and 1000, got {source_fps!r}")
        source_fps = float(source_fps)

    # Legacy second-resolution overlay - still parsed so old configs load and so
    # it can act as the horizontal-overlay fallback below.
    (second_overlay, second_overlay_position, second_overlay_scale,
     second_overlay_x, second_overlay_y) = _parse_overlay_group(data, "second_", project_dir)

    # Orientation-keyed overlays. Each falls back to a legacy overlay when its
    # own file isn't set, so existing projects keep working untouched:
    #   vertical   <- legacy `overlay`
    #   horizontal <- legacy `second_overlay`, then legacy `overlay`
    (vertical_overlay, vertical_overlay_position, vertical_overlay_scale,
     vertical_overlay_x, vertical_overlay_y) = _parse_overlay_group(data, "vertical_", project_dir)
    (horizontal_overlay, horizontal_overlay_position, horizontal_overlay_scale,
     horizontal_overlay_x, horizontal_overlay_y) = _parse_overlay_group(data, "horizontal_", project_dir)

    for _pfx, _path in (("", overlay_path), ("second_", second_overlay),
                        ("vertical_", vertical_overlay), ("horizontal_", horizontal_overlay)):
        _ref = data.get(f"{_pfx}overlay")
        if _ref and _path is None:
            missing_assets.append(f"{_pfx or 'legacy '}overlay: {_ref}")

    if vertical_overlay is None and overlay_path is not None:
        (vertical_overlay, vertical_overlay_position, vertical_overlay_scale,
         vertical_overlay_x, vertical_overlay_y) = (
            overlay_path, overlay_position, overlay_scale, overlay_x, overlay_y)
    if horizontal_overlay is None:
        if second_overlay is not None:
            (horizontal_overlay, horizontal_overlay_position, horizontal_overlay_scale,
             horizontal_overlay_x, horizontal_overlay_y) = (
                second_overlay, second_overlay_position, second_overlay_scale,
                second_overlay_x, second_overlay_y)
        elif overlay_path is not None:
            (horizontal_overlay, horizontal_overlay_position, horizontal_overlay_scale,
             horizontal_overlay_x, horizontal_overlay_y) = (
                overlay_path, overlay_position, overlay_scale, overlay_x, overlay_y)

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
        if "grade" in override:
            _parse_grade(override["grade"], f"overrides.{filename}.grade", project_dir)
        if "speed_ramp" in override:
            _parse_speed_ramp(override["speed_ramp"], f"overrides.{filename}.speed_ramp", project_dir)

    # --- Soundtrack ---------------------------------------------------
    soundtrack = data.get("soundtrack")
    if soundtrack:
        soundtrack_path = Path(soundtrack)
        if not soundtrack_path.is_absolute():
            soundtrack_path = Path.cwd() / soundtrack_path
        if soundtrack_path.exists():
            soundtrack = str(soundtrack_path)
        else:
            _warn_missing(project_dir.name, "soundtrack file", soundtrack)
            missing_assets.append(f"soundtrack: {soundtrack}")
            soundtrack = None
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
            # are already finished — a "Footage" source turns every archived
            # original back into new footage, forever.
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

    # --- Playback reel background (optional; falls back to the Glambot logo) ---
    background = data.get("playback_background")
    background_path = None
    if background:
        p = Path(background)
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.exists():
            background_path = str(p)
        else:
            _warn_missing(project_dir.name, "playback background", background)
            missing_assets.append(f"playback background: {background}")
    background_opacity = _validate_int(
        data.get("playback_background_opacity"), "playback_background_opacity", project_dir, default=50
    )
    if not (0 <= background_opacity <= 100):
        raise ConfigError(
            f"{project_dir.name}/config.json: 'playback_background_opacity' must be 0-100, "
            f"got {background_opacity!r}"
        )

    # --- LAN / offline guest delivery ---------------------------------
    lan_delivery = bool(data.get("lan_delivery", False))
    offline_mode = bool(data.get("offline_mode", False))
    download_pin = data.get("download_pin")
    if download_pin in (None, ""):
        download_pin = None
    else:
        download_pin = str(download_pin)
        if not DOWNLOAD_PIN_RE.match(download_pin):
            raise ConfigError(
                f"{project_dir.name}/config.json: 'download_pin' must be 4-8 digits, got {download_pin!r}"
            )
    # A download_pin is optional for lan_delivery / offline_mode. When absent,
    # the guest download page and gallery are served without a PIN prompt
    # (see glambot/guest.py).

    # --- Advanced editing: colour grade + speed ramp ------------------
    grade = _parse_grade(data.get("grade"), "grade", project_dir)
    speed_ramp = _parse_speed_ramp(data.get("speed_ramp"), "speed_ramp", project_dir)

    # --- Per-project email template (optional; falls back to email_default.txt) ---
    email_subject = data.get("email_subject")
    email_body = data.get("email_body")
    for _name, _val in (("email_subject", email_subject), ("email_body", email_body)):
        if _val is not None and not isinstance(_val, str):
            raise ConfigError(f"{project_dir.name}/config.json: '{_name}' must be a string")
    email_subject = email_subject or None
    email_body = email_body or None

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
        source_fps=source_fps,
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
        vertical_overlay=vertical_overlay,
        vertical_overlay_position=vertical_overlay_position,
        vertical_overlay_scale=vertical_overlay_scale,
        vertical_overlay_x=vertical_overlay_x,
        vertical_overlay_y=vertical_overlay_y,
        horizontal_overlay=horizontal_overlay,
        horizontal_overlay_position=horizontal_overlay_position,
        horizontal_overlay_scale=horizontal_overlay_scale,
        horizontal_overlay_x=horizontal_overlay_x,
        horizontal_overlay_y=horizontal_overlay_y,
        output_dir=output_dir,
        playback_background=background_path,
        playback_background_opacity=background_opacity,
        lan_delivery=lan_delivery,
        download_pin=download_pin,
        offline_mode=offline_mode,
        grade=grade,
        speed_ramp=speed_ramp,
        email_subject=email_subject,
        email_body=email_body,
        missing_assets=missing_assets,
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
