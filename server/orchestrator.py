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

import copy
import hashlib
import json
import random
import threading
import time
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import assemble as assembly
from . import refine as refining
from .backends.base import Backend, ProgressEvent, ShotPaths
from .dubbing import mux_speech
from .render_timings import RenderTimings
from .store import (
    Store, artifact_filename, default_shot, last_saved_frame, render_fingerprint, slugify,
    stale_reason,
)

# statuses that a "render all" should pick up
RERUNNABLE = {"draft", "failed", "blocked", "review", "interrupted"}
MAX_LOG_LINES = 400


class _StampedLog(list):
    """A run's live log that stamps each entry with the server's wall clock
    as it is appended, so every one of the many call sites that add a line
    gets a time without each having to remember to."""

    def append(self, entry):  # type: ignore[override]
        if isinstance(entry, dict) and "time" not in entry:
            entry = {**entry, "time": time.strftime("%H:%M:%S")}
        super().append(entry)


@dataclass
class ShotRun:
    """Live state for one shot in the current batch."""

    shot_id: str
    status: str = "queued"
    progress: float = 0.0
    phase: str = ""
    eta_seconds: float | None = None
    # When the engine last said anything, and where the current phase stood
    # at its last progress report (see ProgressEvent.phase_percent).
    last_output_at: float | None = None
    phase_started_at: float | None = None
    phase_reported_at: float | None = None
    phase_percent: float | None = None
    log: list[dict[str, str]] = field(default_factory=_StampedLog)
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
            "lastOutputAt": self.last_output_at,
            "phaseStartedAt": self.phase_started_at,
            "phaseReportedAt": self.phase_reported_at,
            "phasePercent": self.phase_percent,
            # The server's clock, so the browser (often another machine)
            # measures "how long ago" against the same clock as the above.
            "serverTime": time.time(),
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


def _progress_handler(run: ShotRun) -> Callable[[ProgressEvent], None]:
    """Fold a backend's progress events into *run*'s live state."""
    def on_event(ev: ProgressEvent) -> None:
        if ev.percent:
            run.progress = max(run.progress, ev.percent)
        now = time.time()
        if ev.phase and ev.phase_percent is not None:
            if ev.phase != run.phase or run.phase_started_at is None:
                run.phase_started_at = now
            run.phase_reported_at = now
            run.phase_percent = ev.phase_percent
        if ev.phase:
            run.phase = ev.phase
        if ev.eta_seconds is not None:
            run.eta_seconds = ev.eta_seconds
        if ev.log_line:
            run.last_output_at = now
            run.log.append({"level": ev.log_level, "text": ev.log_line})
            if len(run.log) > MAX_LOG_LINES * 2:
                del run.log[:-MAX_LOG_LINES]
    return on_event


SEED_TAKES_DIR = "seed-takes"
SEED_TAKE_FILE = "take.json"
MAX_SEED_TAKES = 8
REFINE_DIR = "refine-runs"      # per shot; each run replaces the last
REFINE_FILE = "run.json"


