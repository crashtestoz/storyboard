"""Shared dialogue preparation for manual previews and render batches."""
import hashlib
import json
import time
from pathlib import Path
from .dubbing import dub_shot, speaker_for, mux_speech
from .store import speech_fingerprint


def engine_fingerprint(engine):
    config = {k: str(v) for k, v in vars(engine).items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps([engine.id, config], sort_keys=True).encode()).hexdigest()[:16]


def generate_take(ctx, slug, shot_id, payload=None):
    payload = payload or {}
    board = ctx.store.load(slug)
    shots = board.get("shots") or []
    idx = next((i for i, s in enumerate(shots) if s["id"] == shot_id), None)
    if idx is None:
        raise FileNotFoundError("no such shot")
    shot = shots[idx]

    # The line may not be saved yet — the same reason /api/rewrite takes it.
    text = payload.get("text")
    if text is None:
        text = shot.get("dialogue") or ""
    style = payload.get("style")
    if style is None:
        style = shot.get("dialogueStyle") or ""
    style = (style or "").strip()
    dub_mode = payload.get("dubMode")
    if dub_mode is None:
        dub_mode = shot.get("dubMode") or "mix"

    engine = ctx.tts((board.get("defaults") or {}).get("tts"))
    shot_dir = ctx.data_dir / ctx.store.shot_rel_dir(slug, idx + 1)

    # The speaking character's recorded voice is the cloning reference, and
    # their transcript conditions it alongside the audio.
    speaker = speaker_for(shot, board)
    reference = None
    reference_text = ""
    clone_note = ""
    if speaker:
        voice = speaker.get("voice") or {}
        path = voice.get("path")
        if not path:
            clone_note = (
                f"{speaker.get('name') or 'that character'} has no reference "
                "voice clip, so the engine's own voice was used"
            )
        elif not engine.supports_cloning:
            clone_note = (
                f"{engine.label} cannot clone a voice, so "
                f"{speaker.get('name') or 'the character'}'s clip was not used"
            )
        else:
            candidate = ctx.data_dir / path
            if candidate.exists():
                reference = candidate
                reference_text = speaker.get("voiceText") or ""
            else:
                clone_note = f"the reference clip is missing at {path}"

    result = dub_shot(
        engine,
        clip=shot_dir / "clip.mp4",
        shot_dir=shot_dir,
        text=text,
        voice=shot.get("dialogueVoice") or None,
        reference=reference,
        reference_text=reference_text,
        style=style,
        keep_original_audio=dub_mode != "replace",
    )

    def as_url(p: Path) -> str:
        return "/media/" + str(p.relative_to(ctx.data_dir)).replace("\\", "/")

    # The engine's own account of the run, written next to the audio so it
    # survives a reload the way run.log does for a render. A voice that
    # comes back wrong is nearly always the reference clip or its
    # transcript, and neither is visible from the waveform — so this is
    # kept whether the run succeeded or failed.
    speech_log = list(result.log or [])
    if result.speech and result.speech.log:
        speech_log = list(result.speech.log)
    if result.warning:
        speech_log.append(f"[WARN] {result.warning}")
    if clone_note:
        speech_log.append(f"[WARN] {clone_note}")
    if not result.ok and result.error:
        speech_log.append(f"[ERROR] {result.error}")

    log_url = None
    try:
        shot_dir.mkdir(parents=True, exist_ok=True)
        log_path = shot_dir / "speech.log"
        header = [
            f"# {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"# engine: {engine.id} ({engine.label})",
            f"# speaker: {(speaker or {}).get('name') or '(none)'}"
            f"{' — cloned' if reference is not None else ''}",
        ]
        log_path.write_text("\n".join(header + speech_log) + "\n")
        log_url = as_url(log_path)
    except OSError:
        pass   # a log we could not write is not worth failing the take for

    if not result.ok:
        return {"error": result.error, "log": speech_log,
                "speechLogUrl": log_url, "engine": engine.id}

    # The spoken line is kept on the shot so it survives a reload and can
    # be played again without re-synthesising.
    if result.audio:
        shot["dialogueAudioUrl"] = (
            as_url(result.audio) + f"?v={result.audio.stat().st_mtime_ns}"
        )
    else:
        shot["dialogueAudioUrl"] = None
    # What this take says. A render finishing later re-muxes the wav onto
    # the fresh clip, and must not do that once the line has been edited.
    shot["dialogueSpokenText"] = (text or "").strip()
    shot["dialogueSpokenStyle"] = style
    shot["speechFingerprint"] = speech_fingerprint({**shot, "dialogue": text, "dialogueStyle": style}, board)
    if result.video:
        shot["dubUrl"] = as_url(result.video)
    shot["speechLogUrl"] = log_url
    shot["speechEngineFingerprint"] = engine_fingerprint(engine)
    shot["dubAppliedMode"] = dub_mode if result.video else None
    # Keep edits made while synthesis was running; only publish take metadata.
    fresh = ctx.store.load(slug)
    current = next((s for s in fresh.get("shots", []) if s["id"] == shot_id), None)
    if current is None:
        return {"error": "Scene was deleted while dialogue was generating"}
    for key in ("dialogueAudioUrl", "dialogueSpokenText", "dialogueSpokenStyle",
                "speechFingerprint", "speechEngineFingerprint", "dubAppliedMode", "dubUrl", "speechLogUrl"):
        current[key] = shot.get(key)
    ctx.store.save(slug, fresh)

    return {
        "speechEngineFingerprint": shot.get("speechEngineFingerprint"),
        "speechFingerprint": shot.get("speechFingerprint"),
        "log": speech_log,
        "speechLogUrl": log_url,
        "audioUrl": shot["dialogueAudioUrl"],
        "dubUrl": shot.get("dubUrl") if result.video else None,
        "muxed": bool(result.video),
        "engine": engine.id,
        "engineLabel": engine.label,
        "speaker": (speaker or {}).get("name") or "",
        "cloned": reference is not None,
        "voice": result.speech.voice if result.speech else "",
        "seconds": round(result.speech.seconds, 2) if result.speech else 0,
        "warning": result.warning,
        "note": clone_note,
    }


