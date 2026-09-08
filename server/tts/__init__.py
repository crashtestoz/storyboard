"""Text-to-speech engines.

Selected at startup with ``--tts`` and switchable per board from the UI, so
the voice stack is a choice rather than a hardcoded dependency:

  * ``mcc-qwen3``  — Qwen3-TTS through MCC's HTTP service. The same voices the
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
from .mcc_qwen3 import MccQwen3TTS
from .vpipe_moss import VpipeMossTTS

__all__ = [
    "TTSEngine", "Voice", "SpeechResult",
    "MccQwen3TTS", "VpipeMossTTS", "NullTTS",
    "build_tts", "TTS_IDS",
]

TTS_IDS = ("none", "vpipe-moss", "mcc-qwen3")


class NullTTS(TTSEngine):
    id = "none"
    label = "No speech synthesis"

    def health(self) -> tuple[bool, str]:
        return False, "No TTS engine selected — dialogue will not be spoken."

    def synth(self, text, out_path, *, voice=None, reference=None) -> SpeechResult:
        return SpeechResult(engine=self.id, error="no TTS engine selected")


def build_tts(kind: str, *, vpipe_binary: Path, workspace: Path,
              mcc_url: str = "") -> TTSEngine:
    if kind == "vpipe-moss":
        return VpipeMossTTS(binary=vpipe_binary, workspace=workspace)
    if kind == "mcc-qwen3":
        return MccQwen3TTS(base_url=mcc_url)
    if kind in ("none", "", None):
        return NullTTS()
    raise ValueError(f"unknown TTS engine: {kind!r} (expected one of {TTS_IDS})")
