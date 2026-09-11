"""vpipe backend.

Generates a `.vpipeline` file per shot and runs it with
``vpipe --launch <file>``, parsing stdout for progress.

The four templates here are transcriptions of pipelines that were built and
run by hand against real models, not guesses from documentation:

*   ``fl2va``          — text to video+audio
*   ``fl2va`` + anchor — the same, with ``generate-video`` port 5 fed a
                         ``vae-encode`` of a still, so the clip opens on that
                         exact frame (optionally port 6 for the closing frame)
*   ``ref2va``         — ``video-ref-encoder`` with a reference image list,
                         carrying subject/style across the whole clip
*   ``krea2-still``    — a single image, for cheap prompt previews

Two hard-won details are encoded rather than left to the user:

*   MiniMax H3 frame counts must be ``17n + 5`` **and** produce at least 8
    latent frames. ``frames: 5`` is accepted by the generate stage and then
    silently fails at VAE decode, writing nothing — hence a minimum of 39.
*   ``i8_gemm`` is a lossy speed mode that does nothing on M4 and changes the
    picture on M5, so it is off by default here; quality is the point of a
    long unattended render.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

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

# Every dimension here is a multiple of 32, matching H3's rounded canvas and
# still satisfying Krea's multiple-of-16 requirement. Grouped by aspect so the
# UI can offer a ratio rather than a pixel string. Sizes above H3's documented
# 768p canvas are deliberate "try it if you can afford the time/RAM" options
# and remain labelled untested unless a model marks them otherwise.
ASPECT_TABLE: dict[str, list[str]] = {
    "21:9": ["1792x768", "1344x576", "1120x480"],
    "16:9": ["1920x1088", "1536x864", "1344x768", "1280x736",
             "960x544", "832x480", "672x384"],
    "4:3":  ["1280x960", "1024x768", "768x576", "640x480"],
    "1:1":  ["1536x1536", "1344x1344", "1024x1024", "768x768", "640x640"],
    "3:4":  ["960x1280", "768x1024", "576x768"],
    "9:16": ["1088x1920", "864x1536", "768x1344", "544x960", "480x832"],
}
ALL_RESOLUTIONS = [r for group in ASPECT_TABLE.values() for r in group]

# The sizes each model's own documentation actually cites. Anything else in
# ALL_RESOLUTIONS is offered but untested.
H3_TESTED = ["960x544", "832x480", "1344x768"]
KREA_TESTED = ["1024x1024"]

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
        model = shot.get("model") or "fl2va"
        cap = self.capability(model)
        if cap is None:
            raise ValueError(f"unknown model: {model}")
        if not cap.available:
            raise ValueError(cap.unavailable_reason or f"{model} is not available")

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
        prompt = _resolved_prompt(
            shot,
            project,
            with_audio=cap.supports_audio and not draft,
            model=model,
        )
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
            spec, outputs = self._still_spec(shot, paths, prompt, width, height, steps, seed)
            expected_seconds = 150.0
            frames_dir = None
            frames = 0
            save_frames = False
        else:
            frames = cap.frame_rule.snap(int(shot.get("frames") or cap.frame_rule.minimum))
            save_frames = (not draft) or _has_downstream_chain(shot, project)
            if model == "ref2va":
                spec, outputs = self._ref2va_spec(
                    shot, project, paths, prompt, width, height, frames, steps,
                    seed, draft, save_frames
                )
            else:
                spec, outputs = self._fl2va_spec(
                    shot, paths, prompt, width, height, frames, steps, seed,
                    draft, save_frames
                )
            expected_seconds = _estimate_seconds(width, height, frames, steps, model)
            frames_dir = paths.abs_frames if save_frames else None

        spec_path = paths.abs_dir / "shot.vpipeline"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")

        return JobSpec(
            shot_id=shot["id"],
            expected_outputs=outputs,
            frames_dir=frames_dir,
            expected_frames=frames if save_frames else 0,
            expected_seconds=expected_seconds,
            payload={
                "spec_path": str(spec_path),
                "rel_spec": f"{paths.pipe_dir}/shot.vpipeline",
                "cwd": str(self.workspace),
                "model": model,
                "frames": frames,
                "draft": draft,
                "save_frames": save_frames,
            },
            summary=(
                f"{cap.label.split('—')[0].strip()} · {width}x{height}"
                + (f" · {frames}f" if cap.kind == "video" else "")
                + f" · {steps} steps"
                + (" · DRAFT" if draft else "")
            ),
        )

    # -- templates ------------------------------------------------------ #

    def _fl2va_spec(self, shot, paths, prompt, w, h, frames, steps, seed,
                    draft=False, save_frames=True):
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
        stages += _decode_and_save(paths, audio=not draft, save_frames=save_frames)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _ref2va_spec(self, shot, project, paths, prompt, w, h, frames, steps,
                     seed, draft=False, save_frames=True):
        """Reference-conditioned video via video-ref-encoder's file list."""
        refs = _ref2va_references(shot, project, paths,
                                  include_audio_refs=not draft)

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
        stages += _decode_and_save(paths, audio=not draft, save_frames=save_frames)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _still_spec(self, shot, paths, prompt, w, h, steps, seed):
        """Krea-2 Turbo single image."""
        out_rel = f"{paths.pipe_dir}/still.jpeg"
        stages = [
            _model_select("krea/Krea-2-Turbo"),
            _text_prompt(prompt),
            {
                "id": "krea2-model-config",
                "type": "krea2-model-config",
                "iports": [],
                "config": {"lora": "mgwr/M87", "lora_scale": 1.0},
            },
            {
                "id": "scheduler-select",
                "type": "scheduler-select",
                "iports": [],
                "config": {"steps": steps, "shift": 0.3},
            },
            {
                "id": "diffusion-conditioner",
                "type": "diffusion-conditioner",
                "iports": [
                    {"src": "text-prompt", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "model-select", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "krea2-model-config", "oport": 0},
                ],
                "config": {},
            },
            {
                "id": "generate-image",
                "type": "generate-image",
                "iports": [
                    {"src": "diffusion-conditioner", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "model-select", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "scheduler-select", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "", "oport": 0},
                    {"src": "krea2-model-config", "oport": 0},
                ],
                "config": {
                    "height": h,
                    "width": w,
                    "steps": steps,
                    "seed": seed,
                    "i8_gemm": False,
                },
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
        for key in ("startRef", "endRef"):
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
    3. the dialogue as a visual lip-movement cue, not generated speech
    4. the project soundscape — the constant ambient bed, every shot
    5. this shot's sound note — the accents specific to this clip

    All the sound sits at the end, bed before accents, because that is the
    shape H3's shipped examples take ("... the brushwork shimmers as the
    camera closes in. Gentle lapping water, a soft breeze through pines,
    distant birdsong.") and because the model generates the soundtrack in the
    same denoise loop as the picture rather than dubbing it on afterwards —
    so the sound description is conditioning, not metadata.

    Dialogue is kept out of the shot prompt field but still reaches the video
    model as a visual cue. That gives the model a chance to animate a mouth
    while the real voice is generated by TTS and muxed in afterwards.

    Sound parts are dropped for a model with no audio (a still), where they
    would only compete with the visual description.
    """
    parts = []
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
    if not has_shot_refs:
        parts.append((project.get("sceneDescription") or "").strip())

    # Characters appearing in this shot, named so the shot prompt can refer to
    # them ("Kira ducks behind the crate"). Only the ones this shot casts —
    # describing the whole cast every time would dilute the conditioning.
    for ch in _shot_characters(shot, project):
        name = (ch.get("name") or "").strip()
        desc = (ch.get("description") or "").strip()
        if has_shot_refs and ch.get("image") and name:
            parts.append(
                f"{name}: use the character portrait for identity, silhouette, "
                "materials, and color only; follow this shot for pose, camera "
                "angle, and environment."
            )
            continue
        character = _character_description(name, desc)
        if character:
            parts.append(character)

    parts.append((shot.get("prompt") or "").strip())
    if with_audio:
        cue = _dialogue_visual_cue(shot, project)
        if cue:
            parts.append(cue)

    if with_audio:
        if project.get("soundscapeInShots", True) and not has_shot_refs:
            parts.append((project.get("soundscape") or "").strip())
        parts.append((shot.get("soundNote") or "").strip())
        if (shot.get("dialogue") or "").strip():
            parts.append(
                "Generated audio contains ambient sound only: no spoken words, "
                "no voice, and no intelligible dialogue; the voice line is "
                "dubbed separately."
            )
    return " ".join(_sentence(p) for p in parts if p)


def _ref2va_references(
    shot: dict,
    project: dict,
    paths: ShotPaths,
    include_audio_refs: bool = True,
) -> list[str]:
    """Reference order for Ref2VA, strongest shot-local signals first.

    The shot's own image sets clip-specific location/framing first. Character
    portraits preserve identity after that, and project style refs are useful
    background only when there is no local shot reference.
    """
    images: list[str] = []
    sounds: list[str] = []

    def add_image(ref: Any) -> None:
        src = _ref_source(ref, paths)
        if src and src not in images and len(images) < 9:
            images.append(src)

    def add_sound(ref: Any) -> None:
        src = _ref_source(ref, paths)
        if src and src not in sounds and len(sounds) < 3:
            sounds.append(src)

    # A start reference cannot anchor a Ref2VA clip -- that partition packs
    # references instead of keyframes -- but it is the shot's local visual
    # reference, so it should outrank portraits and project-wide style
    # references.
    for ref in _shot_reference_images(shot):
        add_image(ref)

    for ch in _shot_characters(shot, project):
        add_image(ch.get("image"))

    if not _shot_reference_images(shot):
        for r in project.get("styleRefs") or []:
            add_image(r)

    if include_audio_refs:
        for ch in _shot_characters(shot, project):
            add_sound(ch.get("voice"))

    # "audio can never be the only kind": a voice clip with no picture
    # alongside it is not a request Ref2VA accepts, so drop the sound rather
    # than have the encoder refuse the whole thing.
    if sounds and not images:
        sounds = []

    return (images + sounds)[:12]


def _shot_reference_images(shot: dict) -> list[Any]:
    refs = []
    if shot.get("startRef"):
        refs.append(shot.get("startRef"))
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


def _dialogue_visual_cue(shot: dict, project: dict) -> str:
    line = (shot.get("dialogue") or "").strip()
    if not line:
        return ""
    speaker = _speaker_name(shot, project)
    return (
        f"{speaker} speaks the line with natural jaw and lip movement: "
        f"\"{line}\""
    )


def _speaker_name(shot: dict, project: dict) -> str:
    cast = _shot_characters(shot, project)
    speaker_id = shot.get("speakerId") or ""
    if speaker_id:
        for ch in project.get("characters") or []:
            if ch.get("id") == speaker_id and (ch.get("name") or "").strip():
                return ch["name"].strip()
    named = [(ch.get("name") or "").strip() for ch in cast]
    named = [name for name in named if name]
    if named:
        return named[0]
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
    return fixed + denoise


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
