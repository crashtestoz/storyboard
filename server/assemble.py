"""Concatenate the shots into one video.

The last step of a storyboard, and the one that was missing: a board could
render every shot and still leave the user with a folder of clips to join by
hand. This is the `ffmpeg` concat pass the design notes always intended, run
at the end of a full batch and available on its own from the UI.

Two decisions worth stating, because both are ways this could quietly produce
the wrong file:

*   **The dubbed clip wins, but only while it is current.** ``clip-dubbed.mp4``
    is built *from* ``clip.mp4``, so a re-render leaves it describing a video
    that no longer exists. Older than the clip means stale, and then the clip
    itself is the honest choice — a final cut missing a spoken line is a
    smaller lie than one showing the previous take.
*   **A gap is reported, never papered over.** A board with an unrendered shot
    still assembles, because seeing the cut you have is useful, but the result
    names the shots it had to leave out and the caller is expected to say so.

Everything is re-encoded rather than stream-copied. The inputs are not
uniform — a dubbed clip carries AAC audio, an undubbed one whatever the model
wrote, and a still-image model produces no audio track at all — and concat
with ``-c copy`` on mismatched streams produces a file that plays for exactly
as long as the first clip.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FINAL_NAME = "final.mp4"

# Fixed by the models the backends offer, and stated as such in the UI.
FPS = 24
SAMPLE_RATE = 48000


@dataclass
class AssemblyResult:
    ok: bool = False
    path: Path | None = None
    # (shot label, file used) in cut order
    parts: list[tuple[str, str]] = field(default_factory=list)
    # shot labels with no usable clip, in board order
    missing: list[str] = field(default_factory=list)
    seconds: float = 0.0
    settings_fingerprint: str = ""
    error: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.missing)

    def to_json(self, url: str = "") -> dict[str, Any]:
        return {
            "url": url,
            "builtAt": time.time(),
            "parts": [{"shot": label, "file": name} for label, name in self.parts],
            "missing": list(self.missing),
            "seconds": round(self.seconds, 2),
            "partial": self.partial,
            "settingsFingerprint": self.settings_fingerprint,
        }


def shot_clip(shot_dir: Path, shot: dict[str, Any] | None = None) -> Path | None:
    """The clip to put in the cut for this shot, or None if there isn't one."""
    clip = shot_dir / "clip.mp4"
    if not (clip.exists() and clip.stat().st_size > 1024):
        return None
    if shot and shot.get("renderedDialogueSource") == "native":
        return clip
    dubbed = shot_dir / "clip-dubbed.mp4"
    if (
        dubbed.exists()
        and dubbed.stat().st_size > 1024
        and dubbed.stat().st_mtime >= clip.stat().st_mtime
    ):
        if shot is not None:
            line = (shot.get("dialogue") or "").strip()
            spoken = (shot.get("dialogueSpokenText") or "").strip()
            style = (shot.get("dialogueStyle") or "").strip()
            spoken_style = (shot.get("dialogueSpokenStyle") or "").strip()
            if line and spoken and (spoken != line or spoken_style != style):
                return clip
        return dubbed
    return clip


def frame_size(res: str | None) -> tuple[int, int]:
    """Frame size from a ``"WxH"`` project setting, with the usual default.

    The cut has to be one geometry, and this is the one the board asked for —
    not whichever clip happens to be first, which in draft mode is smaller.
    """
    try:
        w, h = str(res or "").lower().split("x")
        return max(16, int(w)), max(16, int(h))
    except (ValueError, AttributeError):
        return 960, 544


def final_path(project_dir: Path) -> Path:
    return project_dir / FINAL_NAME


