"""Render backend contract.

The storyboard UI does not know what generates its video. Everything
backend-specific lives behind this interface so a second backend (ComfyUI)
can be added without touching the orchestrator, the API or the front end.

Three things had to be abstracted rather than assumed, because vpipe and
ComfyUI genuinely differ on each:

*   **How a job is prepared.** vpipe wants a `.vpipeline` file on disk and an
    argv; ComfyUI wants a workflow graph POSTed to a running server. So
    `prepare()` returns an opaque :class:`JobSpec` and only the backend knows
    what is inside it.
*   **How a job runs and reports progress.** vpipe is a subprocess we own and
    parse stdout from; ComfyUI is an HTTP service we poll or subscribe to. So
    `run()` owns the whole execution and merely emits events — the
    orchestrator never assumes there is a process.
*   **What the model can do.** Frame-count rules, whether start/end anchors
    exist, whether reference images are supported — all of that is per-model
    and per-backend, so it is *data* returned by `capabilities()` rather than
    hardcoded in the UI.

What is deliberately NOT abstracted is validation of the result. Deciding
whether a run actually succeeded is the same problem for any backend — did it
write what it said it would, in a plausible amount of time — so that lives
here as :meth:`Backend.validate`, and a backend only adds checks it alone can
make (vpipe: scanning its stdout for known silent-failure patterns).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal


# ---------------------------------------------------------------------------
# Capability description (returned to the UI as JSON)
# ---------------------------------------------------------------------------

@dataclass
class FrameRule:
    """How a model constrains clip length.

    MiniMax H3 only accepts ``17n + 5`` frames and cannot *decode* fewer than
    8 latent frames, which is why a 5-frame request silently produced nothing.
    Expressing that as data lets the UI warn before a job is ever queued.
    """

    kind: Literal["any", "affine"] = "any"
    # for kind="affine": frames must satisfy frames % step == offset
    step: int = 1
    offset: int = 0
    minimum: int = 1
    # human-readable explanation for the UI
    note: str = ""

    def snap(self, frames: int) -> int:
        """Round *frames* up to the next legal value."""
        frames = max(int(frames), self.minimum)
        if self.kind == "any":
            return frames
        while frames % self.step != self.offset or frames < self.minimum:
            frames += 1
        return frames

    def check(self, frames: int) -> str | None:
        """Return a human-readable problem, or None if *frames* is fine."""
        if self.kind == "any":
            return None if frames >= self.minimum else (
                f"needs at least {self.minimum} frames"
            )
        if frames < self.minimum:
            return f"needs at least {self.minimum} frames (would not decode)"
        if frames % self.step != self.offset:
            return f"must be {self.step}n + {self.offset} — will be snapped up"
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "step": self.step,
            "offset": self.offset,
            "minimum": self.minimum,
            "note": self.note,
        }


@dataclass
class ModelCapability:
    """One selectable model, and what the UI may offer for it."""

    id: str
    label: str
    kind: Literal["video", "image"] = "video"
    supports_start_anchor: bool = False
    supports_end_anchor: bool = False
    supports_style_refs: bool = False
    max_style_refs: int = 0
    supports_audio: bool = False
    frame_rule: FrameRule = field(default_factory=FrameRule)
    resolutions: list[str] = field(default_factory=list)
    default_steps: int = 8
    # set False when the weights are not present on this machine yet
    available: bool = True
    unavailable_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "supportsStartAnchor": self.supports_start_anchor,
            "supportsEndAnchor": self.supports_end_anchor,
            "supportsStyleRefs": self.supports_style_refs,
            "maxStyleRefs": self.max_style_refs,
            "supportsAudio": self.supports_audio,
            "frameRule": self.frame_rule.to_json(),
            "resolutions": self.resolutions,
            "defaultSteps": self.default_steps,
            "available": self.available,
            "unavailableReason": self.unavailable_reason,
        }


# ---------------------------------------------------------------------------
# Job / run / validation
# ---------------------------------------------------------------------------

@dataclass
class JobSpec:
    """Everything needed to run one shot, produced by :meth:`Backend.prepare`.

    ``expected_outputs`` is the contract the run is held to afterwards: paths
    the pipeline said it would write. It is the single most useful signal for
    catching a silent failure, because a backend that fails quietly still
    exits cleanly.
    """

    shot_id: str
    # paths (absolute) the run must produce to be considered successful
    expected_outputs: list[Path] = field(default_factory=list)
    # directory expected to fill with per-frame stills, and how many
    frames_dir: Path | None = None
    expected_frames: int = 0
    # seconds; used for the runtime sanity check. 0 = unknown, skip the check.
    expected_seconds: float = 0.0
    # backend's own payload (vpipe: spec file + argv; comfyui: workflow graph)
    payload: dict[str, Any] = field(default_factory=dict)
    # human-readable summary shown in the UI
    summary: str = ""


@dataclass
class ProgressEvent:
    """Emitted during a run so the UI can show something honest."""

    phase: str = ""
    percent: float = 0.0        # 0-100 across the whole job
    detail: str = ""
    log_line: str | None = None
    log_level: str = "INFO"


@dataclass
class RunResult:
    exit_code: int | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    log: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    cancelled: bool = False
    error: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


@dataclass
class Check:
    name: str
    passed: bool | None      # None = not applicable / skipped
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Validation:
    checks: list[Check] = field(default_factory=list)
    # "done" | "failed" | "review"
    verdict: str = "done"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "done"

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "checks": [c.to_json() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class Backend:
    """Base class. Subclasses implement prepare/run/cancel and may extend
    :meth:`validate` with checks only they can make."""

    id: str = "base"
    label: str = "Base"

    # -- description ------------------------------------------------------
    def capabilities(self) -> list[ModelCapability]:
        raise NotImplementedError

    def capability(self, model_id: str) -> ModelCapability | None:
        for c in self.capabilities():
            if c.id == model_id:
                return c
        return None

    def health(self) -> tuple[bool, str]:
        """(usable, message) — surfaced in the UI so a misconfigured backend
        says so up front instead of failing on the first render."""
        return True, ""

    # -- execution --------------------------------------------------------
    def prepare(self, shot: dict, project: dict, paths: "ShotPaths") -> JobSpec:
        raise NotImplementedError

    def run(
        self,
        spec: JobSpec,
        on_event: Callable[[ProgressEvent], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        raise NotImplementedError

    # -- validation -------------------------------------------------------
    def validate(self, spec: JobSpec, result: RunResult) -> Validation:
        """Shared success contract.

        Deliberately not "did it exit 0". Every real failure observed while
        driving vpipe by hand exited 0, because it warns and continues rather
        than crashing — so a clean exit is one signal among several, not the
        answer.
        """
        checks: list[Check] = []

        # 1. exit code
        if result.cancelled:
            return Validation(
                checks=[Check("cancelled", None, "run was stopped")],
                verdict="failed",
                reason="Cancelled before completion.",
            )
        exit_ok = result.exit_code == 0
        checks.append(
            Check(
                "exit code",
                exit_ok,
                f"exit {result.exit_code}" if result.exit_code is not None else "no exit code",
            )
        )

        # 2. output manifest — the strongest signal
        missing: list[str] = []
        empty: list[str] = []
        for p in spec.expected_outputs:
            if not p.exists():
                missing.append(p.name)
            elif p.stat().st_size < 1024:
                empty.append(f"{p.name} ({p.stat().st_size} B)")
        manifest_ok = not missing and not empty
        if missing or empty:
            bits = []
            if missing:
                bits.append("missing: " + ", ".join(missing))
            if empty:
                bits.append("suspiciously small: " + ", ".join(empty))
            detail = "; ".join(bits)
        else:
            detail = ", ".join(
                f"{p.name} ({p.stat().st_size // 1024} KB)" for p in spec.expected_outputs
            ) or "nothing declared"
        checks.append(Check("output manifest", manifest_ok, detail))

        # 3. frame count
        if spec.frames_dir and spec.expected_frames:
            got = len(list(spec.frames_dir.glob("*.png"))) if spec.frames_dir.exists() else 0
            frames_ok = got >= spec.expected_frames
            checks.append(
                Check(
                    "frames written",
                    frames_ok,
                    f"{got}/{spec.expected_frames}",
                )
            )
        else:
            frames_ok = True
            checks.append(Check("frames written", None, "not requested"))

        # 4. runtime sanity — catches a run that "succeeded" far too fast to
        #    have done the work, even if a file happens to exist.
        runtime_ok: bool | None = None
        if spec.expected_seconds > 0:
            ratio = result.seconds / spec.expected_seconds
            runtime_ok = ratio >= 0.25
            checks.append(
                Check(
                    "runtime plausible",
                    runtime_ok,
                    f"{_dur(result.seconds)} vs expected ~{_dur(spec.expected_seconds)}"
                    f" ({ratio * 100:.0f}%)",
                )
            )
        else:
            checks.append(Check("runtime plausible", None, "no baseline yet"))

        # 5. backend-specific checks (log scanning, telemetry, ...)
        checks.extend(self.extra_checks(spec, result))

        hard_failed = [c for c in checks if c.passed is False]
        if not hard_failed:
            return Validation(checks=checks, verdict="done")

        # A run that produced everything it promised but tripped a soft signal
        # is flagged for a human rather than thrown away.
        produced = manifest_ok and frames_ok
        if produced and all(c.name != "output manifest" for c in hard_failed):
            return Validation(
                checks=checks,
                verdict="review",
                reason="Produced its outputs, but "
                + "; ".join(f"{c.name}: {c.detail}" for c in hard_failed)
                + ".",
            )

        return Validation(
            checks=checks,
            verdict="failed",
            reason="; ".join(f"{c.name} — {c.detail}" for c in hard_failed) + ".",
        )

    def extra_checks(self, spec: JobSpec, result: RunResult) -> Iterable[Check]:
        return []


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ---------------------------------------------------------------------------
# Where a shot's files live
# ---------------------------------------------------------------------------

@dataclass
class ShotPaths:
    """Filesystem layout for one shot.

    Two views of the same place, because vpipe resolves relative paths against
    the directory it is launched from: ``abs_*`` for our own checks, ``rel_*``
    for what goes inside a generated pipeline file.
    """

    workspace: Path          # cwd the backend will run in
    abs_dir: Path            # <workspace>/<rel_dir>
    rel_dir: str             # e.g. "projects/falcon/shots/02"

    @property
    def abs_frames(self) -> Path:
        return self.abs_dir / "frames"

    @property
    def rel_frames(self) -> str:
        return f"{self.rel_dir}/frames"

    def ensure(self) -> None:
        self.abs_dir.mkdir(parents=True, exist_ok=True)
        self.abs_frames.mkdir(parents=True, exist_ok=True)
