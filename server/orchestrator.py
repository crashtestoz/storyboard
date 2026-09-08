"""Serial render queue.

One generation at a time, by construction. Running two GPU-bound generations
concurrently on one machine was a real source of trouble when driving vpipe by
hand, so the queue has no concurrency setting to get wrong.

Responsibilities, in order of how much they matter:

1.  **Never run a shot whose input does not exist.** A shot chained to an
    earlier shot's last frame is *blocked* if that shot has no frames, rather
    than rendering against a stale or missing file.
2.  **Decide honestly whether a run worked.** Delegated to the backend's
    ``validate()``; the queue just records the verdict and stops on failure
    instead of marching on through dependents.
3.  **Report progress** as it happens, for the UI to poll.
4.  **Stop cleanly** when asked.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backends.base import Backend, ProgressEvent, ShotPaths
from .store import Store

# statuses that a "render all" should pick up
RERUNNABLE = {"draft", "failed", "blocked", "review", "interrupted"}
MAX_LOG_LINES = 400


@dataclass
class ShotRun:
    """Live state for one shot in the current batch."""

    shot_id: str
    status: str = "queued"
    progress: float = 0.0
    phase: str = ""
    log: list[dict[str, str]] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    validation: dict[str, Any] | None = None
    reason: str = ""
    summary: str = ""
    outputs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "shotId": self.shot_id,
            "status": self.status,
            "progress": round(self.progress, 1),
            "phase": self.phase,
            "log": self.log[-MAX_LOG_LINES:],
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "runtimeSeconds": (
                (self.ended_at or time.time()) - self.started_at
                if self.started_at
                else None
            ),
            "validation": self.validation,
            "reason": self.reason,
            "summary": self.summary,
            "outputs": self.outputs,
        }


class Orchestrator:
    def __init__(self, backend: Backend, store: Store, workspace: Path):
        self.backend = backend
        self.store = store
        self.workspace = Path(workspace)

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._runs: dict[str, ShotRun] = {}
        self._order: list[str] = []
        self._slug: str | None = None
        self._current: str | None = None
        self._batch_started: float | None = None
        self._batch_ended: float | None = None
        self._error: str = ""

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    @property
    def busy(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "busy": self.busy,
                "slug": self._slug,
                "currentShotId": self._current,
                "order": list(self._order),
                "runs": {sid: run.to_json() for sid, run in self._runs.items()},
                "batchStartedAt": self._batch_started,
                "batchEndedAt": self._batch_ended,
                "error": self._error,
                "cancelRequested": self._cancel.is_set(),
            }

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #

    def start(self, slug: str, shot_ids: list[str] | None = None) -> dict[str, Any]:
        if self.busy:
            raise RuntimeError("a render is already running")

        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)

        board = self.store.load(slug)
        shots = board.get("shots") or []
        if shot_ids:
            targets = [s for s in shots if s["id"] in set(shot_ids)]
        else:
            targets = [s for s in shots if s.get("status", "draft") in RERUNNABLE]
        if not targets:
            raise RuntimeError("nothing to render")

        with self._lock:
            self._cancel.clear()
            self._slug = slug
            self._order = [s["id"] for s in targets]
            self._runs = {s["id"]: ShotRun(shot_id=s["id"]) for s in targets}
            self._current = None
            self._batch_started = time.time()
            self._batch_ended = None
            self._error = ""

        # reset persisted state for the shots about to run
        for shot in shots:
            if shot["id"] in self._runs:
                shot.update(status="queued", progress=0, validation=None, outputs=[])
        self.store.save(slug, board)

        self._thread = threading.Thread(
            target=self._run_batch, args=(slug,), name="render-queue", daemon=True
        )
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._cancel.set()
        try:
            self.backend.cancel()
        except NotImplementedError:
            pass
        except Exception:  # noqa: BLE001 - stopping must never raise
            pass
        return self.status()

    # ------------------------------------------------------------------ #
    # the batch
    # ------------------------------------------------------------------ #

    def _run_batch(self, slug: str) -> None:
        try:
            for shot_id in list(self._order):
                if self._cancel.is_set():
                    self._mark_remaining_cancelled()
                    break
                self._run_one(slug, shot_id)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._current = None
                self._batch_ended = time.time()

    def _run_one(self, slug: str, shot_id: str) -> None:
        board = self.store.load(slug)
        shots = board["shots"]
        idx = next((i for i, s in enumerate(shots) if s["id"] == shot_id), None)
        if idx is None:
            return
        shot = shots[idx]
        run = self._runs[shot_id]

        # ---- dependency gate ------------------------------------------
        dep_problem = self._resolve_chain(shot, shots, slug)
        if dep_problem:
            run.status = "blocked"
            run.reason = dep_problem
            run.log.append({"level": "WARN", "text": dep_problem})
            run.log.append(
                {
                    "level": "INFO",
                    "text": "not queued — dependency unmet; fix the upstream "
                            "shot and re-run to release this one",
                }
            )
            shot.update(status="blocked", progress=0)
            self.store.save(slug, board)
            return

        # ---- prepare ---------------------------------------------------
        paths = ShotPaths(
            workspace=self.workspace,
            abs_dir=self.workspace / self.store.shot_rel_dir(slug, idx + 1),
            rel_dir=self.store.shot_rel_dir(slug, idx + 1),
        )
        try:
            spec = self.backend.prepare(shot, board, paths)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.reason = f"could not prepare: {exc}"
            run.log.append({"level": "ERROR", "text": run.reason})
            shot.update(status="failed", progress=0)
            self.store.save(slug, board)
            return

        run.summary = spec.summary
        run.status = "running"
        run.started_at = time.time()
        with self._lock:
            self._current = shot_id
        shot.update(status="running", progress=0)
        self.store.save(slug, board)

        # ---- run -------------------------------------------------------
        def on_event(ev: ProgressEvent) -> None:
            if ev.percent:
                run.progress = max(run.progress, ev.percent)
            if ev.phase:
                run.phase = ev.phase
            if ev.log_line:
                run.log.append({"level": ev.log_level, "text": ev.log_line})
                if len(run.log) > MAX_LOG_LINES * 2:
                    del run.log[:-MAX_LOG_LINES]

        result = self.backend.run(spec, on_event, self._cancel.is_set)
        run.ended_at = time.time()

        # ---- validate --------------------------------------------------
        if result.cancelled or self._cancel.is_set():
            run.status = "interrupted"
            run.reason = "Stopped before completion."
            run.log.append({"level": "INFO", "text": run.reason})
            shot.update(status="interrupted", progress=round(run.progress))
            self.store.save(slug, board)
            return

        # Persist the log next to the outputs before validating, so a run that
        # failed overnight can still be diagnosed after a restart — the whole
        # point of the validation checks is answering "why" hours later, and
        # in-memory logs do not survive that.
        log_url = self._write_log(paths.abs_dir, run, spec, result)

        validation = self.backend.validate(spec, result)
        run.validation = validation.to_json()
        run.status = validation.verdict
        run.reason = validation.reason
        run.progress = 100.0 if validation.ok else run.progress

        outputs = [p for p in spec.expected_outputs if p.exists()]
        run.outputs = [self._as_url(p) for p in outputs]

        shot.update(
            status=validation.verdict,
            progress=round(run.progress),
            runtimeSeconds=round(result.seconds, 1),
            outputs=run.outputs,
            validation=run.validation,
            thumb=self._pick_thumb(spec),
            logUrl=log_url,
        )
        self.store.save(slug, board)

        level = "OK" if validation.ok else "ERROR"
        run.log.append(
            {"level": level, "text": f"{validation.verdict}: {validation.reason or 'all checks passed'}"}
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _resolve_chain(self, shot: dict, shots: list[dict], slug: str) -> str | None:
        """Turn a ``chain`` reference into a real path, or explain why not.

        Returns None when the shot is good to run.
        """
        for key, label in (("startRef", "start"), ("endRef", "end")):
            ref = shot.get(key)
            if not isinstance(ref, dict) or ref.get("kind") != "chain":
                continue
            src_id = ref.get("from")
            src_idx = next((i for i, s in enumerate(shots) if s["id"] == src_id), None)
            if src_idx is None:
                return f"{label} frame chains from a shot that no longer exists"

            frames_dir = (
                self.workspace / self.store.shot_rel_dir(slug, src_idx + 1) / "frames"
            )
            frames = sorted(frames_dir.glob("*.png")) if frames_dir.exists() else []
            if not frames:
                src_title = shots[src_idx].get("title") or f"shot {src_idx + 1}"
                return (
                    f"{label} frame chains from “{src_title}”, which has no "
                    f"rendered frames yet"
                )
            # last frame of the upstream clip
            ref["resolved"] = str(frames[-1].relative_to(self.workspace))
        return None

    def _write_log(self, shot_dir: Path, run: ShotRun, spec, result) -> str | None:
        """Dump this run's stdout plus a short header to <shot>/run.log."""
        try:
            shot_dir.mkdir(parents=True, exist_ok=True)
            dest = shot_dir / "run.log"
            head = [
                f"# shot      {run.shot_id}",
                f"# summary   {run.summary}",
                f"# exit      {result.exit_code}",
                f"# seconds   {round(result.seconds, 1)}",
                f"# expected  ~{round(spec.expected_seconds)}s",
                "",
            ]
            body = [f"[{lvl}] {text}" for lvl, text in result.log]
            dest.write_text("\n".join(head + body) + "\n")
            return self._as_url(dest)
        except OSError:
            return None

    def _pick_thumb(self, spec) -> str | None:
        # a still's own output, else the middle frame of the clip (most
        # settled — the first and last frames of a short clip are the least)
        for p in spec.expected_outputs:
            if p.suffix.lower() in (".jpeg", ".jpg", ".png") and p.exists():
                return self._as_url(p)
        if spec.frames_dir and spec.frames_dir.exists():
            frames = sorted(spec.frames_dir.glob("*.png"))
            if frames:
                return self._as_url(frames[len(frames) // 2])
        return None

    def _as_url(self, p: Path) -> str:
        try:
            rel = Path(p).relative_to(self.workspace)
        except ValueError:
            return str(p)
        return "/media/" + str(rel).replace("\\", "/")

    def _mark_remaining_cancelled(self) -> None:
        for sid, run in self._runs.items():
            if run.status == "queued":
                run.status = "draft"
