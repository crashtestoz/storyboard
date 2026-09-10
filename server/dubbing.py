"""Speak a shot's dialogue, and lay it over the clip once there is one.

Kept out of the render path on purpose. Speech is cheap and the video is not,
so a line can be rewritten and re-spoken in seconds without touching a clip
that took half an hour — and a dud take of the voice never costs a re-render.

That argument runs the other way too, which is why synthesis and muxing are
separate here: hearing a line in a character's voice should not require having
rendered the shot first. Half an hour of video to find out a cloned voice says
the line wrong is exactly the wrong order. So ``speak_line`` stands alone and
``dub_shot`` mixes its result over the video when a video exists.

The muxed result is written alongside the clip as ``clip-dubbed.mp4``; the
original is left untouched so both remain available for comparison.
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
    video: Path | None = None      # None when there was no clip to mux into
    audio: Path | None = None
    speech: SpeechResult | None = None
    error: str = ""
    log: list[str] = field(default_factory=list)
    # set when the line does not fit the clip; the mux still succeeds
    warning: str = ""


# Trailing silence is trimmed at this threshold, keeping this much of a margin
# so a line does not start or end abruptly.
SILENCE_DB = -45
KEEP_MARGIN = 0.08

# EBU R128 target. -16 LUFS is the usual spoken-word level; the true-peak
# ceiling leaves headroom for the mix over the clip's own soundtrack.
TARGET_LUFS = -16
TRUE_PEAK_DB = -1.5


def polish_speech(path: Path) -> tuple[float, list[str]]:
    """Trim silence off both ends and bring the level up. Returns (seconds, log).

    Both problems are real and measured, not theoretical. MOSS 8B runs to its
    token budget emitting silent frames once it has finished the line — one
    take here was 79.4s long with speech ending at 5.2s — and vpipe's own
    source notes that its audio head "degenerates into silent loops". Level is
    the other half: the same take peaked at -23 dBFS, which is far too quiet to
    sit under a generated soundtrack.

    Only the ends are trimmed. Silence *inside* a line is the pauses between
    words and sentences, and removing that would make the delivery unnatural.
    """
    log: list[str] = []
    ffmpeg = shutil.which("ffmpeg")
    before = _duration(path)
    if not ffmpeg:
        log.append("[WARN] ffmpeg not on PATH — speech not trimmed or levelled")
        return before, log

    tmp = path.with_suffix(".polish.wav")
    # silenceremove only trims the *start*, so the tail is done by reversing,
    # trimming the new start, and reversing back.
    trim = (
        f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB"
        f":start_silence={KEEP_MARGIN}"
    )
    filt = (
        f"{trim},areverse,{trim},areverse,"
        f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK_DB}:LRA=11"
    )
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(path),
         "-af", filt, "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        log.append(f"[WARN] could not trim/level the speech: {' '.join(tail)}")
        return before, log

    tmp.replace(path)
    after = _duration(path)
    if before and after and before - after > 0.05:
        log.append(
            f"[INFO] trimmed {before - after:.1f}s of silence off the ends "
            f"({before:.1f}s -> {after:.1f}s)"
        )
    log.append(f"[INFO] levelled to {TARGET_LUFS} LUFS, true peak {TRUE_PEAK_DB} dB")
    return after or before, log


def speaker_for(shot: dict, board: dict) -> dict | None:
    """Which cast member says this shot's line.

    An explicit ``speakerId`` wins, and is honoured even for someone not in
    this shot's cast — a voice over a shot they do not appear in is a real
    thing to want. Otherwise it comes from the shot's own cast, preferring
    someone who has a recorded voice: a shot with one character in it should
    not have to be told who is talking.
    """
    cast = {c["id"]: c for c in (board.get("characters") or []) if c.get("id")}
    in_shot = [cid for cid in (shot.get("characterIds") or []) if cid in cast]

    chosen = shot.get("speakerId") or ""
    if chosen and chosen in cast:
        return cast[chosen]
    for cid in in_shot:
        if (cast[cid].get("voice") or {}).get("path"):
            return cast[cid]
    return cast[in_shot[0]] if in_shot else None


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


def speak_line(
    engine: TTSEngine,
    *,
    shot_dir: Path,
    text: str,
    voice: str | None = None,
    reference: Path | None = None,
    reference_text: str = "",
    style: str = "",
) -> SpeechResult:
    """Synthesise *text* to ``dialogue.wav``. No video involved.

    This is what a preview needs: the line, in the right voice, audible now.
    """
    text = (text or "").strip()
    if not text:
        return SpeechResult(engine=engine.id, error="this shot has no dialogue to speak")

    shot_dir.mkdir(parents=True, exist_ok=True)
    # Only the cloning engines take a transcript; the others ignore the kwarg
    # via **_ in their signature, so this stays one call site.
    speech = engine.synth(
        text,
        shot_dir / "dialogue.wav",
        voice=voice,
        reference=reference,
        reference_text=reference_text,
        style=style,
    )
    if not speech.ok:
        return speech

    # Every engine gets the same treatment, because the failure modes are the
    # model's rather than the integration's: a tail of silence and a level too
    # low to sit in a mix. Done here rather than per engine so no engine can
    # be added later that quietly skips it.
    seconds, plog = polish_speech(speech.path)
    speech.seconds = seconds or speech.seconds
    speech.log = list(speech.log or []) + plog
    return speech


def dub_shot(
    engine: TTSEngine,
    *,
    clip: Path,
    shot_dir: Path,
    text: str,
    voice: str | None = None,
    reference: Path | None = None,
    reference_text: str = "",
    style: str = "",
    keep_original_audio: bool = True,
) -> DubResult:
    """Synthesise *text*, and mux it onto *clip* if the clip exists.

    A missing clip is not an error. The speech is the useful artefact on its
    own — you can hear whether the line and the voice are right — and there is
    no reason to withhold it until the shot has been rendered.
    """
    speech = speak_line(engine, shot_dir=shot_dir, text=text, voice=voice,
                        reference=reference, reference_text=reference_text,
                        style=style)
    if not speech.ok:
        return DubResult(error=speech.error or "speech synthesis failed",
                         speech=speech, log=speech.log)
    speech_path = speech.path

    if not clip.exists():
        # Nothing to lay it over yet; the line itself is still the point.
        return DubResult(ok=True, video=None, audio=speech_path, speech=speech)

    muxed, error, log, warning = mux_speech(
        clip=clip, speech=speech_path, shot_dir=shot_dir,
        speech_seconds=speech.seconds,
        keep_original_audio=keep_original_audio,
    )
    if error:
        return DubResult(error=error, speech=speech, log=log)
    if muxed is None:
        # ffmpeg missing: the line itself is still worth returning.
        return DubResult(ok=True, video=None, audio=speech_path, speech=speech,
                         warning=warning, log=log)
    return DubResult(ok=True, video=muxed, audio=speech_path, speech=speech,
                     warning=warning, log=log)


def mux_speech(
    *,
    clip: Path,
    speech: Path,
    shot_dir: Path,
    speech_seconds: float = 0.0,
    keep_original_audio: bool = True,
) -> tuple[Path | None, str, list[str], str]:
    """Lay *speech* over *clip*, writing ``clip-dubbed.mp4`` beside it.

    Split out from :func:`dub_shot` because the two halves are needed apart:
    a line is usually spoken before the shot has been rendered (that is the
    whole point — half an hour of video to learn the take is wrong is the
    wrong order), and at that moment there is no clip to mux into. So the
    render has to be able to come back afterwards and do just this half,
    without paying for synthesis again.

    Returns ``(output or None, error, log, warning)``. A None output with no
    error means ffmpeg was unavailable.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return (
            None, "",
            [],
            "ffmpeg not found on PATH, so the speech was not mixed onto the "
            "clip — the line itself is above",
        )

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
            ffmpeg, "-y", "-i", str(clip), "-i", str(speech),
            "-filter_complex", filt,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(out),
        ]
    else:
        argv = [
            ffmpeg, "-y", "-i", str(clip), "-i", str(speech),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(out),
        ]

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        return None, f"ffmpeg failed (exit {proc.returncode})", tail, ""

    # -shortest keeps the muxed file to the video's length, so a line longer
    # than its clip is cut off. That is the right file to produce, but it must
    # not happen silently — the fix is a longer clip or a shorter line.
    warning = ""
    spoken = speech_seconds or _duration(speech)
    clip_seconds = _duration(clip)
    if clip_seconds and spoken > clip_seconds + 0.15:
        warning = (
            f"the spoken line runs {spoken:.1f}s but the clip is only "
            f"{clip_seconds:.1f}s, so it is cut off — lengthen the shot or "
            f"shorten the line"
        )
    return out, "", [], warning
