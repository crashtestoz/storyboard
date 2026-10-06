"""What a shot's current clip was actually rendered with.

``renderedWith`` on a shot: {engine, turbo, steps, width, height, frames}.
The orchestrator writes it at the end of every render. A locked shot keeps
it as its frozen record, so its resolution, length and steps read as the
clip it holds rather than as whatever the project is set to now.

Clips rendered before the record existed get a best-effort one: the picture
size from the file itself, steps and engine from the render-timing log row
whose duration matches the shot's own runtime, and the shot's own fields
for anything neither can tell.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

GEOMETRY = ("width", "height", "frames")


def _clip_path(shot: dict[str, Any], data_dir: Path) -> Path | None:
    for url in shot.get("outputs") or []:
        if isinstance(url, str) and url.startswith("/media/") and url.split("?")[0].endswith(".mp4"):
            return data_dir / unquote(urlsplit(url).path[len("/media/"):])
    return None


def _probe_size(path: Path) -> tuple[int, int] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        w, h = (int(v) for v in out.split(",")[:2])
        return w, h
    except Exception:  # noqa: BLE001 - best effort
        return None


def _timing_row(shot: dict[str, Any], timings_path: Path | None,
                width: int, height: int) -> dict[str, Any] | None:
    """The render-timing row for this clip: same size, and a duration that
    matches the shot's recorded runtime to the tenth of a second."""
    secs = shot.get("runtimeSeconds")
    if not secs or timings_path is None:
        return None
    try:
        rows = json.loads(timings_path.read_text()).get("renders") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    hits = [r for r in rows
            if abs(float(r.get("seconds") or 0) - float(secs)) < 0.06
            and r.get("width") == width and r.get("height") == height]
    return hits[-1] if hits else None


def ensure_render_record(shot: dict[str, Any], data_dir: Path,
                         timings_path: Path | None = None) -> bool:
    """Fill in ``renderedWith`` where it lacks the clip's geometry.

    Returns True when the shot changed. A shot with no rendered clip is left
    alone: there is nothing to describe.
    """
    rec = dict(shot.get("renderedWith") or {})
    if all(rec.get(k) for k in GEOMETRY) and rec.get("steps"):
        return False
    clip = _clip_path(shot, data_dir)
    size = _probe_size(clip) if clip else None
    if size is None:
        return False
    w, h = size
    row = _timing_row(shot, timings_path, w, h)
    model = str((row or {}).get("model") or "")
    rec.setdefault("engine", "h3c" if model.startswith("h3c") else
                   "vpipe" if model else "")
    rec.setdefault("turbo", model.startswith("h3c-turbo"))
    rec["steps"] = rec.get("steps") or (row or {}).get("steps") or shot.get("steps")
    rec["width"], rec["height"] = w, h
    rec["frames"] = rec.get("frames") or shot.get("frames")
    shot["renderedWith"] = rec
    return True
