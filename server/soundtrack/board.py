"""A board's soundtrack: its settings, whether it can be made yet, and making it.

The music is generated ahead of the ffmpeg pass rather than inside it, and
cached as ``soundtrack.wav`` in the project folder under a key made of
everything that shapes it — engine, model, prompt, seed, reference clip and
the cut's length. Re-assembling with nothing changed reuses the file, so the
same board always gets the same music and assembly stays a matter of seconds.

Nothing is generated until every shot has a clip. Until then the cut's length
is not known, and music composed to the wrong length either stops early or is
cut off mid-phrase.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import assemble as assembly
from . import DEFAULT_MODEL, MODELS, SoundtrackEngine

RENDER_NAME = "soundtrack.wav"
REFERENCE_NAME = "soundtrack-reference.wav"

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # "generate" with an engine, or "upload" — the file in assembly.backgroundAudio
    "source": "generate",
    "engine": "",
    "model": DEFAULT_MODEL,
    "prompt": "",
    # {path, url, label} of an uploaded clip that sets the tone
    "reference": None,
    # 0 = ignore the reference, 1 = stay close to it (melody included)
    "referenceStrength": 0.5,
    "seed": 0,
    "duck": True,
    "duckDb": 12.0,
    "duckAttack": 0.15,
    "duckRelease": 0.5,
    # Ask H3 to leave music out of each shot's generated audio.
    "noMusicInShots": False,
}

# One generation at a time: two would compete for the same unified memory.
_LOCK = threading.Lock()


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number)) if number == number else default


def settings(board: dict[str, Any]) -> dict[str, Any]:
    """The board's soundtrack settings, defaults filled in and values bounded.

    A board from before the Soundtrack tab has no ``soundtrack`` key; if it
    had a background file attached, that file was its soundtrack and still is.
    """
    raw = board.get("soundtrack")
    if not isinstance(raw, dict):
        legacy = bool((board.get("assembly") or {}).get("backgroundAudio"))
        raw = {"enabled": True, "source": "upload"} if legacy else {}
    s = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    s["enabled"] = bool(s["enabled"])
    s["source"] = s["source"] if s["source"] in ("generate", "upload") else "generate"
    s["model"] = s["model"] if s["model"] in MODELS else DEFAULT_MODEL
    s["prompt"] = str(s["prompt"] or "").strip()
    s["reference"] = s["reference"] if isinstance(s["reference"], dict) and s["reference"].get("path") else None
    s["referenceStrength"] = _clamp(s["referenceStrength"], 0, 1, 0.5)
    try:
        s["seed"] = int(s["seed"] or 0)
    except (TypeError, ValueError):
        s["seed"] = 0
    s["duck"] = bool(s["duck"])
    s["duckDb"] = _clamp(s["duckDb"], 0, 30, 12.0)
    s["duckAttack"] = _clamp(s["duckAttack"], 0, 2, 0.15)
    s["duckRelease"] = _clamp(s["duckRelease"], 0, 5, 0.5)
    s["noMusicInShots"] = bool(s["noMusicInShots"])
    return s


def no_music_in_shots(board: dict[str, Any]) -> bool:
    s = settings(board)
    return s["enabled"] and s["noMusicInShots"]


def seed_for(board: dict[str, Any], slug: str = "") -> int:
    """The seed, or a stable one derived from the board so it never drifts."""
    seed = settings(board)["seed"]
    if seed:
        return seed
    basis = str(board.get("createdAt") or slug or board.get("name") or "")
    return int(hashlib.sha256(basis.encode()).hexdigest()[:7], 16)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_path(board: dict[str, Any], data_dir: Path) -> Path | None:
    ref = settings(board)["reference"]
    if not ref:
        return None
    path = (data_dir / ref["path"]).resolve()
    if not path.is_relative_to(data_dir.resolve()) or not path.is_file():
        return None
    return path


def plan(board: dict[str, Any], project_dir: Path) -> dict[str, Any]:
    """Whether every shot has a clip, and if so how long the cut is."""
    parts = assembly.board_parts(board, project_dir)
    missing = [label for label, clip in parts if clip is None]
    out: dict[str, Any] = {"shots": len(parts), "missing": missing, "seconds": None, "error": ""}
    if not parts:
        out["error"] = "this storyboard has no shots"
        return out
    if missing:
        return out
    options = {"transitionSeconds": (board.get("assembly") or {}).get("transitionSeconds"),
               "trims": assembly.clip_trims(board, project_dir)}
    try:
        _, total = assembly.cut_timings([clip for _, clip in parts], options)
    except (ValueError, TypeError) as exc:
        out["error"] = str(exc)
        return out
    out["seconds"] = round(total, 2)
    return out


def target_seconds(board: dict[str, Any], cut_seconds: float) -> float:
    """What the model is asked for: the cut, or the model's longest."""
    return round(min(cut_seconds, MODELS[settings(board)["model"]]["maxSeconds"]), 2)


