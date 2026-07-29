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

VALID_OVERLAY_POSITIONS = {
    "full",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "custom",
}

VALID_DELIVERY_MODES = {"email", "qr_only"}


class ConfigError(ValueError):
    """Raised when a project's config.json is missing or invalid."""


@dataclass
class Trim:
    start: str
    end: str


@dataclass
class ProjectConfig:
    recipient_email: str
    bitrate: str
    resolution: str
    aspect_ratio: str
    overlay: str
    overlay_position: str
    trim: Trim
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_dir: Path | None = None
    fps: int | None = None
    overlay_scale: int | None = None
    overlay_x: float | None = None
    overlay_y: float | None = None
    delivery_mode: str = "email"

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])

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


def load_config(project_dir: Path) -> ProjectConfig:
    """Load and validate config.json from a project folder.

    Raises ConfigError with a human-readable message on any problem so it can
    be surfaced directly in the review app / logs.
    """
    config_path = project_dir / "config.json"
    if not config_path.exists():
        raise ConfigError(f"{project_dir.name}: no config.json found")

    try:
        data = json.loads(config_path.read_text())
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

    overlay = _require(data, "overlay", project_dir)
    # overlay path may be relative to the repo root (e.g. "overlays/brand.png")
    overlay_path = Path(overlay)
    if not overlay_path.is_absolute():
        overlay_path = Path.cwd() / overlay_path
    if not overlay_path.exists():
        raise ConfigError(
            f"{project_dir.name}/config.json: overlay file not found: {overlay}"
        )

    overlay_position = data.get("overlay_position", "full")
    if overlay_position not in VALID_OVERLAY_POSITIONS:
        raise ConfigError(
            f"{project_dir.name}/config.json: overlay_position must be one of "
            f"{sorted(VALID_OVERLAY_POSITIONS)}, got {overlay_position!r}"
        )

    fps = data.get("fps")
    if fps is not None:
        if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
            raise ConfigError(f"{project_dir.name}/config.json: 'fps' must be a positive integer, got {fps!r}")

    overlay_scale = data.get("overlay_scale")
    if overlay_scale is not None:
        if not isinstance(overlay_scale, (int, float)) or isinstance(overlay_scale, bool) or not (1 <= overlay_scale <= 100):
            raise ConfigError(
                f"{project_dir.name}/config.json: 'overlay_scale' must be a number between 1 and 100, got {overlay_scale!r}"
            )
        overlay_scale = int(overlay_scale)

    overlay_x = data.get("overlay_x")
    overlay_y = data.get("overlay_y")
    if overlay_position == "custom":
        for name, value in (("overlay_x", overlay_x), ("overlay_y", overlay_y)):
            if value is None or not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 <= value <= 100):
                raise ConfigError(
                    f"{project_dir.name}/config.json: '{name}' must be a number between 0 and 100 "
                    f"when overlay_position is 'custom', got {value!r}"
                )
    overlay_x = float(overlay_x) if overlay_x is not None else None
    overlay_y = float(overlay_y) if overlay_y is not None else None

    trim_data = _require(data, "trim", project_dir)
    if not isinstance(trim_data, dict) or "start" not in trim_data or "end" not in trim_data:
        raise ConfigError(
            f"{project_dir.name}/config.json: 'trim' must be an object with 'start' and 'end'"
        )
    _validate_timestamp(trim_data["start"], "trim.start", project_dir)
    _validate_timestamp(trim_data["end"], "trim.end", project_dir)
    trim = Trim(start=str(trim_data["start"]), end=str(trim_data["end"]))

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

    return ProjectConfig(
        recipient_email=recipient_email,
        bitrate=str(bitrate),
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        overlay=str(overlay_path),
        overlay_position=overlay_position,
        trim=trim,
        overrides=overrides,
        project_dir=project_dir,
        fps=fps,
        overlay_scale=overlay_scale,
        overlay_x=overlay_x,
        overlay_y=overlay_y,
        delivery_mode=delivery_mode,
    )
