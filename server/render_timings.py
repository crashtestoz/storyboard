"""How long renders actually took on this machine.

The only basis for any time estimate: the same shot takes ~35 minutes on one
Mac and under 5 on another, so a formula fitted on one machine is wrong
everywhere else -- and a runtime check against it flags good renders on a
fast machine as suspiciously quick. Kept per machine, beside
server-config.json and gitignored like it.
"""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any

FILE_NAME = "render-timings.json"
MAX_ROWS = 200


class RenderTimings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.path.read_text()).get("renders")
        except (OSError, json.JSONDecodeError, AttributeError):
            return []
        return rows if isinstance(rows, list) else []

    def record(self, *, model: str, width: int, height: int, frames: int,
               steps: int, seconds: float) -> None:
        if not model or seconds <= 0 or min(width, height, frames, steps) <= 0:
            return
        with self._lock:
            rows = self._load()
            rows.append({"model": model, "width": width, "height": height,
                         "frames": frames, "steps": steps,
                         "seconds": round(seconds, 1), "at": round(time.time())})
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"renders": rows[-MAX_ROWS:]}, indent=1) + "\n")
            tmp.replace(self.path)

    def estimate(self, *, model: str, width: int, height: int, frames: int,
                 steps: int) -> float | None:
        """Seconds, from this machine's own renders of *model* -- or None
        when none has been timed here yet, rather than a guess.

        An identical geometry uses the median of its recent runs. Otherwise
        the measured render closest in size is scaled by the work ratio
        (pixels x frames x steps), which is only approximate across very
        different sizes but is always anchored to a real measurement.
        """
        rows = [r for r in self._load() if r.get("model") == model
                and all(isinstance(r.get(k), (int, float)) and r[k] > 0
                        for k in ("width", "height", "frames", "steps", "seconds"))]
        if not rows:
            return None
        key = (width, height, frames, steps)
        same = [r["seconds"] for r in rows
                if (r["width"], r["height"], r["frames"], r["steps"]) == key]
        if same:
            return float(statistics.median(same[-5:]))
        work = width * height * frames * steps
        if work <= 0:
            return None

        def row_work(r: dict[str, Any]) -> float:
            return r["width"] * r["height"] * r["frames"] * r["steps"]

        nearest = min(rows, key=lambda r: abs(math.log(row_work(r) / work)))
        return float(nearest["seconds"]) * work / row_work(nearest)