def final_stale_reason(board: dict[str, Any], project_dir: Path) -> str:
    """Why the assembled cut no longer matches the shots on disk.

    Empty when it is current, or when there is nothing to compare against
    because no cut has been built. The comparison is by modification time
    against the clips the recorded cut was made from, which is the only thing
    that actually decides whether the file is out of date.
    """
    record = board.get("finalVideo") or {}
    out = final_path(project_dir)
    if not record or not out.exists():
        return "not built yet" if _any_clip(board, project_dir) else ""

    if record.get("settingsFingerprint", assembly_fingerprint({})) != assembly_fingerprint(board):
        return "cut settings, trims or soundtrack changed"
    built = out.stat().st_mtime
    for i, shot in enumerate(board.get("shots") or []):
        if shot.get("dubUrl") and shot.get("renderedDialogueSource") != "native" and shot.get("dubAppliedMode") != shot.get("dubMode", "mix"):
            return "dialogue mix settings need to be applied again"
        clip = shot_clip(project_dir / "shots" / f"{i + 1:02d}", shot)
        if clip is None:
            continue
        if clip.stat().st_mtime > built:
            return "a shot has been re-rendered since this was assembled"

    recorded = record.get("parts") or []
    current = [
        clip
        for i, shot in enumerate(board.get("shots") or [])
        if (
            clip := shot_clip(project_dir / "shots" / f"{i + 1:02d}", shot)
        ) is not None
    ]
    if len(current) != len(recorded):
        return f"assembled from {len(recorded)} clip(s); {len(current)} are available now"
    for clip, part in zip(current, recorded):
        old = (part or {}).get("file") or ""
        if old and old != clip.name:
            return f"assembled with {old}; {clip.name} is available now"
    return ""


def unmixed_dialogue(board: dict[str, Any], project_dir: Path) -> dict[str, str]:
    """Shots whose spoken line exists but is not on the clip going in the cut.

    A line is normally spoken *before* the shot is rendered — that is the point
    of keeping synthesis out of the render path — and at that moment there is
    no video to mux into. A render finishing later now lays the take down (see
    ``Orchestrator._relay_speech``), but a clip rendered before that existed
    has a ``dialogue.wav`` beside it and silence in it, and the cut would go
    out without the line. If the selected clip already has audio, do not guess:
    without recognition we cannot prove the line is absent.
    """
    out: dict[str, str] = {}
    for i, shot in enumerate(board.get("shots") or []):
        line = (shot.get("dialogue") or "").strip()
        if not line:
            continue
        shot_dir = project_dir / "shots" / f"{i + 1:02d}"
        speech = shot_dir / "dialogue.wav"
        clip = shot_dir / "clip.mp4"
        if not (speech.exists() and clip.exists()):
            continue
        if shot_clip(shot_dir, shot) != clip:
            continue    # the dubbed clip is current, so the line is in the cut
        if _has_audio(clip):
            continue
        spoken = (shot.get("dialogueSpokenText") or "").strip()
        style = (shot.get("dialogueStyle") or "").strip()
        spoken_style = (shot.get("dialogueSpokenStyle") or "").strip()
        if spoken and spoken != line:
            out[shot["id"]] = "the line was edited after it was last spoken — generate it again"
        elif spoken_style != style:
            out[shot["id"]] = "the voice direction changed after it was last spoken — generate it again"
        else:
            out[shot["id"]] = (
                "spoken, but not mixed onto the clip — press “Generate” "
                "to lay it down"
            )
    return out


def _any_clip(board: dict[str, Any], project_dir: Path) -> bool:
    return any(
        shot_clip(project_dir / "shots" / f"{i + 1:02d}") is not None
        for i in range(len(board.get("shots") or []))
    )


def assembly_fingerprint(board: dict) -> str:
    settings = board.get("assembly") or {}
    trims = [[s.get("id"), s.get("trimIn", 0), s.get("trimOut", 0)]
             for s in board.get("shots", []) if s.get("trimIn") or s.get("trimOut")]
    payload: list[Any] = [settings, trims]
    # Only boards that have opened the Soundtrack settings carry the key, so
    # every cut assembled before it existed keeps its fingerprint.
    if "soundtrack" in board:
        payload.append([board.get("soundtrack"),
                        (board.get("soundtrackRender") or {}).get("key")])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def board_parts(board: dict, project_dir: Path) -> list[tuple[str, Path | None]]:
    """(label, clip or None) for every shot, in cut order."""
    parts = []
    for i, shot in enumerate(board.get("shots") or []):
        label = shot.get("title") or f"shot {i + 1}"
        parts.append((f"{i + 1:02d} {label}",
                      shot_clip(project_dir / "shots" / f"{i + 1:02d}", shot)))
    return parts


