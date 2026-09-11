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
        }


def shot_clip(shot_dir: Path, shot: dict[str, Any] | None = None) -> Path | None:
    """The clip to put in the cut for this shot, or None if there isn't one."""
    clip = shot_dir / "clip.mp4"
    if not (clip.exists() and clip.stat().st_size > 1024):
        return None
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
            if line and (spoken != line or spoken_style != style):
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

    built = out.stat().st_mtime
    for i, shot in enumerate(board.get("shots") or []):
        clip = shot_clip(project_dir / "shots" / f"{i + 1:02d}", shot)
        if clip is None:
            continue
        if clip.stat().st_mtime > built:
            return "a shot has been re-rendered since this was assembled"

    listed = len(record.get("parts") or [])
    have = sum(
        1
        for i in range(len(board.get("shots") or []))
        if shot_clip(
            project_dir / "shots" / f"{i + 1:02d}",
            (board.get("shots") or [])[i],
        ) is not None
    )
    if have != listed:
        return f"assembled from {listed} clip(s); {have} are available now"
    return ""


def unmixed_dialogue(board: dict[str, Any], project_dir: Path) -> dict[str, str]:
    """Shots whose spoken line exists but is not on the clip going in the cut.

    A line is normally spoken *before* the shot is rendered — that is the point
    of keeping synthesis out of the render path — and at that moment there is
    no video to mux into. A render finishing later now lays the take down (see
    ``Orchestrator._relay_speech``), but a clip rendered before that existed
    has a ``dialogue.wav`` beside it and silence in it, and the cut would go
    out without the line. Nothing about the file says so, so it is said here.
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


def assemble(
    parts: list[tuple[str, Path | None]],
    out: Path,
    width: int,
    height: int,
    fps: int = FPS,
) -> AssemblyResult:
    """Join *parts* — (label, clip or None) in cut order — into *out*."""
    res = AssemblyResult()
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

    argv: list[str] = [ffmpeg, "-hide_banner", "-v", "error", "-y"]
    filters: list[str] = []
    pairs: list[str] = []
    n = 0

    for _label, clip in clips:
        argv += ["-i", str(clip)]
        vi = n
        n += 1
        # Every clip is forced to one geometry, sample aspect and frame rate.
        # concat demands it, and a board *can* hold clips of different sizes:
        # frame size is a project setting now, but shots rendered before it
        # moved kept their own, and draft mode halves it.
        filters.append(
            f"[{vi}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={fps},format=yuv420p[v{vi}]"
        )
        if _has_audio(clip):
            filters.append(
                f"[{vi}:a]aresample={SAMPLE_RATE}:async=1,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{vi}]"
            )
        else:
            # A silent stretch of the right length, so concat still gets one
            # audio stream per segment. Without it a soundless still in the
            # middle of the board desynchronises everything after it.
            seconds = _duration(clip) or 1.0
            argv += [
                "-f", "lavfi", "-t", f"{seconds:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
            ]
            ai = n
            n += 1
            filters.append(
                f"[{ai}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a{vi}]"
            )
        pairs.append(f"[v{vi}][a{vi}]")

    filters.append("".join(pairs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")

    out.parent.mkdir(parents=True, exist_ok=True)
    argv += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]

    started = time.time()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=180 + 120 * len(clips),
        )
    except subprocess.TimeoutExpired:
        res.error = "ffmpeg timed out joining the clips"
        return res

    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
        res.log = (proc.stderr or "").strip().splitlines()[-8:]
        res.error = f"ffmpeg failed (exit {proc.returncode})"
        return res

    res.ok = True
    res.path = out
    res.parts = [(label, clip.name) for label, clip in clips]
    res.seconds = _duration(out)
    res.log = [
        f"[INFO] joined {len(clips)} clip(s) into {out.name} "
        f"({out.stat().st_size // 1024} KB, {res.seconds:.1f}s) "
        f"in {time.time() - started:.1f}s"
    ]
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
