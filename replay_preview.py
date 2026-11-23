"""ReplayMod 1.21 player packet indexing for the optional camera preview."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_PREVIEW_CACHE_VERSION = 4


def _cache_file(source: Path) -> Path:
    recording = source / "recording.tmcpr" if source.is_dir() else source
    stat = recording.stat()
    identity = f"{recording.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    key = hashlib.sha1(identity).hexdigest()
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "ReplayModTimelineFixer" / "camera-cache"
    return root / f"{key}.json"


def load_preview_data(source: Path, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Load cached entity and block-frame data or decode the replay in Node."""
    target_text = json.dumps(targets, separators=(",", ":"), sort_keys=True)
    target_hash = hashlib.sha1(f"{_PREVIEW_CACHE_VERSION}|{target_text}".encode("utf-8")).hexdigest()
    cache = _cache_file(source).with_name(f"{_cache_file(source).stem}-{target_hash}.json")
    if cache.is_file():
        try:
            value = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("entities"), dict) and isinstance(value.get("scenes"), dict):
                return value
        except (OSError, json.JSONDecodeError):
            pass

    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for camera preview. Install Node.js, then run npm install in the app folder.")
    script = Path(__file__).with_name("replay_preview.js")
    cache.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [node, str(script), str(source)],
        cwd=script.parent,
        input=target_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip() and not line.startswith("Decoded ")]
        raise RuntimeError(lines[0] if lines else "Replay decoder failed.")
    try:
        preview_data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Replay decoder returned invalid data.") from exc
    if not isinstance(preview_data, dict) or not isinstance(preview_data.get("entities"), dict):
        raise RuntimeError("Replay decoder returned invalid entity and block data.")

    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(preview_data, separators=(",", ":")), encoding="utf-8")
    temporary.replace(cache)
    return preview_data


def project_player(
    camera_position: tuple[float, float, float],
    camera_rotation: tuple[float, float, float],
    player_position: tuple[float, float, float],
    width: float,
    height: float,
    fov_degrees: float = 70.0,
) -> tuple[float, float] | None:
    """Project a Minecraft world position into the camera frame, if in view."""
    projected = project_camera_point(camera_position, camera_rotation, player_position, width, height, fov_degrees)
    if projected is None:
        return None
    screen_x, screen_y, _depth = projected
    if not (0 <= screen_x <= width and 0 <= screen_y <= height):
        return None
    return screen_x, screen_y


def project_camera_point(
    camera_position: tuple[float, float, float],
    camera_rotation: tuple[float, float, float],
    world_position: tuple[float, float, float],
    width: float,
    height: float,
    fov_degrees: float = 70.0,
) -> tuple[float, float, float] | None:
    """Project a world point and return screen coordinates plus camera depth."""
    import math

    px, py, pz = world_position
    cx, cy, cz = camera_position
    yaw, pitch, roll = (math.radians(value) for value in camera_rotation)
    dx, dy, dz = px - cx, py - cy, pz - cz

    depth = -dx * math.sin(yaw) * math.cos(pitch) - dy * math.sin(pitch) + dz * math.cos(yaw) * math.cos(pitch)
    unrolled_right = dx * math.cos(yaw) + dz * math.sin(yaw)
    unrolled_up = -dx * math.sin(yaw) * math.sin(pitch) + dy * math.cos(pitch) + dz * math.cos(yaw) * math.sin(pitch)
    screen_right = unrolled_right * math.cos(roll) + unrolled_up * math.sin(roll)
    screen_up = unrolled_up * math.cos(roll) - unrolled_right * math.sin(roll)
    if depth <= 0.05:
        return None

    focal = width / (2 * math.tan(math.radians(fov_degrees) / 2))
    screen_x = width / 2 + screen_right / depth * focal
    screen_y = height / 2 - screen_up / depth * focal
    return screen_x, screen_y, depth