def clip_trims(board: dict, project_dir: Path) -> dict[str, list[float]]:
    trims = {}
    for i, shot in enumerate(board.get("shots", []), 1):
        clip = shot_clip(project_dir / "shots" / f"{i:02d}", shot)
        if clip:
            trims[str(clip)] = [shot.get("trimIn", 0), shot.get("trimOut", 0)]
    return trims


def board_options(board: dict, project_dir: Path, data_dir: Path) -> dict:
    from .soundtrack.board import settings as soundtrack_settings, usable_render

    options = dict(board.get("assembly") or {})
    options["fingerprint"] = assembly_fingerprint(board)
    options["trims"] = clip_trims(board, project_dir)
    ref = options.pop("backgroundAudio", None)
    music = soundtrack_settings(board)
    if music["enabled"] and music["source"] == "upload" and ref:
        path = (data_dir / ref["path"]).resolve()
        if not path.is_relative_to(data_dir.resolve()) or not path.is_file():
            raise ValueError("Soundtrack audio is missing or outside the projects folder")
        options["backgroundPath"] = str(path)
    elif music["enabled"] and music["source"] == "generate":
        path = usable_render(board, project_dir, data_dir)
        if path:
            options["backgroundPath"] = str(path)
    if options.get("backgroundPath") and music["duck"]:
        options["duck"] = {
            "db": music["duckDb"], "attack": music["duckAttack"],
            "release": music["duckRelease"], "keys": duck_keys(board, project_dir),
        }
    return options


def duck_keys(board: dict, project_dir: Path) -> dict[str, dict[str, Any]]:
    """Where each clip's dialogue is, for ducking the soundtrack under it.

    By the time a clip reaches the cut its line is already mixed into its
    audio together with H3's sound effects, so that audio cannot tell the
    two apart. The recorded take can: a dubbed clip's ``dialogue.wav`` starts
    at the clip's first frame (see ``dubbing.mux_speech``), so its speech is
    exactly where the line is. A native-dialogue clip has no separate take —
    H3 spoke the line itself — so its whole length is treated as dialogue:
    ducking too much music is recoverable, losing a line is not.
    """
    keys: dict[str, dict[str, Any]] = {}
    for i, shot in enumerate(board.get("shots") or [], 1):
        if not (shot.get("dialogue") or "").strip():
            continue
        shot_dir = project_dir / "shots" / f"{i:02d}"
        clip = shot_clip(shot_dir, shot)
        if clip is None:
            continue
        if shot.get("renderedDialogueSource") == "native":
            keys[str(clip)] = {"whole": True}
        elif clip.name == "clip-dubbed.mp4" and (shot_dir / "dialogue.wav").is_file():
            keys[str(clip)] = {"speech": str(shot_dir / "dialogue.wav")}
    return keys


def cut_timings(clips: list[Path], options: dict) -> tuple[list[tuple[float, float]], float]:
    """(trim start, kept duration) per clip and the cut's total length.

    The one place the cut's length is worked out, so the soundtrack generated
    ahead of assembly is exactly as long as the video it goes under.
    """
    transition = _seconds(options.get("transitionSeconds"), "Transition")
    if transition > 2:
        raise ValueError("Transition must be between 0 and 2")
    timings = []
    for clip in clips:
        start, tail = (options.get("trims") or {}).get(str(clip), [0, 0])
        start, tail = _seconds(start, "Trim start"), _seconds(tail, "Trim end")
        duration = _duration(clip) - start - tail
        if duration <= 0 or (transition and duration <= 2 * transition):
            raise ValueError(f"{clip.parent.name}: trims leave too little video for this transition")
        timings.append((start, duration))
    total = sum(d for _, d in timings)
    if transition and len(clips) > 1:
        total -= transition * (len(clips) - 1)
    return timings, total


def clip_offsets(timings: list[tuple[float, float]], transition: float) -> list[float]:
    """Where each clip starts in the cut; a crossfade overlaps neighbours."""
    offsets, at = [], 0.0
    overlap = transition if len(timings) > 1 else 0.0
    for _, duration in timings:
        offsets.append(at)
        at += duration - overlap
    return offsets


