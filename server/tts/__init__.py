"""Speech engines, described by a config file rather than hardcoded.

A voice model in this network is usually **already running somewhere with an
owner** — Qwen3-TTS as a FastAPI process next to MCC, MCC's own endpoint
driving a sherpa-onnx binary from that app's environment. So this project
links to services by name and URL instead of installing models of its own.
That keeps one copy of the weights, one lifecycle to maintain, and this
project's "nothing to install" property.

Services are listed in ``tts-services.json`` at the project root, created with
sensible defaults on first run and meant to be edited::

    {
      "services": [
        {"id": "qwen3-clone", "label": "Qwen3-TTS voice clone (OptiPlex)",
         "kind": "qwen3-clone", "url": "http://127.0.0.1:8790"},
        {"id": "mcc-sherpa",  "label": "MCC sherpa-onnx voice",
         "kind": "mcc-sherpa", "url": "http://127.0.0.1:3000"},
        {"id": "vpipe-moss",  "label": "MOSS-TTS via vpipe (local)",
         "kind": "vpipe-moss"}
      ]
    }

``kind`` selects the client; ``id`` is what a board stores. Adding another
instance is a new entry, not a code change. The one local engine
(``vpipe-moss``) needs no URL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import SpeechResult, TTSEngine, Voice
from .http_services import MccSherpaTTS, Qwen3CloneTTS
from .vpipe_moss import VpipeMossTTS

__all__ = [
    "TTSEngine", "Voice", "SpeechResult",
    "NullTTS", "load_engines", "DEFAULT_SERVICES", "CONFIG_NAME",
]

CONFIG_NAME = "tts-services.json"

DEFAULT_SERVICES: list[dict[str, Any]] = [
    {
        "id": "vpipe-moss",
        "label": "MOSS-TTS 8B via vpipe (local, clones a voice)",
        "kind": "vpipe-moss",
    },
    {
        "id": "qwen3-clone",
        "label": "Qwen3-TTS voice clone (same server MCC uses)",
        "kind": "qwen3-clone",
        # MCC's default. It binds 127.0.0.1 on the machine it runs on, so
        # point this at that host (and rebind or tunnel it) to use it remotely.
        "url": "http://127.0.0.1:8790",
    },
    {
        "id": "mcc-sherpa",
        "label": "MCC sherpa-onnx voice (plain, no cloning)",
        "kind": "mcc-sherpa",
        "url": "http://127.0.0.1:3000",
    },
]


class NullTTS(TTSEngine):
    id = "none"
    label = "No speech synthesis"

    def health(self) -> tuple[bool, str]:
        return False, "No speech engine selected — dialogue will not be spoken."

    def synth(self, text, out_path, *, voice=None, reference=None, **_) -> SpeechResult:
        return SpeechResult(engine=self.id, error="no speech engine selected")


def load_config(project_root: Path) -> list[dict[str, Any]]:
    """Read ``tts-services.json``, writing the defaults if it is absent."""
    path = Path(project_root) / CONFIG_NAME
    if not path.exists():
        try:
            path.write_text(json.dumps({"services": DEFAULT_SERVICES}, indent=2) + "\n")
        except OSError:
            return list(DEFAULT_SERVICES)
        return list(DEFAULT_SERVICES)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A broken config should not stop the app starting; fall back and say
        # so through the engine's own health message.
        return [{"id": "config-error", "label": f"unreadable {CONFIG_NAME}",
                 "kind": "broken"}]
    services = doc.get("services")
    return services if isinstance(services, list) else list(DEFAULT_SERVICES)


class BrokenTTS(TTSEngine):
    def __init__(self, service_id: str, label: str, why: str):
        self.id = service_id
        self.label = label
        self._why = why

    def health(self) -> tuple[bool, str]:
        return False, self._why

    def synth(self, text, out_path, *, voice=None, reference=None, **_) -> SpeechResult:
        return SpeechResult(engine=self.id, error=self._why)


def build_one(entry: dict[str, Any], *, vpipe_binary: Path,
              workspace: Path) -> TTSEngine:
    kind = entry.get("kind") or entry.get("id")
    sid = str(entry.get("id") or kind or "unnamed")
    label = str(entry.get("label") or sid)
    url = str(entry.get("url") or "")

    if kind == "vpipe-moss":
        eng = VpipeMossTTS(binary=vpipe_binary, workspace=workspace)
        eng.id, eng.label = sid, label
        return eng
    if kind == "qwen3-clone":
        return Qwen3CloneTTS(sid, label, url)
    if kind == "mcc-sherpa":
        return MccSherpaTTS(sid, label, url)
    return BrokenTTS(
        sid, label,
        f"unknown service kind {kind!r} in {CONFIG_NAME} "
        "(expected vpipe-moss, qwen3-clone or mcc-sherpa)",
    )


def load_engines(project_root: Path, *, vpipe_binary: Path,
                 workspace: Path) -> dict[str, TTSEngine]:
    engines: dict[str, TTSEngine] = {"none": NullTTS()}
    for entry in load_config(project_root):
        eng = build_one(entry, vpipe_binary=vpipe_binary, workspace=workspace)
        engines[eng.id] = eng
    return engines
