"""Text-to-speech engines.

Selected at startup with ``--tts`` and switchable per board from the UI, so
the voice stack is a choice rather than a hardcoded dependency:

  * ``http-qwen3``  — Qwen3-TTS through a dashboard's HTTP service. The same voices the
                     rest of that system uses; needs ``--tts-url``.
  * ``vpipe-moss`` — MOSS-TTS 8B through vpipe's own text-to-speech stage.
                     Fully local, no extra service, and can clone a voice from
                     a character's reference clip.
  * ``none``       — no speech. Dialogue is written into the storyboard but
                     nothing is synthesised.
"""

from __future__ import annotations

from pathlib import Path

from .base import SpeechResult, TTSEngine, Voice
from .http_qwen3 import HttpQwen3TTS
from .vpipe_moss import VpipeMossTTS

__all__ = [
    "TTSEngine", "Voice", "SpeechResult",
    "HttpQwen3TTS", "VpipeMossTTS", "NullTTS",
    "build_tts", "TTS_IDS",
]

TTS_IDS = ("none", "vpipe-moss", "http-qwen3")


class NullTTS(TTSEngine):
    id = "none"
    label = "No speech synthesis"

    def health(self) -> tuple[bool, str]:
        return False, "No TTS engine selected — dialogue will not be spoken."

    def synth(self, text, out_path, *, voice=None, reference=None) -> SpeechResult:
        return SpeechResult(engine=self.id, error="no TTS engine selected")


def build_tts(kind: str, *, vpipe_binary: Path, workspace: Path,
              tts_url: str = "") -> TTSEngine:
    if kind == "vpipe-moss":
        return VpipeMossTTS(binary=vpipe_binary, workspace=workspace)
    if kind == "http-qwen3":
        return HttpQwen3TTS(base_url=tts_url)
    if kind in ("none", "", None):
        return NullTTS()
    raise ValueError(f"unknown TTS engine: {kind!r} (expected one of {TTS_IDS})")
