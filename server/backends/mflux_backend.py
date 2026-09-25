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

Text-to-image, plus img2img for Create Stills' chained mid/end phases.
The identity-reference path (a Start Ref or cast portrait steering the
still) is vpipe Krea-2 only; here those references reach the model as
their text description, through the same prompt vpipe builds.
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
from .vpipe_backend import (
    ALL_RESOLUTIONS,
    SKETCH_STYLE_PREFIX,
    _align_up,
    _draft_geometry,
    _ref_source,
    _resolved_prompt,
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


def load_engines(project_root: Path) -> list[MfluxEngine]:
    """Read ``mflux-engines.json``, writing the defaults if it is absent."""
    path = Path(project_root) / CONFIG_NAME
    entries: list[dict[str, Any]] = DEFAULT_ENGINES
    if path.exists():
        try:
            doc = json.loads(path.read_text())
            if isinstance(doc.get("engines"), list):
                entries = doc["engines"]
        except (OSError, json.JSONDecodeError):
            pass
    else:
        try:
            path.write_text(json.dumps({"engines": DEFAULT_ENGINES}, indent=2) + "\n")
        except OSError:
            pass
    return [MfluxEngine(e) for e in entries if isinstance(e, dict) and e.get("id")]


class MfluxStills(Backend):
    id = "mflux"
    label = "mflux (still images)"

    def __init__(self, engines: list[MfluxEngine]):
        self.engines = {e.id: e for e in engines}
        self._proc: subprocess.Popen | None = None

    def handles(self, model_id: str | None) -> bool:
        return bool(model_id) and model_id in self.engines

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
            width, height, steps = _draft_geometry(width, height, steps, MFLUX_SIZE_ALIGN)
        width = _align_up(width, MFLUX_SIZE_ALIGN)
        height = _align_up(height, MFLUX_SIZE_ALIGN)

        # "krea2-still" so the prompt is built exactly as for the vpipe still.
        prompt = _resolved_prompt(shot, project, with_audio=False, model="krea2-still")
        if draft and defaults.get("sketch"):
            prompt = f"{SKETCH_STYLE_PREFIX} {prompt}"

        out = paths.abs_dir / "still.jpeg"
        argv = [cmd, "--model", engine.model]
        if engine.base_model:
            argv += ["--base-model", engine.base_model]
        if engine.quantize:
            argv += ["--quantize", str(engine.quantize)]
        argv += ["--prompt", prompt, "--width", str(width), "--height", str(height),
                 "--steps", str(steps)]
        seed = int(shot.get("seed") or 0)
        if seed:
            argv += ["--seed", str(seed)]
        chain_ref = _ref_source(shot.get("_chainRef"), paths)
        if chain_ref:
            strength = float(shot.get("imgStrength") or 0.6)
            argv += ["--image", chain_ref, str(strength)]
        argv += engine.extra_args

        return JobSpec(
            shot_id=shot["id"],
            expected_outputs=[out],
            payload={"engine": "mflux", "argv": argv, "cwd": str(paths.abs_dir),
                     "model": engine.id, "output": str(out)},
            summary=f"{engine.label} · {width}x{height} · {steps} steps"
                    + (" · DRAFT" if draft else ""),
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