def prepare_recording(ctx, slug, shot_id, generate_missing=True):
    """Reuse current recordings, generate stale takes, and apply audio-only edits."""
    from .backends.vpipe_backend import _effective_video_model, _clones_voice
    board = ctx.store.load(slug)
    shot = next(s for s in board["shots"] if s["id"] == shot_id)
    if not (shot.get("dialogue") or "").strip():
        return
    model, _ = _effective_video_model(shot, board)
    if shot.get("dialogueSource") == "native":
        if not _clones_voice(shot, board, model):
            raise ValueError("Native speech requires Ref2VA and a cast speaker with a voice reference")
        return
    if _clones_voice(shot, board, model):
        return
    idx = board["shots"].index(shot)
    folder = ctx.data_dir / ctx.store.shot_rel_dir(slug, idx + 1)
    audio = folder / "dialogue.wav"
    engine = ctx.tts((board.get("defaults") or {}).get("tts"))
    fingerprint = speech_fingerprint(shot, board)
    current = (audio.exists() and audio.stat().st_size > 1024
               and shot.get("dialogueSpokenText", "").strip() == shot["dialogue"].strip()
               and shot.get("dialogueSpokenStyle", "").strip() == shot.get("dialogueStyle", "").strip()
               and shot.get("speechFingerprint") in (None, fingerprint)
               and shot.get("speechEngineFingerprint") in (None, engine_fingerprint(engine)))
    if not current:
        if not generate_missing:
            raise ValueError("Dialogue is missing or changed; use Prepare all dialogue before assembling")
        ok, message = engine.health()
        if not ok:
            raise ValueError(message)
        speaker = speaker_for(shot, board) or {}
        voice = (speaker.get("voice") or {}).get("path")
        if voice and (not engine.supports_cloning or not (ctx.data_dir / voice).is_file()):
            raise ValueError("The selected character voice cannot be cloned: check the speech engine and reference file")
        result = generate_take(ctx, slug, shot_id)
        if result.get("error"):
            raise ValueError(result["error"])
        return
    clip = folder / "clip.mp4"
    if clip.exists():
        out, error, _, _ = mux_speech(clip=clip, speech=audio, shot_dir=folder,
                                    keep_original_audio=shot.get("dubMode") != "replace")
        if error:
            raise ValueError(error)
        if out:
            fresh = ctx.store.load(slug)
            target = next((s for s in fresh["shots"] if s["id"] == shot_id), None)
            if target:
                target["dubUrl"] = "/media/" + str(out.relative_to(ctx.data_dir))
                target["dubAppliedMode"] = shot.get("dubMode", "mix")
                ctx.store.save(slug, fresh)
