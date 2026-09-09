"""Text-to-speech engine contract.

Speech is a separate concern from picture generation, so it gets its own
pluggable layer rather than being folded into the render backend: the video
model and the voice engine are chosen independently, and either can be
swapped without touching the other.

Why speech is synthesised separately at all, rather than asked of the video
model: MiniMax H3 does generate a soundtrack in the same denoise loop as the
picture, but everything its documentation demonstrates is ambient or musical —
water, wind, birdsong, engine noise. Nothing claims intelligible, lip-synced
dialogue from a written line. So a spoken line is produced by a TTS engine and
muxed onto the finished clip, which is reliable, re-runnable without
re-rendering the video, and lets the voice be chosen per character.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Voice:
    """One selectable voice."""

    id: str
    label: str
    # a voice the engine ships, versus one cloned from a reference clip
    kind: str = "preset"      # "preset" | "clone"
    language: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "language": self.language}


@dataclass
class SpeechResult:
    path: Path | None = None
    seconds: float = 0.0
    engine: str = ""
    voice: str = ""
    log: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.path is not None and self.path.exists()


class TTSEngine:
    """Base class for a speech engine."""

    id: str = "base"
    label: str = "Base"

    # does this engine accept a reference clip to clone a voice from?
    supports_cloning: bool = False

    # can it turn a clip back into text? Cloning and transcription are
    # separate capabilities and one does not imply the other: MOSS clones a
    # voice but has no speech recognition, so a UI that offered "transcribe"
    # on the strength of cloning alone sent people to a dead end.
    supports_transcription: bool = False

    def health(self) -> tuple[bool, str]:
        """(usable, message). Surfaced in the UI so a missing model or an
        unreachable service says so before a render, not after."""
        return True, ""

    def voices(self) -> list[Voice]:
        return []

    def synth(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        reference: Path | None = None,
        **kwargs: Any,
    ) -> SpeechResult:
        """Write speech for *text* to *out_path* (a .wav).

        ``reference`` is an audio clip to clone the voice from, for engines
        that support it — which is how a character's recorded voice reaches
        their generated dialogue.
        """
        raise NotImplementedError
