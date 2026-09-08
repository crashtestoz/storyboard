"""Speak a shot's dialogue and lay it over the rendered clip.

Kept out of the render path on purpose. Speech is cheap and the video is not,
so a line can be rewritten and re-dubbed in seconds without touching a clip
that took half an hour — and a dud take of the voice never costs a re-render.

The result is written alongside the clip as ``clip-dubbed.mp4``; the original
is left untouched so both remain available for comparison.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .tts.base import SpeechResult, TTSEngine


@dataclass
class DubResult:
    ok: bool = False
    video: Path | None = None
    audio: Path | None = None
    speech: SpeechResult | None = None
    error: str = ""
    log: list[str] = field(default_factory=list)
    # set when the line does not fit the clip; the mux still succeeds
    warning: str = ""


def _duration(path: Path) -> float:
    """Media length via ffprobe; 0.0 when it cannot be determined."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def dub_shot(
    engine: TTSEngine,
    *,
    clip: Path,
    shot_dir: Path,
    text: str,
    voice: str | None = None,
    reference: Path | None = None,
    keep_original_audio: bool = True,
) -> DubResult:
    """Synthesise *text* and mux it onto *clip*."""
    text = (text or "").strip()
    if not text:
        return DubResult(error="this shot has no dialogue to speak")
    if not clip.exists():
        return DubResult(error="render the shot before dubbing it")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return DubResult(error="ffmpeg not found on PATH — needed to mux the speech")

    shot_dir.mkdir(parents=True, exist_ok=True)
    speech_path = shot_dir / "dialogue.wav"
    speech = engine.synth(text, speech_path, voice=voice, reference=reference)
    if not speech.ok:
        return DubResult(error=speech.error or "speech synthesis failed",
                         speech=speech, log=speech.log)

    out = shot_dir / "clip-dubbed.mp4"

    if keep_original_audio:
        # Mix speech over the generated soundtrack rather than replacing it:
        # the engine roar and spray are part of what the video model made, and
        # dropping them for one line would be a downgrade. Speech is lifted a
        # little and the bed ducked so the line stays intelligible.
        filt = (
            "[0:a]volume=0.55[bed];"
            "[1:a]volume=1.4,apad[voice];"
            "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        argv = [
            ffmpeg, "-y", "-i", str(clip), "-i", str(speech_path),
            "-filter_complex", filt,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(out),
        ]
    else:
        argv = [
            ffmpeg, "-y", "-i", str(clip), "-i", str(speech_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(out),
        ]

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        return DubResult(
            error=f"ffmpeg failed (exit {proc.returncode})",
            speech=speech,
            log=tail,
        )

    # -shortest keeps the muxed file to the video's length, so a line longer
    # than its clip is cut off. That is the right file to produce, but it must
    # not happen silently — the fix is a longer clip or a shorter line.
    warning = ""
    clip_seconds = _duration(clip)
    if clip_seconds and speech.seconds > clip_seconds + 0.15:
        warning = (
            f"the spoken line runs {speech.seconds:.1f}s but the clip is only "
            f"{clip_seconds:.1f}s, so it is cut off — lengthen the shot or "
            f"shorten the line"
        )

    return DubResult(ok=True, video=out, audio=speech_path, speech=speech,
                     warning=warning)
