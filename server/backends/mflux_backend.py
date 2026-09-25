"""Still images through mflux, alongside whatever renders the video.

mflux (https://github.com/filipstrand/mflux) is an MLX image generator
installed as a set of CLI tools (``uv tool install mflux``). It is used here
the way the speech engines are: linked, not installed. Each engine is one
entry in ``mflux-engines.json`` at the project root -- a CLI command and a
``--model`` value -- so pointing an engine at a different checkpoint (a
pre-quantized Hugging Face repo, a local folder) is an edit, not a code
change. mflux fetches and caches the weights itself on first use, so the
default entries name mflux's own model aliases rather than any vpipe path.

Only the Create Stills preview uses these engines. They are exposed as
``kind="image"`` capabilities so the orchestrator can pick one by id, and
:class:`WithMfluxStills` routes those ids here while every other model
still goes to the video backend.

Reference images reach an mflux engine as far as its generator allows
(``MfluxEngine.references``):

* ``edit`` — the ``*-edit`` generators (Qwen-Image Edit, FLUX.2 edit) take
  several reference images: the shot's Start Ref, its characters' portraits
  and its Reference images (up to ``maxReferences``, default 3).
* ``img2img`` — every other generator takes one init image, so only a
  hand-picked Start Ref seeds the still (the still *is* the opening frame).
  A portrait as an init image would copy the portrait's framing, so cast
  stay as their text descriptions.
* ``none`` — text prompt only.

Whatever the mode, the prompt is the same one vpipe builds, cast
descriptions included, so an engine with no image input still gets them in
words.
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
    Backend,
    Check,
    FrameRule,
    JobSpec,
    ModelCapability,
    ProgressEvent,
    RunResult,
    ShotPaths,
)
from ..local_config import ensure_local_copy
from .vpipe_backend import (
    ALL_RESOLUTIONS,
    SKETCH_STYLE_PREFIX,
    _align_up,
    _draft_geometry,
    _ref_source,
    _resolved_prompt,
    _shot_characters,
    _wh,
)

CONFIG_NAME = "mflux-engines.json"

DEFAULT_ENGINES: list[dict[str, Any]] = [
    {
        "id": "mflux-z-image-turbo",
        "label": "Z-Image Turbo via mflux",
        "command": "mflux-generate-z-image-turbo",
        "model": "z-image-turbo",
        "quantize": 8,
        "steps": 8,
    },
    {
        "id": "mflux-krea2",
        "label": "Krea-2 Turbo via mflux",
        "command": "mflux-generate-krea2",
        "model": "krea2",
        "quantize": 8,
        "steps": 8,
    },
]

MFLUX_SIZE_ALIGN = 16

# tqdm's denoise bar: " 50%|█████     | 4/8 [00:28<00:26,  6.60s/it]".
# Hugging Face download bars share the shape but count bytes ("B/s").
PROGRESS_RE = re.compile(r"(?P<pct>\d{1,3})%\|")
ETA_RE = re.compile(r"<(?:(?P<h>\d+):)?(?P<m>\d+):(?P<s>\d+)")
FAILURE_PATTERNS = (
    (r"Traceback \(most recent call last\)", "mflux raised a Python exception"),
    (r"GatedRepoError|401 Client Error|Access to model .* is restricted",
     "the model is gated on Hugging Face: accept its terms there and set HF_TOKEN"),
    (r"out of memory|OutOfMemory|\[METAL\].*(?:allocate|memory)",
     "ran out of memory"),
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def hub_cache_dir() -> Path:
    """Where huggingface_hub (and so mflux) caches repos."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _quantization(model_dir: Path) -> str:
    """mflux stamps ``quantization_level`` into the safetensors metadata of a
    model it saved quantized; a full-precision checkpoint has none."""
    for f in sorted(model_dir.rglob("*.safetensors"))[:3]:
        try:
            with open(f, "rb") as fh:
                n = int.from_bytes(fh.read(8), "little")
                if n > 100_000_000:
                    continue
                meta = json.loads(fh.read(n)).get("__metadata__") or {}
        except (OSError, ValueError):
            continue
        if meta.get("quantization_level"):
            return str(meta["quantization_level"])
    return ""