def cache_key(board: dict[str, Any], engine_id: str, seconds: float, data_dir: Path,
              slug: str = "") -> str:
    s = settings(board)
    ref = reference_path(board, data_dir)
    payload = [engine_id, s["model"], s["prompt"], seed_for(board, slug),
               target_seconds(board, seconds),
               _file_hash(ref) if ref else None,
               round(s["referenceStrength"], 3) if ref else None]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def pick_engine(engines: dict[str, SoundtrackEngine], wanted: str) -> SoundtrackEngine | None:
    if wanted and wanted in engines:
        return engines[wanted]
    healthy = [e for e in engines.values() if e.health()[0]]
    return (healthy or list(engines.values()) or [None])[0]


def status(board: dict[str, Any], project_dir: Path, data_dir: Path,
           engines: dict[str, SoundtrackEngine], slug: str = "") -> dict[str, Any]:
    """Where the soundtrack stands, for the Settings tab and assembly.

    ``state`` is one of: off, upload, waiting (shots still to render),
    blocked (the cut cannot be measured), ready (nothing generated yet),
    stale (generated for different settings or a different cut), current.
    """
    s = settings(board)
    record = board.get("soundtrackRender") or None
    out: dict[str, Any] = {"enabled": s["enabled"], "source": s["source"], "render": record,
                           "maxSeconds": MODELS[s["model"]]["maxSeconds"]}
    if not s["enabled"]:
        return {**out, "state": "off", "message": "No soundtrack in the final cut."}
    if s["source"] == "upload":
        has = bool((board.get("assembly") or {}).get("backgroundAudio"))
        return {**out, "state": "upload" if has else "blocked",
                "message": "Your audio file is mixed under the cut, looping if it is shorter."
                if has else "Choose an audio file to use as the soundtrack."}
    p = plan(board, project_dir)
    out.update(missing=p["missing"], shots=p["shots"], seconds=p["seconds"])
    if p["error"]:
        return {**out, "state": "blocked", "message": p["error"]}
    if p["missing"]:
        return {**out, "state": "waiting",
                "message": f"Waiting for {len(p['missing'])} of {p['shots']} scene(s) to render — "
                           "the soundtrack is generated once the cut's length is known."}
    engine = pick_engine(engines, s["engine"])
    if engine is None:
        return {**out, "state": "blocked",
                "message": "No soundtrack engine is set up — run setup/install-stable-audio-3.sh."}
    target = target_seconds(board, p["seconds"])
    key = cache_key(board, engine.id, p["seconds"], data_dir, slug)
    out.update(engine=engine.id, targetSeconds=target, key=key,
               loops=target < p["seconds"] - 0.05)
    render = data_dir / (record or {}).get("path", "") if record else None
    if record and record.get("key") == key and render and render.is_file():
        return {**out, "state": "current", "message": "Generated and current."}
    if not s["prompt"]:
        return {**out, "state": "blocked", "message": "Describe the music to generate."}
    if record:
        return {**out, "state": "stale",
                "message": "The settings or the cut changed since this was generated — "
                           "it is generated again at assembly."}
    return {**out, "state": "ready", "message": "Ready — generated at assembly, or now."}


