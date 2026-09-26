"""Soundtrack engines — music for the whole cut, described by a config file.

Same "link, don't vendor" rule as the speech engines (see ``server/tts``):
the music model lives in its own folder with its own environment and
weights, installed by ``setup/install-stable-audio-3.sh``, and this project
only knows where it is. Engines are listed in ``soundtrack-services.json``::

    {
      "services": [
        {"id": "sa3-mlx", "label": "Stable Audio 3 (MLX, local)",
         "kind": "sa3-mlx",
         "path": "/Volumes/KINGSTON/ai-diffusers/stable-audio-3/optimized/mlx"}
      ]
    }

The music is kept separate from the Background sound effects on purpose.
Those are rendered by H3 inside each shot; this is one continuous piece laid
under the finished cut, which a per-shot model cannot produce.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..local_config import ensure_local_copy

CONFIG_NAME = "soundtrack-services.json"
KNOWN_KINDS = ("sa3-mlx",)

# The DiT choices the MLX runtime offers for music, with the longest clip each
# was trained for. sm-sfx is left out: it makes sound effects, and those are
# H3's job here.
MODELS: dict[str, dict[str, Any]] = {
    "sm-music": {"label": "Small music — fast", "maxSeconds": 120, "decoder": "same-s"},
    "medium": {"label": "Medium — higher quality", "maxSeconds": 380, "decoder": "same-l"},
}
DEFAULT_MODEL = "sm-music"


@dataclass
class SoundtrackResult:
    ok: bool = False
    path: Path | None = None
    seconds: float = 0.0
    error: str = ""
    log: list[str] = field(default_factory=list)


class SoundtrackEngine:
    id = "none"
    label = "No soundtrack engine"
    kind = "none"

    def health(self) -> tuple[bool, str]:
        return False, "No soundtrack engine is configured — run setup/install-stable-audio-3.sh."

    def models(self) -> list[dict[str, Any]]:
        return []

    def generate(self, prompt: str, out: Path, *, seconds: float, model: str,
                 seed: int, init_audio: Path | None = None,
                 init_noise_level: float = 1.0) -> SoundtrackResult:
        return SoundtrackResult(error=self.health()[1])

    def to_json(self) -> dict[str, Any]:
        ok, msg = self.health()
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "healthy": ok, "message": msg, "models": self.models()}


class BrokenEngine(SoundtrackEngine):
    def __init__(self, sid: str, label: str, why: str):
        self.id, self.label, self._why = sid, label, why

    def health(self) -> tuple[bool, str]:
        return False, self._why


def load_config(project_root: Path) -> list[dict[str, Any]]:
    path = ensure_local_copy(project_root, CONFIG_NAME, {"services": []})
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return [{"id": "config-error", "label": f"unreadable {CONFIG_NAME}", "kind": "broken"}]
    services = doc.get("services")
    return services if isinstance(services, list) else []


def build_one(entry: dict[str, Any]) -> SoundtrackEngine:
    kind = entry.get("kind")
    sid = str(entry.get("id") or kind or "unnamed")
    label = str(entry.get("label") or sid)
    if kind == "sa3-mlx":
        from .sa3_mlx import Sa3MlxEngine
        return Sa3MlxEngine(sid, label, Path(str(entry.get("path") or "")).expanduser())
    return BrokenEngine(sid, label, f"unknown soundtrack engine kind {kind!r} in {CONFIG_NAME} "
                                    f"(expected {', '.join(KNOWN_KINDS)})")


def load_engines(project_root: Path) -> dict[str, SoundtrackEngine]:
    engines: dict[str, SoundtrackEngine] = {}
    for entry in load_config(project_root):
        if isinstance(entry, dict):
            eng = build_one(entry)
            engines[eng.id] = eng
    return engines
