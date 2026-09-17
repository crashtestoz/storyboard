"""vpipe backend.

Generates a `.vpipeline` file per shot and runs it with
``vpipe --launch <file>``, parsing stdout for progress.

The five templates here are transcriptions of pipelines that were built and
run by hand against real models, not guesses from documentation:

*   ``fl2va``          — text to video+audio
*   ``fl2va`` + anchor — the same, with ``generate-video`` port 5 fed a
                         ``vae-encode`` of a still, so the clip opens on that
                         exact frame (optionally port 6 for the closing frame)
*   ``ref2va``         — ``video-ref-encoder`` with a reference image list,
                         carrying subject/style across the whole clip
*   ``krea2-still``    — a single image, for cheap prompt previews
*   ``wan-i2v``        — text (+ optional Start frame anchor) to silent
                         video on Wan 2.2's A14B, port 5 fed the same shape
                         of ``vae-encode`` FL2VA uses, minus End frame
                         (Wan's DiT has no port for one) and minus audio
                         (Wan has none)

Hard-won details encoded rather than left to the user:

*   MiniMax H3 frame counts must be ``17n + 5`` **and** produce at least 8
    latent frames. ``frames: 5`` is accepted by the generate stage and then
    silently fails at VAE decode, writing nothing — hence a minimum of 39.
*   Wan frame counts must be ``4k + 1`` (its VAE's own chunking; see
    ``WAN_FRAME_RULE``).
*   Wan is not guidance-distilled the way H3 is: a negative prompt has to be
    wired to ``diffusion-conditioner`` and on to ``generate-video``'s iport1
    or its ``guidance_scale`` config is a no-op (see ``WAN_NEGATIVE_PROMPT``).
*   ``i8_gemm`` is a lossy speed mode that does nothing on M4 and changes the
    picture on M5, so it is off by default here; quality is the point of a
    long unattended render.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from ..dubbing import speaker_for
from .base import (
    Backend,
    Check,
    FrameRule,
    JobSpec,
    ModelCapability,
    ProgressEvent,
    RunResult,
    ShotPaths,
)

# --- H3's frame arithmetic -------------------------------------------------
# frames = 17n + 5; latent frames = (frames - 5) / 17 * 5 + 2; decode needs >= 8
H3_FRAME_RULE = FrameRule(
    kind="affine",
    step=17,
    offset=5,
    minimum=39,
    note="MiniMax H3 packs video 17 frames at a time keeping 5 latents, and "
         "its VAE cannot decode fewer than 8 latent frames — so 39 "
         "(1.6s @ 24fps) is the shortest clip that decodes at all.",
)

# MiniMax H3 tiles at its VAE's 16x spatial stride times the DiT's 2x2 patch,
# so generate-video rounds any frame size UP to a multiple of 32. Sizes are
# snapped to it here rather than left to that rounding, because a start-frame
# anchor is vae-encoded at the size this file asks for: if the generate stage
# moves to a different one, the anchor latent's [z, n, h/16, w/16] no longer
# matches, and it is dropped with a warning ("keyframe latent does not
# match ... generating without an anchor") -- the clip renders as plain
# text-to-video and the reference image silently does nothing.
H3_SIZE_ALIGN = 32

# Wan's video VAE compresses in 4-frame chunks after a 1-frame first chunk
# (4k+1) -- see MetalWanVae::align_num_frames in vpipe -- so only 4k+1 has a
# latent form at all; a count that misses it is rounded UP, never truncated.
# There is no documented decode floor the way H3 has one (>= 8 latent
# frames), but a handful of frames is not a clip worth generating, so the
# minimum below is a practical floor, not a model limit.
WAN_FRAME_RULE = FrameRule(
    kind="affine",
    step=4,
    offset=1,
    minimum=41,
    note="Wan packs video 4 frames at a time after a 1-frame first chunk, "
         "so only 4k+1 has a latent form; 41 (~1.7s @ 24fps) is a "
         "practical floor for a clip worth generating, not a hard model "
         "minimum.",
)

# Wan's VAE is 8x spatial and the DiT patches 2x on top (H3's VAE is 16x,
# hence its 32 above), so Wan's legal grid is 16 -- see GenerateVideoStage's
# wan branch in vpipe (align_size_(8 * patch_h, 8 * patch_w)).
WAN_SIZE_ALIGN = 16

# H3-Base generates at a 768-pixel short edge by default. The first entry in
# each group is that base canvas, rounded to the model's required multiple of
# 32. The smaller entries are useful local preview canvases. The separate
# H3-Regenerate-2K service is not part of this local backend, so 2K sizes are
# deliberately not presented as native H3-Base output choices.
ASPECT_TABLE: dict[str, list[str]] = {
    "21:9": ["1792x768", "1344x576", "1120x480"],
    "16:9": ["1344x768", "960x544", "832x480"],
    "4:3":  ["1024x768", "768x576", "640x480"],
    "1:1":  ["768x768", "576x576", "480x480"],
    "3:4":  ["768x1024", "576x768", "480x640"],
    "9:16": ["768x1344", "544x960", "480x832"],
}
ALL_RESOLUTIONS = [r for group in ASPECT_TABLE.values() for r in group]
H3_BASE_RESOLUTIONS = [group[0] for group in ASPECT_TABLE.values()]

# The sizes each model's own documentation actually cites. Anything else in
# ALL_RESOLUTIONS is offered but untested.
H3_TESTED = ["960x544", "832x480", "1344x768"]
KREA_TESTED = ["1024x1024"]
# Empty on purpose: no resolution has actually been rendered with this model
# on this machine yet (unlike H3/Krea-2 above, whose lists came from a real
# run). Offered at every size in ALL_RESOLUTIONS, tested at none.
WAN_TESTED: list[str] = []

# Prepended to the prompt in sketch mode (see the "sketch" local in
# prepare()) — a plain style instruction, not a technical trick, so it goes
# in the same text every other visual instruction already travels through.
# H3 is guidance-distilled (no negative prompt, no CFG scale — see the vpipe
# docs), so it has no lever to push *away* from its photoreal default; text
# alone only ever nudges it, it does not override it. That is what
# _sketchify_ref() is for on the Ref2VA path — an edge-detected reference
# image is a much stronger signal than any wording here, since the model's
# whole job with a reference is to reproduce what it shows. This text still
# matters for a shot with no reference images to sketch (plain FL2VA) and as
# a second push alongside a sketchified reference.
# Wan, unlike H3, is NOT guidance-distilled -- classifier-free guidance is
# only real when a negative prompt is wired in (see generate-video-stage.h's
# iport1 doc: without one, guidance is forced to 1 and the second forward
# pass is skipped, i.e. the guidance_scale config knob does nothing). This
# is the negative prompt Wan's own reference examples ship.
WAN_NEGATIVE_PROMPT = (
    "Bright tones, overexposed, static, blurred details, subtitles, style, "
    "artwork, painting, picture, still, overall gray, worst quality, low "
    "quality, JPEG compression artifacts, ugly, incomplete, extra fingers, "
    "poorly drawn hands, poorly drawn faces, deformed, disfigured, "
    "malformed limbs, fused fingers, cluttered background, three legs, "
    "many people in the background, walking backwards"
)

SKETCH_STYLE_PREFIX = (
    "STYLE: black-and-white hand-drawn pencil storyboard sketch — heavy, "
    "dark graphite contour lines on plain white paper, loose scratchy "
    "linework, flat monotone, absolutely no color, no photographic detail, "
    "no rendered lighting, shading or reflections. A rough animatic "
    "drawing, not a photograph."
)

# Activates Krea-2's in-context reference-edit path (the ComfyUI-Krea2Edit
# node's mechanism) — see _still_spec()'s identity_ref_path handling. Fetched
# once via a plain model-fetch vpipeline into the workspace; not bundled or
# auto-downloaded, same footing as every other model this app links to
# rather than installs. The directory holds three rank variants — v1_2 is
# the publisher's recommended full-rank one, so the path is explicit rather
# than a bare registered name (which would be ambiguous with three files).
KREA2_IDENTITY_EDIT_LORA = (
    "models/conradlocke/krea2-identity-edit/krea2_identity_edit_v1_2.safetensors"
)

# stdout patterns that mean "this run failed even though it will exit 0"
SILENT_FAILURE_PATTERNS = [
    (r"video decode failed", "VAE decode refused the request and skipped"),
    (r"reference rows are not \[", "reference rows were emitted in the wrong shape"),
    (r"audio decode failed", "audio VAE decode failed"),
    (r"the vision tower produced nothing", "reference encoding produced nothing"),
    (r"encode failed", "an encode step failed"),
    (r"could not be found|not found in the model registry", "a model was missing"),
    # Narrow on purpose: a bare "refused" also appears in the benign
    # wired-pool warning ("the box refused to wire 0 MB"), which is graceful
    # degradation, not failure.
    (r"refused the request|refuses at|refusing to run", "a stage refused the request"),
]

# warnings that look alarming and are not failures
BENIGN_PATTERNS = [
    r"wired pool",              # graceful fall back to reclaimable memory
    r"Full Disk Access",
    r"permission not yet granted",
    r"no camera found",
    r"no Sparkle",
    r"PATH missing required dirs",
]

PROGRESS_RE = re.compile(
    r"\[PROGRESS\]\s+(?P<pct>\d+)%\s+of\s+'(?P<phase>[^']+)'\s+completed"
)
LEVEL_RE = re.compile(r"^\[(?P<lvl>INFO|WARN|ERROR|NORMAL|PROGRESS|DEBUG)\]\s*(?P<rest>.*)$")

# rough share of wall clock each phase takes, for a single overall percentage
PHASE_WEIGHTS = {
    "encoding references": 0.10,
    "denoise": 0.75,
    "vae decode": 0.13,
    "quantize": 1.0,
}
PHASE_ORDER = ["encoding references", "denoise", "vae decode"]


class VpipeBackend(Backend):
    id = "vpipe"
    label = "vpipe (Apple Silicon, Metal)"

    def __init__(self, binary: Path, workspace: Path):
        self.binary = Path(binary)
        self.workspace = Path(workspace)
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------ #
    # description
    # ------------------------------------------------------------------ #

    def health(self) -> tuple[bool, str]:
        if not self.binary.exists():
            return False, f"vpipe binary not found at {self.binary}"
        if not os.access(self.binary, os.X_OK):
            return False, f"vpipe binary is not executable: {self.binary}"
        if not self.workspace.exists():
            return False, f"workspace not found at {self.workspace}"
        if not (self.workspace / "models").exists():
            return False, (
                f"no models/ under {self.workspace} — vpipe resolves the model "
                "registry relative to its working directory, so this must be "
                "the directory the models were prepared in"
            )
        return True, ""

    def _model_present(self, rel: str) -> bool:
        """Is this model on disk *and* complete?

        The directory existing is not enough: vpipe writes ``<name>.part``
        beside a shard it is still fetching, so a model being downloaded right
        now looks present. Offering it in the UI would hand the user a render
        that fails minutes later, so treat any ``.part`` under the model tree
        as "not ready".
        """
        root = self.workspace / "models" / rel
        if not root.exists():
            return False
        try:
            return not any(root.rglob("*.part"))
        except OSError:
            return False

    def capabilities(self) -> list[ModelCapability]:
        fl2va = self._model_present("local/MiniMax-H3-FL2VA-8bit")
        ref2va = self._model_present("local/MiniMax-H3-Ref2VA-8bit")
        krea = self._model_present("krea/Krea-2-Turbo")
        lora = self._model_present("mgwr/M87")
        wan_i2v = self._model_present("local/Wan2.2-I2V-A14B-8bit")

        return [
            ModelCapability(
                id="fl2va",
                label="MiniMax H3 · FL2VA — text / frame anchors → video + audio",
                kind="video",
                supports_start_anchor=True,
                supports_end_anchor=True,
                supports_style_refs=False,
                supports_audio=True,
                frame_rule=H3_FRAME_RULE,
                resolutions=ALL_RESOLUTIONS,
                tested_resolutions=H3_TESTED,
                size_align=H3_SIZE_ALIGN,
                default_steps=8,
                available=fl2va,
                unavailable_reason=""
                if fl2va
                else "local/MiniMax-H3-FL2VA-8bit not prepared in this workspace",
            ),
            ModelCapability(
                id="ref2va",
                label="MiniMax H3 · Ref2VA — reference images → video + audio",
                kind="video",
                # Ref2VA packs references instead of keyframes; the two are
                # mutually exclusive, so it offers no anchors at all.
                supports_start_anchor=False,
                supports_end_anchor=False,
                supports_style_refs=True,
                max_style_refs=9,
                supports_audio=True,
                frame_rule=H3_FRAME_RULE,
                resolutions=ALL_RESOLUTIONS,
                tested_resolutions=H3_TESTED,
                size_align=H3_SIZE_ALIGN,
                default_steps=8,
                available=ref2va,
                unavailable_reason=""
                if ref2va
                else "local/MiniMax-H3-Ref2VA-8bit not prepared in this workspace",
            ),
            ModelCapability(
                id="wan-i2v",
                label="Wan 2.2 · I2V-A14B — text / start frame → video (silent)",
                kind="video",
                # Wan's I2V conditioning is one clip-shaped tensor; iport6
                # (End frame) is documented as IGNORED by wan outright, not
                # just unwired-by-default, so this template never sends one.
                supports_start_anchor=True,
                supports_end_anchor=False,
                supports_style_refs=False,
                supports_audio=False,
                frame_rule=WAN_FRAME_RULE,
                resolutions=ALL_RESOLUTIONS,
                tested_resolutions=WAN_TESTED,
                size_align=WAN_SIZE_ALIGN,
                # Not distilled -- the checkpoint's own reference examples
                # run ~40 steps at guidance 3.5, a real multiple of H3's 8.
                default_steps=40,
                available=wan_i2v,
                unavailable_reason=""
                if wan_i2v
                else "local/Wan2.2-I2V-A14B-8bit not prepared in this workspace",
            ),
            ModelCapability(
                id="krea2-still",
                label="Krea-2 Turbo — still image (fast prompt preview)",
                kind="image",
                supports_start_anchor=False,
                supports_end_anchor=False,
                supports_style_refs=False,
                supports_audio=False,
                frame_rule=FrameRule(kind="any", minimum=1),
                resolutions=ALL_RESOLUTIONS,
                tested_resolutions=KREA_TESTED,
                default_steps=8,
                available=krea,
                unavailable_reason=""
                if krea
                else "krea/Krea-2-Turbo not downloaded in this workspace"
                + ("" if lora else " (and the mgwr/M87 LoRA is missing)"),
            ),
        ]

    # ------------------------------------------------------------------ #
    # prepare
    # ------------------------------------------------------------------ #

    def prepare(self, shot: dict, project: dict, paths: ShotPaths) -> JobSpec:
        paths.ensure()
        model, automatic_reason = _effective_video_model(shot, project)
        cap = self.capability(model)
        if cap is None:
            raise ValueError(f"unknown model: {model}")
        if not cap.available:
            raise ValueError(cap.unavailable_reason or f"{model} is not available")
        # Create Stills uses a synthetic Krea-2 image job, but it copies the
        # shot so it can reuse the scene/cast/reference layers. Dialogue is
        # irrelevant to that image preview and must not require a prepared
        # dialogue.wav take or block native speech validation below.
        if (
            cap.kind == "video"
            and shot.get("dialogueSource") == "recording"
            and (shot.get("dialogue") or "").strip()
        ):
            from ..store import speech_fingerprint
            speech = paths.abs_dir / "dialogue.wav"
            if not speech.exists():
                raise ValueError(
                    "Dialogue source is set to a separate recording, but no "
                    "dialogue take exists. Select a healthy speech engine, "
                    "press Generate in the Dialogue panel, preview the take, "
                    "then render again."
                )
            if (shot.get("dialogueSpokenText", "").strip() != shot["dialogue"].strip()
                    or shot.get("dialogueSpokenStyle", "").strip() != shot.get("dialogueStyle", "").strip()
                    or (shot.get("speechFingerprint") and shot["speechFingerprint"] != speech_fingerprint(shot, project))):
                raise ValueError(
                    "Dialogue recording is out of date because the line or "
                    "voice direction changed. Press Generate in the Dialogue "
                    "panel before rendering again."
                )

        # Resolution is a project setting, not a per-shot one: a storyboard
        # produces one video, and mixing frame sizes between shots would just
        # mean rescaling them all back together later. A shot-level value is
        # still honoured as a fallback for boards written before the move.
        res = (
            (project.get("defaults") or {}).get("resolution")
            or shot.get("resolution")
            or cap.resolutions[0]
        )
        width, height = _wh(res)
        draft = bool((project.get("defaults") or {}).get("draft"))
        # Sketch is a draft-only sub-option — it means nothing on its own,
        # since it exists to make an already-fast draft faster still.
        sketch = draft and bool((project.get("defaults") or {}).get("sketch"))
        # Draft trades visual fidelity for speed (a small frame, fewer
        # steps) — it does not mean silent. Dialogue, sound accents and the
        # background bed are exactly what a draft exists to let someone
        # check quickly, alongside the motion, before spending the time on a
        # full-quality render. Sketch is the one exception: it is a silent,
        # motion-only pass (see the frame-count comment below), so there is
        # no dialogue or sound to generate in the first place.
        with_audio = cap.supports_audio and not sketch
        if (
            cap.kind == "video"
            and shot.get("dialogue")
            and shot.get("dialogueSource") == "native"
            and not _clones_voice(shot, project, model)
        ):
            if model == "ref2va" or model == "fl2va":
                raise ValueError(
                    "H3 native speech requires Ref2VA and a cast speaker with a "
                    "reference voice clip. Remove the Start/End frame anchors, add "
                    "the voice clip, or switch Dialogue source to a separate recording."
                )
            raise ValueError(
                f"Native voice cloning is a MiniMax H3 Ref2VA feature; {model} "
                "has no such capability. Switch Dialogue source to a separate "
                "recording, or select Ref2VA."
            )
        prompt = _resolved_prompt(
            shot,
            project,
            with_audio=with_audio,
            model=model,
        )
        if sketch:
            prompt = f"{SKETCH_STYLE_PREFIX} {prompt}"
        # Whether this render will speak its own dialogue in the cloned
        # voice — the orchestrator needs to know this so it does not also
        # relay a leftover TTS take onto the clip afterwards, which is
        # exactly the "second voice" bug this replaced. False whenever audio
        # is not being generated at all (draft mode), since there is then no
        # voice for H3 to clone anything into.
        voice_cloned_natively = with_audio and _clones_voice(shot, project, model)
        steps = int(shot.get("steps") or cap.default_steps)
        seed = int(shot.get("seed") or 0)

        if draft:
            width, height, steps = _draft_geometry(
                width, height, steps, cap.size_align
            )
        # Full-size too: most of ASPECT_TABLE is a multiple of 32 already, but
        # 640x368 and 960x720 are not, and an un-snapped size costs the anchor.
        width = _align_up(width, cap.size_align)
        height = _align_up(height, cap.size_align)

        if cap.kind == "image":
            # Two distinct reference mechanisms, not one:
            #
            # 1. Chain continuation ("_chainRef", set only by "Create
            #    Stills" for its mid/end phases) — plain img2img from the
            #    previous phase's own output. A pixel-level continuation,
            #    not an edit, so it stays on the ordinary ref-latent path.
            #
            # 2. Identity reference (this shot's own Start Ref, or the
            #    first selected character's portrait when no anchor is
            #    set) — routed through Krea-2's identity-edit LoRA instead
            #    of plain img2img. Cast appearance is otherwise never
            #    shown to Krea-2 at all, only described in words, which
            #    "Create Stills" showed is not enough on its own.
            #
            # Krea-2 takes exactly one reference either way, so only the
            # first candidate found is used, never a blend of several.
            chain_ref_path = _ref_source(shot.get("_chainRef"), paths)
            identity_ref_path = None
            if not chain_ref_path:
                identity_ref_path = _ref_source(shot.get("startRef"), paths)
                if not identity_ref_path:
                    wanted = set(shot.get("characterIds") or [])
                    for ch in project.get("characters") or []:
                        if ch.get("id") in wanted and ch.get("image"):
                            identity_ref_path = _ref_source(ch.get("image"), paths)
                            if identity_ref_path:
                                break
            chain_strength = float(shot.get("imgStrength") or 0.6) if chain_ref_path else 0.0
            spec, outputs = self._still_spec(
                shot, paths, prompt, width, height, steps, seed,
                chain_ref_path=chain_ref_path, chain_strength=chain_strength,
                identity_ref_path=identity_ref_path,
            )
            expected_seconds = 150.0
            frames_dir = None
            frames = 0
            render_frames = 0
            stretch_factor = 1.0
            save_frames = False
        else:
            frames = cap.frame_rule.snap(int(shot.get("frames") or cap.frame_rule.minimum))
            # Sketch renders the fewest frames the model can decode at all —
            # real motion from the real model, just far fewer samples of it —
            # then run() stretches the clip back out to this shot's actual
            # length by holding frames, so it drops into the cut at the
            # right duration without costing anything to regenerate.
            render_frames = cap.frame_rule.minimum if sketch else frames
            stretch_factor = (frames / render_frames) if sketch else 1.0
            save_frames = (not draft) or _has_downstream_chain(shot, project)
            if model == "ref2va":
                spec, outputs = self._ref2va_spec(
                    shot, project, paths, prompt, width, height, render_frames,
                    steps, seed, draft, save_frames, with_audio, sketch
                )
            elif model == "wan-i2v":
                spec, outputs = self._wan_i2v_spec(
                    shot, paths, prompt, width, height, render_frames, steps,
                    seed, save_frames, sketch
                )
            else:
                spec, outputs = self._fl2va_spec(
                    shot, paths, prompt, width, height, render_frames, steps,
                    seed, draft, save_frames, with_audio, sketch
                )
            expected_seconds = _estimate_seconds(width, height, render_frames, steps, model)
            frames_dir = paths.abs_frames if save_frames else None

        spec_path = paths.abs_dir / "shot.vpipeline"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")

        return JobSpec(
            shot_id=shot["id"],
            expected_outputs=outputs,
            frames_dir=frames_dir,
            expected_frames=render_frames if save_frames else 0,
            expected_seconds=expected_seconds,
            payload={
                "spec_path": str(spec_path),
                "rel_spec": f"{paths.pipe_dir}/shot.vpipeline",
                "cwd": str(self.workspace),
                "model": model,
                "frames": render_frames,
                "draft": draft,
                "save_frames": save_frames,
                "voiceClonedNatively": voice_cloned_natively,
                "sketch": sketch,
                "sketchStretchFactor": stretch_factor,
            },
            summary=(
                f"{cap.label.split('—')[0].strip()} · {width}x{height}"
                + (f" · {frames}f" if cap.kind == "video" else "")
                + f" · {steps} steps"
                + (" · SKETCH" if sketch else " · DRAFT" if draft else "")
                + (f" · {automatic_reason}" if automatic_reason else "")
            ),
        )

    # -- templates ------------------------------------------------------ #

    def _fl2va_spec(self, shot, paths, prompt, w, h, frames, steps, seed,
                    draft=False, save_frames=True, with_audio=True, sketch=False):
        """Text-to-video, plus optional first/last frame anchors on ports 5/6."""
        stages: list[dict] = [
            _model_select("local/MiniMax-H3-FL2VA-8bit"),
            _text_prompt(prompt),
            {
                "id": "diffusion-conditioner",
                "type": "diffusion-conditioner",
                "iports": [
                    {"src": "text-prompt", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {"unload_when_idle": "always"},
            },
            _h3_config(),
        ]

        # anchors: a still becomes a latent via load-image -> resample -> vae-encode.
        # lanczos because the anchor is the only place the source picture's
        # detail enters the model; everything after it is latents.
        anchor_ports: dict[int, str] = {}
        for port, key, sid in ((5, "startRef", "start"), (6, "endRef", "end")):
            ref = shot.get(key)
            src = _ref_source(ref, paths)
            if not src:
                continue
            if sketch:
                # A pinned frame is vae-encoded pixel-for-pixel, so a
                # sketchified anchor guarantees that exact frame is a line
                # drawing rather than hoping the text carries it there.
                src = _sketchify_ref(src, paths.abs_dir / "sketch-refs")
            stages += [
                {
                    "id": f"load-{sid}",
                    "type": "load-image",
                    "iports": [],
                    "config": {"url": [src]},
                },
                {
                    "id": f"resample-{sid}",
                    "type": "image-resample",
                    "iports": [{"src": f"load-{sid}", "oport": 0}],
                    "config": {
                        "width": w,
                        "height": h,
                        "fit": "crop",
                        "algorithm": "lanczos",
                    },
                },
                {
                    "id": f"vae-encode-{sid}",
                    "type": "vae-encode",
                    "iports": [
                        {"src": f"resample-{sid}", "oport": 0},
                        {"src": "model-select", "oport": 0},
                    ],
                    "config": {},
                },
            ]
            anchor_ports[port] = f"vae-encode-{sid}"

        iports = _empty_ports(10)
        iports[0] = {"src": "diffusion-conditioner", "oport": 0}
        iports[2] = {"src": "model-select", "oport": 0}
        for port, stage_id in anchor_ports.items():
            iports[port] = {"src": stage_id, "oport": 0}
        iports[9] = {"src": "minimax-h3-model-config", "oport": 0}

        stages.append(_generate_video(iports, w, h, frames, steps, seed))
        # Audio decodes whenever this render is generating audio at all — see
        # the with_audio comment in prepare(): draft trims the picture, not
        # the sound; sketch is the one case that trims both.
        stages += _decode_and_save(paths, audio=with_audio, save_frames=save_frames)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _wan_i2v_spec(self, shot, paths, prompt, w, h, frames, steps, seed,
                      save_frames=True, sketch=False):
        """Text-to-video, plus an optional Start-frame anchor on port 5.

        Wan has no End-frame port at all (see the module docstring) and no
        audio, so this is the FL2VA template with those two removed and a
        negative prompt added -- Wan is not guidance-distilled, so without
        one `generate-video`'s guidance_scale config does nothing (see
        WAN_NEGATIVE_PROMPT).
        """
        stages: list[dict] = [
            _model_select("local/Wan2.2-I2V-A14B-8bit"),
            _text_prompt(prompt),
            {
                "id": "text-prompt-negative",
                "type": "text-prompt",
                "iports": [],
                "config": {"text": WAN_NEGATIVE_PROMPT},
            },
            {
                "id": "diffusion-conditioner",
                "type": "diffusion-conditioner",
                "iports": [
                    {"src": "text-prompt", "oport": 0},
                    {"src": "text-prompt-negative", "oport": 0},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {"unload_when_idle": "always"},
            },
            _wan_config(),
        ]

        iports = _empty_ports(10)
        iports[0] = {"src": "diffusion-conditioner", "oport": 0}
        iports[1] = {"src": "diffusion-conditioner", "oport": 1}
        iports[2] = {"src": "model-select", "oport": 0}

        # Same load -> resample -> vae-encode shape FL2VA uses for its start
        # anchor, except `frames` has to be told to vae-encode too: Wan's
        # conditioning latent is the encoding of the image followed by
        # (frames - 1) blank frames, not of the image alone (see
        # vae-encode-stage's `frames` config doc) -- so it must match
        # generate-video's `frames` exactly, the same "must match" rule
        # Ref2VA's reference encoder follows.
        src = _ref_source(shot.get("startRef"), paths)
        if src:
            if sketch:
                src = _sketchify_ref(src, paths.abs_dir / "sketch-refs")
            stages += [
                {
                    "id": "load-start",
                    "type": "load-image",
                    "iports": [],
                    "config": {"url": [src]},
                },
                {
                    "id": "resample-start",
                    "type": "image-resample",
                    "iports": [{"src": "load-start", "oport": 0}],
                    "config": {
                        "width": w,
                        "height": h,
                        "fit": "crop",
                        "algorithm": "lanczos",
                    },
                },
                {
                    "id": "vae-encode-start",
                    "type": "vae-encode",
                    "iports": [
                        {"src": "resample-start", "oport": 0},
                        {"src": "model-select", "oport": 0},
                    ],
                    "config": {"frames": frames},
                },
            ]
            iports[5] = {"src": "vae-encode-start", "oport": 0}

        iports[9] = {"src": "wan2-model-config", "oport": 0}

        stages.append(_generate_video(iports, w, h, frames, steps, seed))
        stages += _decode_and_save(paths, audio=False, save_frames=save_frames)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _ref2va_spec(self, shot, project, paths, prompt, w, h, frames, steps,
                     seed, draft=False, save_frames=True, with_audio=True,
                     sketch=False):
        """Reference-conditioned video via video-ref-encoder's file list."""
        # Voice/style audio references still go in during a draft — without
        # them H3 has nothing to clone a character's voice from, and a
        # draft is exactly when someone wants to hear whether that voice is
        # right before spending full-quality render time on it. Sketch is
        # silent by design, so there is no voice to clone into and no point
        # sending the reference at all.
        if shot.get("continuityRef") and not _ref_source(shot["continuityRef"], paths):
            raise ValueError("Previous scene reference has not been resolved; render its source first")
        refs = _ref2va_references(
            shot, project, paths, include_audio_refs=with_audio, sketch=sketch
        )

        stages: list[dict] = [
            _model_select("local/MiniMax-H3-Ref2VA-8bit"),
            _text_prompt(prompt),
            _h3_config(with_audio_timestep=True),
            {
                "id": "video-ref-encoder",
                "type": "video-ref-encoder",
                "iports": [
                    {"src": "text-prompt", "oport": 0},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {
                    "references": refs,
                    # must match generate-video's frames exactly — a mismatch
                    # is a shape error 50 layers deep
                    "frames": frames,
                    "reference_image_short_edge": 512 if draft else 1024,
                    "unload_when_idle": "auto",
                },
            },
        ]

        iports = _empty_ports(10)
        iports[0] = {"src": "video-ref-encoder", "oport": 0}
        iports[2] = {"src": "model-select", "oport": 0}
        iports[7] = {"src": "video-ref-encoder", "oport": 1}
        iports[8] = {"src": "video-ref-encoder", "oport": 2}
        iports[9] = {"src": "minimax-h3-model-config", "oport": 0}

        stages.append(_generate_video(iports, w, h, frames, steps, seed))
        stages += _decode_and_save(paths, audio=with_audio, save_frames=save_frames)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _still_spec(self, shot, paths, prompt, w, h, steps, seed,
                    chain_ref_path=None, chain_strength=0.0,
                    identity_ref_path=None):
        """Krea-2 Turbo single image, in one of three modes.

        Plain text-to-image when neither reference is set.

        ``chain_ref_path`` + ``chain_strength`` (> 0) make this an ordinary
        img2img pass: the reference is vae-encoded and wired onto
        generate-image's ref-latent iport, which for Krea-2 doubles as the
        img2img init (see generate-image-stage.h). This is a pixel-level
        continuation — "Create Stills" uses it to chain mid/end from the
        previous phase's own output instead of three unrelated rolls that
        happen to share a prompt.

        ``identity_ref_path`` instead routes through Krea-2's identity-edit
        LoRA (conradlocke/krea2-identity-edit, fetched into
        models/conradlocke/krea2-identity-edit/): the reference goes to
        *both* generate-image's ref-latent iport (at strength 0 — a clean
        anchor, not an img2img blend, per the model catalogue's own wiring
        note) and diffusion-conditioner's ref_image iport, where Krea-2's
        Qwen3-VL tower grounds the prompt in what the reference actually
        shows. This is what lets a still start from an actual character
        portrait instead of only a text description of one. The two modes
        are mutually exclusive by construction (prepare() never sets both).
        """
        out_rel = f"{paths.pipe_dir}/still.jpeg"
        use_chain = bool(chain_ref_path) and chain_strength > 0.0
        use_identity = bool(identity_ref_path) and not use_chain

        krea2_config = {"lora": "mgwr/M87", "lora_scale": 1.0}
        if use_identity:
            # The identity/few-step slot takes the edit adapter; the style
            # slot keeps the existing aesthetic LoRA active alongside it —
            # the pairing krea2-model-config's own docs describe.
            krea2_config = {
                "lora": KREA2_IDENTITY_EDIT_LORA,
                "lora_scale": 1.0,
                "lora2": "mgwr/M87",
                "lora2_scale": 1.0,
            }

        stages = [
            _model_select("krea/Krea-2-Turbo"),
            _text_prompt(prompt),
            {
                "id": "krea2-model-config",
                "type": "krea2-model-config",
                "iports": [],
                "config": krea2_config,
            },
            {
                "id": "scheduler-select",
                "type": "scheduler-select",
                "iports": [],
                "config": {"steps": steps, "shift": 0.3},
            },
        ]

        cond_ref_iport = {"src": "", "oport": 0}
        if use_identity:
            stages += [
                {
                    "id": "load-identity-ref",
                    "type": "load-image",
                    "iports": [],
                    "config": {"url": [identity_ref_path]},
                },
                {
                    "id": "resample-identity-ref",
                    "type": "image-resample",
                    "iports": [{"src": "load-identity-ref", "oport": 0}],
                    "config": {
                        "width": w,
                        "height": h,
                        "fit": "crop",
                        "algorithm": "lanczos",
                    },
                },
            ]
            cond_ref_iport = {"src": "resample-identity-ref", "oport": 0}

        stages.append({
            "id": "diffusion-conditioner",
            "type": "diffusion-conditioner",
            "iports": [
                {"src": "text-prompt", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "model-select", "oport": 0},
                cond_ref_iport,
                {"src": "", "oport": 0},
                {"src": "krea2-model-config", "oport": 0},
            ],
            "config": {},
        })

        ref_iport = {"src": "", "oport": 0}
        ref_strength = None
        if use_chain:
            # Same load -> resample -> vae-encode shape FL2VA uses for its
            # frame anchors — resampled to this render's own size so the
            # latent geometry matches, the same reason that matters there.
            stages += [
                {
                    "id": "load-still-ref",
                    "type": "load-image",
                    "iports": [],
                    "config": {"url": [chain_ref_path]},
                },
                {
                    "id": "resample-still-ref",
                    "type": "image-resample",
                    "iports": [{"src": "load-still-ref", "oport": 0}],
                    "config": {
                        "width": w,
                        "height": h,
                        "fit": "crop",
                        "algorithm": "lanczos",
                    },
                },
                {
                    "id": "vae-encode-still-ref",
                    "type": "vae-encode",
                    "iports": [
                        {"src": "resample-still-ref", "oport": 0},
                        {"src": "model-select", "oport": 0},
                    ],
                    "config": {},
                },
            ]
            ref_iport = {"src": "vae-encode-still-ref", "oport": 0}
            ref_strength = chain_strength
        elif use_identity:
            stages.append({
                "id": "vae-encode-identity-ref",
                "type": "vae-encode",
                "iports": [
                    {"src": "resample-identity-ref", "oport": 0},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {},
            })
            ref_iport = {"src": "vae-encode-identity-ref", "oport": 0}
            ref_strength = 0.0

        generate_config = {
            "height": h,
            "width": w,
            "steps": steps,
            "seed": seed,
            "i8_gemm": False,
        }
        if ref_strength is not None:
            generate_config["strength"] = ref_strength

        stages += [
            {
                "id": "generate-image",
                "type": "generate-image",
                "iports": [
                    {"src": "diffusion-conditioner", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "model-select", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "scheduler-select", "oport": 0},
                    ref_iport,
                    {"src": "", "oport": 0},
                    {"src": "krea2-model-config", "oport": 0},
                ],
                "config": generate_config,
            },
            {
                "id": "vae-decode",
                "type": "vae-decode",
                "iports": [
                    {"src": "generate-image", "oport": 0},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {},
            },
            {
                "id": "save-image",
                "type": "save-image",
                "iports": [{"src": "vae-decode", "oport": 0}],
                "config": {"path": out_rel, "quality": 95},
            },
        ]
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               [paths.abs_dir / "still.jpeg"]

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def run(
        self,
        spec: JobSpec,
        on_event: Callable[[ProgressEvent], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        argv = [str(self.binary), "--launch", spec.payload["rel_spec"]]
        result = RunResult(started_at=time.time())

        proc = subprocess.Popen(
            argv,
            cwd=spec.payload["cwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._proc = proc
        phase_pct: dict[str, float] = {}

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if not line:
                    continue

                m = LEVEL_RE.match(line)
                level = m.group("lvl") if m else "INFO"
                text = m.group("rest") if m else line
                result.log.append((level, text))

                pm = PROGRESS_RE.search(line)
                if pm:
                    phase = pm.group("phase")
                    phase_pct[phase] = float(pm.group("pct"))
                    on_event(
                        ProgressEvent(
                            phase=phase,
                            percent=_overall(phase_pct),
                            detail=text,
                            log_line=text,
                            log_level=level,
                        )
                    )
                else:
                    on_event(ProgressEvent(percent=_overall(phase_pct),
                                           log_line=text, log_level=level))

                if should_cancel():
                    self._terminate(proc, spec)
                    result.cancelled = True
                    break

            proc.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            result.error = f"{type(exc).__name__}: {exc}"
            self._terminate(proc, spec)
        finally:
            result.exit_code = proc.returncode
            result.ended_at = time.time()
            self._proc = None

        # Sketch rendered the fewest frames the model can decode, not this
        # shot's actual length — stretch the clip back out now, once, rather
        # than at every later place something reads its duration (assembly,
        # the player, a future dub pass).
        factor = float(spec.payload.get("sketchStretchFactor") or 1.0)
        if (
            not result.cancelled
            and not result.error
            and result.exit_code == 0
            and spec.payload.get("sketch")
            and factor > 1.01
            and spec.expected_outputs
        ):
            _stretch_clip(spec.expected_outputs[0], factor, result.log)

        return result

    def _terminate(self, proc: subprocess.Popen, spec: JobSpec) -> None:
        """Escalate, because vpipe does not always answer the polite signals.

        Observed by hand: SIGTERM and then SIGINT (its documented Ctrl-C path)
        both went unanswered for several seconds. Nothing has been written yet
        at that point in most runs, so a hard kill is safe — and we can check
        that rather than assume it.
        """
        if proc.poll() is not None:
            return
        wrote_anything = any(p.exists() for p in spec.expected_outputs)
        for sig, grace in ((signal.SIGTERM, 5), (signal.SIGINT, 5)):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=grace if wrote_anything else 2)
                return
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def cancel(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def extra_checks(self, spec: JobSpec, result: RunResult) -> Iterable[Check]:
        # A line already classified as benign is excluded from the
        # silent-failure scan, rather than only being counted separately.
        # Without this the two lists do not interact and a known-harmless
        # warning can still fail the run: "wired pool: the box refused to
        # wire 0 MB" is graceful degradation, and it tripped a pattern
        # looking for a refusal.
        def benign(text: str) -> bool:
            return any(re.search(pat, text, re.I) for pat in BENIGN_PATTERNS)

        joined = "\n".join(text for _lvl, text in result.log if not benign(text))

        hits: list[str] = []
        for pattern, meaning in SILENT_FAILURE_PATTERNS:
            if re.search(pattern, joined, re.IGNORECASE):
                hits.append(meaning)
        yield Check(
            "log scan",
            not hits,
            "; ".join(sorted(set(hits))) if hits else "no silent-failure patterns",
        )

        # Only meaningful for video: a run that never reached the denoise loop
        # did not generate anything, whatever its exit code says.
        if spec.expected_frames:
            saw_denoise = any(
                "denoise" in text and "PROGRESS" in lvl for lvl, text in result.log
            ) or "of 'denoise'" in joined
            yield Check(
                "denoise ran",
                saw_denoise,
                "denoise progress reported" if saw_denoise
                else "no denoise progress ever reported",
            )

        benign_lines = [
            text for lvl, text in result.log if lvl == "WARN" and benign(text)
        ]
        if benign_lines:
            yield Check(
                "warnings classified",
                None,
                f"{len(benign_lines)} benign warning(s) ignored (e.g. wired pool)",
            )


# ---------------------------------------------------------------------------
# stage helpers
# ---------------------------------------------------------------------------

def _model_select(hf_dir: str) -> dict:
    return {
        "id": "model-select",
        "type": "model-select",
        "iports": [],
        "config": {"hf_dir": hf_dir},
    }


def _text_prompt(text: str) -> dict:
    return {
        "id": "text-prompt",
        "type": "text-prompt",
        "iports": [],
        "config": {"text": text},
    }


def _h3_config(with_audio_timestep: bool = False) -> dict:
    cfg = {
        "video_shift": 12.0,
        "audio_shift": 3.0,
        "condition_timestep": 1.0,
        "audio_seconds": 0.0,
    }
    if with_audio_timestep:
        cfg["condition_audio_timestep"] = 1.0
    return {
        "id": "minimax-h3-model-config",
        "type": "minimax-h3-model-config",
        "iports": [],
        "config": cfg,
    }


def _wan_config() -> dict:
    # boundary_ratio deliberately omitted -- unset, generate-video reads it
    # from the checkpoint's own model_index.json (0.9) rather than this
    # stage overriding it; see wan2-model-config-stage's own doc on why an
    # absent key must not be emitted as if it were a choice.
    return {
        "id": "wan2-model-config",
        "type": "wan2-model-config",
        "iports": [],
        "config": {"guidance_scale": 3.5, "guidance_scale_2": 3.5},
    }


def _empty_ports(n: int) -> list[dict]:
    return [{"src": "", "oport": 0} for _ in range(n)]


def _generate_video(iports, w, h, frames, steps, seed) -> dict:
    return {
        "id": "generate-video",
        "type": "generate-video",
        "iports": iports,
        "config": {
            "height": h,
            "width": w,
            "frames": frames,
            "fps": 24,
            "steps": steps,
            "seed": seed,
            # lossy accelerated mode: no-op on M4, alters the picture on M5.
            "i8_gemm": False,
            "unload_when_idle": "always",
        },
    }


def _decode_and_save(paths: ShotPaths, audio: bool,
                     save_frames: bool = True) -> list[dict]:
    stages = [
        {
            "id": "vae-decode",
            "type": "vae-decode",
            "iports": [
                {"src": "generate-video", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {},
        },
        {
            "id": "rgb-to-video",
            "type": "rgb-to-video",
            "iports": [{"src": "vae-decode", "oport": 0}],
            "config": {"fps": 24},
        },
    ]
    if save_frames:
        stages.append({
            "id": "save-frames",
            "type": "save-image",
            "iports": [{"src": "vae-decode", "oport": 0}],
            "config": {
                "path": f"{paths.pipe_frames}/frame-%04d.png",
                "format": "png",
            },
        })
    save_iports = [{"src": "rgb-to-video", "oport": 0}]
    if audio:
        stages.insert(
            0,
            {
                "id": "audio-vae-decode",
                "type": "audio-vae-decode",
                "iports": [
                    {"src": "generate-video", "oport": 1},
                    {"src": "model-select", "oport": 0},
                ],
                "config": {},
            },
        )
        save_iports.append({"src": "audio-vae-decode", "oport": 0})
    stages.append(
        {
            "id": "save-video",
            "type": "save-video",
            "iports": save_iports,
            "config": {
                "output_url": f"{paths.pipe_dir}/clip.mp4",
                "enable_video": True,
                "enable_audio": audio,
            },
        }
    )
    return stages


def _outputs(paths: ShotPaths) -> list[Path]:
    return [paths.abs_dir / "clip.mp4"]


def _stretch_clip(path: Path, factor: float, log: list[tuple[str, str]]) -> None:
    """Hold sketch mode's frames to fill this shot's real duration.

    Sketch renders at 24fps same as any other clip — it just asks for far
    fewer of them (see the frame-count comment in prepare()). ``setpts``
    slows the timeline by ``factor`` and the matching output ``-r`` makes
    ffmpeg fill it back in by holding each rendered frame rather than
    inventing motion between them, which is exactly what a low-sample
    preview should look like: choppy, not smoothed over.

    Best-effort: a clip left at its short, rendered length is still usable
    (just shorter than the shot calls for), so a failure here is a warning
    in the log, not a reason to fail the render.
    """
    if not path.exists():
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.append(("WARN", "ffmpeg not on PATH — sketch clip left at its "
                             "rendered length, not stretched to match the "
                             "shot's full duration"))
        return
    tmp = path.with_suffix(".stretch.mp4")
    cmd = [
        ffmpeg, "-y", "-i", str(path),
        "-vf", f"setpts={factor:.6f}*PTS",
        "-r", "24", "-an",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        tmp.replace(path)
        log.append(("INFO", f"sketch clip held {factor:.2f}x to match this "
                             f"shot's full length"))
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not fatal
        log.append(("WARN", f"could not stretch sketch clip to full "
                             f"duration: {exc}"))
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------

def _align_up(v: int, align: int) -> int:
    """Round up to a multiple of `align` — the same direction generate-video
    rounds, so snapping here is a no-op there rather than a second move."""
    if align <= 1:
        return v
    return ((v + align - 1) // align) * align


def _draft_geometry(w: int, h: int, steps: int,
                    align: int = 16) -> tuple[int, int, int]:
    """Shrink a request so a draft answers "does the shot work" quickly.

    What is safe to cut, and what is not:

    *   **Pixels** — the big lever. Denoise cost is proportional to frame area,
        so the draft caps the long edge at 384 px and never uses more than
        half-size. Rounded UP to `align`, the multiple the model tiles at.
        Up rather than down because generate-video rounds up: a draft that
        rounded the other way would be re-rounded there, and a start-frame
        anchor encoded at this size would stop matching.
    *   **Steps** — cut to 4. Below 8 is nominally the Turbo LoRA's territory
        rather than the raw model's, but for judging whether a camera move and
        a composition work, noisy output is the correct trade.
    *   **Frames — NOT cut.** The whole point of a draft here is checking
        motion, and a shorter clip is a different motion. Length is preserved.
    *   **Seed — NOT changed** (handled by the caller). Same seed keeps the
        draft indicative of the final; it will not be identical, because
        changing the resolution changes the latent geometry, but the framing
        and the move carry over.
    """
    scale = min(0.5, 384.0 / max(w, h))
    dw = _align_up(max(align, round(w * scale)), align)
    dh = _align_up(max(align, round(h * scale)), align)
    return dw, dh, max(4, min(steps, 4))


def _wh(res: str) -> tuple[int, int]:
    try:
        w, h = res.lower().split("x")
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return 960, 544


def _has_downstream_chain(shot: dict, project: dict) -> bool:
    shot_id = shot.get("id")
    if not shot_id:
        return False
    for other in project.get("shots") or []:
        for key in ("startRef", "endRef", "continuityRef"):
            ref = other.get(key)
            if (
                isinstance(ref, dict)
                and ref.get("kind") == "chain"
                and ref.get("from") == shot_id
            ):
                return True
    return False


def _resolved_prompt(
    shot: dict,
    project: dict,
    with_audio: bool = True,
    model: str = "",
) -> str:
    """Parts in the order MiniMax H3's own examples use.

    1. the project scene description — subject and style, every shot
    1b. the cast this shot uses — "Name: description", so the shot prompt can
        refer to them by name
    2. this shot's prompt — action, camera and mood, this shot only
    3. the dialogue — spoken outright when H3 has the speaker's own voice
       clip to clone from (Ref2VA only), otherwise a silent lip-movement cue
       for TTS to dub in afterwards
    4. the project soundscape — the constant ambient bed, every shot
    5. this shot's sound note — the accents specific to this clip

    All the sound sits at the end, bed before accents, because that is the
    shape H3's shipped examples take ("... the brushwork shimmers as the
    camera closes in. Gentle lapping water, a soft breeze through pines,
    distant birdsong.") and because the model generates the soundtrack in the
    same denoise loop as the picture rather than dubbing it on afterwards —
    so the sound description is conditioning, not metadata.

    Ref2VA already sends the speaking character's voice clip in as a
    soundtrack reference (see ``_ref2va_references``) — verified to be
    enough on its own for H3 to reproduce a line accurately, so asking it to
    speak is asking for something it can already do, not hoping for a happy
    accident. FL2VA has no mechanism to tell H3 what anyone sounds like, so
    there dialogue stays a silent cue and TTS is still the only source of a
    voice.

    Sound parts are dropped for a model with no audio (a still), where they
    would only compete with the visual description.
    """
    parts = []
    if model == "fl2va":
        # FL2VA's image inputs are true first/last-frame anchors rather than
        # members of Ref2VA's <Picture N> list. Put the temporal alignment at
        # the beginning of the prompt, as required by the H3 video guide.
        alignment = []
        if shot.get("startRef"):
            alignment.append("Picture 1 is the Start frame at 0.00 seconds")
        if shot.get("endRef"):
            picture_number = 2 if shot.get("startRef") else 1
            frames = int(shot.get("frames") or 0)
            seconds = max(frames - 1, 0) / 24 if frames else 0
            alignment.append(
                f"Picture {picture_number} is the End frame at {seconds:.2f} seconds"
            )
        if alignment:
            parts.append(
                "How the reference pictures align with the target video — "
                + "; ".join(alignment) + "."
            )
    has_shot_refs = model == "ref2va" and bool(_shot_reference_images(shot))
    if has_shot_refs:
        parts.append(
            "The shot reference image set is the primary visual reference for "
            "this clip's location, framing, lighting and surface details."
        )
        parts.append(
            "Use character portraits for identity and material details only; "
            "do not copy their pose, camera angle, or background."
        )
    view = _view_constraint(shot)
    if view:
        parts.append(view)
    parts.append((project.get("sceneDescription") or "").strip())
    parts.append("Shared scene and Cast details are defaults. Explicit shot instructions take priority for actions, setting, and appearance changes.")

    # Characters appearing in this shot, named so the shot prompt can refer to
    # them ("Kira ducks behind the crate"). Only the ones this shot casts —
    # describing the whole cast every time would dilute the conditioning.
    for ch in _shot_characters(shot, project):
        name = (ch.get("name") or "").strip()
        desc = (ch.get("description") or "").strip()
        character = _character_description(name, desc)
        if character:
            parts.append(character)
        if model == "ref2va" and ch.get("image"):
            parts.append(
                f"{name or 'The selected character'}: use the character portrait "
                "and Cast description together to maintain appearance. Use the "
                "portrait for visual identity and the Cast description for "
                "persistent appearance details, clothing, and equipment. Ignore "
                "pose and background in the portrait or Cast description. "
                "Follow the scene prompt for actions, expressions, posture, "
                "camera, environment, and any explicit appearance changes."
            )

    bindings = _reference_bindings(shot, project, model)
    if model == "ref2va" and any(
        entry["name"] in ("Start frame", "End frame") for entry in bindings
    ):
        parts.append(
            "The Start frame and End frame references describe the intended "
            "opening and closing composition. Ref2VA uses them as ordered "
            "visual references rather than hard-pinned keyframes; preserve "
            "their identity, layout and continuity while following the shot "
            "action."
        )
    if shot.get("continuityRef") and model == "ref2va":
        parts.append("Continue naturally from the previous scene reference. Preserve its environment, "
                     "lighting, character position and direction of motion at the opening, while "
                     "using the original cast portraits for identity. This is continuity guidance, "
                     "not a fixed first frame.")
    for entry in bindings:
        parts.append(f"{entry['token']}: {entry['name']} reference; use for {entry['role']} only")
    shot_prompt = (shot.get("prompt") or "").strip()
    tags = {e["tag"]: e["token"] for e in bindings if e["tag"]}
    def replace_tag(match):
        tag = match.group(1)
        if tag not in tags:
            # FL2VA has no prompt-addressable reference-image list. A tag can
            # legitimately remain in the prompt when a user switches away
            # from Ref2VA; keep its meaning as ordinary words and let the UI
            # explain that the image is inactive instead of failing prepare.
            if model != "ref2va":
                return tag.replace("-", " ").replace("_", " ")
            raise ValueError(f"Unknown or unsupported reference tag @{tag}")
        return tags[tag]
    parts = [re.sub(r"@([A-Za-z0-9_-]+)", replace_tag, part) for part in parts]
    shot_prompt = re.sub(r"@([A-Za-z0-9_-]+)", replace_tag, shot_prompt)
    parts.append(shot_prompt)
    line = (shot.get("dialogue") or "").strip()
    clones_voice = bool(line) and _clones_voice(shot, project, model)
    if with_audio:
        cue = _dialogue_visual_cue(shot, project, clones_voice=clones_voice)
        if cue:
            parts.append(cue)

    if with_audio:
        if project.get("soundscapeInShots", True):
            parts.append((project.get("soundscape") or "").strip())
        parts.append((shot.get("soundNote") or "").strip())
        if line and not clones_voice:
            parts.append(
                "Generated audio contains ambient sound only: no spoken words, "
                "no voice, and no intelligible dialogue; the voice line is "
                "dubbed separately."
            )
        elif line and clones_voice:
            # The shared soundscape may quite reasonably say "no speech" for
            # an otherwise quiet scene. Once native H3 dialogue is selected,
            # that ambient constraint must not cancel the explicit spoken
            # line or its corresponding mouth movement.
            parts.append(
                "Dialogue priority: the selected character must speak the "
                "specified line aloud in the referenced voice; any 'no speech' "
                "instruction applies only to background or unrelated voices."
            )
    return " ".join(_sentence(p) for p in parts if p)


def _effective_video_model(shot: dict, project: dict) -> tuple[str, str]:
    """Choose H3's video mode from the shot's frame-anchor inputs, unless a
    different model was explicitly requested.

    FL2VA is the only H3 mode that can wire a supplied image to the
    first/last frame ports, so either Start frame or End frame activates it
    for the H3 pair below. With no anchors, Ref2VA is the useful default
    because it can consume character, style, object and other reference
    material. The persisted ``model`` field remains a compatibility/default
    field; this decision is intentionally derived from the actual inputs so
    old boards route correctly too.

    ``krea2-still`` and ``wan-i2v`` are not part of that automatic H3
    routing — both are only ever used when explicitly requested, since
    neither is a drop-in default for the other two: Wan has no reference-list
    mode to fall back to the way Ref2VA is H3's fallback, and forcing a
    board that has always rendered H3 onto a different engine because it
    happens to have a Start frame set would be a surprising, silent switch.
    """
    if shot.get("continuityRef") and (shot.get("startRef") or shot.get("endRef")):
        raise ValueError("Choose reference continuity or Start/End frame anchors, not both")
    requested = (
        shot.get("model")
        or (project.get("defaults") or {}).get("model")
        or "ref2va"
    )
    if requested in ("krea2-still", "wan-i2v"):
        return requested, ""
    if shot.get("startRef") or shot.get("endRef"):
        return "fl2va", "using FL2VA for Start/End frame anchors"
    return "ref2va", "" if requested == "ref2va" else "using Ref2VA without frame anchors"


def _ref2va_references(
    shot: dict,
    project: dict,
    paths: ShotPaths,
    include_audio_refs: bool = True,
    sketch: bool = False,
) -> list[str]:
    """Reference order for Ref2VA, strongest shot-local signals first.

    Start/end and other shot-local images come first. Character portraits and
    project style refs follow in the same request so the model can use every
    available reference until its image limit is reached.
    """
    images: list[str] = []
    sounds: list[str] = []

    def add_image(ref: Any) -> None:
        src = _ref_source(ref, paths)
        if sketch and src:
            src = _sketchify_ref(src, paths.abs_dir / "sketch-refs")
        if src and src not in images and len(images) < 9:
            images.append(src)

    def add_sound(ref: Any) -> None:
        src = _ref_source(ref, paths)
        if src and src not in sounds and len(sounds) < 3:
            sounds.append(src)

    # This helper is only used by Ref2VA preparation. The effective model
    # routes any shot with a Start/End anchor to FL2VA before this is called;
    # when called directly, keep the historical ordered-reference behaviour.
    for entry in _reference_bindings(shot, project, "ref2va"):
        add_image(entry["ref"])

    # Only the character who actually speaks this shot's line — every other
    # cast member's voice clip would just be a second, unlabelled candidate
    # voice for the same line, which is exactly the kind of ambiguity that
    # caused H3 to blend/duplicate a voice instead of cloning one cleanly.
    if include_audio_refs and shot.get("dialogueSource", "auto") != "recording" and (shot.get("dialogue") or "").strip():
        speaker = speaker_for(shot, project)
        if speaker:
            add_sound(speaker.get("voice"))

    # "audio can never be the only kind": a voice clip with no picture
    # alongside it is not a request Ref2VA accepts, so drop the sound rather
    # than have the encoder refuse the whole thing.
    if sounds and not images:
        sounds = []

    return (images + sounds)[:12]


def _reference_bindings(shot: dict, project: dict, model: str) -> list[dict]:
    """One ordered manifest shared by prompt labels and image encoder inputs."""
    candidates = []
    if model == "ref2va":
        if shot.get("continuityRef"):
            candidates.append((shot["continuityRef"], "Previous scene",
                               "opening composition, position, lighting and direction of travel; retain original cast identity"))
        if shot.get("startRef"):
            candidates.append((shot["startRef"], "Start frame", "opening frame and composition"))
        if shot.get("endRef"):
            candidates.append((shot["endRef"], "End frame", "closing frame and composition"))
        candidates.extend((r, "Shot reference", "environment and composition") for r in (shot.get("referenceImages") or []))
        candidates.extend((c["image"], c.get("name") or "Character", "character identity")
                          for c in _shot_characters(shot, project) if c.get("image"))
        # Keep project-wide references in the same Ref2VA request even when a
        # shot also has local references. The model's nine-image limit is
        # enforced below, with shot-local inputs taking priority by order.
        candidates.extend((r, "Project style", "style") for r in project.get("styleRefs") or [])
    # FL2VA anchors are wired directly to the model's keyframe ports. They
    # are not members of a prompt-addressable reference list.
    result, seen, tags = [], {}, set()
    for ref, name, role in candidates:
        data = ref if isinstance(ref, dict) else {"path": ref}
        key = data.get("path") or data.get("resolved") or ("chain:" + data.get("from", "") if data.get("kind") == "chain" else "")
        if not key:
            continue
        tag = (data.get("tag") or "").strip().lstrip("@")
        if tag and (not re.fullmatch(r"[A-Za-z0-9_-]+", tag) or tag in tags):
            raise ValueError(f"Reference tags must be unique letters, digits, hyphens or underscores: @{tag}")
        if key in seen:
            existing = next(entry for entry in result if entry["token"] == seen[key])
            existing["name"] += " / " + name
            existing["role"] += "; " + (data.get("role") or role)
            if tag:
                raise ValueError("Use one reference entry per tagged image")
            continue
        if len(result) >= 9:
            raise ValueError("Ref2VA accepts at most 9 unique images; remove unused references")
        token = f"<Picture {len(result) + 1}>"
        seen[key] = token
        if tag:
            tags.add(tag)
        result.append(dict(ref=ref, token=token, tag=tag, name=name,
                           role=data.get("role") or role))
    return result


def _shot_reference_images(shot: dict) -> list[Any]:
    """All image references for Ref2VA, including labeled frame references."""
    refs = []
    if shot.get("continuityRef"):
        refs.append(shot["continuityRef"])
    if shot.get("startRef"):
        refs.append(shot.get("startRef"))
    if shot.get("endRef"):
        refs.append(shot.get("endRef"))
    refs.extend(shot.get("referenceImages") or [])
    return refs


def _view_constraint(shot: dict) -> str:
    prompt = (shot.get("prompt") or "").lower()
    rear_words = ("from behind", "from the rear", "rear view", "back view")
    if any(word in prompt for word in rear_words):
        return (
            "Camera constraint: keep the visible character facing away from "
            "the camera from the first frame; show back plating and rear "
            "silhouette, not a front-facing portrait, hands-on-hips pose, "
            "face, eyes, chest plate, or front torso."
        )
    return ""


def _clones_voice(shot: dict, project: dict, model: str) -> bool:
    """True when this render can hand H3 the speaking character's own voice
    clip as an audio reference, so it can be asked to actually say the line
    instead of only moving its mouth to it.

    Only Ref2VA takes soundtrack references at all (see
    ``_ref2va_references``) — FL2VA has no mechanism to tell H3 what anyone
    sounds like, so asking it to "speak in their own voice" there would be
    asking it to invent one, not clone one.
    """
    if model != "ref2va" or shot.get("dialogueSource", "auto") == "recording":
        return False
    speaker = speaker_for(shot, project)
    return bool(speaker and (speaker.get("voice") or {}).get("path"))


def _dialogue_visual_cue(
    shot: dict, project: dict, clones_voice: bool = False
) -> str:
    line = (shot.get("dialogue") or "").strip()
    if not line:
        return ""
    speaker = _speaker_name(shot, project)
    if clones_voice:
        style = (shot.get("dialogueStyle") or "").strip()
        delivery = f" Delivery: {style}." if style else ""
        return (
            f"{speaker} speaks aloud, in their own voice from the reference "
            f"clip, saying exactly: \"{line}\"{delivery}"
        )
    return (
        f"{speaker} speaks the line with natural jaw and lip movement: "
        f"\"{line}\""
    )


def _speaker_name(shot: dict, project: dict) -> str:
    speaker = speaker_for(shot, project)
    if speaker and (speaker.get("name") or "").strip():
        return speaker["name"].strip()
    return "The visible character"


def _sentence(text: str) -> str:
    """End a fragment so the pieces do not run together when joined.

    Without this, "Kira: a wiry pilot" followed by "Kira leans out" reads as
    one run-on clause, which is worse conditioning than two clear ones.
    """
    text = text.strip()
    if text[-1:] in ".!?;:,":
        return text
    if len(text) >= 2 and text[-1] in "\"”'" and text[-2] in ".!?;:,":
        return text
    return text + "."


def _character_description(name: str, desc: str) -> str:
    """Name a cast description once, even if an AI proposal included it."""
    name = (name or "").strip()
    desc = (desc or "").strip()
    if not desc:
        return ""
    if name and desc.lower().startswith(name.lower() + ":"):
        return desc
    return f"{name}: {desc}" if name else desc


def _shot_characters(shot: dict, project: dict) -> list[dict]:
    """The cast members this shot uses, in the board's cast order."""
    wanted = set(shot.get("characterIds") or [])
    if not wanted:
        return []
    return [c for c in (project.get("characters") or []) if c.get("id") in wanted]


def _ref_source(ref: Any, paths: ShotPaths) -> str | None:
    """Absolute path a load-image stage can open.

    Stored reference paths are relative to the data directory, which keeps a
    board portable. They are made absolute here rather than left relative,
    because the data directory is not necessarily under the workspace vpipe
    runs in.
    """
    if not ref:
        return None
    if isinstance(ref, str):
        rel = ref
    elif ref.get("kind") == "chain":
        rel = ref.get("resolved") or ""
    else:
        rel = ref.get("path") or ref.get("resolved") or ""
    if not rel:
        return None
    return rel if Path(rel).is_absolute() else paths.pipe_path(rel)


def _sketchify_ref(src: str, sketch_dir: Path) -> str:
    """An edge-detected, line-art copy of a reference image, for sketch mode.

    A style word in the prompt is a nudge; what a reference image shows is
    close to a mandate — Ref2VA's whole job is reproducing it (see
    SKETCH_STYLE_PREFIX). So handing it a photo and asking in text for a
    sketch fights the mechanism; handing it an actual line drawing does not.

    A blur before edge-detection is what keeps this looking like loose
    pencil strokes rather than a speckle of every JPEG artefact — verified
    by hand against these boards' own reference photos.

    Best-effort: falls back to the original photo if ffmpeg is missing or
    the pass fails, since a photoreal reference still beats no reference.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not Path(src).exists():
        return src
    sketch_dir.mkdir(parents=True, exist_ok=True)
    out = sketch_dir / f"{Path(src).stem}.png"
    cmd = [
        ffmpeg, "-y", "-i", src,
        "-vf", "gblur=sigma=1.8,format=gray,edgedetect=mode=wires:high=0.35:low=0.12,negate",
        "-frames:v", "1", str(out),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        return str(out)
    except Exception:  # noqa: BLE001 - a photo reference is a fine fallback
        return src


def _estimate_seconds(w: int, h: int, frames: int, steps: int,
                      model: str = "fl2va") -> float:
    """Baseline for the runtime sanity check.

    Only used to catch a run that finished implausibly fast, so it needs to be
    roughly right rather than precise — the check trips below 25% of this.

    Fitted to two measured runs on this machine (M4 Pro, 48 GB, 960x544,
    8 steps): 124 frames took 27m 44s with ~24m of that in denoise, and 39
    frames took 7m 32s with ~5m in denoise. Denoise is not linear in frames
    (attention grows faster than the sequence), and those two points give an
    exponent of about 1.37:

        log(1438/296) / log(124/39) ~= 1.37
        denoise ~= 1.95 * frames**1.37

    Plus a fixed floor for model load, the 32B prompt encode and VAE decode.
    Predicts 26.8m and 7.8m against the measured 27.7m and 7.5m.
    """
    px = (w * h) / (960 * 544)
    fixed = 170.0
    denoise = 1.95 * (frames ** 1.37) * (steps / 8.0) * px

    # Ref2VA packs its references into the same sequence being denoised, and
    # encodes each one twice up front (vision tower, then video VAE). Measured
    # on this machine: ~35 min against ~27 min for the same geometry on FL2VA
    # with one image reference. Approximate, and only used to spot a run that
    # finished implausibly fast.
    if model == "ref2va":
        return (fixed + denoise) * 1.3
    if model == "wan-i2v":
        # UNMEASURED — no Wan clip has actually been timed on this machine
        # yet. Rough reasoning, not a fit: Wan is not guidance-distilled (2
        # forward passes/step, H3 has 1) and its shipped examples run ~40
        # steps against H3's 8, so total DiT compute per clip is plausibly
        # an order of magnitude higher per frame even though each of its
        # two resident 14B experts is smaller than H3's 33B stack. Replace
        # this multiplier with a fitted curve once a clip has been timed —
        # until then it only has to be roughly right, per this function's
        # own docstring.
        return (fixed + denoise) * 3.0
    return fixed + denoise


def estimate_render_seconds(shot: dict, project: dict) -> float | None:
    """Predicted wall-clock seconds to render *shot*, for a human (or the
    Storyboard AD assistant) asking "how long will this take" before
    spending the time. None for a shot that renders as a still
    (``krea2-still``) or whose reference setup is currently invalid, since
    the frame-based formula below does not apply to either.

    Built from the same pieces the render path itself uses — model
    selection, draft geometry — and calls the same :func:`_estimate_seconds`
    the runtime-plausibility check validates a finished run against, so a
    prediction given before rendering and that check's baseline never
    quietly disagree.
    """
    try:
        model, _ = _effective_video_model(shot, project)
    except ValueError:
        return None
    if model == "krea2-still":
        return None
    defaults = project.get("defaults") or {}
    w, h = _wh(shot.get("resolution") or defaults.get("resolution") or "960x544")
    frames = shot.get("frames") or defaults.get("frames") or 124
    steps = shot.get("steps") or defaults.get("steps") or 8
    if defaults.get("draft"):
        w, h, steps = _draft_geometry(w, h, steps)
    return _estimate_seconds(w, h, frames, steps, model)


def _overall(phase_pct: dict[str, float]) -> float:
    """Blend per-phase percentages into one overall number.

    Phases run in order, so reaching a later one means every earlier one is
    done — that is what keeps the bar from jumping backwards when vpipe moves
    from 'denoise' to 'vae decode' and restarts its own count at 0%.

    Only phases this run *actually has* are counted. Not every pipeline has
    every phase: a plain text-to-video run never encodes references, so
    crediting that phase's weight would start the bar at 10% before any work
    had happened. The weights are renormalised over the phases in play.
    """
    if not phase_pct:
        return 0.0

    seen = [p for p in PHASE_ORDER if p in phase_pct]
    if not seen:
        # a phase we do not model (e.g. 'quantize') — report it directly
        return round(min(99.0, max(phase_pct.values())), 1)

    furthest = max(PHASE_ORDER.index(p) for p in seen)
    # phases that count: those we have seen, plus any still to come
    counted = [
        p for i, p in enumerate(PHASE_ORDER) if p in phase_pct or i > furthest
    ]
    denom = sum(PHASE_WEIGHTS.get(p, 0.0) for p in counted) or 1.0

    total = 0.0
    for i, phase in enumerate(PHASE_ORDER):
        if phase not in counted:
            continue
        weight = PHASE_WEIGHTS.get(phase, 0.0)
        if i < furthest:
            total += weight                                   # finished
        elif i == furthest:
            total += weight * phase_pct[phase] / 100.0        # in progress
    return round(min(99.0, total / denom * 100.0), 1)