def usable_render(board: dict[str, Any], project_dir: Path, data_dir: Path) -> Path | None:
    """The generated file, but only if it was made for this board as it is now."""
    record = board.get("soundtrackRender") or {}
    if not record.get("path"):
        return None
    path = data_dir / record["path"]
    if not path.is_file():
        return None
    p = plan(board, project_dir)
    if p["seconds"] is None:
        return None
    key = cache_key(board, record.get("engine", ""), p["seconds"], data_dir)
    return path if key == record.get("key") else None


def _loop_reference(ref: Path, out: Path, seconds: float) -> str:
    """The reference as the MLX runtime wants it: 44.1 kHz 16-bit stereo WAV,
    looped to the full length, since it zero-pads a short one and the music
    would then be steered towards silence for the rest of the cut."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg is not on PATH, so the reference clip cannot be prepared"
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(ref),
         "-t", f"{seconds:.2f}", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not out.is_file():
        return "the reference clip could not be read: " + (proc.stderr or "").strip()[-200:]
    return ""


def prepare(ctx, slug: str) -> dict[str, Any]:
    """Make sure the soundtrack the cut needs exists; generate it if not.

    Returns a note for the assembled cut's record — ``state`` is off, upload,
    current (reused), generated, skipped (not possible yet) or failed. Never
    raises for a soundtrack problem: the cut is still worth assembling
    without its music, and the note says why it has none.
    """
    board = ctx.store.load(slug)
    project_dir = ctx.store.project_dir(slug)
    engines = ctx.soundtrack_engines()
    st = status(board, project_dir, ctx.data_dir, engines, slug)
    state = st["state"]
    if state in ("off", "upload", "current"):
        return {"state": state, "message": st["message"]}
    if state in ("waiting", "blocked"):
        return {"state": "skipped", "message": st["message"]}

    s = settings(board)
    engine = engines[st["engine"]]
    ok, why = engine.health()
    if not ok:
        return {"state": "failed", "message": why}
    if not _LOCK.acquire(blocking=False):
        return {"state": "failed", "message": "a soundtrack is already being generated"}
    try:
        seconds = st["targetSeconds"]
        init, strength = None, s["referenceStrength"]
        ref = reference_path(board, ctx.data_dir)
        if ref and strength > 0:
            init = project_dir / REFERENCE_NAME
            err = _loop_reference(ref, init, seconds)
            if err:
                return {"state": "failed", "message": err}
        temporary = project_dir / "soundtrack.generating.wav"
        result = engine.generate(
            s["prompt"], temporary, seconds=seconds, model=s["model"],
            seed=seed_for(board, slug), init_audio=init,
            # strength 0..1 → σmax 1.0..0.4, the range upstream calls typical
            init_noise_level=1.0 - 0.6 * strength,
        )
        if not result.ok:
            return {"state": "failed", "message": result.error, "log": result.log}
        out = project_dir / RENDER_NAME
        temporary.replace(out)
        rel = str(out.relative_to(ctx.data_dir)).replace("\\", "/")
        now = time.time()
        record = {
            "path": rel, "url": f"/media/{rel}?v={int(now)}", "key": st["key"],
            "engine": engine.id, "model": s["model"], "seed": seed_for(board, slug),
            "seconds": seconds, "prompt": s["prompt"], "builtAt": now,
        }
        # Saved on a fresh copy: generating takes a while, and the board may
        # have been edited meanwhile.
        fresh = ctx.store.load(slug)
        fresh["soundtrackRender"] = record
        ctx.store.save(slug, fresh)
        return {"state": "generated", "message": f"Generated {seconds:.1f}s of music.",
                "log": result.log}
    finally:
        _LOCK.release()
