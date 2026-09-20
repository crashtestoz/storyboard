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
3.  **Render what the board now says, not what it said once.** A shot marked
    done is skipped, which is right — a re-render costs half an hour — but
    "done" has to mean "done *from this*". Each render records a fingerprint
    of its inputs, and a batch picks up any shot whose inputs have moved
    since, including one whose start frame comes from a shot being re-run.
5.  **Produce the video.** A board of finished clips is not the deliverable;
    the cut is. A full batch ends by concatenating the shots (see
    ``assemble``), and says so either way.
6.  **Report progress** as it happens, for the UI to poll.
7.  **Stop cleanly** when asked.
"""

from __future__ import annotations

import hashlib
import threading
import time
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import assemble as assembly
from .backends.base import Backend, ProgressEvent, ShotPaths
from .dubbing import mux_speech
from .store import Store, last_saved_frame, render_fingerprint, stale_reason

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
    eta_seconds: float | None = None
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
            "etaSeconds": (
                round(self.eta_seconds) if self.eta_seconds is not None else None
            ),
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
    def __init__(self, backend: Backend, store: Store, workspace: Path,
                 data_dir: Path | None = None):
        self.prepare_dialogue = None
        self.backend = backend
        self.store = store
        self.workspace = Path(workspace)
        # The backend runs in the workspace; the shots it reads and writes live
        # in the data directory. The same place by default, not necessarily.
        self.data_dir = Path(data_dir) if data_dir else Path(workspace)

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
        self._operation = "render"
        # Why each shot was queued, so "Render all" can say what it picked up
        # rather than leaving the user to infer it from what changes.
        self._queued_because: dict[str, str] = {}
        # Last concat pass: {"state", "message", "url", ...}. Reported with the
        # batch, because a batch that rendered every shot and produced no video
        # has not finished the job.
        self._assembly: dict[str, Any] | None = None

        # "Create Stills" — a separate, much shorter job (three Krea-2 stills,
        # not a shot render), but it still shares the one GPU vpipe drives, so
        # it gets its own thread and its own small piece of status rather than
        # reusing _runs/_order, which are shaped around a whole render batch.
        self._stills_thread: threading.Thread | None = None
        self._stills_shot_id: str | None = None
        self._stills_phase: str = ""
        self._stills_progress: float = 0.0
        self._stills_error: str = ""
        self._stills_log: list[dict[str, str]] = []
        # Filled in as each phase finishes, not just at the end — so the UI
        # can show "start" the moment it is done instead of waiting on "mid"
        # and "end" too.
        self._stills_results: dict[str, Any] = {}

        # Batch Render — render several projects back to back, each with
        # its own board's own settings, for an overnight run. Reuses the
        # single-project _prime_batch/_run_batch machinery project by
        # project on self._thread (so "busy" spans the whole queue, not
        # just one project — still one GPU job at a time); this is just
        # the bookkeeping for which project is up next.
        self._project_queue_total: list[str] = []
        self._project_queue_remaining: list[str] = []
        self._project_queue_done: list[str] = []
        self._project_queue_current: str | None = None
        self._project_queue_errors: dict[str, str] = {}
        self._project_batch_active: bool = False

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    @property
    def busy(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def stills_busy(self) -> bool:
        t = self._stills_thread
        return t is not None and t.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "busy": self.busy,
                "operation": self._operation,
                "slug": self._slug,
                "currentShotId": self._current,
                "order": list(self._order),
                "runs": {sid: run.to_json() for sid, run in self._runs.items()},
                "batchStartedAt": self._batch_started,
                "batchEndedAt": self._batch_ended,
                "error": self._error,
                "cancelRequested": self._cancel.is_set(),
                "queuedBecause": dict(self._queued_because),
                "assembly": self._assembly,
                "stills": {
                    "busy": self.stills_busy,
                    "shotId": self._stills_shot_id,
                    "phase": self._stills_phase,
                    "progress": round(self._stills_progress, 1),
                    "error": self._stills_error,
                    "log": self._stills_log[-MAX_LOG_LINES:],
                    "results": dict(self._stills_results),
                },
                "projectBatch": {
                    "active": self._project_batch_active,
                    "current": self._project_queue_current,
                    "total": list(self._project_queue_total),
                    "remaining": list(self._project_queue_remaining),
                    "done": list(self._project_queue_done),
                    "errors": dict(self._project_queue_errors),
                },
            }

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #

    def start(self, slug: str, shot_ids: list[str] | None = None) -> dict[str, Any]:
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("still previews are generating — one GPU job at a time")

        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)

        whole_board = self._prime_batch(slug, shot_ids)

        self._thread = threading.Thread(
            target=self._run_batch, args=(slug, whole_board),
            name="render-queue", daemon=True,
        )
        self._thread.start()
        return self.status()

    def start_dialogue(self, slug: str) -> dict:
        """Prepare/reuse recordings without changing video render state."""
        if self.busy or self.stills_busy:
            raise RuntimeError("Wait for the current render or still previews to finish")
        if not self.prepare_dialogue:
            raise RuntimeError("Dialogue preparation is unavailable")
        board = self.store.load(slug)
        self._cancel.clear()
        self._operation = "dialogue"
        self._slug = slug
        self._order = [s["id"] for s in board["shots"] if (s.get("dialogue") or "").strip()]
        self._runs = {sid: ShotRun(shot_id=sid) for sid in self._order}
        self._error = ""
        self._assembly = None
        self._queued_because = {sid: "prepare dialogue only; video is unchanged" for sid in self._order}
        self._batch_started = time.time()
        self._batch_ended = None

        def work():
            try:
                for sid in self._order:
                    if self._cancel.is_set():
                        break
                    self._current = sid
                    run = self._runs[sid]
                    run.status, run.phase = "running", "Preparing dialogue only"
                    self.prepare_dialogue(slug, sid)
                    run.status, run.progress = "done", 100
            except Exception as exc:
                self._error = str(exc)
                self._runs[sid].status = "failed"
                self._runs[sid].reason = str(exc)
            finally:
                self._current = None
                self._batch_ended = time.time()
        self._thread = threading.Thread(target=work, name="dialogue-queue", daemon=True)
        self._thread.start()
        return self.status()

    def _prime_batch(self, slug: str, shot_ids: list[str] | None) -> bool:
        """Load *slug*, pick its targets, and set up the run state that
        ``_run_batch`` expects — the part of ``start()`` that a single
        project's render and a multi-project batch both need, unchanged
        either way. Returns ``whole_board`` (whether to assemble after).
        """
        self._operation = "render"
        board = self.store.load(slug)
        shots = board.get("shots") or []
        # An explicit list is exactly that — the user asked for these shots.
        # No list means the whole board, which also means assembling the cut.
        index = {s["id"]: i for i, s in enumerate(shots)}
        for i, scene in enumerate(shots):
            for key in ("startRef", "endRef", "continuityRef"):
                ref = scene.get(key)
                if isinstance(ref, dict) and ref.get("kind") == "chain":
                    if ref.get("from") not in index or index[ref["from"]] >= i:
                        raise ValueError("A scene can only continue from an existing earlier scene; fix missing, forward or cyclic links")
        whole_board = not shot_ids
        if shot_ids:
            wanted = set(shot_ids)
            targets = [s for s in shots if s["id"] in wanted]
            because = {s["id"]: "asked for by name" for s in targets}
        else:
            targets, because = self._pending(board)
        # Include stale or missing prerequisites for selected renders.
        wanted = {s["id"] for s in targets}
        for scene in reversed(shots):
            if scene["id"] not in wanted:
                continue
            for key in ("startRef", "endRef", "continuityRef"):
                ref = scene.get(key)
                if not isinstance(ref, dict) or ref.get("kind") != "chain":
                    continue
                source = shots[index[ref["from"]]]
                frame_dir = self.data_dir / self.store.shot_rel_dir(slug, index[source["id"]] + 1) / "frames"
                if source.get("status") != "done" or stale_reason(source, board) or not last_saved_frame(frame_dir, int(source.get("frames") or 0)):
                    wanted.add(source["id"])
                    because.setdefault(source["id"], "required by a dependent scene")
        targets = [s for s in shots if s["id"] in wanted]
        if not targets and not whole_board:
            raise RuntimeError("nothing to render")
        # A whole-board run with nothing stale is not a no-op: it still owes
        # the user the assembled cut. Only a board with no clips at all has
        # genuinely nothing to do.
        if not targets and not _any_output(shots):
            raise RuntimeError(
                "nothing to render — no shot has a prompt to render or a clip "
                "to assemble yet"
            )

        with self._lock:
            self._cancel.clear()
            self._slug = slug
            self._order = [s["id"] for s in targets]
            self._runs = {s["id"]: ShotRun(shot_id=s["id"]) for s in targets}
            self._queued_because = because
            self._assembly = None
            self._current = None
            self._batch_started = time.time()
            self._batch_ended = None
            self._error = ""

        # reset persisted state for the shots about to run, and say up front
        # why each one is in the batch — an unexplained 36-minute re-render of
        # a shot that looked finished is indistinguishable from a bug.
        for shot in shots:
            if shot["id"] in self._runs:
                shot.update(
                    status="queued", reason="", progress=0,
                    validation=None, outputs=[]
                )
                why = because.get(shot["id"])
                if why:
                    self._runs[shot["id"]].log.append(
                        {"level": "INFO", "text": f"queued — {why}"}
                    )
        self.store.save(slug, board)
        return whole_board

    def start_project_batch(self, slugs: list[str]) -> dict[str, Any]:
        """Render several projects back to back — each one's own "Render
        all", automatically, in the order given — for an overnight run.
        """
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("still previews are generating — one GPU job at a time")
        slugs = [s for s in dict.fromkeys(slugs) if s]  # de-dup, keep order
        if not slugs:
            raise RuntimeError("no projects selected")

        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)

        with self._lock:
            self._cancel.clear()
            self._project_queue_total = list(slugs)
            self._project_queue_remaining = list(slugs)
            self._project_queue_done = []
            self._project_queue_current = None
            self._project_queue_errors = {}
            self._project_batch_active = True

        self._thread = threading.Thread(
            target=self._run_project_queue, name="project-batch", daemon=True,
        )
        self._thread.start()
        return self.status()

    def _run_project_queue(self) -> None:
        try:
            for slug in list(self._project_queue_total):
                if self._cancel.is_set():
                    break
                with self._lock:
                    self._project_queue_current = slug
                try:
                    whole_board = self._prime_batch(slug, None)
                    self._run_batch(slug, whole_board)
                except Exception as exc:  # noqa: BLE001 - one bad project
                    # should not sink the rest of an overnight queue
                    with self._lock:
                        self._project_queue_errors[slug] = str(exc)
                with self._lock:
                    if slug in self._project_queue_remaining:
                        self._project_queue_remaining.remove(slug)
                    self._project_queue_done.append(slug)
        finally:
            with self._lock:
                self._project_queue_current = None
                self._project_batch_active = False

    def _pending(self, board: dict) -> tuple[list[dict], dict[str, str]]:
        """The shots a whole-board run should render, in board order.

        Not simply "the ones not marked done". A shot is also pending when its
        clip no longer matches the board — a rewritten prompt, a swapped
        reference, a changed frame size — because otherwise a run over a board
        of finished shots renders nothing, reports success, and leaves a cut
        built from the words the user replaced. That happened.
        """
        shots = board.get("shots") or []
        because: dict[str, str] = {}

        for shot in shots:
            status = shot.get("status", "draft")
            if status in RERUNNABLE:
                because[shot["id"]] = f"status is “{status}”"
                continue
            why = stale_reason(shot, board)
            if why:
                because[shot["id"]] = why

        # A start-frame anchor is a picture, and re-rendering the shot it comes
        # from makes it a different picture — so the shot after it no longer
        # follows on from the frame it was built to continue. Chase the chain
        # until it stops growing, because chains can be several long.
        changed = True
        while changed:
            changed = False
            for shot in shots:
                if shot["id"] in because:
                    continue
                for key in ("startRef", "endRef", "continuityRef"):
                    ref = shot.get(key)
                    if (
                        isinstance(ref, dict)
                        and ref.get("kind") == "chain"
                        and ref.get("from") in because
                    ):
                        because[shot["id"]] = (
                            "continues from a shot being re-rendered, so its "
                            "anchor frame will change"
                        )
                        changed = True
                        break

        return [s for s in shots if s["id"] in because], because

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
    # stills ("Create Stills" — start/mid/end previews, not a shot render)
    # ------------------------------------------------------------------ #

    # Order matters here: it is also the order shown in the UI.
    STILL_PHASES = (
        ("start", "The very start of this shot, before the described action "
                  "gets underway — the opening pose and composition"),
        ("mid", "The midpoint of this shot, in the middle of the described "
                "action"),
        ("end", "The very end of this shot, the instant the described "
                "action finishes — the closing pose and composition"),
    )

    def create_stills(
        self, slug: str, shot_id: str, phase_prompts: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("stills are already generating for this board")

        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)

        board = self.store.load(slug)
        shot = next((s for s in board.get("shots") or [] if s["id"] == shot_id), None)
        if shot is None:
            raise RuntimeError("shot not found")
        if not (shot.get("prompt") or "").strip():
            raise RuntimeError("nothing to sketch — this shot has no prompt yet")

        with self._lock:
            self._cancel.clear()
            self._stills_shot_id = shot_id
            self._stills_phase = "starting"
            self._stills_progress = 0.0
            self._stills_error = ""
            self._stills_log = []
            self._stills_results = {}

        self._stills_thread = threading.Thread(
            target=self._run_stills, args=(slug, shot_id, phase_prompts or {}),
            name="create-stills", daemon=True,
        )
        self._stills_thread.start()
        return self.status()

    def _run_stills(
        self, slug: str, shot_id: str, phase_prompts: dict[str, str]
    ) -> None:
        try:
            board = self.store.load(slug)
            idx = next(
                (i for i, s in enumerate(board["shots"]) if s["id"] == shot_id), None
            )
            if idx is None:
                raise RuntimeError("shot not found")
            shot = board["shots"][idx]
            base_rel = f"{self.store.shot_rel_dir(slug, idx + 1)}/stills"
            results: dict[str, Any] = {}

            # "Small" (the default) always uses draft geometry regardless of
            # this board's own draft toggle — fast, and fine for judging
            # composition. "Large" renders at the project's real resolution
            # and step count instead, slower but big enough to feed back in
            # as a reference image. Sketch's line-art styling is a separate,
            # opt-in concern this button does not imply either way.
            large = ((board.get("defaults") or {}).get("stillsSize") or "small") == "large"
            still_project = dict(board)
            still_project["defaults"] = {
                **(board.get("defaults") or {}), "draft": not large, "sketch": False,
            }

            prev_still: Path | None = None
            for i, (key, phase_hint) in enumerate(self.STILL_PHASES):
                if self._cancel.is_set():
                    raise RuntimeError("stopped before completion")
                with self._lock:
                    self._stills_phase = key
                    self._stills_progress = (i / len(self.STILL_PHASES)) * 100

                # A synthetic shot, not a real one: always Krea-2 regardless
                # of what this shot is set to render as, since stills are a
                # fast preview, not the shot's own model. Scene, cast and any
                # style refs still come from _resolved_prompt via prepare().
                still_shot = dict(shot)
                still_shot["model"] = "krea2-still"
                still_shot["steps"] = 8 if large else 4
                # An LLM-extracted description of what THIS shot's own prompt
                # actually establishes at this point in the action (see
                # llm.describe_still_phases) beats a generic phase label —
                # "the start of this shot" means nothing to a model with no
                # sense of time, but "a distant shape high above the ocean"
                # does. Falls back to the old generic hint per-phase if the
                # extraction is unavailable or didn't cover this one.
                extracted = (phase_prompts or {}).get(key)
                still_shot["prompt"] = (
                    extracted or f"{phase_hint}. {shot.get('prompt') or ''}".strip()
                )
                if prev_still is not None:
                    # Build each later still from the one before it — plain
                    # img2img continuation, a separate field from startRef
                    # (see prepare()'s krea2-still branch) so it never gets
                    # confused with a real anchor/identity reference, which
                    # uses a different mechanism (the identity-edit LoRA).
                    # Moderate strength: enough freedom to move toward this
                    # phase's prompt, not enough to redesign the subject.
                    # The *first* still keeps dict(shot)'s own startRef and
                    # characterIds untouched — those drive the identity-edit
                    # path instead (see prepare()).
                    still_shot["_chainRef"] = str(prev_still)
                    still_shot["imgStrength"] = 0.55
                paths = ShotPaths(
                    workspace=self.workspace,
                    abs_dir=self.data_dir / base_rel / key,
                    rel_dir=f"{base_rel}/{key}",
                    data_dir=self.data_dir,
                )
                spec = self.backend.prepare(still_shot, still_project, paths)

                def on_event(ev: ProgressEvent, key=key) -> None:
                    if ev.log_line:
                        with self._lock:
                            self._stills_log.append(
                                {"level": ev.log_level, "text": f"[{key}] {ev.log_line}"}
                            )

                result = self.backend.run(spec, on_event, self._cancel.is_set)
                if result.cancelled or self._cancel.is_set():
                    raise RuntimeError("stopped before completion")
                if result.error or result.exit_code != 0:
                    raise RuntimeError(
                        result.error or f"vpipe exited {result.exit_code}"
                    )
                out = next((p for p in spec.expected_outputs if p.exists()), None)
                if out is None:
                    raise RuntimeError(f"{key} still did not produce an image")
                results[key] = {"url": self._as_url(out)}
                prev_still = out

                # Save and publish this phase's result now, not just at the
                # end — so "start" shows up the moment it is done instead of
                # waiting on "mid" and "end" too, and so a cancel or crash
                # partway through still leaves whatever finished in place.
                with self._lock:
                    self._stills_results = dict(results)
                fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
                if fresh_shot is not None:
                    fresh_shot["stills"] = dict(results)
                    self.store.save(slug, fresh_board)

            with self._lock:
                self._stills_progress = 100.0
                self._stills_phase = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            with self._lock:
                self._stills_error = str(exc)

    # ------------------------------------------------------------------ #
    # the batch
    # ------------------------------------------------------------------ #

    def _run_batch(self, slug: str, assemble_after: bool = False) -> None:
        try:
            # Prepare all dialogue before the first expensive video job. Whole-board
            # runs also remux current clips whose audio mix setting changed.
            board = self.store.load(slug)
            speech_ids = [s["id"] for s in board["shots"]] if assemble_after else list(self._order)
            if self.prepare_dialogue:
                for sid in speech_ids:
                    if self._cancel.is_set():
                        self._mark_remaining_cancelled()
                        return
                    self._current = sid
                    run = self._runs.get(sid)
                    if run:
                        run.phase = "Preparing dialogue"
                    try:
                        self.prepare_dialogue(slug, sid)
                    except Exception as exc:
                        # A shot whose dialogue setup is wrong (e.g. native
                        # speech with no cast voice reference) is that one
                        # shot's problem, not the whole batch's — block just
                        # this shot and keep preparing/rendering the rest,
                        # the same way a failed render or an unmet
                        # dependency only stops the shot it happened to.
                        reason = f"Dialogue preparation failed: {exc}"
                        if run:
                            run.status, run.reason = "blocked", reason
                            run.log.append({"level": "ERROR", "text": reason})
                        fresh_board, fresh_shot = self._reload_shot(slug, sid)
                        if fresh_shot is not None:
                            fresh_shot.update(status="blocked", reason=reason, progress=0)
                            self.store.save(slug, fresh_board)
                        if sid in self._order:
                            self._order.remove(sid)
            for shot_id in list(self._order):
                if self._cancel.is_set():
                    self._mark_remaining_cancelled()
                    break
                self._run_one(slug, shot_id)
            if assemble_after and not self._cancel.is_set():
                self._assemble(slug)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            fresh = self.store.load(slug)
            for shot in fresh.get("shots", []):
                run = self._runs.get(shot["id"])
                if run and run.status == "queued":
                    run.status, run.reason = "blocked", self._error
                    shot.update(status="blocked", reason=self._error)
            self.store.save(slug, fresh)
        finally:
            with self._lock:
                self._current = None
                self._batch_ended = time.time()

    def _reload_shot(self, slug: str, shot_id: str) -> tuple[dict, dict | None]:
        """The board and this shot, re-read from disk — not the copy this run
        started from.

        A render can run for tens of minutes, and every save before this one
        saved *the whole board*. Writing back the in-memory copy loaded at
        the top of ``_run_one`` would silently discard any edit made
        anywhere on the board while it was in flight: a different shot's
        prompt, the scene description, a shot added from the UI mid-render —
        all of it, gone the moment this shot's result was saved. Re-reading
        right before every save, and updating only the fields a render
        actually owns, is what keeps a long render from clobbering work that
        happened beside it. Returns ``(board, None)`` if the shot was
        deleted from under a running render — nothing to attach the result
        to, but the board itself still needs to come back to the caller.
        """
        board = self.store.load(slug)
        shot = next((s for s in board.get("shots") or [] if s["id"] == shot_id), None)
        return board, shot

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
            fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
            if fresh_shot is not None:
                fresh_shot.update(status="blocked", reason=dep_problem, progress=0)
                self.store.save(slug, fresh_board)
            return

        # ---- prepare ---------------------------------------------------
        paths = ShotPaths(
            workspace=self.workspace,
            abs_dir=self.data_dir / self.store.shot_rel_dir(slug, idx + 1),
            rel_dir=self.store.shot_rel_dir(slug, idx + 1),
            data_dir=self.data_dir,
        )
        try:
            spec = self.backend.prepare(shot, board, paths)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.reason = f"could not prepare: {exc}"
            run.log.append({"level": "ERROR", "text": run.reason})
            fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
            if fresh_shot is not None:
                fresh_shot.update(status="failed", reason=run.reason, progress=0)
                self.store.save(slug, fresh_board)
            return

        # A render owns the generated stills for this scene. Clear both the
        # frame dump used for chaining and the optional start/mid/end preview
        # images before writing the new take, so a shorter re-render cannot
        # leave an old tail available to the next scene.
        removed_stills = self._clear_scene_stills(paths.abs_dir)

        # Stamp the start so validation can still distinguish any unexpected
        # old output from files written by this run.
        spec.started_at = time.time()

        run.summary = spec.summary
        run.status = "running"
        run.started_at = time.time()
        if removed_stills:
            run.log.append(
                {
                    "level": "INFO",
                    "text": f"cleared {removed_stills} generated still(s) from the previous take",
                }
            )
        with self._lock:
            self._current = shot_id
        fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
        if fresh_shot is not None:
            fresh_shot.update(status="running", progress=0, stills={})
            self.store.save(slug, fresh_board)

        # ---- run -------------------------------------------------------
        def on_event(ev: ProgressEvent) -> None:
            if ev.percent:
                run.progress = max(run.progress, ev.percent)
            if ev.phase:
                run.phase = ev.phase
            if ev.eta_seconds is not None:
                run.eta_seconds = ev.eta_seconds
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
            fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
            if fresh_shot is not None:
                fresh_shot.update(
                    status="interrupted", reason=run.reason,
                    progress=round(run.progress)
                )
                self.store.save(slug, fresh_board)
            return

        validation = self.backend.validate(spec, result)
        run.validation = validation.to_json()
        run.status = validation.verdict
        run.reason = validation.reason
        run.progress = 100.0 if validation.ok else run.progress

        # Persist the log next to the outputs after validation, so a run that
        # failed overnight can still be diagnosed after a restart — the whole
        # point of the validation checks is answering "why" hours later, and
        # in-memory logs do not survive that.
        log_url = self._write_log(paths.abs_dir, run, spec, result)

        outputs = [p for p in spec.expected_outputs if p.exists()]
        run.outputs = [self._as_url(p) for p in outputs]

        # Computed from *this run's own* board and shot — the snapshot it was
        # actually rendered against, never a freshly re-read copy. The whole
        # point of a fingerprint is catching a clip that no longer matches
        # what the board currently says; computing it from a post-render
        # reload would make an edit made *during* the render look like it was
        # already accounted for, and hide exactly the staleness this exists
        # to catch.
        fingerprint = render_fingerprint(shot, board)

        # True when this render asked H3 to speak the dialogue itself, in the
        # cloned voice, instead of staying silent for TTS to dub in
        # afterwards (see vpipe_backend.py's _clones_voice). Relaying a
        # leftover TTS take onto a clip that already speaks the line is
        # exactly the "second voice" bug that feature exists to remove —
        # so here it must not run, no matter how old a dialogue.wav is
        # sitting in the shot's folder from before that shot ever cloned its
        # own voice.
        voice_cloned_natively = bool(spec.payload.get("voiceClonedNatively"))

        fresh_board, fresh_shot = self._reload_shot(slug, shot_id)
        if fresh_shot is not None:
            fresh_shot.update(
                status=validation.verdict,
                reason=validation.reason or "",
                progress=round(run.progress),
                runtimeSeconds=round(result.seconds, 1),
                outputs=run.outputs,
                validation=run.validation,
                thumb=self._pick_thumb(spec),
                logUrl=log_url,
                # so a draft is never mistaken for a finished shot later
                renderedAs="draft" if spec.payload.get("draft") else "final",
                renderFingerprint=fingerprint,
                renderedDialogueSource="native" if voice_cloned_natively else "recording",
            )
            if voice_cloned_natively:
                # A dub from before this shot cloned its own voice would
                # otherwise keep winning in the preview (it is preferred over
                # the shot's own outputs) and look like the fresh render is
                # still doubled, when the clip itself is clean. The old
                # dialogue.wav / clip-dubbed.mp4 files are left on disk —
                # harmless, just no longer referenced.
                fresh_shot["dubUrl"] = None
            elif validation.ok:
                self._relay_speech(fresh_shot, paths.abs_dir, run)
            self.store.save(slug, fresh_board)

        level = "OK" if validation.ok else "ERROR"
        run.log.append(
            {"level": level, "text": f"{validation.verdict}: {validation.reason or 'all checks passed'}"}
        )

    # ------------------------------------------------------------------ #
    # after the render
    # ------------------------------------------------------------------ #

    def _relay_speech(self, shot: dict, shot_dir: Path, run: ShotRun) -> None:
        """Put an already-spoken line back onto the clip that just rendered.

        Speech is synthesised outside the render path, and usually *before* the
        render — hearing whether a cloned voice says the line right should not
        cost half an hour of video first. But that means the mux had no clip to
        write into at the time, so without this the line exists as a wav next
        to a silent clip and never reaches the cut. Exactly what happened to
        the one shot in this board that has dialogue.

        Only ffmpeg runs here; nothing is re-synthesised.
        """
        line = (shot.get("dialogue") or "").strip()
        if not line:
            return
        speech = shot_dir / "dialogue.wav"
        if not (speech.exists() and speech.stat().st_size > 1024):
            return
        # A line edited after it was spoken must not be muxed from the old
        # take: the wav says something the board no longer does.
        spoken = (shot.get("dialogueSpokenText") or "").strip()
        if spoken and spoken != line:
            run.log.append(
                {
                    "level": "WARN",
                    "text": "the dialogue was edited after it was last spoken, "
                            "so it was not mixed onto the clip — press "
                            "Generate again",
                }
            )
            return
        style = (shot.get("dialogueStyle") or "").strip()
        spoken_style = (shot.get("dialogueSpokenStyle") or "").strip()
        if spoken and spoken_style != style:
            run.log.append(
                {
                    "level": "WARN",
                    "text": "the voice direction changed after the line was "
                            "last spoken, so it was not mixed onto the clip "
                            "— press Generate again",
                }
            )
            return

        clip = shot_dir / "clip.mp4"
        if not clip.exists():
            return
        try:
            out, error, log, warning = mux_speech(
                clip=clip, speech=speech, shot_dir=shot_dir,
                keep_original_audio=shot.get("dubMode") != "replace",
            )
        except Exception as exc:  # noqa: BLE001 - a failed mux is not a failed render
            run.log.append({"level": "WARN", "text": f"could not mix in the spoken line: {exc}"})
            return

        for line_text in log:
            run.log.append({"level": "INFO", "text": line_text})
        if error:
            run.log.append({"level": "WARN", "text": f"spoken line not mixed in: {error}"})
            return
        if warning:
            run.log.append({"level": "WARN", "text": warning})
        if out is None:
            return
        shot["dubUrl"] = self._as_url(out)
        shot["dubAppliedMode"] = shot.get("dubMode", "mix")
        run.log.append(
            {"level": "OK", "text": f"spoken line mixed onto the clip -> {out.name}"}
        )

    def _assemble(self, slug: str) -> None:
        """Concatenate the board's clips into one video.

        Runs at the end of a whole-board batch. Recorded on the board and in
        the batch status either way: a run that renders everything and then
        silently fails to produce the deliverable is the failure this whole
        module exists to prevent.
        """
        board = self.store.load(slug)
        shots = board.get("shots") or []
        if not shots:
            self._set_assembly("skipped", "this storyboard has no shots")
            return

        parts: list[tuple[str, Path | None]] = []
        for i, shot in enumerate(shots):
            shot_dir = self.data_dir / self.store.shot_rel_dir(slug, i + 1)
            label = shot.get("title") or f"shot {i + 1}"
            parts.append((f"{i + 1:02d} {label}", assembly.shot_clip(shot_dir, shot)))

        width, height = assembly.frame_size(
            (board.get("defaults") or {}).get("resolution")
        )
        project_dir = self.store.project_dir(slug)
        self._set_assembly("running", f"joining {len(shots)} shot(s)")

        result = assembly.assemble(
            parts, assembly.final_path(project_dir), width, height,
            options=assembly.board_options(board, project_dir, self.data_dir)
        )
        if not result.ok:
            self._set_assembly("failed", result.error, log=result.log)
            return

        url = self._as_url(result.path)
        # Re-read rather than reuse the copy loaded at the top of this
        # method: assembling can take a while for a long board, and saving
        # that stale copy would discard any edit made anywhere on the board
        # while the clips were being joined.
        fresh_board = self.store.load(slug)
        fresh_board["finalVideo"] = result.to_json(url)
        self.store.save(slug, fresh_board)

        message = (
            f"{len(result.parts)} clip(s), {result.seconds:.1f}s"
            + (
                " — incomplete, missing: " + ", ".join(result.missing)
                if result.missing
                else ""
            )
        )
        self._set_assembly(
            "partial" if result.partial else "done",
            message,
            url=url,
            log=result.log,
        )

    def _set_assembly(self, state: str, message: str, url: str = "",
                      log: list[str] | None = None) -> None:
        with self._lock:
            self._assembly = {
                "state": state,
                "message": message,
                "url": url,
                "log": list(log or []),
                "at": time.time(),
            }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _resolve_chain(self, shot: dict, shots: list[dict], slug: str) -> str | None:
        """Turn a ``chain`` reference into a real path, or explain why not.

        Returns None when the shot is good to run.
        """
        for key, label in (("startRef", "start"), ("endRef", "end"), ("continuityRef", "continuity")):
            ref = shot.get(key)
            if not isinstance(ref, dict) or ref.get("kind") != "chain":
                continue
            src_id = ref.get("from")
            src_idx = next((i for i, s in enumerate(shots) if s["id"] == src_id), None)
            if src_idx is None:
                return f"{label} frame chains from a shot that no longer exists"

            if src_idx >= next(i for i, s in enumerate(shots) if s["id"] == shot["id"]):
                return "Continuity sources must be earlier scenes; forward links and cycles are not supported"
            source_run = self._runs.get(src_id)
            if source_run and source_run.status != "done":
                return f"{label} source has not completed successfully ({source_run.status})"
            if shots[src_idx].get("status") in {"failed", "blocked", "interrupted", "review"}:
                return f"{label} source needs a successful render first"
            frames_dir = (
                self.data_dir / self.store.shot_rel_dir(slug, src_idx + 1) / "frames"
            )
            frame = last_saved_frame(
                frames_dir, int(shots[src_idx].get("frames") or 0)
            )
            if frame is None:
                src_title = shots[src_idx].get("title") or f"shot {src_idx + 1}"
                return (
                    f"{label} frame chains from “{src_title}”, which has no "
                    f"rendered frames yet"
                )
            # last frame of the upstream clip
            ref["resolved"] = str(frame.relative_to(self.data_dir))
            ref["contentHash"] = hashlib.sha256(frame.read_bytes()).hexdigest()
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

    def _clear_scene_stills(self, shot_dir: Path) -> int:
        """Remove generated still outputs for one scene before its render."""
        removed = 0
        for directory in (shot_dir / "frames", shot_dir / "stills"):
            if not directory.exists():
                continue
            try:
                # Keep the root itself: vpipe's SaveImage stage expects the
                # prepared frames directory to be present when it starts.
                for item in list(directory.iterdir()):
                    if item.is_dir() and not item.is_symlink():
                        removed += sum(
                            1 for child in item.rglob("*") if child.is_file()
                        )
                        shutil.rmtree(item)
                    else:
                        removed += int(item.is_file())
                        item.unlink()
            except OSError:
                # Cleanup is best effort. The render's validation remains the
                # authority if a file is locked or disappears mid-cleanup.
                continue
        return removed

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
            rel = Path(p).relative_to(self.data_dir)
        except ValueError:
            return str(p)
        url = "/media/" + str(rel).replace("\\", "/")
        # A re-render overwrites clip.mp4 (and the thumb frame, dub, log...)
        # at the SAME path every time, and media is served with Cache-Control
        # max-age=60 (_send_file). Without something in the URL that changes
        # when the file's bytes do, both the browser's cache and the front
        # end's own <video>/<img> reuse (renderPreview's `reuse()`, keyed on
        # the src string) keep showing the take from before this render —
        # the file on disk is right, only the tab is stale. mtime is exactly
        # "did the bytes change", so it is the version, not a random cache
        # buster: two renders that raced to the same mtime second would
        # collide, but that only means one extra stale second, not a wrong
        # file.
        try:
            url = f"{url}?t={int(Path(p).stat().st_mtime)}"
        except OSError:
            pass
        return url

    def _mark_remaining_cancelled(self) -> None:
        board = self.store.load(self._slug) if self._slug else None
        for sid, run in self._runs.items():
            if run.status == "queued":
                run.status, run.reason = "interrupted", "Stopped before this scene began"
                if board:
                    shot = next((s for s in board["shots"] if s["id"] == sid), None)
                    if shot:
                        shot.update(status="interrupted", reason=run.reason)
        if board:
            self.store.save(self._slug, board)


def _any_output(shots: list[dict]) -> bool:
    return any(sh.get("outputs") for sh in shots)
