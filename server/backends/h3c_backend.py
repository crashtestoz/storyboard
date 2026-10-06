"""h3.c backend — MiniMax H3 through antirez's native Metal engine.

https://github.com/antirez/h3.c runs H3 as one plain C/Metal program: ``h3 -d
MODEL_DIR -p PROMPT -o clip.mp4`` with flags for anchors and references. It
renders the same two H3 modes the vpipe backend drives, FL2VA and Ref2VA, from
the original BF16 checkpoint, and on a large-memory Mac keeps the whole DiT
resident instead of vpipe's 8-bit conversion.

Everything about *what* a shot asks for is shared with :class:`VpipeBackend`
by subclassing it: the prompt, reference order and ``<Picture N>`` bindings,
voice-clip selection, dialogue checks, draft/sketch geometry and anchors all
come from the same code, so a board renders the same way on either engine.
Only the two H3 templates are overridden, to describe an h3 command line
instead of a vpipeline graph, and ``run()`` drives that command.

Differences from vpipe that are encoded here rather than left to surprise:

*   **Model layout.** h3.c reads the Hugging Face ``MiniMaxAI/MiniMax-H3``
    snapshot as published (``FL2VA/…`` and ``Ref2VA/…`` side by side, BF16).
    vpipe's ``local/MiniMax-H3-*-8bit`` conversions are a different layout and
    do not load.
*   **Mode selection.** h3.c picks Ref2VA whenever a reference is passed and
    FL2VA otherwise; there is no flag. A Ref2VA shot with no references
    therefore renders on the FL2VA checkpoint as plain text-to-video, which
    is what an empty reference list asks for anyway.
*   **Frames.** Same ``17n + 5`` rule, but h3.c decodes from 22 frames up (one
    trained decoder chunk) where vpipe needed 39, and refuses above 362.
*   **Canvas.** Width x height must not exceed 768 x 1344 pixels, so the
    21:9 base size (1792x768) is not offered.
*   **Shaders.** ``h3_shaders.metal`` is compiled at run time from the
    working directory, so the process is launched from the binary's folder.
*   **Progress.** Written to stderr as ``\\r<phase> N/M``. Text-mode pipes
    turn each ``\\r`` into a line break, so every update reads as its own line.
*   **Exit status is meaningful.** h3.c prints ``h3: <reason>`` and exits 1
    on failure, unlike vpipe's warn-and-continue, so no silent-failure
    pattern list is needed.
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

from .base import (
    Check,
    FrameRule,
    JobSpec,
    ModelCapability,
    ProgressEvent,
    RunResult,
    ShotPaths,
)
from .vpipe_backend import (
    ALL_RESOLUTIONS,
    H3_SIZE_ALIGN,
    VpipeBackend,
    _denoise_eta_seconds,
    _effective_video_model,
    _overall,
    _ref2va_references,
    _ref_source,
    _sketchify_ref,
    _stretch_clip,
)

H3C_FRAME_RULE = FrameRule(
    kind="affine",
    step=17,
    offset=5,
    minimum=22,
    note="MiniMax H3 packs video 17 frames at a time keeping 5 latents; h3.c "
         "decodes from one trained 22-frame chunk (0.9s @ 24fps) up to 362 "
         "frames (15s).",
)
H3C_MAX_FRAMES = 362
H3C_MAX_PIXELS = 768 * 1344

H3C_RESOLUTIONS = [
    r for r in ALL_RESOLUTIONS
    if int(r.split("x")[0]) * int(r.split("x")[1]) <= H3C_MAX_PIXELS
]
# The canvases h3.c's own README reports validating, intersected with the
# ones this app offers.
H3C_TESTED = [r for r in ("1344x768", "768x1344", "1024x768", "768x1024", "768x768")
              if r in H3C_RESOLUTIONS]

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}

# Performance knobs, from "h3cOptions" in server-config.json. The defaults
# are h3.c's own close-quality defaults: every layer, every denoiser
# evaluation, full residency. A 128 GB machine has no reason to trade any of
# that away; see h3.c's README for what each one costs.
DEFAULT_OPTIONS: dict[str, Any] = {
    "layers": 50,            # 50 exact, 45 fast, 40 aggressive
    "reuse": 1,              # 1 close, 2 fast, 3 aggressive
    "coreReuse": 0,          # 0 = off; 1-6, exclusive with reuse > 1
    "tokenReduction": False,
    "ssdStreaming": False,   # ~2 GiB DiT residency instead of ~36.5, slower
    "int8RowFc2": False,     # M5 only
    "refImageSize": "match",  # or "max"
    "defaultSteps": 20,
    # A Turbo LoRA baked into a copy-on-write clone of the model (see
    # h3-turbo/fold_ref2va_turbo.py): {"enabled", "modelDir", "steps"}.
    # Draft Ref2VA renders only -- the clone's FL2VA is the stock model, and
    # finals always use the full model.
    "turbo": {},
}

# h3.c phase -> the three phases the shared progress blend understands.
PHASE_MAP = {
    "tokenizer": "encoding references",
    "text encoder": "encoding references",
    "Qwen vision": "encoding references",
    "video VAE encoder": "encoding references",
    "audio VAE encoder": "encoding references",
    "refine text": "encoding references",
    "load transformer core": "encoding references",
    "denoise": "denoise",
    "denoise enqueue": "denoise",
    "preview VAE load": "vae decode",
    "audio VAE": "vae decode",
    "video VAE load": "vae decode",
    "FFmpeg": "vae decode",
}
# Sub-phases of decode run in this order; each gets an equal slice so the bar
# keeps moving instead of sitting at 100% of the first one.
DECODE_ORDER = ["audio VAE", "video VAE load", "FFmpeg"]

PROGRESS_RE = re.compile(r"^(?P<phase>[A-Za-z][A-Za-z0-9 ]*?)\s+(?P<done>\d+)/(?P<total>\d+)\s*$")


class H3cBackend(VpipeBackend):
    id = "h3c"
    label = "h3.c (MiniMax H3, native Metal)"
    SPEC_FILE = "shot.h3c.json"

    def __init__(self, binary: Path, model_dir: Path, workspace: Path,
                 options: dict[str, Any] | None = None):
        super().__init__(binary=binary, workspace=workspace)
        self.model_dir = Path(model_dir)
        self.options = {**DEFAULT_OPTIONS, **(options or {})}

    # ------------------------------------------------------------------ #
    # description
    # ------------------------------------------------------------------ #

    def health(self) -> tuple[bool, str]:
        if not self.binary.is_file():
            return False, (f"h3 binary not found at {self.binary} — build it "
                           "with `make -j8` in the h3.c checkout")
        if not os.access(self.binary, os.X_OK):
            return False, f"h3 binary is not executable: {self.binary}"
        if not (self.binary.parent / "h3_shaders.metal").is_file():
            return False, (f"h3_shaders.metal not found beside {self.binary} — "
                           "h3 compiles its shaders from its own folder")
        if not self.model_dir.is_dir():
            return False, f"MiniMax-H3 model directory not found at {self.model_dir}"
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            return False, "ffmpeg and ffprobe must be on PATH for h3 to encode video"
        return True, ""

    def _variant_present(self, variant: str, marker: str) -> bool:
        """Is this checkpoint on disk *and* finished downloading?

        ``hf download --local-dir`` keeps in-flight files as ``*.incomplete``
        under ``.cache/huggingface/download``; any of those for this variant
        means a render would fail minutes in.
        """
        if not (self.model_dir / variant / marker).is_file():
            return False
        pending = self.model_dir / ".cache" / "huggingface" / "download" / variant
        try:
            return not (pending.exists() and any(pending.rglob("*.incomplete")))
        except OSError:
            return False

    def capabilities(self) -> list[ModelCapability]:
        fl2va = self._variant_present("FL2VA", "transformer/config.json")
        ref2va = self._variant_present("Ref2VA", "transformer/model.safetensors.index.json")
        common = dict(
            kind="video",
            supports_audio=True,
            frame_rule=H3C_FRAME_RULE,
            resolutions=H3C_RESOLUTIONS,
            tested_resolutions=H3C_TESTED,
            size_align=H3_SIZE_ALIGN,
            default_steps=int(self.options["defaultSteps"]),
            engine="h3c",
        )
        return [
            ModelCapability(
                id="fl2va",
                label="MiniMax H3 · FL2VA (h3.c) — text / frame anchors → video + audio",
                supports_start_anchor=True,
                supports_end_anchor=True,
                available=fl2va,
                unavailable_reason="" if fl2va
                else f"FL2VA checkpoint not found under {self.model_dir}",
                **common,
            ),
            ModelCapability(
                id="ref2va",
                label="MiniMax H3 · Ref2VA (h3.c) — reference images → video + audio"
                + (f" · Turbo LoRA for drafts ({self._turbo()['steps']} steps)" if self._turbo() else ""),
                supports_style_refs=True,
                max_style_refs=9,
                # h3.c needs FL2VA's text encoder and VAEs even for Ref2VA,
                # and falls back to FL2VA for a shot with no references.
                available=ref2va and fl2va,
                unavailable_reason="" if (ref2va and fl2va)
                else f"Ref2VA checkpoint not found under {self.model_dir}"
                if fl2va else f"FL2VA checkpoint not found under {self.model_dir}",
                **common,
            ),
        ]

    # ------------------------------------------------------------------ #
    # prepare
    # ------------------------------------------------------------------ #

    def prepare(self, shot: dict, project: dict, paths: ShotPaths) -> JobSpec:
        model, _ = _effective_video_model(shot, project)
        if model not in ("fl2va", "ref2va"):
            raise ValueError(
                f"{model} is only available on the vpipe backend; h3.c renders "
                "MiniMax H3 only. Set the video engine to Automatic (MiniMax "
                "H3) in Settings, or use an mflux engine for stills."
            )
        spec = super().prepare(shot, project, paths)
        # Kept apart from vpipe's measurements of the same model id: the
        # runtime-plausible check compares against these, and the two engines
        # run at very different speeds.
        spec.payload["timingModel"] = f"h3c:{spec.payload['model']}"
        spec.payload["engine"] = "h3c"
        # argv() swaps in the baked Turbo model and its step count for a job
        # with references; say so here too, so the timing history, the
        # summary line and the shot's render record report what actually ran.
        # Turbo is a DRAFT tool: fast, but flatter and less realistic than the
        # full model, so a final render never uses it.
        spec_path = Path(spec.payload["spec_path"])
        job = json.loads(spec_path.read_text())
        turbo = self._turbo()
        use = bool(turbo and job.get("references") and spec.payload.get("draft"))
        job["turbo"] = use
        spec_path.write_text(json.dumps(job, indent=2) + "\n")
        if use:
            old = spec.payload["steps"]
            spec.payload["steps"] = turbo["steps"]
            spec.payload["turbo"] = True
            spec.payload["timingModel"] = f"h3c-turbo:{spec.payload['model']}"
            spec.summary = spec.summary.replace(
                f" · {old} steps", f" · {turbo['steps']} steps · Turbo LoRA")
        return spec

    def _check_geometry(self, w: int, h: int, frames: int) -> None:
        if w * h > H3C_MAX_PIXELS:
            raise ValueError(
                f"{w}x{h} is larger than h3.c's 768x1344-pixel limit; choose a "
                "smaller resolution in Settings (21:9 at 1344x576 fits)."
            )
        if frames > H3C_MAX_FRAMES:
            raise ValueError(
                f"{frames} frames is longer than h3.c's 362-frame (15s) limit; "
                "shorten this shot."
            )

    def _fl2va_spec(self, shot, paths, prompt, w, h, frames, steps, seed,
                    draft=False, save_frames=True, with_audio=True, sketch=False):
        self._check_geometry(w, h, frames)
        anchors: dict[str, str] = {}
        for key, flag in (("startRef", "first"), ("endRef", "last")):
            src = _ref_source(shot.get(key), paths)
            if not src:
                continue
            if sketch:
                src = _sketchify_ref(src, paths.abs_dir / "sketch-refs")
            anchors[flag] = src
        job = self._job(shot, paths, prompt, w, h, frames, steps, seed,
                        save_frames, with_audio)
        job["firstFrame"] = anchors.get("first")
        job["lastFrame"] = anchors.get("last")
        return job, [paths.abs_dir / "clip.mp4"]

    def _ref2va_spec(self, shot, project, paths, prompt, w, h, frames, steps,
                     seed, draft=False, save_frames=True, with_audio=True,
                     sketch=False):
        self._check_geometry(w, h, frames)
        start = shot.get("startRef")
        if isinstance(start, dict) and start.get("kind") == "chain" and not _ref_source(start, paths):
            raise ValueError("Start frame chains from a previous scene that has not been resolved; render its source first")
        refs = _ref2va_references(
            shot, project, paths, include_audio_refs=with_audio, sketch=sketch
        )
        job = self._job(shot, paths, prompt, w, h, frames, steps, seed,
                        save_frames, with_audio)
        job["references"] = [_classify_ref(r) for r in refs]
        return job, [paths.abs_dir / "clip.mp4"]

    def _job(self, shot, paths, prompt, w, h, frames, steps, seed,
             save_frames, with_audio) -> dict:
        return {
            "id": f"shot-{shot['id']}",
            "engine": "h3c",
            "prompt": prompt,
            "width": w,
            "height": h,
            "frames": frames,
            "steps": steps,
            "seed": seed,
            "output": str(paths.abs_dir / "clip.mp4"),
            "framesDir": str(paths.abs_frames) if save_frames else None,
            "withAudio": with_audio,
            "references": [],
            "firstFrame": None,
            "lastFrame": None,
        }

    def _turbo(self) -> dict[str, Any] | None:
        """The baked Turbo model to use for Ref2VA, or None.

        On whenever the folded checkpoint is configured and on disk (an
        explicit "enabled": false in server-config.json still turns it off);
        a deleted clone falls back to the stock model's small drafts.
        """
        t = self.options.get("turbo")
        if not isinstance(t, dict) or not t.get("enabled", True) or not t.get("modelDir"):
            return None
        root = Path(str(t["modelDir"])).expanduser()
        if not (root / "Ref2VA" / "transformer").is_dir():
            return None
        return {"modelDir": root, "steps": int(t.get("steps", 4))}

    def draft_turbo(self) -> int:
        t = self._turbo()
        return int(t["steps"]) if t else 0

    def turbo_draft_full_size(self, model: str) -> bool:
        # Only Ref2VA has a baked Turbo model; an FL2VA draft still shrinks.
        return model == "ref2va" and bool(self._turbo())

    def argv(self, job: dict) -> list[str]:
        """The h3 command line for one prepared job."""
        o = self.options
        steps = int(job["steps"])
        model_dir = self.model_dir
        turbo = self._turbo()
        # Decided in prepare(): a draft with references (h3.c renders Ref2VA
        # exactly then; FL2VA is unchanged in the Turbo clone).
        if turbo and job.get("turbo"):
            model_dir = turbo["modelDir"]
            steps = turbo["steps"]
        argv = [
            str(self.binary),
            "-d", str(model_dir),
            "-p", job["prompt"],
            "-o", job["output"],
            "--width", str(job["width"]),
            "--height", str(job["height"]),
            "--frames", str(job["frames"]),
            "--steps", str(steps),
            "--seed", str(int(job["seed"])),
            "--layers", str(int(o["layers"])),
        ]
        core_reuse = int(o.get("coreReuse") or 0)
        if core_reuse > 0:
            argv += ["--core-reuse", str(core_reuse)]
        else:
            # h3.c warns that reuse at 2-7 steps leaves too few fresh
            # evaluations, so a shot at 7 or fewer always gets reuse 1.
            reuse = 1 if steps <= 7 else int(o["reuse"])
            argv += ["--reuse", str(reuse)]
        if o.get("tokenReduction"):
            argv.append("--token-reduction")
        if o.get("ssdStreaming"):
            argv.append("--ssd-streaming")
        elif o.get("int8RowFc2"):
            argv.append("--use-int8-row-fc2")
        if job.get("firstFrame"):
            argv += ["--first-frame", job["firstFrame"]]
        if job.get("lastFrame"):
            argv += ["--last-frame", job["lastFrame"]]
        if job.get("references"):
            argv += ["--ref-image-size", str(o.get("refImageSize") or "match")]
            for ref in job["references"]:
                argv += [ref["flag"], ref["path"]]
        if job.get("framesDir"):
            argv += ["--frames-dir", _ppm_dir(Path(job["framesDir"]))]
        return argv

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def run(
        self,
        spec: JobSpec,
        on_event: Callable[[ProgressEvent], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        job = json.loads(Path(spec.payload["spec_path"]).read_text())
        argv = self.argv(job)
        result = RunResult(started_at=time.time())
        if job.get("framesDir"):
            ppm = Path(_ppm_dir(Path(job["framesDir"])))
            shutil.rmtree(ppm, ignore_errors=True)
            ppm.mkdir(parents=True, exist_ok=True)

        def log(level: str, text: str) -> None:
            result.log.append((level, text))
            result.log_times.append(time.strftime("%H:%M:%S"))

        log("INFO", "$ " + " ".join(_quote(a) for a in argv[:1] + argv[1:3])
            + " -p <prompt> " + " ".join(_quote(a) for a in argv[5:]))

        proc = subprocess.Popen(
            argv,
            cwd=str(self.binary.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._proc = proc
        phase_pct: dict[str, float] = {}
        last_logged: dict[str, int] = {}
        denoise_started_at: float | None = None

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n").strip()
                if not line:
                    continue
                pm = PROGRESS_RE.match(line)
                if pm and pm.group("phase") in PHASE_MAP:
                    phase = pm.group("phase")
                    done, total = int(pm.group("done")), int(pm.group("total"))
                    pct = 100.0 * done / total if total else 0.0
                    shared = PHASE_MAP[phase]
                    if shared == "vae decode" and phase in DECODE_ORDER:
                        i = DECODE_ORDER.index(phase)
                        pct = (i + pct / 100.0) / len(DECODE_ORDER) * 100.0
                    phase_pct[shared] = max(phase_pct.get(shared, 0.0), pct)
                    eta = None
                    # The clock starts at "denoise 0/N", when the first step
                    # begins. Starting it at 1/N instead drops a whole step
                    # from the pace, which on an 8-step render made the
                    # estimate about half the real remaining time.
                    if shared == "denoise" and denoise_started_at is None:
                        denoise_started_at = time.time()
                    if shared == "denoise" and done > 0:
                        eta = _denoise_eta_seconds(
                            denoise_started_at, phase_pct["denoise"], time.time()
                        )
                    # Every block/step is an update; only log each tenth and
                    # the finish, like vpipe's [PROGRESS] cadence.
                    decile = int(100 * done / total) // 10 if total else 10
                    text = f"{phase} {done}/{total}"
                    logged = None
                    if decile != last_logged.get(phase) or done == total:
                        last_logged[phase] = decile
                        log("PROGRESS", text)
                        logged = text
                    on_event(ProgressEvent(
                        phase=shared,
                        percent=_overall(phase_pct),
                        detail=text,
                        log_line=logged,
                        log_level="PROGRESS",
                        eta_seconds=eta,
                        phase_percent=phase_pct[shared],
                    ))
                else:
                    level = _level(line)
                    log(level, line)
                    on_event(ProgressEvent(percent=_overall(phase_pct),
                                           log_line=line, log_level=level))

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

        ok = not result.cancelled and not result.error and result.exit_code == 0
        if job.get("framesDir"):
            if ok:
                _ppm_to_png(Path(_ppm_dir(Path(job["framesDir"]))),
                            Path(job["framesDir"]), result.log)
            shutil.rmtree(_ppm_dir(Path(job["framesDir"])), ignore_errors=True)

        # h3.c always generates audio. Sketch is silent by design: the
        # stretch below drops the track, and a clip too short to need
        # stretching drops it here instead.
        factor = float(spec.payload.get("sketchStretchFactor") or 1.0)
        if ok and spec.payload.get("sketch") and factor > 1.01 and spec.expected_outputs:
            _stretch_clip(spec.expected_outputs[0], factor, result.log)
        elif ok and not job.get("withAudio") and spec.expected_outputs:
            _strip_audio(spec.expected_outputs[0], result.log)
        return result

    def _terminate(self, proc: subprocess.Popen, spec: JobSpec) -> None:
        """SIGINT, then SIGTERM, then kill. The CLI installs no signal
        handlers, so the first one normally ends it at once."""
        if proc.poll() is not None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def extra_checks(self, spec: JobSpec, result: RunResult) -> Iterable[Check]:
        errors = [text for lvl, text in result.log if lvl == "ERROR"]
        yield Check(
            "h3 errors",
            not errors,
            "; ".join(errors[-3:]) if errors else "none reported",
        )
        saw_denoise = any(lvl == "PROGRESS" and text.startswith("denoise")
                          for lvl, text in result.log)
        yield Check(
            "denoise ran",
            saw_denoise,
            "denoise progress reported" if saw_denoise
            else "no denoise progress ever reported",
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _classify_ref(path: str) -> dict[str, str]:
    """The h3 flag that matches a reference's media type.

    Order is preserved by the caller: h3.c numbers images ``<Picture N>`` in
    the order given, which is what the prompt's bindings were written for.
    """
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return {"flag": "--ref-image", "path": path}
    if ext in VIDEO_EXTS:
        # A cast voice clip may be a video; only its sound is wanted, but a
        # video reference with audio is the closest h3.c offers.
        return {"flag": "--ref-video", "path": path}
    return {"flag": "--ref-audio", "path": path}


def _ppm_dir(frames_dir: Path) -> str:
    return str(frames_dir.parent / ".h3-frames-ppm")


def _ppm_to_png(src: Path, dest: Path, log: list[tuple[str, str]]) -> None:
    """h3.c writes frames as PPM; the rest of the app reads frame-%04d.png
    (thumbnails, chained Start frames). Both number from 0000."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not any(src.glob("frame-*.ppm")):
        log.append(("WARN", "no PPM frames to convert — frames/ left empty"))
        return
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("frame-*.png"):
        old.unlink(missing_ok=True)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-start_number", "0",
           "-i", str(src / "frame-%04d.ppm"),
           "-start_number", "0", str(dest / "frame-%04d.png")]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
        log.append(("INFO", f"converted {len(list(dest.glob('frame-*.png')))} frames to PNG"))
    except Exception as exc:  # noqa: BLE001 - validation reports the gap
        log.append(("WARN", f"could not convert frames to PNG: {exc}"))


def _strip_audio(path: Path, log: list[tuple[str, str]]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.exists():
        return
    tmp = path.with_suffix(".silent.mp4")
    try:
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
                        "-c:v", "copy", "-an", str(tmp)],
                       check=True, capture_output=True, text=True, timeout=120)
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - a clip with sound is still usable
        log.append(("WARN", f"could not drop audio track: {exc}"))
        tmp.unlink(missing_ok=True)


def _level(line: str) -> str:
    low = line.lower()
    if low.startswith("h3: warning"):
        return "WARN"
    if low.startswith("h3: wrote") or low.startswith("h3: graphical"):
        return "INFO"
    if low.startswith("h3: "):
        # Every other "h3: ..." line the CLI prints is a reason it stopped.
        return "ERROR"
    return "INFO"


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", arg) else json.dumps(arg)