def _snapshot(repo_dir: Path) -> Path | None:
    """The newest snapshot of a cached repo, if it is completely downloaded."""
    blobs = repo_dir / "blobs"
    if blobs.is_dir() and any(blobs.glob("*.incomplete")):
        return None
    snaps = [p for p in (repo_dir / "snapshots").glob("*") if p.is_dir()]
    snaps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for snap in snaps:
        if any(snap.rglob("*.safetensors")):
            return snap
    return None


def _size_bytes(repo_dir: Path) -> int:
    blobs = repo_dir / "blobs"
    try:
        return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file())
    except OSError:
        return 0


def still_references(shot: dict, project: dict, paths: ShotPaths) -> list[tuple[str, str]]:
    """(absolute path, label) for each reference image a still can use: a
    hand-picked Start Ref, the shot's characters' portraits, then its own
    Reference images — characters ahead of scenery, since an edit model
    takes only a few images and who is in the frame matters most. An auto-chained Start
    Ref (the previous shot's last frame) is left out, as the vpipe still
    leaves it out: nobody chose it for this shot's subject. Files that are
    missing are skipped, and each image is listed once."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(ref: Any, label: str) -> None:
        path = _ref_source(ref, paths)
        if path and path not in seen and Path(path).is_file():
            seen.add(path)
            found.append((path, label))

    start = shot.get("startRef")
    if start and not (isinstance(start, dict) and start.get("kind") == "chain"):
        add(start, "Start frame (opening composition)")
    for ch in _shot_characters(shot, project):
        if ch.get("image"):
            add(ch["image"], f"{ch.get('name') or 'character'} (character identity)")
    for ref in shot.get("referenceImages") or []:
        add(ref, "shot reference (environment and composition)")
    return found


class MfluxEngine:
    def __init__(self, entry: dict[str, Any]):
        self.id = str(entry["id"])
        self.label = str(entry.get("label") or self.id)
        self.command = str(entry.get("command") or "mflux-generate")
        self.model = str(entry.get("model") or "")
        self.base_model = str(entry.get("baseModel") or "")
        self.quantize = entry.get("quantize")
        self.steps = int(entry.get("steps") or 8)
        self.extra_args = [str(a) for a in entry.get("extraArgs") or []]
        # Cached Hugging Face repos whose name contains any of these (letters
        # and digits only, lowercased) are offered as this engine's model.
        self.match = [_norm(m) for m in entry.get("match") or []] or [
            _norm(self.base_model or self.model)
        ]
        # How reference images reach this generator (see the module
        # docstring). "auto" goes by the command: the *-edit generators take
        # --image-paths, the rest take a single --image init image.
        mode = str(entry.get("references") or "auto")
        if mode == "auto":
            mode = "edit" if self.command.rstrip("/").split("/")[-1].endswith("-edit") else "img2img"
        self.references = mode if mode in ("edit", "img2img", "none") else "none"
        self.image_strength = float(entry.get("imageStrength") or 0.4)
        self.max_references = int(entry.get("maxReferences") or 3)

    @property
    def family(self) -> str:
        """``--base-model`` for a checkpoint that is not mflux's own alias."""
        return self.base_model or ("" if "/" in self.model else self.model)

    def resolve_command(self) -> str | None:
        """The CLI's absolute path. ``uv tool install`` puts it in
        ~/.local/bin, which a server started outside a login shell may not
        have on PATH, so that is checked explicitly."""
        if os.path.isabs(self.command):
            return self.command if os.access(self.command, os.X_OK) else None
        found = shutil.which(self.command)
        if found:
            return found
        local = Path.home() / ".local" / "bin" / self.command
        return str(local) if os.access(local, os.X_OK) else None