def speech_intervals(speech: Path, start: float, duration: float) -> list[tuple[float, float]]:
    """Spans of *speech* that are voiced, in the clip's trimmed time."""
    ffmpeg = shutil.which("ffmpeg")
    length = _duration(speech)
    if not ffmpeg or length <= 0:
        return [(0.0, duration)]    # cannot see inside it: assume it all speaks
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(speech),
             "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return [(0.0, duration)]
    silences, opened = [], None
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            opened = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and opened is not None:
            silences.append((opened, float(line.split("silence_end:")[1].split()[0])))
            opened = None
    if opened is not None:
        silences.append((opened, length))
    voiced, at = [], 0.0
    for s, e in silences:
        if s > at:
            voiced.append((at, s))
        at = max(at, e)
    if at < length:
        voiced.append((at, length))
    out = []
    for s, e in voiced:
        s, e = max(0.0, s - start), min(duration, e - start)
        if e > s:
            out.append((s, e))
    return out


def duck_expression(intervals: list[tuple[float, float]], db: float,
                    attack: float, release: float) -> str:
    """An ffmpeg ``volume`` expression that dips by *db* over each interval.

    The dialogue's timing is known before the mix, so the dip can start
    *attack* seconds ahead of the first word instead of reacting to it the
    way a sidechain compressor must, and recovers over *release* after the
    last. Spans closer together than a dip and a recovery are merged, so the
    music does not pump between sentences. Each span contributes a 0..1
    trapezoid; merged spans never overlap, so their sum stays within 0..1.
    """
    attack, release = max(attack, 0.01), max(release, 0.01)
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s - merged[-1][1] < attack + release:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if not merged or db <= 0:
        return "1"
    depth = 1 - 10 ** (-db / 20)
    ramps = "+".join(
        f"max(0,min(1,min((t-{s - attack:.3f})/{attack:.3f},({e + release:.3f}-t)/{release:.3f})))"
        for s, e in merged
    )
    return f"1-{depth:.4f}*({ramps})"


def _seconds(value, label):
    value = float(value or 0)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return value