class Orchestrator:
    def __init__(self, backend: Backend, store: Store, workspace: Path,
                 data_dir: Path | None = None,
                 timings: RenderTimings | None = None):
        self.prepare_dialogue = None
        # Set by the app: (slug) -> note. Generates the soundtrack before the
        # cut is joined, once every shot has its clip.
        self.prepare_soundtrack = None
        # This machine's measured render times -- the only source of an
        # expected duration (see render_timings.py).
        self.timings = timings
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

        # "Create Stills" — a separate, much shorter job (one still, not a
        # shot render), but it still shares the one GPU the renders use, so
        # it gets its own thread and its own small piece of status rather than
        # reusing _runs/_order, which are shaped around a whole render batch.
        self._stills_thread: threading.Thread | None = None
        self._stills_shot_id: str | None = None
        self._stills_kind = "still"   # "still" (Create Image) or "reference"
        # Shot ids are only unique within a board, and the phone view has to
        # say which project the stills belong to.
        self._stills_slug: str | None = None
        self._stills_phase: str = ""
        self._stills_progress: float = 0.0
        self._stills_error: str = ""
        self._stills_log: list[dict[str, str]] = _StampedLog()
        # Filled in as the still finishes, before the job wraps up.
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

        # Seed comparison — one shot rendered at several seeds into takes
        # beside it, never over its own clip (see start_seed_sweep).
        self._seed_sweep: dict[str, Any] | None = None

        # Auto-refine — draft one shot, have the AD's model review the clip,
        # adjust the prompt, repeat (see start_refine). Like a seed sweep it
        # renders beside the shot and never touches the board.
        self._refine: dict[str, Any] | None = None

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

    def touches(self, slug: str, shot_id: str) -> bool:
        """Is a running job working on, or queued to work on, this shot?"""
        with self._lock:
            if self.stills_busy and self._stills_slug == slug and self._stills_shot_id == shot_id:
                return True
            if not self.busy or self._slug != slug:
                return False
            if self._seed_sweep and self._seed_sweep.get("shotId") == shot_id:
                return True
            if self._refine and self._refine.get("shotId") == shot_id:
                return True
            # Queued counts too: queuing already cleared its clip.
            return self._current == shot_id or shot_id in self._runs

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
                "seedSweep": dict(self._seed_sweep) if self._seed_sweep else None,
                "refine": copy.deepcopy(self._refine) if self._refine else None,
                "stills": {
                    "busy": self.stills_busy,
                    "kind": self._stills_kind,
                    "shotId": self._stills_shot_id,
                    "slug": self._stills_slug,
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
        self._seed_sweep = None
        self._slug = slug
        self._order = [s["id"] for s in board["shots"]
                       if (s.get("dialogue") or "").strip() and not s.get("locked")]
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
        self._seed_sweep = None
        board = self.store.load(slug)
        shots = board.get("shots") or []
        # An explicit list is exactly that — the user asked for these shots.
        # No list means the whole board, which also means assembling the cut.
        index = {s["id"]: i for i, s in enumerate(shots)}
        for i, scene in enumerate(shots):
            for key in ("startRef", "endRef"):
                ref = scene.get(key)
                if isinstance(ref, dict) and ref.get("kind") == "chain":
                    if ref.get("from") not in index or index[ref["from"]] >= i:
                        raise ValueError("A scene can only continue from an existing earlier scene; fix missing, forward or cyclic links")
        whole_board = not shot_ids
        if shot_ids:
            wanted = set(shot_ids)
            asked = [s for s in shots if s["id"] in wanted]
            targets = [s for s in asked if not s.get("locked")]
            if asked and not targets:
                raise RuntimeError(
                    "that shot is locked — unlock it to render it again"
                    if len(asked) == 1 else "those shots are all locked — unlock them to render them again"
                )
            because = {s["id"]: "asked for by name" for s in targets}
        else:
            targets, because = self._pending(board)
        # Include stale or missing prerequisites for selected renders.
        wanted = {s["id"] for s in targets}
        for scene in reversed(shots):
            if scene["id"] not in wanted:
                continue
            for key in ("startRef", "endRef"):
                ref = scene.get(key)
                if not isinstance(ref, dict) or ref.get("kind") != "chain":
                    continue
                source = shots[index[ref["from"]]]
                if source.get("locked"):
                    # Its frames are what they are; the chain gate in
                    # _run_one blocks the dependent if there are none.
                    continue
                frame_dir = self.data_dir / self.store.shot_rel_dir(slug, index[source["id"]] + 1) / "frames"
                if source.get("status") != "done" or stale_reason(source, board) or not last_saved_frame(frame_dir, int(source.get("frames") or 0)):
                    wanted.add(source["id"])
                    because.setdefault(source["id"], "required by a dependent scene")
        # Never render into a folder holding a locked shot's clip (folders are
        # numbered by position, so a moved board can line one up that way).
        locked_dirs = self.store.locked_dirs(slug, board)
        clashes = {s["id"] for i, s in enumerate(shots)
                   if s["id"] in wanted
                   and locked_dirs.get(self.store.shot_rel_dir(slug, i + 1), s["id"]) != s["id"]}
        if clashes and not whole_board and clashes == wanted:
            raise RuntimeError(
                "that shot's folder holds a locked shot's clip — move the shots "
                "back, or unlock the locked one, before rendering it"
            )
        wanted -= clashes
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
            if shot.get("locked"):
                continue
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
                if shot["id"] in because or shot.get("locked"):
                    continue
                for key in ("startRef", "endRef"):
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
    # stills ("Create Stills" — a single preview image, not a shot render)
    # ------------------------------------------------------------------ #

    # One still: the opening composition, which is also the frame most useful
    # fed back in as a Start frame. (It used to be start/mid/end, then
    # start/end chained by img2img; a single preview is all that is needed.)
    STILL_PHASES = (
        ("start", "The very start of this shot, before the described action "
                  "gets underway — the opening pose and composition"),
    )

    def create_stills(
        self, slug: str, shot_id: str, phase_prompts: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("stills are already generating for this board")

        board = self.store.load(slug)
        shot = next((s for s in board.get("shots") or [] if s["id"] == shot_id), None)
        if shot is None:
            raise RuntimeError("shot not found")
        if shot.get("locked"):
            raise RuntimeError("this shot is locked — unlock it to create a new image")
        if not (shot.get("prompt") or "").strip():
            raise RuntimeError("nothing to sketch — this shot has no prompt yet")

        self._check_still_engine(board)
        return self._start_stills_job(slug, shot_id, "still", self._run_stills,
                                      (slug, shot_id, phase_prompts or {}))

    def _check_still_engine(self, board: dict) -> str:
        still_model = self._still_model(board)
        cap = self.backend.capability(still_model)
        if cap is None:
            raise RuntimeError(f"unknown still engine: {still_model}")
        if not cap.available:
            raise RuntimeError(cap.unavailable_reason or f"{still_model} is not available")
        mflux = getattr(self.backend, "mflux", None)
        if not (mflux and mflux.handles(still_model)):
            ok, msg = self.backend.health()
            if not ok:
                raise RuntimeError(msg)
        return still_model

    def _start_stills_job(self, slug: str, shot_id: str | None, kind: str,
                          target: Callable[..., None], args: tuple) -> dict[str, Any]:
        with self._lock:
            self._cancel.clear()
            self._stills_kind = kind
            self._stills_shot_id = shot_id
            self._stills_slug = slug
            self._stills_phase = "starting"
            self._stills_progress = 0.0
            self._stills_error = ""
            self._stills_log = _StampedLog()
            self._stills_results = {}

        self._stills_thread = threading.Thread(
            target=target, args=args, name=f"create-{kind}", daemon=True,
        )
        self._stills_thread.start()
        return self.status()

    # ------------------------------------------------------------------ #
    # reference images made from a description
    # ------------------------------------------------------------------ #

    #: What each kind of generated reference shows, and what it must not. A
    #: prop or a place must have nobody in it: Ref2VA uses every picture it
    #: is given, so a stray face on a headset becomes a face in the scene.
    REFERENCE_KINDS = {
        "character": ("portrait",
                      "Character reference portrait. {who}. The whole figure in view, "
                      "standing, centred and facing the camera, against a plain neutral "
                      "studio background with clear even lighting. One person only, no "
                      "scenery, no props beyond what they wear or carry, no text."),
        "prop": ("prop",
                 "Product reference photograph of an object: {who}. The object alone, "
                 "isolated, centred and entirely in view, on a plain neutral studio "
                 "background with clear even lighting. No people, no person wearing "
                 "or holding it, no face, no head, no hands, no mannequin, no body "
                 "parts, no text."),
        "location": ("location",
                     "Location reference: {who}. An empty establishing view of the "
                     "place, showing its layout, materials and lighting. No people, no "
                     "characters, no figures, no text."),
    }

    def create_character_image(self, slug: str, name: str, description: str,
                               kind: str = "character") -> dict[str, Any]:
        """A reference image made from a description: a cast member's
        portrait, a prop on its own, or an empty location.

        The still engine renders the description in the board's render style
        and the image is filed in the project's refs/ like an upload; the
        caller (Cast editor, or a scene's Reference images) puts it to use
        from status ``results.image``. Takes text, not an id, so it works
        for a character not saved yet.
        """
        if kind not in self.REFERENCE_KINDS:
            raise ValueError(f"unknown kind of reference image: {kind}")
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("an image is already generating — one GPU job at a time")
        name, description = (name or "").strip(), (description or "").strip()
        if not description:
            raise ValueError("write a description first — the image is made from it")
        board = self.store.load(slug)
        self._check_still_engine(board)
        return self._start_stills_job(slug, None, "reference", self._run_character_image,
                                      (slug, name, description, kind))

    def _run_character_image(self, slug: str, name: str, description: str,
                             kind: str = "character") -> None:
        try:
            board = self.store.load(slug)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            work_rel = f"{slugify(slug)}/reference-images/{stamp}"
            work_abs = self.data_dir / work_rel
            work_abs.mkdir(parents=True, exist_ok=True)

            description = description.rstrip(" .")
            who = f"{name}: {description}" if name else description
            suffix, template = self.REFERENCE_KINDS[kind]
            prompt = template.format(who=who)
            # Only the subject and the look: the scene description would put
            # a location (and its people) around it.
            project = dict(board)
            project["sceneDescription"] = ""
            project["soundscape"] = ""
            project["defaults"] = {**(board.get("defaults") or {}), "draft": False, "sketch": False}
            # A standing figure wants a tall frame: the project's size, turned
            # to portrait if it is landscape. Props and places keep its shape.
            w, _, h = str(project["defaults"].get("resolution") or "960x544").partition("x")
            if kind == "character" and w.isdigit() and h.isdigit() and int(w) > int(h):
                project["defaults"]["resolution"] = f"{h}x{w}"
            still_model = self._still_model(board)
            steps, seed = still_params(board.get("defaults") or {}, True)
            shot = default_shot(board.get("defaults"))
            shot.update(id=f"character-{stamp}", prompt=prompt, model=still_model,
                        steps=steps, _fixedSteps=True, seed=seed)
            paths = ShotPaths(workspace=self.workspace, abs_dir=work_abs, rel_dir=work_rel,
                              data_dir=self.data_dir)
            with self._lock:
                self._stills_phase = "image"
                self._stills_progress = 10.0
            spec = self.backend.prepare(shot, project, paths)

            def on_event(ev: ProgressEvent) -> None:
                if ev.log_line:
                    with self._lock:
                        self._stills_log.append({"level": ev.log_level, "text": f"[{kind}] {ev.log_line}"})

            result = self.backend.run(spec, on_event, self._cancel.is_set)
            if result.cancelled or self._cancel.is_set():
                raise RuntimeError("stopped before completion")
            if result.error or result.exit_code != 0:
                raise RuntimeError(
                    result.error or f"{still_model} exited {result.exit_code}: "
                    + next((t for lvl, t in reversed(result.log) if lvl == "ERROR"), "")
                )
            out = next((p for p in spec.expected_outputs if p.exists()), None)
            if out is None:
                raise RuntimeError("the image engine did not produce an image")

            # Filed in refs/ like an upload, so the picker offers it too.
            refs = self.store.refs_dir(slug)
            refs.mkdir(parents=True, exist_ok=True)
            base = slugify(name or description)[:40] or kind
            dest = refs / f"{base}-{suffix}{out.suffix}"
            n = 2
            while dest.exists():
                dest = refs / f"{base}-{suffix}-{n}{out.suffix}"
                n += 1
            shutil.copy2(out, dest)
            rel = str(dest.relative_to(self.data_dir)).replace("\\", "/")
            with self._lock:
                self._stills_results = {"image": {
                    "kind": "upload", "path": rel, "url": "/media/" + rel, "label": dest.name,
                }}
                self._stills_progress = 100.0
                self._stills_phase = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            with self._lock:
                self._stills_error = str(exc)

    def _still_model(self, board: dict) -> str:
        """The board's still engine; "auto" is the first available image
        model, vpipe's Krea-2 ahead of the mflux engines."""
        choice = (board.get("defaults") or {}).get("stillsEngine") or "auto"
        if choice != "auto":
            return choice
        images = [c for c in self.backend.capabilities() if c.kind == "image"]
        available = next((c.id for c in images if c.available), None)
        return available or "krea2-still"

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
            # as a reference image. Create Image's style follows the global
            # Render Style unless its still-only style override is selected.
            large = ((board.get("defaults") or {}).get("stillsSize") or "small") == "large"
            still_project = dict(board)
            still_style = ((board.get("defaults") or {}).get("stillsStyle") or "global")
            if still_style in ("colored-pencil", "pencil"):
                # This override is local to the synthetic still project; the
                # saved board style continues to drive all video renders.
                still_project["renderStyle"] = (
                    "Black-and-white pencil storyboard sketch on lightly textured "
                    "white paper. Visible graphite pencil strokes, hand-drawn "
                    "contours, loose readable linework, and soft graphite shading. "
                    "Monochrome only, with no colour. Clearly illustrated, not a "
                    "photograph or photorealistic render."
                    if still_style == "pencil" else
                    "Coloured-pencil storyboard illustration on lightly textured "
                    "off-white paper. Visible layered coloured-pencil strokes, "
                    "hand-drawn graphite contours, loose readable linework, and "
                    "soft coloured-pencil shading. Clearly illustrated, not a "
                    "photograph or photorealistic render."
                )
            still_project["defaults"] = {
                **(board.get("defaults") or {}), "draft": not large, "sketch": False,
            }

            still_model = self._still_model(board)
            steps, seed = still_params(board.get("defaults") or {}, large)
            for i, (key, phase_hint) in enumerate(self.STILL_PHASES):
                if self._cancel.is_set():
                    raise RuntimeError("stopped before completion")
                with self._lock:
                    self._stills_phase = key
                    self._stills_progress = (i / len(self.STILL_PHASES)) * 100

                # A synthetic shot, not a real one: the board's still engine
                # regardless of what this shot is set to render as, since
                # stills are a fast preview, not the shot's own model. Scene,
                # cast and any style refs still come from _resolved_prompt.
                still_shot = dict(shot)
                still_shot["model"] = still_model
                still_shot["steps"] = steps
                # An explicit step count survives the draft geometry that
                # Small otherwise applies (which caps steps at 8).
                still_shot["_fixedSteps"] = True
                still_shot["seed"] = seed
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
                        result.error
                        or f"{still_model} exited {result.exit_code}: "
                        + next((t for lvl, t in reversed(result.log) if lvl == "ERROR"), "")
                    )
                out = next((p for p in spec.expected_outputs if p.exists()), None)
                if out is None:
                    raise RuntimeError(f"{key} still did not produce an image")
                try:
                    named_out = self._publish_named_artifact(out, board, idx + 1)
                except OSError as exc:
                    named_out = out
                    with self._lock:
                        self._stills_log.append({
                            "level": "WARN", "text": f"could not create named still file: {exc}"
                        })
                results[key] = {"url": self._as_url(named_out), "seed": seed, "steps": steps}

                # Save and publish the result as soon as it exists, so a
                # cancel or crash afterwards still leaves it in place.
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
            speech_ids = ([s["id"] for s in board["shots"] if not s.get("locked")]
                          if assemble_after else list(self._order))
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

        # ---- lock gate -------------------------------------------------
        # Locked after the batch was queued, or the board moved so this
        # shot's folder now holds a locked shot's clip.
        owner = self.store.locked_dirs(slug, board).get(self.store.shot_rel_dir(slug, idx + 1))
        if shot.get("locked") or (owner and owner != shot_id):
            run.status = "cancelled"
            run.reason = ("locked — not rendered" if shot.get("locked")
                          else "its folder holds a locked shot's clip — not rendered")
            run.log.append({"level": "INFO", "text": run.reason})
            return

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
        spec.expected_seconds = self._expected_seconds(spec) or 0.0

        # A render owns the generated stills for this scene. Clear both the
        # frame dump used for chaining and the optional start/end preview
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
        result = self.backend.run(spec, _progress_handler(run), self._cancel.is_set)
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
        # A run that produced its outputs is a real measurement, whatever a
        # soft check made of it -- "review" included.
        if self.timings is not None and validation.verdict in ("done", "review"):
            g = self._geometry(spec)
            if g:
                self.timings.record(**g, seconds=result.seconds)
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
        published_outputs = []
        for output in outputs:
            try:
                published_outputs.append(self._publish_named_artifact(output, board, idx + 1))
            except OSError as exc:
                published_outputs.append(output)
                run.log.append({"level": "WARN", "text": f"could not create named output file: {exc}"})
        outputs = published_outputs
        run.outputs = [self._as_url(p) for p in outputs]

        # Computed from *this run's own* board and shot — the snapshot it was
        # actually rendered against, never a freshly re-read copy. The whole
        # point of a fingerprint is catching a clip that no longer matches
        # what the board currently says; computing it from a post-render
        # reload would make an edit made *during* the render look like it was
        # already accounted for, and hide exactly the staleness this exists
        # to catch.
        fingerprint = render_fingerprint(shot, board)

        # True when this render asked H3 to speak the dialogue itself --
        # cloned from a reference clip, or in a voice H3 judged fits the
        # scene when no clip was available (see vpipe_backend.py's
        # _speaks_line_aloud) -- instead of staying silent for TTS to dub in
        # afterwards. Relaying a leftover TTS take onto a clip that already
        # speaks the line is exactly the "second voice" bug that feature
        # exists to remove — so here it must not run, no matter how old a
        # dialogue.wav is sitting in the shot's folder from before that shot
        # ever spoke its own line natively.
        native_dialogue_spoken = bool(spec.payload.get("nativeDialogueSpoken"))

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
                renderedDialogueSource="native" if native_dialogue_spoken else "recording",
                # What actually ran -- the engine and Turbo can change the
                # step count from the shot's own, and both can be switched
                # after the render, so the clip's summary reads this.
                renderedWith={
                    "engine": spec.payload.get("engine") or getattr(self.backend, "id", ""),
                    "steps": spec.payload.get("steps"),
                    "turbo": bool(spec.payload.get("turbo")),
                    # The clip's own geometry: a locked shot reports these
                    # rather than the project's current size (see
                    # render_record.py). Frames is the clip's length, which
                    # a sketch reaches by holding frames.
                    "width": spec.payload.get("width"),
                    "height": spec.payload.get("height"),
                    "frames": fresh_shot.get("frames"),
                },
            )
            if native_dialogue_spoken:
                # A dub from before this shot spoke its own line natively
                # would otherwise keep winning in the preview (it is
                # preferred over the shot's own outputs) and look like the
                # fresh render is still doubled, when the clip itself is
                # clean. The old dialogue.wav / clip-dubbed.mp4 files are
                # left on disk — harmless, just no longer referenced.
                fresh_shot["dubUrl"] = None
            elif validation.ok:
                self._relay_speech(fresh_shot, paths.abs_dir, run)
            self.store.save(slug, fresh_board)

        level = "OK" if validation.ok else "ERROR"
        run.log.append(
            {"level": level, "text": f"{validation.verdict}: {validation.reason or 'all checks passed'}"}
        )

    # ------------------------------------------------------------------ #
    # seed comparison
    # ------------------------------------------------------------------ #

    def seed_takes_dir(self, slug: str, shot_id: str) -> Path:
        """Where one shot's seed takes live: keyed by shot id, not position,
        so reordering the board never hands a shot another shot's takes."""
        if not shot_id or "/" in shot_id or shot_id.startswith("."):
            raise ValueError("bad shot id")
        return self.store.project_dir(slug) / SEED_TAKES_DIR / shot_id

    def _read_take(self, take_dir: Path) -> dict[str, Any] | None:
        try:
            return json.loads((take_dir / SEED_TAKE_FILE).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def start_seed_sweep(self, slug: str, shot_id: str, count: int = 4,
                         seeds: list[int] | None = None) -> dict[str, Any]:
        """Render one shot at several seeds, each into its own take folder.

        Everything but the seed stays as the board says, so the takes differ
        only by the starting noise — which is what makes them comparable.
        None of it touches the shot's own clip, frames or status; a take
        becomes the shot's clip only when picked (``adopt_seed_take``).
        Unless *seeds* are given, picks the lowest seeds that are neither the
        shot's current one nor already a take.
        """
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("still previews are generating — one GPU job at a time")
        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)
        board = self.store.load(slug)
        shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
        if shot is None:
            raise ValueError("no such shot")
        if shot.get("locked"):
            raise RuntimeError("this shot is locked — unlock it to try other seeds")
        if seeds:
            seeds = list(dict.fromkeys(int(n) for n in seeds if 0 <= int(n) < 2**31))
        else:
            count = max(1, min(int(count or 0), MAX_SEED_TAKES))
            taken = {int(shot.get("seed") or 0)}
            root = self.seed_takes_dir(slug, shot_id)
            if root.is_dir():
                taken |= {int(t["seed"]) for d in root.iterdir()
                          if (t := self._read_take(d)) and "seed" in t}
            seeds, n = [], 0
            while len(seeds) < count:
                if n not in taken:
                    seeds.append(n)
                n += 1
        seeds = seeds[:MAX_SEED_TAKES]
        if not seeds:
            raise ValueError("no seeds to render")

        self._cancel.clear()
        self._operation = "seeds"
        self._slug = slug
        self._order = [f"{shot_id}:seed:{n}" for n in seeds]
        self._runs = {rid: ShotRun(shot_id=rid) for rid in self._order}
        self._seed_sweep = {"shotId": shot_id, "seeds": seeds, "slug": slug}
        self._error = ""
        self._assembly = None
        self._queued_because = {rid: f"seed comparison — seed {n}" for rid, n in zip(self._order, seeds)}
        self._batch_started = time.time()
        self._batch_ended = None

        def work() -> None:
            try:
                for rid, n in zip(self._order, seeds):
                    if self._cancel.is_set():
                        break
                    self._run_seed_take(slug, shot_id, n, self._runs[rid])
                for run in self._runs.values():
                    if run.status == "queued":
                        run.status, run.reason = "interrupted", "Stopped before this seed began"
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._current = None
                    self._batch_ended = time.time()

        self._thread = threading.Thread(target=work, name="seed-sweep", daemon=True)
        self._thread.start()
        return self.status()

    def _run_seed_take(self, slug: str, shot_id: str, seed: int, run: ShotRun) -> None:
        board = self.store.load(slug)
        shots = board["shots"]
        idx = next((i for i, s in enumerate(shots) if s["id"] == shot_id), None)
        if idx is None:
            run.status, run.reason = "failed", "the shot was deleted"
            return
        # A copy: the board's own shot keeps its seed and its chain refs as
        # they were — only this take renders with the new seed.
        shot = copy.deepcopy(shots[idx])
        shot["seed"] = seed
        with self._lock:
            self._current = run.shot_id
        problem = self._resolve_chain(shot, shots, slug)
        if problem:
            run.status, run.reason = "blocked", problem
            run.log.append({"level": "WARN", "text": problem})
            return

        take_dir = self.seed_takes_dir(slug, shot_id) / f"seed-{seed}"
        if take_dir.exists():
            shutil.rmtree(take_dir)
        take_dir.mkdir(parents=True)
        # A dubbed line's take lives with the shot; the take needs its own
        # copy for the render to find it.
        shot_dir = self.data_dir / self.store.shot_rel_dir(slug, idx + 1)
        if (shot_dir / "dialogue.wav").is_file():
            shutil.copy2(shot_dir / "dialogue.wav", take_dir / "dialogue.wav")
        paths = ShotPaths(
            workspace=self.workspace,
            abs_dir=take_dir,
            rel_dir=str(take_dir.relative_to(self.data_dir)),
            data_dir=self.data_dir,
        )
        try:
            spec = self.backend.prepare(shot, board, paths)
        except Exception as exc:  # noqa: BLE001
            run.status, run.reason = "failed", f"could not prepare: {exc}"
            run.log.append({"level": "ERROR", "text": run.reason})
            return
        spec.expected_seconds = self._expected_seconds(spec) or 0.0
        spec.started_at = time.time()
        run.summary = f"seed {seed} · {spec.summary}"
        run.status = "running"
        run.started_at = time.time()
        result = self.backend.run(spec, _progress_handler(run), self._cancel.is_set)
        run.ended_at = time.time()

        if result.cancelled or self._cancel.is_set():
            run.status, run.reason = "interrupted", "Stopped before completion."
            shutil.rmtree(take_dir, ignore_errors=True)
            return
        validation = self.backend.validate(spec, result)
        if self.timings is not None and validation.verdict in ("done", "review"):
            g = self._geometry(spec)
            if g:
                self.timings.record(**g, seconds=result.seconds)
        run.validation = validation.to_json()
        run.status, run.reason = validation.verdict, validation.reason
        run.progress = 100.0 if validation.ok else run.progress
        self._write_log(take_dir, run, spec, result)
        run.outputs = [self._as_url(p) for p in spec.expected_outputs if p.exists()]
        take = {
            "shotId": shot_id,
            "seed": seed,
            "status": validation.verdict,
            "reason": validation.reason or "",
            "runtimeSeconds": round(result.seconds, 1),
            "renderedAt": time.time(),
            "renderedAs": "draft" if spec.payload.get("draft") else "final",
            "nativeDialogueSpoken": bool(spec.payload.get("nativeDialogueSpoken")),
            "fingerprint": render_fingerprint(shot, board),
            "validation": run.validation,
            "outputs": [p.name for p in spec.expected_outputs if p.exists()],
        }
        (take_dir / SEED_TAKE_FILE).write_text(json.dumps(take, indent=2) + "\n")
        run.log.append({"level": "OK" if validation.ok else "ERROR",
                        "text": f"seed {seed}: {validation.verdict}: {validation.reason or 'all checks passed'}"})

    def seed_takes(self, slug: str, shot_id: str) -> list[dict[str, Any]]:
        """This shot's finished seed takes, lowest seed first.

        ``current`` says whether a take still matches what the board says
        now, apart from its seed — a take of an older prompt is still worth
        seeing, but not worth mistaking for a comparison of this one.
        """
        board = self.store.load(slug)
        shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
        root = self.seed_takes_dir(slug, shot_id)
        if shot is None or not root.is_dir():
            return []
        out = []
        for d in root.iterdir():
            take = self._read_take(d)
            if not take or "seed" not in take:
                continue
            probe = dict(shot, seed=take["seed"])
            clip = next((d / n for n in take.get("outputs") or [] if (d / n).is_file()), None)
            frames = sorted((d / "frames").glob("*.png")) if (d / "frames").is_dir() else []
            out.append({
                **{k: take.get(k) for k in ("seed", "status", "reason", "runtimeSeconds",
                                            "renderedAt", "renderedAs")},
                "current": take.get("fingerprint") == render_fingerprint(probe, board),
                "clipUrl": self._as_url(clip) if clip else None,
                "thumbUrl": self._as_url(frames[len(frames) // 2]) if frames else None,
                "logUrl": self._as_url(d / "run.log") if (d / "run.log").is_file() else None,
            })
        return sorted(out, key=lambda t: t["seed"])

    def delete_seed_takes(self, slug: str, shot_id: str) -> int:
        if self.busy and (self._seed_sweep or {}).get("shotId") == shot_id:
            raise RuntimeError("this shot's seeds are still rendering")
        root = self.seed_takes_dir(slug, shot_id)
        n = sum(1 for d in root.iterdir() if d.is_dir()) if root.is_dir() else 0
        shutil.rmtree(root, ignore_errors=True)
        return n

    def adopt_seed_take(self, slug: str, shot_id: str, seed: int) -> dict[str, Any]:
        """Make one seed take the shot's own clip, as if it had rendered there.

        Copies its clip, frames and log into the shot's folder and sets the
        shot's seed, so the pick costs a file copy rather than another
        render. The take's fingerprint carries over: if the board has moved
        on since the take rendered, the shot shows as out of date, exactly
        as a normal render would. Shots chained from this one see a new last
        frame and go stale for the same reason.
        """
        if self.busy:
            raise RuntimeError("wait for the current render to finish")
        if any(s["id"] == shot_id and s.get("locked") for s in self.store.load(slug).get("shots") or []):
            raise RuntimeError("this shot is locked — unlock it to use another take")
        take_dir = self.seed_takes_dir(slug, shot_id) / f"seed-{int(seed)}"
        take = self._read_take(take_dir)
        if not take:
            raise ValueError(f"no take for seed {seed}")
        if take.get("status") not in ("done", "review"):
            raise ValueError(f"seed {seed} did not render successfully")
        clip = next((take_dir / n for n in take.get("outputs") or []
                     if (take_dir / n).suffix == ".mp4" and (take_dir / n).is_file()), None)
        if clip is None:
            raise ValueError(f"seed {seed}'s clip is missing")
        board = self.store.load(slug)
        idx = next((i for i, s in enumerate(board["shots"]) if s["id"] == shot_id), None)
        if idx is None:
            raise ValueError("no such shot")
        shot = board["shots"][idx]
        shot_dir = self.data_dir / self.store.shot_rel_dir(slug, idx + 1)
        shot_dir.mkdir(parents=True, exist_ok=True)
        self._clear_scene_stills(shot_dir)
        dest = shot_dir / "clip.mp4"
        shutil.copy2(clip, dest)
        frames = sorted((take_dir / "frames").glob("*.png")) if (take_dir / "frames").is_dir() else []
        if frames:
            (shot_dir / "frames").mkdir(exist_ok=True)
            for f in frames:
                shutil.copy2(f, shot_dir / "frames" / f.name)
        log_url = None
        if (take_dir / "run.log").is_file():
            shutil.copy2(take_dir / "run.log", shot_dir / "run.log")
            log_url = self._as_url(shot_dir / "run.log")
        named = self._publish_named_artifact(dest, board, idx + 1)
        copied = sorted((shot_dir / "frames").glob("*.png")) if frames else []
        native = bool(take.get("nativeDialogueSpoken"))
        shot.update(
            seed=int(take["seed"]),
            status=take["status"],
            reason=take.get("reason") or "",
            progress=100,
            runtimeSeconds=take.get("runtimeSeconds"),
            outputs=[self._as_url(named)],
            validation=take.get("validation"),
            thumb=self._as_url(copied[len(copied) // 2]) if copied else None,
            logUrl=log_url,
            stills={},
            renderedAs=take.get("renderedAs") or "final",
            renderFingerprint=take.get("fingerprint"),
            renderedDialogueSource="native" if native else "recording",
        )
        if native:
            shot["dubUrl"] = None
        else:
            self._relay_speech(shot, shot_dir, ShotRun(shot_id=shot_id))
        return self.store.save(slug, board)

    # ------------------------------------------------------------------ #
    # after the render
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # auto-refine: draft -> review -> adjust the prompt -> repeat
    # ------------------------------------------------------------------ #

    def refine_dir(self, slug: str, shot_id: str) -> Path:
        if not shot_id or "/" in shot_id or shot_id.startswith("."):
            raise ValueError("bad shot id")
        return self.store.project_dir(slug) / REFINE_DIR / shot_id

    def refine_result(self, slug: str, shot_id: str) -> dict[str, Any] | None:
        """The last finished refine run for this shot, or None."""
        try:
            return json.loads((self.refine_dir(slug, shot_id) / REFINE_FILE).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def start_refine(self, slug: str, shot_id: str, reviewer, *, requirement: str = "",
                     max_attempts: int = refining.DEFAULT_ATTEMPTS,
                     pass_percent: int = refining.DEFAULT_PASS_PERCENT,
                     draft: bool = True, checks: list[str] | None = None) -> dict[str, Any]:
        """Render a shot, review the clip, rewrite its prompt, and go again.

        Each attempt renders (a draft, unless *draft* is False) into its own
        folder beside the shot, then *reviewer* — the AD's model — answers a
        fixed checklist from stills of the clip. The loop stops when
        *pass_percent* of the checks are met, after *max_attempts* renders,
        when the model has nothing further to change, on Stop, or on an
        error. The board is never edited: the best prompt comes back as a
        proposal, and applying it is the user's call. *checks*, when given
        (the user's own, edited on the Start card), replace the checklist the
        model would have written.
        """
        if self.busy:
            raise RuntimeError("a render is already running")
        if self.stills_busy:
            raise RuntimeError("still previews are generating — one GPU job at a time")
        ok, msg = self.backend.health()
        if not ok:
            raise RuntimeError(msg)
        board = self.store.load(slug)
        idx = next((i for i, s in enumerate(board["shots"]) if s["id"] == shot_id), None)
        if idx is None:
            raise ValueError("no such shot")
        shot = board["shots"][idx]
        if shot.get("locked"):
            raise RuntimeError("this shot is locked — unlock it to refine it")
        if not (shot.get("prompt") or "").strip():
            raise RuntimeError("this shot has no prompt yet")
        max_attempts = max(1, min(int(max_attempts or 0), refining.MAX_ATTEMPTS))
        pass_percent = max(1, min(int(pass_percent or 0), 100))
        checks = [c.strip()[:240] for c in checks or [] if isinstance(c, str) and c.strip()]
        checks = checks[:refining.MAX_CHECKS]

        root = self.refine_dir(slug, shot_id)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)

        self._cancel.clear()
        self._operation = "refine"
        self._slug = slug
        self._order, self._runs, self._queued_because = [], {}, {}
        self._error = ""
        self._assembly = None
        self._batch_started = time.time()
        self._batch_ended = None
        self._refine = {
            "slug": slug, "shotId": shot_id, "number": idx + 1,
            "requirement": requirement, "maxAttempts": max_attempts,
            "passPercent": pass_percent, "draft": draft,
            "state": "starting",
            "phase": "Starting" if checks else "Writing the checklist",
            "checks": list(checks), "attempts": [], "stopReason": "", "stopDetail": "",
            "best": None, "summary": "", "proposal": None,
        }

        def note(**fields) -> None:
            with self._lock:
                self._refine.update(fields)

        def work() -> None:
            try:
                self._refine_loop(slug, shot_id, idx, reviewer, root, note,
                                  max_attempts, pass_percent, draft, checks)
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                note(stopReason="error", stopDetail=f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    self._finish_refine(root)
                finally:
                    with self._lock:
                        self._current = None
                        self._batch_ended = time.time()

        self._thread = threading.Thread(target=work, name="refine", daemon=True)
        self._thread.start()
        return self.status()

    def _refine_loop(self, slug, shot_id, idx, reviewer, root, note,
                     max_attempts, pass_percent, draft, checks) -> None:
        board = self.store.load(slug)
        shot = board["shots"][idx]
        if not checks:
            note(phase="Writing the checklist")
            checks = reviewer.checklist(shot, board)
            note(checks=checks)
        prompt = shot["prompt"]
        history: list[dict[str, Any]] = []
        for n in range(1, max_attempts + 1):
            if self._cancel.is_set():
                note(stopReason="stopped")
                return
            rid = f"{shot_id}:refine:{n}"
            run = ShotRun(shot_id=rid)
            with self._lock:
                self._runs[rid] = run
                self._order.append(rid)
                self._queued_because[rid] = f"auto-refine attempt {n} of {max_attempts}"
            attempt: dict[str, Any] = {"n": n, "prompt": prompt, "score": None, "results": [],
                                       "overall": "", "clipUrl": None, "status": "rendering",
                                       "reason": ""}
            with self._lock:
                self._refine["attempts"].append(attempt)
            note(state="rendering", phase=f"Attempt {n} of {max_attempts}: rendering a "
                                           f"{'draft' if draft else 'full-quality'} clip")
            clip = self._refine_render(slug, shot_id, idx, prompt, root / f"attempt-{n}", run, draft)
            if self._cancel.is_set():
                note(stopReason="stopped")
                return
            if clip is None:
                with self._lock:
                    attempt.update(status="failed", reason=run.reason)
                note(stopReason="render-failed", stopDetail=run.reason)
                return
            with self._lock:
                attempt.update(status="reviewing", clipUrl=self._as_url(clip))
            note(state="reviewing", phase=f"Attempt {n} of {max_attempts}: reviewing the clip")
            seconds = refining.clip_seconds(
                clip, max(int(board["shots"][idx].get("frames") or 0), 1) / 24.0)
            verdict = reviewer.judge(shot, board, checks, clip, seconds, clip.parent)
            with self._lock:
                attempt.update(status="reviewed", score=verdict["score"],
                               results=verdict["results"], overall=verdict["overall"])
            history.append(dict(attempt))
            if verdict["score"] >= pass_percent:
                note(stopReason="passed")
                return
            if n == max_attempts:
                note(stopReason="max-attempts")
                return
            if self._cancel.is_set():
                note(stopReason="stopped")
                return
            note(state="adjusting", phase=f"Attempt {n} of {max_attempts}: adjusting the prompt")
            revised = reviewer.revise(shot, board, prompt, verdict["results"], history[:-1])
            if not revised or revised.strip() == prompt.strip():
                note(stopReason="no-change",
                     stopDetail="The model had nothing further to change in the prompt.")
                return
            prompt = revised

    def _refine_render(self, slug, shot_id, idx, prompt, attempt_dir, run, draft) -> Path | None:
        """Render one attempt into *attempt_dir*; its clip, or None (see run.reason)."""
        board = self.store.load(slug)
        shots = board["shots"]
        # A copy of the board and the shot: the real ones keep their prompt,
        # their draft setting and their status as they were.
        shot = copy.deepcopy(shots[idx])
        shot["prompt"] = prompt
        board["defaults"] = {**(board.get("defaults") or {}), "draft": draft}
        with self._lock:
            self._current = run.shot_id
        problem = self._resolve_chain(shot, shots, slug)
        if problem:
            run.status, run.reason = "blocked", problem
            return None
        attempt_dir.mkdir(parents=True)
        paths = ShotPaths(
            workspace=self.workspace, abs_dir=attempt_dir,
            rel_dir=str(attempt_dir.relative_to(self.data_dir)), data_dir=self.data_dir,
        )
        try:
            spec = self.backend.prepare(shot, board, paths)
        except Exception as exc:  # noqa: BLE001
            run.status, run.reason = "failed", f"could not prepare: {exc}"
            return None
        spec.expected_seconds = self._expected_seconds(spec) or 0.0
        spec.started_at = time.time()
        run.summary = f"refine · {spec.summary}"
        run.status, run.started_at = "running", time.time()
        result = self.backend.run(spec, _progress_handler(run), self._cancel.is_set)
        run.ended_at = time.time()
        if result.cancelled or self._cancel.is_set():
            run.status, run.reason = "interrupted", "Stopped before completion."
            shutil.rmtree(attempt_dir, ignore_errors=True)
            return None
        validation = self.backend.validate(spec, result)
        if self.timings is not None and validation.verdict in ("done", "review"):
            g = self._geometry(spec)
            if g:
                self.timings.record(**g, seconds=result.seconds)
        run.validation = validation.to_json()
        run.status, run.reason = validation.verdict, validation.reason
        run.progress = 100.0 if validation.ok else run.progress
        self._write_log(attempt_dir, run, spec, result)
        clip = next((p for p in spec.expected_outputs if p.suffix == ".mp4" and p.exists()), None)
        run.outputs = [self._as_url(p) for p in spec.expected_outputs if p.exists()]
        return clip if validation.ok and clip else None

    def _finish_refine(self, root: Path) -> None:
        """Pick the best attempt, write the summary and the prompt proposal."""
        with self._lock:
            r = self._refine
            for a in r["attempts"]:
                if a["status"] in ("rendering", "reviewing"):
                    a["status"] = "interrupted"
            reviewed = [a for a in r["attempts"] if a["score"] is not None]
            best = max(reviewed, key=lambda a: (a["score"], a["n"]), default=None)
            reason = r["stopReason"] or "stopped"
            r["stopReason"], r["state"], r["phase"] = reason, "finished", ""
            r["best"] = best["n"] if best else None
            name = f"Scene {r['number']}"
            scores = ", ".join(f"{a['n']}: {a['score']}%" for a in reviewed) or "none reviewed"
            lead = {
                "passed": f"{name} passed its checklist on attempt {reviewed[-1]['n'] if reviewed else '?'}.",
                "max-attempts": f"{name} used all {r['maxAttempts']} attempts without meeting "
                                f"{r['passPercent']}% of its checks.",
                "no-change": f"{name}: the model had nothing further to change.",
                "stopped": f"{name}: stopped.",
                "render-failed": f"{name}: a draft render failed — {r['stopDetail'] or 'see the render log'}.",
                "error": f"{name}: the refine run hit an error — {r['stopDetail']}.",
            }.get(reason, f"{name}: finished.")
            text = f"{lead} Scores by attempt — {scores}."
            first = reviewed[0] if reviewed else None
            if best and first and best["n"] != first["n"] and best["score"] > first["score"]:
                r["proposal"] = {"tool": "update_shot", "shotId": r["shotId"],
                                 "fields": {"prompt": best["prompt"]}}
                text += (f" Attempt {best['n']}'s prompt scored best; apply it to the shot, "
                         "then render for real.")
            elif best:
                text += " The shot's own prompt scored as well as any revision, so there is nothing to apply."
            r["summary"] = text
            snapshot = copy.deepcopy(r)
        try:
            (root / REFINE_FILE).write_text(json.dumps(snapshot, indent=2) + "\n")
        except OSError:
            pass

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
        music = None
        if self.prepare_soundtrack:
            self._set_assembly("running", "generating the soundtrack")
            music = self.prepare_soundtrack(slug)
            board = self.store.load(slug)
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
        if music:
            fresh_board["finalVideo"]["soundtrack"] = music
        self.store.save(slug, fresh_board)

        message = (
            f"{len(result.parts)} clip(s), {result.seconds:.1f}s"
            + (
                " — incomplete, missing: " + ", ".join(result.missing)
                if result.missing
                else ""
            )
            + (
                f" — no soundtrack: {music['message']}"
                if music and music.get("state") in ("failed", "skipped")
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
        for key, label in (("startRef", "start"), ("endRef", "end")):
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

    @staticmethod
    def _geometry(spec) -> dict[str, Any] | None:
        p = spec.payload or {}
        try:
            # timingModel keeps engines that share a model id (vpipe and
            # h3c both render "ref2va") from being timed against each other.
            g = {"model": str(p.get("timingModel") or p["model"]), "width": int(p["width"]),
                 "height": int(p["height"]), "frames": int(p["frames"]),
                 "steps": int(p["steps"])}
        except (KeyError, TypeError, ValueError):
            return None
        return g if g["frames"] > 0 else None

    def _expected_seconds(self, spec) -> float | None:
        g = self._geometry(spec)
        if self.timings is None or g is None:
            return None
        return self.timings.estimate(**g)

    def accept_review(self, slug: str, shot_id: str) -> dict[str, Any]:
        """The user vouching for a take the checks flagged but did not fail.

        Marks it done both on the board and in this batch's live run state:
        the UI overlays the live run's status on the board's, and a shot
        chained from this one is gated on that live status too, so updating
        only the board left it looking -- and acting -- like "review".
        """
        board = self.store.load(slug)
        idx = next((i for i, s in enumerate(board.get("shots") or [])
                    if s["id"] == shot_id), None)
        if idx is None:
            raise RuntimeError("shot not found")
        if board["shots"][idx].get("locked"):
            raise RuntimeError("this shot is locked — unlock it to change its review state")
        shot = board["shots"][idx]
        run = self._runs.get(shot_id)
        status = run.status if run else shot.get("status")
        if status != "review":
            raise RuntimeError("only a shot flagged for review can be accepted")
        if run is not None:
            run.status, run.reason, run.progress = "done", "", 100.0
        validation = dict(shot.get("validation") or {})
        validation.update(verdict="done", reason="", acceptedByUser=True)
        shot.update(status="done", reason="", progress=100, validation=validation)
        if run is not None:
            run.validation = validation
        # Held back from a flagged take; a clean one would have had it.
        if shot.get("renderedDialogueSource") != "native":
            shot_dir = self.data_dir / self.store.shot_rel_dir(slug, idx + 1)
            self._relay_speech(shot, shot_dir, run or ShotRun(shot_id=shot_id))
        self.store.save(slug, board)
        return self.status()

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
                f"# expected  "
                + (f"~{round(spec.expected_seconds)}s" if spec.expected_seconds
                   else "unknown (no render of this model timed here yet)"),
                "",
            ]
            times = getattr(result, "log_times", None) or []
            if len(times) == len(result.log):
                body = [f"{t} [{lvl}] {text}" for t, (lvl, text) in zip(times, result.log)]
            else:
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

    @staticmethod
    def _publish_named_artifact(source: Path, board: dict[str, Any], scene_number: int) -> Path:
        """Copy a generated still or clip to a readable scene-named sibling."""
        if source.suffix.lower() in (".jpeg", ".jpg", ".png", ".webp"):
            kind = "still"
        elif source.suffix.lower() == ".mp4":
            kind = "clip"
        else:
            return source
        named = source.with_name(
            artifact_filename(board.get("name") or "storyboard", scene_number, kind, source.suffix)
        )
        if named != source:
            shutil.copy2(source, named)
        return named

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


STILLS_MAX_STEPS = 50


def still_params(defaults: dict, large: bool) -> tuple[int, int]:
    """(steps, seed) for one Create Stills run.

    Steps 0 means automatic: 4 at Small, 8 at Large. Seed 0 means a fresh
    random one per run -- chosen here, not left to the engine, because vpipe
    treats 0 as a real seed while mflux treats "no seed" as random, and the
    seed actually used is recorded with the stills either way.
    """
    def num(key: str, cast, default):
        try:
            return cast(defaults.get(key) or default)
        except (TypeError, ValueError):
            return default

    steps = num("stillsSteps", int, 0)
    steps = min(max(steps, 1), STILLS_MAX_STEPS) if steps > 0 else (8 if large else 4)
    seed = num("stillsSeed", int, 0)
    seed = seed if 0 < seed < 2**31 else random.randint(1, 2**31 - 1)
    return steps, seed


def _any_output(shots: list[dict]) -> bool:
    return any(sh.get("outputs") for sh in shots)