def load_config(project_root: Path) -> list[dict[str, Any]]:
    """The raw entries of this machine's ``mflux-engines.json``, first creating
    it from the committed ``mflux-engines-sample.json`` if it is absent."""
    path = ensure_local_copy(project_root, CONFIG_NAME, {"engines": DEFAULT_ENGINES})
    entries: list[dict[str, Any]] = DEFAULT_ENGINES
    try:
        doc = json.loads(path.read_text())
        if isinstance(doc.get("engines"), list):
            entries = doc["engines"]
    except (OSError, json.JSONDecodeError):
        pass
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def load_engines(project_root: Path) -> list[MfluxEngine]:
    return [MfluxEngine(e) for e in load_config(project_root)]


class MfluxStills(Backend):
    id = "mflux"
    label = "mflux (still images)"

    def __init__(self, engines: list[MfluxEngine], settings_path: Path | None = None):
        self.engines = {e.id: e for e in engines}
        # Per-machine model choice ({"mfluxModels": {engine id: model}}),
        # kept with the other machine-local settings, not in a board.
        self.settings_path = settings_path
        self._proc: subprocess.Popen | None = None

    def handles(self, model_id: str | None) -> bool:
        return bool(model_id) and model_id in self.engines

    def reload(self, project_root: Path) -> None:
        """Re-read mflux-engines.json after Settings edits it."""
        self.engines = {e.id: e for e in load_engines(project_root)}

    # -- which checkpoint an engine runs ---------------------------------

    def cached_models(self, engine: MfluxEngine) -> list[dict[str, Any]]:
        """Completely downloaded Hugging Face repos this engine can run,
        pre-quantized ones first (smaller, and no quantize pass at load)."""
        found = []
        root = hub_cache_dir()
        try:
            dirs = [d for d in root.iterdir() if d.name.startswith("models--")]
        except OSError:
            return []
        for d in dirs:
            repo = d.name[len("models--"):].replace("--", "/", 1)
            if not any(m and m in _norm(repo) for m in engine.match):
                continue
            snap = _snapshot(d)
            if snap is None:
                continue
            found.append({"model": repo, "sizeBytes": _size_bytes(d),
                          "quantization": _quantization(snap)})
        found.sort(key=lambda c: (not c["quantization"], c["model"].lower()))
        return found

    def _selected(self) -> dict[str, str]:
        if not self.settings_path or not self.settings_path.exists():
            return {}
        try:
            doc = json.loads(self.settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        sel = doc.get("mfluxModels")
        return sel if isinstance(sel, dict) else {}

    def valid_choice(self, engine_id: str, model: str) -> bool:
        engine = self.engines.get(engine_id)
        if engine is None:
            return False
        if not model:
            return True
        if model in {c["model"] for c in self.cached_models(engine)}:
            return True
        return Path(model).expanduser().is_absolute() and Path(model).expanduser().is_dir()

    def resolve(self, engine: MfluxEngine) -> dict[str, Any]:
        """The chosen model if it is still usable, else a downloaded one,
        and only when nothing is downloaded the engine's default -- which
        mflux fetches itself."""
        cached = self.cached_models(engine)
        by_name = {c["model"]: c for c in cached}
        chosen = str(self._selected().get(engine.id) or "")
        local = Path(chosen).expanduser() if chosen else None
        if chosen and (chosen in by_name or (local and local.is_absolute() and local.is_dir())):
            model, source = chosen, "selected"
            quant = by_name[chosen]["quantization"] if chosen in by_name else _quantization(local)
        elif cached:
            model, source, quant = cached[0]["model"], "cached", cached[0]["quantization"]
        else:
            model, source, quant = engine.model, "download", ""
        return {"model": model, "source": source, "quantization": quant,
                "chosen": chosen}

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for e in self.engines.values():
            r = self.resolve(e)
            out.append({
                "id": e.id,
                "references": e.references,
                "defaultModel": e.model,
                "chosen": r["chosen"],
                "resolved": {k: r[k] for k in ("model", "source", "quantization")},
                "cached": self.cached_models(e),
            })
        return out

    def capabilities(self) -> list[ModelCapability]:
        caps = []
        for e in self.engines.values():
            cmd = e.resolve_command()
            caps.append(ModelCapability(
                id=e.id,
                label=f"{e.label} — still image (fast prompt preview)",
                kind="image",
                engine="mflux",
                frame_rule=FrameRule(kind="any", minimum=1),
                resolutions=ALL_RESOLUTIONS,
                default_steps=e.steps,
                size_align=MFLUX_SIZE_ALIGN,
                available=cmd is not None,
                unavailable_reason="" if cmd else (
                    f"{e.command} not found — install mflux with "
                    "`uv tool install mflux`"
                ),
            ))
        return caps

    def prepare(self, shot: dict, project: dict, paths: ShotPaths) -> JobSpec:
        paths.ensure()
        engine = self.engines[shot["model"]]
        cmd = engine.resolve_command()
        if cmd is None:
            raise ValueError(f"{engine.command} not found — install mflux with "
                             "`uv tool install mflux`")

        defaults = project.get("defaults") or {}
        res = defaults.get("resolution") or shot.get("resolution") or ALL_RESOLUTIONS[0]
        width, height = _wh(res)
        steps = int(shot.get("steps") or engine.steps)
        draft = bool(defaults.get("draft"))
        if draft:
            width, height, draft_steps = _draft_geometry(width, height, steps, MFLUX_SIZE_ALIGN)
            if not shot.get("_fixedSteps"):
                steps = draft_steps
        width = _align_up(width, MFLUX_SIZE_ALIGN)
        height = _align_up(height, MFLUX_SIZE_ALIGN)

        # "krea2-still" so the prompt is built exactly as for the vpipe still.
        prompt = _resolved_prompt(shot, project, with_audio=False, model="krea2-still")
        if draft and defaults.get("sketch"):
            prompt = f"{SKETCH_STYLE_PREFIX} {prompt}"

        out = paths.abs_dir / "still.jpeg"
        picked = self.resolve(engine)
        model = picked["model"]
        argv = [cmd, "--model", model]
        if model != engine.model and engine.family:
            argv += ["--base-model", engine.family]
        elif engine.base_model:
            argv += ["--base-model", engine.base_model]
        if engine.quantize and not picked["quantization"]:
            argv += ["--quantize", str(engine.quantize)]
        refs = still_references(shot, project, paths)
        used: list[tuple[str, str]] = []
        if engine.references == "edit" and refs:
            used = refs[: engine.max_references]
            argv += ["--image-paths", *[path for path, _ in used]]
            # Say which picture is which, as the video render does.
            listing = "; ".join(f"image {i + 1}: {label}" for i, (_, label) in enumerate(used))
            prompt = f"Reference images — {listing}. {prompt}"
        elif engine.references == "img2img":
            opening = next(((path, label) for path, label in refs if label.startswith("Start frame")), None)
            if opening:
                used = [opening]
                argv += ["--image", opening[0], str(engine.image_strength)]
        argv += ["--prompt", prompt, "--width", str(width), "--height", str(height),
                 "--steps", str(steps)]
        seed = int(shot.get("seed") or 0)
        if seed:
            argv += ["--seed", str(seed)]
        argv += engine.extra_args

        return JobSpec(
            shot_id=shot["id"],
            expected_outputs=[out],
            payload={"engine": "mflux", "argv": argv, "cwd": str(paths.abs_dir),
                     "model": engine.id, "output": str(out)},
            summary=f"{engine.label} · {model} · {width}x{height} · {steps} steps"
                    + (" · DRAFT" if draft else "")
                    + (f" · references: {', '.join(label for _, label in used)}" if used
                       else " · text prompt only"),
        )

    def run(
        self,
        spec: JobSpec,
        on_event: Callable[[ProgressEvent], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        result = RunResult(started_at=time.time())
        spec.started_at = result.started_at
        # mflux never overwrites: given an existing path it writes name_1.ext
        # beside it. So it renders to a fresh name, which replaces the real
        # output only on success -- a failed or cancelled run keeps the last
        # good still.
        out = Path(spec.payload["output"])
        tmp = out.with_name(f"{out.stem}.{int(result.started_at * 1000)}{out.suffix}")
        # Text mode's universal newlines split tqdm's \r redraws into lines.
        proc = subprocess.Popen(
            spec.payload["argv"] + ["--output", str(tmp)],
            cwd=spec.payload["cwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._proc = proc
        last_pct: float | None = None
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                pm = PROGRESS_RE.search(line)
                is_download = pm is not None and "B/s" in line
                if pm and not is_download:
                    pct = float(pm.group("pct"))
                    if pct == last_pct:
                        continue
                    last_pct = pct
                    em = ETA_RE.search(line)
                    eta = None
                    if em:
                        eta = (int(em.group("h") or 0) * 3600
                               + int(em.group("m")) * 60 + int(em.group("s")))
                    result.log.append(("INFO", line))
                    on_event(ProgressEvent(phase="denoise", percent=pct, detail=line,
                                           log_line=line, eta_seconds=eta))
                else:
                    level = "ERROR" if "Error" in line or "Traceback" in line else "INFO"
                    result.log.append((level, line))
                    on_event(ProgressEvent(
                        phase="downloading model" if is_download else "",
                        percent=last_pct or 0.0, log_line=line, log_level=level))
                if should_cancel():
                    self._terminate(proc)
                    result.cancelled = True
                    break
            proc.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            result.error = f"{type(exc).__name__}: {exc}"
            self._terminate(proc)
        finally:
            result.exit_code = proc.returncode
            result.ended_at = time.time()
            self._proc = None
        if result.exit_code == 0 and not result.cancelled and not result.error:
            if tmp.exists():
                os.replace(tmp, out)
            else:
                result.error = f"mflux exited cleanly but did not write {tmp.name}"
        else:
            tmp.unlink(missing_ok=True)
        return result

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

    def cancel(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def extra_checks(self, spec: JobSpec, result: RunResult) -> Iterable[Check]:
        joined = "\n".join(text for _lvl, text in result.log)
        hits = [meaning for pat, meaning in FAILURE_PATTERNS
                if re.search(pat, joined, re.IGNORECASE)]
        yield Check("log scan", not hits,
                    "; ".join(hits) if hits else "no failure patterns")


class WithMfluxStills(Backend):
    """The video backend, plus mflux still engines routed by model id."""

    def __init__(self, inner: Backend, mflux: MfluxStills):
        self.inner = inner
        self.mflux = mflux
        self.id = inner.id
        self.label = inner.label

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def health(self) -> tuple[bool, str]:
        return self.inner.health()

    def capabilities(self) -> list[ModelCapability]:
        return self.inner.capabilities() + self.mflux.capabilities()

    def prepare(self, shot: dict, project: dict, paths: ShotPaths) -> JobSpec:
        if self.mflux.handles(shot.get("model")):
            return self.mflux.prepare(shot, project, paths)
        return self.inner.prepare(shot, project, paths)

    def run(self, spec, on_event, should_cancel) -> RunResult:
        if spec.payload.get("engine") == "mflux":
            return self.mflux.run(spec, on_event, should_cancel)
        return self.inner.run(spec, on_event, should_cancel)

    def cancel(self) -> None:
        self.mflux.cancel()
        self.inner.cancel()

    def validate(self, spec, result):
        if spec.payload.get("engine") == "mflux":
            return self.mflux.validate(spec, result)
        return self.inner.validate(spec, result)
