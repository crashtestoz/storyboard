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

H3_RESOLUTIONS = ["960x544", "832x480", "1344x768"]
KREA_RESOLUTIONS = ["1024x1024", "960x544", "1344x768"]

# stdout patterns that mean "this run failed even though it will exit 0"
SILENT_FAILURE_PATTERNS = [
    (r"video decode failed", "VAE decode refused the request and skipped"),
    (r"reference rows are not \[", "reference rows were emitted in the wrong shape"),
    (r"audio decode failed", "audio VAE decode failed"),
    (r"the vision tower produced nothing", "reference encoding produced nothing"),
    (r"encode failed", "an encode step failed"),
    (r"could not be found|not found in the model registry", "a model was missing"),
    (r"refused", "a stage refused the request"),
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
                resolutions=H3_RESOLUTIONS,
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
                resolutions=H3_RESOLUTIONS,
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
                resolutions=KREA_RESOLUTIONS,
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
        prompt = _resolved_prompt(shot, project)
        steps = int(shot.get("steps") or cap.default_steps)
        seed = int(shot.get("seed") or 0)

        if cap.kind == "image":
            spec, outputs = self._still_spec(shot, paths, prompt, width, height, steps, seed)
            expected_seconds = 150.0
            frames_dir = None
            frames = 0
        else:
            frames = cap.frame_rule.snap(int(shot.get("frames") or cap.frame_rule.minimum))
            if model == "ref2va":
                spec, outputs = self._ref2va_spec(
                    shot, project, paths, prompt, width, height, frames, steps, seed
                )
            else:
                spec, outputs = self._fl2va_spec(
                    shot, paths, prompt, width, height, frames, steps, seed
                )
            expected_seconds = _estimate_seconds(width, height, frames, steps)
            frames_dir = paths.abs_frames

        spec_path = paths.abs_dir / "shot.vpipeline"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")

        return JobSpec(
            shot_id=shot["id"],
            expected_outputs=outputs,
            frames_dir=frames_dir,
            expected_frames=frames if cap.kind == "video" else 0,
            expected_seconds=expected_seconds,
            payload={
                "spec_path": str(spec_path),
                "rel_spec": f"{paths.rel_dir}/shot.vpipeline",
                "cwd": str(self.workspace),
                "model": model,
                "frames": frames,
            },
            summary=(
                f"{cap.label.split('—')[0].strip()} · {width}x{height}"
                + (f" · {frames}f" if cap.kind == "video" else "")
                + f" · {steps} steps"
            ),
        )

    # -- templates ------------------------------------------------------ #

    def _fl2va_spec(self, shot, paths, prompt, w, h, frames, steps, seed):
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
        stages += _decode_and_save(paths, audio=True)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _ref2va_spec(self, shot, project, paths, prompt, w, h, frames, steps, seed):
        """Reference-conditioned video via video-ref-encoder's file list."""
        refs: list[str] = []
        for r in (project.get("styleRefs") or [])[:9]:
            src = _ref_source(r, paths)
            if src:
                refs.append(src)
        # a shot-level start reference is meaningless to Ref2VA as an anchor,
        # but is still useful as one more subject reference.
        own = _ref_source(shot.get("startRef"), paths)
        if own and own not in refs and len(refs) < 9:
            refs.append(own)

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
                    "reference_image_short_edge": 1024,
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
        stages += _decode_and_save(paths, audio=True)
        return {"id": f"shot-{shot['id']}", "stages": stages, "subpipelines": []}, \
               _outputs(paths)

    def _still_spec(self, shot, paths, prompt, w, h, steps, seed):
        """Krea-2 Turbo single image."""
        out_rel = f"{paths.rel_dir}/still.jpeg"
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
        joined = "\n".join(text for _lvl, text in result.log)

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

        benign = [
            text
            for lvl, text in result.log
            if lvl == "WARN" and any(re.search(p, text, re.I) for p in BENIGN_PATTERNS)
        ]
        if benign:
            yield Check(
                "warnings classified",
                None,
                f"{len(benign)} benign warning(s) ignored (e.g. wired pool)",
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


def _decode_and_save(paths: ShotPaths, audio: bool) -> list[dict]:
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
        {
            "id": "save-frames",
            "type": "save-image",
            "iports": [{"src": "vae-decode", "oport": 0}],
            "config": {
                "path": f"{paths.rel_frames}/frame-%04d.png",
                "format": "png",
            },
        },
    ]
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
                "output_url": f"{paths.rel_dir}/clip.mp4",
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

def _wh(res: str) -> tuple[int, int]:
    try:
        w, h = res.lower().split("x")
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return 960, 544


def _resolved_prompt(shot: dict, project: dict) -> str:
    """Two-tier prompt: project scene description, then this shot's direction.

    The scene description carries subject and style (true of every shot); the
    shot prompt carries action, camera and mood (true of this one only).
    """
    scene = (project.get("sceneDescription") or "").strip()
    own = (shot.get("prompt") or "").strip()
    return f"{scene} {own}".strip() if scene else own


def _ref_source(ref: Any, paths: ShotPaths) -> str | None:
    """Path a load-image stage can open, relative to the vpipe workspace."""
    if not ref:
        return None
    if isinstance(ref, str):
        return ref
    if ref.get("kind") == "chain":
        return ref.get("resolved") or None
    return ref.get("path") or ref.get("resolved") or None


def _estimate_seconds(w: int, h: int, frames: int, steps: int) -> float:
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