def assemble(
    parts: list[tuple[str, Path | None]],
    out: Path,
    width: int,
    height: int,
    fps: int = FPS,
    options: dict | None = None,
) -> AssemblyResult:
    """Join *parts* — (label, clip or None) in cut order — into *out*."""
    options = options or {}
    res = AssemblyResult(settings_fingerprint=options.get("fingerprint", assembly_fingerprint({})))
    res.missing = [label for label, clip in parts if clip is None]
    clips = [(label, clip) for label, clip in parts if clip is not None]

    if not clips:
        res.error = (
            "nothing to assemble — no shot has a rendered clip yet"
            if parts
            else "this storyboard has no shots"
        )
        return res

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        res.error = "ffmpeg is not on PATH, so the shots cannot be joined"
        return res

    try:
        transition = _seconds(options.get("transitionSeconds"), "Transition")
        fade = _seconds(options.get("audioFadeSeconds"), "Audio fade")
        gain = _seconds(options.get("backgroundVolume", 0.15), "Background volume")
        if transition > 2 or fade > 2 or gain > 2:
            raise ValueError("Transition, fade and background volume must be between 0 and 2")
        timings, _ = cut_timings([clip for _, clip in clips], options)
    except (ValueError, TypeError) as exc:
        res.error = str(exc)
        return res

    argv = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-filter_complex_threads", "1"]
    for _, clip in clips:
        argv += ["-i", str(clip)]
    background = options.get("backgroundPath")
    if background:
        argv += ["-stream_loop", "-1", "-i", background]
    # A boundary between two clips is either crossfaded (acrossfade, below) or
    # a hard cut. Only a hard-cut edge should get its own afade: a boundary
    # already smoothed by acrossfade would otherwise ramp down twice (once
    # here, once in the crossfade itself), producing an audible dip in the
    # middle of what is meant to be a seamless blend.
    crossfading = bool(transition and len(clips) > 1)
    filters = []
    for i, ((_, clip), (start, duration)) in enumerate(zip(clips, timings)):
        filters.append(
            f"[{i}:v]trim=start={start}:duration={duration},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={fps},format=yuv420p,settb=AVTB[v{i}]"
        )
        if _has_audio(clip):
            audio = f"[{i}:a]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS"
        else:
            audio = f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=duration={duration}"
        audio += f",aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
        if options.get("normalizeAudio"):
            audio += ",loudnorm=I=-16:TP=-1.5:LRA=11"
        audio += f",apad,atrim=duration={duration},asetpts=PTS-STARTPTS"
        fade_in = fade if (i == 0 or not crossfading) else 0
        fade_out = fade if (i == len(clips) - 1 or not crossfading) else 0
        if fade_in:
            fade_in = min(fade_in, duration / 2)
            audio += f",afade=t=in:d={fade_in}"
        if fade_out:
            fade_out = min(fade_out, duration / 2)
            audio += f",afade=t=out:st={duration-fade_out}:d={fade_out}"
        filters.append(audio + f"[a{i}]")
    total = sum(d for _, d in timings)
    if transition and len(clips) > 1:
        accumulated = timings[0][1]
        video, audio = "v0", "a0"
        for i in range(1, len(clips)):
            filters.append(f"[{video}][v{i}]xfade=transition=fade:duration={transition}:offset={accumulated-transition}[vx{i}]")
            filters.append(f"[{audio}][a{i}]acrossfade=d={transition}:c1=tri:c2=tri[ax{i}]")
            accumulated += timings[i][1] - transition
            video, audio = f"vx{i}", f"ax{i}"
        filters += [f"[{video}]null[v]", f"[{audio}]anull[baseaudio]"]
        total = accumulated
    else:
        pairs = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
        filters.append(pairs + f"concat=n={len(clips)}:v=1:a=1[v][baseaudio]")
    if background:
        duck = ""
        if options.get("duck"):
            spec = options["duck"]
            offsets = clip_offsets(timings, transition if crossfading else 0.0)
            spans = []
            for (_, clip), (start, duration), at in zip(clips, timings, offsets):
                key = (spec.get("keys") or {}).get(str(clip))
                if not key:
                    continue
                local = ([(0.0, duration)] if key.get("whole")
                         else speech_intervals(Path(key["speech"]), start, duration))
                spans += [(at + s, at + e) for s, e in local]
            expression = duck_expression(spans, float(spec.get("db") or 0),
                                         float(spec.get("attack") or 0),
                                         float(spec.get("release") or 0))
            if expression != "1":
                duck = f",volume='{expression}':eval=frame"
                res.log.append(f"[INFO] soundtrack ducked {spec.get('db')} dB under "
                               f"{len(spans)} dialogue span(s)")
        filters.append(f"[{len(clips)}:a]aresample={SAMPLE_RATE},asetpts=PTS-STARTPTS,"
                       f"atrim=duration={total},volume={gain}{duck},afade=t=in:d=0.1,"
                       f"afade=t=out:st={max(0, total-0.1)}:d=0.1[bed]")
        filters.append("[baseaudio][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:latency=1[a]")
    else:
        filters.append("[baseaudio]anull[a]")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.stem + ".assembling.mp4")
    argv += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
             "-t", str(total), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(temporary)]

    started = time.time()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=180 + 120 * len(clips),
        )
    except subprocess.TimeoutExpired:
        res.error = "ffmpeg timed out joining the clips"
        return res

    if proc.returncode != 0 or not temporary.exists() or temporary.stat().st_size < 1024:
        res.log = (proc.stderr or "").strip().splitlines()[-8:]
        res.error = f"ffmpeg failed (exit {proc.returncode})"
        return res

    temporary.replace(out)
    res.ok = True
    res.path = out
    res.parts = [(label, clip.name) for label, clip in clips]
    res.seconds = _duration(out)
    res.log = [
        f"[INFO] joined {len(clips)} clip(s) into {out.name} "
        f"({out.stat().st_size // 1024} KB, {res.seconds:.1f}s) "
        f"in {time.time() - started:.1f}s"
    ] + res.log
    if res.missing:
        res.log.append(
            "[WARN] left out, not rendered: " + ", ".join(res.missing)
        )
    return res


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------

def _ffprobe(*args: str) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", *args],
            capture_output=True, text=True, timeout=20,
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001 - probing must never break a render
        return ""


def _has_audio(path: Path) -> bool:
    """True when the file carries at least one audio stream.

    Assumed absent when ffprobe is unavailable: adding silence to a clip that
    already has sound would lose it, whereas the reverse only costs a probe.
    """
    return bool(
        _ffprobe(
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path),
        )
    )


def _duration(path: Path) -> float:
    try:
        return float(
            _ffprobe(
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            )
            or 0
        )
    except ValueError:
        return 0.0
