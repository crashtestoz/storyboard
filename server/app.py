"""HTTP server: static UI + JSON API + generated media.

Standard library only, deliberately — there is nothing to install, which is
the same property that makes ``serve.sh`` work the way ``vpipe-web-ui`` does.

Routes
------
``GET  /``                      the UI
``GET  /api/info``              backend id/label, health, model capabilities
``GET  /api/boards``            saved storyboards
``POST /api/boards``            create ``{name}``  |  import ``{board, name?}``
``GET  /api/boards/<slug>``     load one board
``PUT  /api/boards/<slug>``     save one board
``DELETE /api/boards/<slug>``   remove the board file (keeps rendered media)
``POST /api/boards/<slug>/rename``   rename ``{name}`` — moves the folder too
``POST /api/rewrite``          restyle a shot prompt, the scene description, or the
                                background sound with a local language model
``GET  /api/boards/<slug>/export``   download the board as JSON
``POST /api/boards/<slug>/refs``     upload a reference image / voice (raw body)
``POST /api/boards/<slug>/refs/adopt``  copy an existing project file in
``POST /api/transcribe``        transcribe a reference clip ``{path, engine?}``
``POST /api/boards/<slug>/shots/<id>/dub``  speak the shot's dialogue, mux it
``POST /api/render``            start ``{slug, shotIds?}``
``POST /api/render-batch``      render several projects in sequence ``{slugs}``
``POST /api/stills``            start/mid/end Krea-2 previews ``{slug, shotId}``
``POST /api/stop``              stop the running batch
``GET  /api/status``            live queue state (polled by the UI)
``POST /api/server-settings``   set the global projects folder ``{dataDir}`` —
                                 read back from /api/info; takes effect on restart
``POST /api/server-settings/restart``  restart the process onto the new folder
``GET  /media/<path>``          generated clips / frames / uploads
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import assemble as assembly
from .backends.base import Backend
from .orchestrator import Orchestrator
from .llm import LLMService, describe_character, describe_still_phases, rewrite_prompt
from .llm import load_services as load_llm_services
from .store import Store, default_shot, render_fingerprint, stale_reason
from .tts import load_engines as load_tts_engines
from .tts.base import TTSEngine

MAX_UPLOAD = 32 * 1024 * 1024

# Written by the Settings dialog's "global projects folder" field, read by
# server/__main__.py at the next startup. Lives beside llm-services.json and
# tts-services.json — one more "linked, not installed" bit of local config —
# rather than inside data_dir itself, since data_dir is the very thing it names.
SERVER_CONFIG_NAME = "server-config.json"
# Extensions are the reliable signal here: browsers disagree about audio MIME
# types (Safari says audio/mp3, others audio/mpeg, some send nothing at all or
# application/octet-stream), and the client is ours. The content type is only
# used as a hint when the name has no usable extension.
ALLOWED_EXTENSIONS = {
    # images: style references, frame anchors, character portraits
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    # audio: character voice clips, also usable as Ref2VA soundtrack references
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}

# content type -> extension, for the case where the filename has none
TYPE_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mpeg3": ".mp3",
    "audio/x-mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/ogg": ".ogg", "audio/opus": ".opus",
}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class Context:
    """Shared, immutable-ish wiring handed to every request."""

    def __init__(self, ui_root: Path, workspace: Path, store: Store,
                 backend: Backend, orch: Orchestrator, *,
                 data_dir: Path | None = None,
                 data_dir_source: str = "default",
                 vpipe_binary: Path | None = None,
                 default_tts: str = "none",
                 default_llm: str = "none"):
        self.ui_root = ui_root.resolve()
        self.workspace = workspace.resolve()
        # Where the user's storyboards and uploads live. Defaults to the
        # workspace, so an existing install is unaffected.
        self.data_dir = (data_dir or workspace).resolve()
        # Where this value came from, highest priority first: an explicit
        # --data-dir flag, the SBV_DATA_DIR env var, server-config.json (what
        # the Settings dialog writes), or the hardcoded default. Anything
        # other than "config" or "default" means editing it from Settings
        # will not take effect until whatever outranks it is removed.
        self.data_dir_source = data_dir_source
        self.store = store
        self.backend = backend
        self.orch = orch
        self.vpipe_binary = vpipe_binary
        self.default_tts = default_tts
        self.default_llm = default_llm
        from .speech import prepare_recording
        self.orch.prepare_dialogue = lambda slug, sid: prepare_recording(self, slug, sid)
        # Set by the /restart endpoint; main() checks this after the server
        # loop exits to decide whether to exec a fresh process or just stop.
        self.restart_requested = False

    # tts-services.json and llm-services.json are the "linked, not
    # installed" configs this app explicitly documents as editable without
    # a code change — but until now that edit still needed a full server
    # restart to take effect, because the services were built once at
    # startup and cached for the process's lifetime. Re-reading here instead
    # means editing either file takes effect on the very next dub, rewrite,
    # transcribe or render — not after a restart. Both files are small and
    # these classes do no eager I/O at construction, so re-parsing on every
    # call is not worth caching.
    def tts_engines(self) -> dict[str, TTSEngine]:
        return load_tts_engines(
            self.ui_root, vpipe_binary=self.vpipe_binary, workspace=self.workspace
        )

    def llm_services(self) -> dict[str, LLMService]:
        return load_llm_services(self.ui_root)

    def llm(self, sid: str | None) -> LLMService:
        services = self.llm_services()
        return services.get(sid or self.default_llm) or services.get("none")

    def transcriber(self, kind: str | None) -> TTSEngine | None:
        """A healthy engine that can transcribe — the asked-for one if it can.

        Transcription and cloning are separate capabilities. The board's
        speech engine may clone beautifully and have no speech recognition at
        all (MOSS does exactly that), and refusing on those grounds was
        pointless when another configured service could do the job. So this
        prefers what was asked for and otherwise finds one that works.
        """
        engines = self.tts_engines()
        wanted = engines.get(kind or self.default_tts)
        if wanted and wanted.supports_transcription and wanted.health()[0]:
            return wanted
        for eng in engines.values():
            if eng.supports_transcription and eng.health()[0]:
                return eng
        return None

    def tts(self, kind: str | None) -> TTSEngine:
        engines = self.tts_engines()
        return engines.get(kind or self.default_tts) or engines.get("none")


class Handler(BaseHTTPRequestHandler):
    server_version = "storyboard/0.2"
    ctx: Context  # injected below

    # -- plumbing -------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        if self.path.startswith("/api/status"):
            return  # polled every second; would drown the console
        super().log_message(fmt, *args)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD:
            raise ValueError("payload too large")
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routing --------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/media/"):
                return self._serve_media(path[len("/media/"):])
            return self._serve_static(path)
        except BrokenPipeError:
            pass
        except ValueError as exc:
            # A bad query parameter is the caller's mistake, not the server's.
            # do_POST already drew this distinction; GET reported 500 for it.
            self._err(400, str(exc))
        except FileNotFoundError as exc:
            self._err(404, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            return self._api_post(path)
        except ValueError as exc:
            self._err(400, str(exc))
        except FileNotFoundError as exc:
            self._err(404, str(exc))
        except RuntimeError as exc:
            self._err(409, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_PUT(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/boards/([^/]+)", path)
        if not m:
            return self._err(404, "not found")
        try:
            board = self._read_json()
            saved = self.ctx.store.save(m.group(1), board)
            return self._send_json(
                {
                    "slug": m.group(1),
                    "board": saved,
                    "stale": self._staleness(m.group(1), saved),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(400, f"{type(exc).__name__}: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/library":
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                rel = (q.get("path") or [""])[0]
                kind = (q.get("kind") or ["image"])[0]
                if kind not in Store.LIBRARY_EXTS:
                    raise ValueError(
                        f"unknown library kind {kind!r} "
                        f"(expected {', '.join(sorted(Store.LIBRARY_EXTS))})"
                    )
                if not rel:
                    raise ValueError("path is required")
                return self._send_json(
                    self.ctx.store.delete_library_item(rel, kind=kind)
                )
            except FileNotFoundError as exc:
                return self._err(404, str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._err(400, f"{type(exc).__name__}: {exc}")

        m = re.fullmatch(r"/api/boards/([^/]+)", path)
        if not m:
            return self._err(404, "not found")
        self.ctx.store.delete(m.group(1))
        return self._send_json({"ok": True})

    # -- API: GET -------------------------------------------------------- #

    def _api_get(self, path: str) -> None:
        ctx = self.ctx

        if path == "/api/info":
            ok, msg = ctx.backend.health()
            return self._send_json(
                {
                    "backend": {"id": ctx.backend.id, "label": ctx.backend.label,
                                "healthy": ok, "message": msg},
                    "workspace": str(ctx.workspace),
                    "dataDir": str(ctx.data_dir),
                    "dataDirSource": ctx.data_dir_source,
                    "models": [c.to_json() for c in ctx.backend.capabilities()],
                    "llm": {
                        "default": ctx.default_llm,
                        "services": [s.to_json() for s in ctx.llm_services().values()],
                    },
                    "tts": {
                        "default": ctx.default_tts,
                        "engines": [
                            {
                                "id": kind,
                                "label": eng.label,
                                "healthy": eng.health()[0],
                                "message": eng.health()[1],
                                "supportsCloning": eng.supports_cloning,
                                "supportsTranscription": eng.supports_transcription,
                                "voices": [v.to_json() for v in eng.voices()],
                            }
                            for kind, eng in ctx.tts_engines().items()
                        ],
                    },
                }
            )

        if path == "/api/boards":
            return self._send_json({"boards": ctx.store.list_boards()})

        m = re.fullmatch(r"/api/boards/([^/]+)", path)
        if m:
            try:
                board = ctx.store.load(m.group(1))
            except FileNotFoundError:
                return self._err(404, "no such storyboard")
            return self._send_json(
                {
                    "slug": m.group(1),
                    "board": board,
                    "stale": self._staleness(m.group(1), board),
                }
            )

        m = re.fullmatch(r"/api/boards/([^/]+)/export", path)
        if m:
            slug = m.group(1)
            try:
                raw = ctx.store.export(slug).encode()
            except FileNotFoundError:
                return self._err(404, "no such storyboard")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{slug}.storyboard.json"',
            )
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)

        if path == "/api/library":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = (q.get("kind") or ["image"])[0]
            if kind not in Store.LIBRARY_EXTS:
                raise ValueError(
                    f"unknown library kind {kind!r} "
                    f"(expected {', '.join(sorted(Store.LIBRARY_EXTS))})"
                )
            items = ctx.store.library(kind=kind)
            # "images" is kept alongside "items" so an older cached page keeps
            # working after a reload lands mid-session.
            return self._send_json({"items": items, "images": items})

        if path == "/api/status":
            return self._send_json(ctx.orch.status())

        return self._err(404, "unknown endpoint")

    # -- API: POST ------------------------------------------------------- #

    def _api_post(self, path: str) -> None:
        ctx = self.ctx

        if path == "/api/boards":
            payload = self._read_json()
            if payload.get("board"):
                slug, board = ctx.store.import_board(
                    payload["board"], name=payload.get("name")
                )
            else:
                slug, board = ctx.store.create(payload.get("name") or "Untitled storyboard")
            return self._send_json({"slug": slug, "board": board}, 201)

        m = re.fullmatch(r"/api/boards/([^/]+)/shots", path)
        if m:
            slug = m.group(1)
            board = ctx.store.load(slug)
            shot = default_shot(board.get("defaults"))
            payload = self._read_json() or {}
            shot.update({k: v for k, v in payload.items() if k in shot})
            board["shots"].append(shot)
            ctx.store.save(slug, board)
            return self._send_json({"slug": slug, "board": board, "shot": shot}, 201)

        m = re.fullmatch(r"/api/boards/([^/]+)/rename", path)
        if m:
            payload = self._read_json() or {}
            # A rename moves the project folder, and the orchestrator is
            # holding paths into it — so it waits until the queue is idle
            # rather than pulling the folder out from under a running render.
            if ctx.orch.busy:
                return self._send_json(
                    {"error": "A render is running. Stop it, or wait for it "
                              "to finish, before renaming the project."},
                    409,
                )
            new_slug, board = ctx.store.rename(m.group(1), payload.get("name") or "")
            return self._send_json({"slug": new_slug, "board": board})

        m = re.fullmatch(r"/api/boards/([^/]+)/refs", path)
        if m:
            return self._upload_ref(m.group(1))

        m = re.fullmatch(r"/api/boards/([^/]+)/refs/adopt", path)
        if m:
            payload = self._read_json()
            rel = payload.get("path")
            if not rel:
                raise ValueError("path is required")
            return self._send_json(ctx.store.adopt(m.group(1), rel), 201)

        m = re.fullmatch(r"/api/boards/([^/]+)/shots/([^/]+)/dub", path)
        if m:
            return self._dub(m.group(1), m.group(2))

        m = re.fullmatch(r"/api/boards/([^/]+)/shots/([^/]+)/accept", path)
        if m:
            return self._accept_take(m.group(1), m.group(2))

        if path == "/api/render-preview":
            from .backends.vpipe_backend import (
                SKETCH_STYLE_PREFIX, _clones_voice, _effective_video_model,
                _reference_bindings, _resolved_prompt,
            )
            payload = self._read_json() or {}
            board, shot = payload["board"], payload["shot"]
            model, automatic_reason = _effective_video_model(shot, board)
            refs = _reference_bindings(shot, board, model)
            cap = ctx.backend.capability(model)
            audio = bool(cap and cap.supports_audio and not ((board.get("defaults") or {}).get("draft") and (board.get("defaults") or {}).get("sketch")))
            warnings = []
            if automatic_reason:
                warnings.append(automatic_reason[0].upper() + automatic_reason[1:] + ".")
            native = audio and _clones_voice(shot, board, model)
            if shot.get("dialogueSource") == "native" and not native:
                warnings.append("Native dialogue requires Ref2VA and a speaker reference voice.")
            source = "H3 native speech" if native else "Dialogue-window recording"
            if not audio:
                source = "No generated audio"
            prompt = _resolved_prompt(shot, board, with_audio=audio, model=model)
            if (board.get("defaults") or {}).get("draft") and (board.get("defaults") or {}).get("sketch"):
                prompt = f"{SKETCH_STYLE_PREFIX} {prompt}"
            return self._send_json({"prompt": prompt,
                "model": model, "dialogueSource": source,
                "references": [{k: v for k, v in r.items() if k != "ref"} for r in refs],
                "warnings": warnings})

        if path == "/api/rewrite":
            payload = self._read_json() or {}
            slug = payload.get("slug")
            shot_id = payload.get("shotId")
            field = payload.get("field")
            if not slug:
                raise ValueError("slug is required")
            if not shot_id and field not in ("sceneDescription", "soundscape"):
                raise ValueError(
                    "shotId, or field ('sceneDescription' or 'soundscape'), is required"
                )

            board = ctx.store.load(slug)
            service = ctx.llm(payload.get("service") or board.get("defaults", {}).get("llm"))

            if shot_id and field == "soundNote":
                shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
                if shot is None:
                    raise FileNotFoundError(f"no shot {shot_id} in {slug}")

                text = payload.get("text")
                if text is None:
                    text = shot.get("soundNote") or ""

                # Give the model both scene layers. Sound accents are additional
                # local background sounds, so the shared Background Sound is an
                # explicit exclusion list rather than something to rewrite.
                proposal = rewrite_prompt(
                    service,
                    text,
                    scene=board.get("sceneDescription") or "",
                    soundscape=board.get("soundscape") or "",
                    context=shot.get("prompt") or "",
                    context_label=(
                        "This shot's own prompt, already written elsewhere. Use "
                        "its location and moment to find additional local "
                        "background sounds, but do not repeat the prompt or turn "
                        "foreground action into sound effects:"
                    ),
                    kind="soundNote",
                )
            elif shot_id:
                shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
                if shot is None:
                    raise FileNotFoundError(f"no shot {shot_id} in {slug}")

                # The text is taken from the request, not from the stored shot:
                # the user may not have saved the words they just typed.
                text = payload.get("text")
                if text is None:
                    text = shot.get("prompt") or ""

                cap = next(
                    (c for c in ctx.backend.capabilities() if c.id == shot.get("model")),
                    None,
                )
                wanted = set(shot.get("characterIds") or [])
                cast = [c for c in (board.get("characters") or []) if c.get("id") in wanted]

                proposal = rewrite_prompt(
                    service,
                    text,
                    scene=board.get("sceneDescription") or "",
                    characters=cast,
                    reference_images=_rewrite_reference_images(
                        shot, cast, board.get("styleRefs")
                    ),
                    reference_files=_materialize_rewrite_images(
                        ctx.data_dir,
                        _rewrite_reference_images(
                            shot, cast, board.get("styleRefs")
                        ),
                    ),
                    kind="still" if (cap and cap.kind == "image") else "shot",
                )
            elif field == "sceneDescription":
                # No cast context here: the scene description is the
                # surroundings only, and each character already has its own
                # separate description — repeating them here would tempt the
                # model into describing people, which this field is not for.
                text = payload.get("text")
                if text is None:
                    text = board.get("sceneDescription") or ""

                proposal = rewrite_prompt(
                    service,
                    text,
                    reference_images=_rewrite_reference_images(
                        None, [], board.get("styleRefs")
                    ),
                    reference_files=_materialize_rewrite_images(
                        ctx.data_dir,
                        _rewrite_reference_images(
                            None, [], board.get("styleRefs")
                        ),
                    ),
                    kind="scene",
                )
            else:  # field == "soundscape"
                text = payload.get("text")
                if text is None:
                    text = board.get("soundscape") or ""

                proposal = rewrite_prompt(
                    service,
                    text,
                    scene=board.get("sceneDescription") or "",
                    kind="soundscape",
                )

            return self._send_json(
                {"text": proposal, "service": service.label, "model": service.model}
            )

        if path == "/api/describe-character":
            payload = self._read_json() or {}
            image_rel = payload.get("image")
            if not image_rel:
                raise ValueError("image is required")

            image = self._resolve_within(ctx.data_dir, image_rel)
            if image is None or not image.is_file():
                raise FileNotFoundError(f"no such reference image: {image_rel}")
            ctype, _ = mimetypes.guess_type(str(image))
            if not (ctype or "").startswith("image/"):
                raise ValueError("the selected reference is not an image")

            voice = None
            voice_rel = payload.get("voice")
            if voice_rel:
                voice = self._resolve_within(ctx.data_dir, voice_rel)
                vtype, _ = mimetypes.guess_type(str(voice or voice_rel))
                if voice is None or not voice.is_file():
                    raise FileNotFoundError(f"no such reference voice: {voice_rel}")
                if not (vtype or "").startswith("audio/"):
                    raise ValueError("the selected reference voice is not audio")

            service = ctx.llm(payload.get("service") or ctx.default_llm)
            proposal = describe_character(
                service,
                image,
                voice=voice,
                name=payload.get("name") or "",
                current=payload.get("description") or "",
            )
            return self._send_json(
                {
                    # Keep ``text`` as the character-only value for older
                    # clients; named fields carry the new split result.
                    "text": proposal["character"],
                    "character": proposal["character"],
                    "environment": proposal["environment"],
                    "service": service.label,
                    "model": service.model,
                }
            )

        if path == "/api/transcribe":
            return self._transcribe()

        if path == "/api/render":
            payload = self._read_json()
            slug = payload.get("slug")
            if not slug:
                raise ValueError("slug is required")
            if isinstance(payload.get("board"), dict):
                ctx.store.save(slug, payload["board"])
            return self._send_json(ctx.orch.start(slug, payload.get("shotIds")))

        if path == "/api/prepare-dialogue":
            payload = self._read_json() or {}
            if not payload.get("slug"):
                raise ValueError("slug is required")
            return self._send_json(ctx.orch.start_dialogue(payload["slug"]))

        if path == "/api/render-batch":
            payload = self._read_json()
            slugs = payload.get("slugs")
            if not isinstance(slugs, list) or not slugs:
                raise ValueError("slugs (a non-empty list) is required")
            return self._send_json(ctx.orch.start_project_batch(slugs))

        if path == "/api/stills":
            payload = self._read_json()
            slug = payload.get("slug")
            shot_id = payload.get("shotId")
            if not slug or not shot_id:
                raise ValueError("slug and shotId are required")
            # A generic "the start of this shot" means nothing to a model
            # with no sense of time — asking the configured rewrite model to
            # extract what the shot's own prompt actually says happens at
            # each of the three points gives Create Stills something concrete
            # to render instead of the same single moment three times. Best-
            # effort: create_stills() falls back to a generic phase label per
            # phase when this comes back empty (no service configured, or it
            # errored) rather than blocking the button on it.
            board = ctx.store.load(slug)
            shot = next((s for s in board.get("shots") or [] if s["id"] == shot_id), None)
            phase_prompts = {}
            if shot is not None:
                service = ctx.llm((board.get("defaults") or {}).get("llm"))
                phase_prompts = describe_still_phases(service, shot.get("prompt") or "")
            return self._send_json(ctx.orch.create_stills(slug, shot_id, phase_prompts))

        if path == "/api/stop":
            return self._send_json(ctx.orch.stop())

        if path == "/api/assemble":
            return self._assemble()

        if path == "/api/server-settings":
            payload = self._read_json() or {}
            raw = (payload.get("dataDir") or "").strip()
            if not raw:
                raise ValueError("dataDir is required")
            target = Path(raw).expanduser()
            if not target.is_absolute():
                raise ValueError("dataDir must be an absolute path")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"cannot use that folder: {exc}") from exc
            doc = {"dataDir": str(target)}
            cfg = ctx.ui_root / SERVER_CONFIG_NAME
            tmp = cfg.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2) + "\n")
            tmp.replace(cfg)
            return self._send_json(
                {
                    "ok": True,
                    "dataDir": str(target),
                    "active": str(target) == str(ctx.data_dir),
                    "overridden": ctx.data_dir_source in ("cli", "env"),
                }
            )

        if path == "/api/server-settings/restart":
            if ctx.orch.busy:
                return self._err(
                    409, "a render is running — stop it before restarting the server"
                )
            ctx.restart_requested = True
            self._send_json({"ok": True})
            server = self.server

            def _shutdown_soon() -> None:
                # The response above must reach the browser before the
                # listening socket goes away, so shutdown() runs a beat later
                # and from a different thread than serve_forever() — calling
                # it from the same thread that runs the loop deadlocks.
                time.sleep(0.3)
                server.shutdown()

            threading.Thread(target=_shutdown_soon, daemon=True).start()
            return

        return self._err(404, "unknown endpoint")

    def _staleness(self, slug: str, board: dict[str, Any]) -> dict[str, Any]:
        """Which renders no longer match the board, and why.

        Sent beside the board rather than inside it. The front end round-trips
        the board object it is given straight back on the next save, so a
        server-computed field placed in there would be persisted into
        ``storyboard.json`` as though the user had written it.
        """
        return {
            "shots": {
                shot["id"]: reason
                for shot in (board.get("shots") or [])
                if (reason := stale_reason(shot, board))
            },
            "final": assembly.final_stale_reason(
                board, self.ctx.store.project_dir(slug)
            ),
            "dialogue": assembly.unmixed_dialogue(
                board, self.ctx.store.project_dir(slug)
            ),
        }

    def _accept_take(self, slug: str, shot_id: str) -> None:
        """Record this shot's existing clip as a render of the board as it is.

        The user asserting what the server cannot prove. Needed because the
        rule that catches a stale clip — no recorded fingerprint means it
        cannot be shown to match — also catches every clip rendered before
        fingerprints existed, and re-rendering half an hour of good video to
        establish a fact the user already knows is the wrong trade. Only the
        record moves; no file is touched.
        """
        ctx = self.ctx
        board = ctx.store.load(slug)
        shot = next(
            (s for s in (board.get("shots") or []) if s["id"] == shot_id), None
        )
        if shot is None:
            raise FileNotFoundError(f"no shot {shot_id} in {slug}")
        if not shot.get("outputs"):
            return self._err(409, "this shot has nothing rendered to keep")

        shot["renderFingerprint"] = render_fingerprint(shot, board)
        ctx.store.save(slug, board)
        return self._send_json(
            {
                "shotId": shot_id,
                "renderFingerprint": shot["renderFingerprint"],
                "stale": self._staleness(slug, board),
            }
        )

    def _assemble(self) -> None:
        """Join this board's clips into one video, on demand.

        A whole-board render does this at the end, but it has to be available
        on its own: nothing needs re-rendering after a single shot is re-run,
        and half an hour of GPU time is the wrong price for a concat.
        """
        ctx = self.ctx
        payload = self._read_json() or {}
        slug = payload.get("slug")
        if not slug:
            raise ValueError("slug is required")
        if ctx.orch.busy:
            return self._err(409, "a render is running — assembling now would "
                                  "join a clip that is still being written")

        board = ctx.store.load(slug)
        from .speech import prepare_recording
        for i, shot in enumerate(board.get("shots", []), 1):
            if (ctx.store.project_dir(slug) / "shots" / f"{i:02d}" / "clip.mp4").exists():
                prepare_recording(ctx, slug, shot["id"], generate_missing=False)
        board = ctx.store.load(slug)
        shots = board.get("shots") or []
        parts: list[tuple[str, Path | None]] = []
        for i, shot in enumerate(shots):
            shot_dir = ctx.data_dir / ctx.store.shot_rel_dir(slug, i + 1)
            label = shot.get("title") or f"shot {i + 1}"
            parts.append((f"{i + 1:02d} {label}", assembly.shot_clip(shot_dir, shot)))

        width, height = assembly.frame_size(
            (board.get("defaults") or {}).get("resolution")
        )
        project_dir = ctx.store.project_dir(slug)
        result = assembly.assemble(
            parts, assembly.final_path(project_dir), width, height,
            options=assembly.board_options(board, project_dir, ctx.data_dir)
        )
        if not result.ok:
            return self._send_json(
                {"error": result.error, "log": result.log,
                 "missing": result.missing}, 409
            )

        url = "/media/" + str(
            result.path.relative_to(ctx.data_dir)
        ).replace("\\", "/")
        board["finalVideo"] = result.to_json(url)
        ctx.store.save(slug, board)
        return self._send_json(
            {
                "finalVideo": board["finalVideo"],
                "log": result.log,
                "stale": self._staleness(slug, board),
            }
        )

    def _transcribe(self) -> None:
        """Transcribe a reference clip so the transcript can be reviewed.

        The reference implementation's own speech page makes this a visible
        step — upload, transcribe, then
        correct the text before it conditions the voice — because a
        mis-transcription silently degrades the clone. Same here rather than
        transcribing invisibly at dub time.
        """
        payload = self._read_json()
        rel = payload.get("path")
        engine_id = payload.get("engine")
        if not rel:
            raise ValueError("path is required")

        clip = (self.ctx.data_dir / rel).resolve()
        clip.relative_to(self.ctx.data_dir.resolve())
        if not clip.is_file():
            raise FileNotFoundError(f"no such clip: {rel}")

        engine = self.ctx.transcriber(engine_id)
        if engine is None:
            configured = [
                e.label for e in self.ctx.tts_engines().values()
                if e.supports_transcription
            ]
            return self._err(
                409,
                "No speech service that can transcribe is available"
                + (
                    f" ({', '.join(configured)} can, but is unreachable). "
                    if configured
                    else " — none of the configured services offers it. "
                )
                + "Type what the clip says instead.",
            )
        text, err = engine.transcribe(clip)
        if err or not text:
            return self._err(409, err or "transcription returned no text")
        return self._send_json({"text": text, "engine": engine.id})

    def _dub(self, slug: str, shot_id: str) -> None:
        if self.ctx.orch.busy or self.ctx.orch.stills_busy:
            return self._err(409, "Wait for the render to finish before generating a separate take")
        from .speech import generate_take
        result = generate_take(self.ctx, slug, shot_id, self._read_json() or {})
        return self._send_json(result, 409 if result.get("error") else 200)

    def _upload_ref(self, slug: str) -> None:
        """Raw-body image upload; the filename comes from a header.

        Raw body rather than multipart because the stdlib has no multipart
        parser worth using and the client is ours — ``fetch(file)`` sends the
        bytes directly.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw_name = self.headers.get("X-Filename") or ""
        name_ext = Path(raw_name).suffix.lower()

        # the extension decides; the content type only fills in when there is
        # no usable extension
        ext = name_ext if name_ext in ALLOWED_EXTENSIONS else TYPE_TO_EXT.get(ctype)
        if ext is None:
            raise ValueError(
                f"unsupported file {raw_name or '(unnamed)'} "
                f"(type {ctype or 'unknown'}). Accepted: "
                f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty upload")
        if length > MAX_UPLOAD:
            raise ValueError("file too large (32 MB limit)")

        stem = SAFE_NAME.sub("_", Path(raw_name).stem)[:48] or "ref"

        refs = self.ctx.store.refs_dir(slug)
        refs.mkdir(parents=True, exist_ok=True)
        dest = refs / f"{stem}{ext}"
        n = 2
        while dest.exists():
            dest = refs / f"{stem}-{n}{ext}"
            n += 1
        dest.write_bytes(self.rfile.read(length))

        rel = dest.relative_to(self.ctx.data_dir)
        return self._send_json(
            {
                # stored relative to the data directory, which keeps a
                # project folder portable; made absolute at render time
                "path": str(rel).replace("\\", "/"),
                # url the browser can display it from
                "url": "/media/" + str(rel).replace("\\", "/"),
                "label": dest.name,
            },
            201,
        )

    # -- files ----------------------------------------------------------- #

    def _resolve_within(self, root: Path, rel: str) -> Path | None:
        """Join and confirm the result is still inside *root*."""
        rel = urllib.parse.unquote(rel)
        candidate = (root / posixpath.normpath("/" + rel).lstrip("/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = self._resolve_within(self.ctx.ui_root, rel)
        if target is None or not target.is_file():
            return self._err(404, "not found")
        return self._send_file(target, cache=False)

    def _serve_media(self, rel: str) -> None:
        # Media lives under the data directory, which is not necessarily the
        # vpipe workspace. The traversal check is anchored on the same root
        # the paths are relative to, so the two cannot drift apart.
        target = self._resolve_within(self.ctx.data_dir, rel)
        if target is None or not target.is_file():
            return self._err(404, "not found")
        return self._send_file(target, cache=True)

    def _send_file(self, target: Path, *, cache: bool) -> None:
        ctype, _ = mimetypes.guess_type(str(target))
        size = target.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Cache-Control", "public, max-age=60" if cache else "no-store"
        )
        self.end_headers()
        with target.open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                self.wfile.write(chunk)


def _rewrite_reference_images(
    shot: dict[str, Any] | None,
    cast: list[dict[str, Any]],
    style_refs: list[Any] | None = None,
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []

    def add(role: str, ref: Any) -> None:
        if not _is_image_ref(ref):
            return
        label = _ref_label(ref)
        if not label:
            return
        item = {
            "role": (ref.get("role") if isinstance(ref, dict) else "") or role,
            "label": label,
            "summary": _label_summary(label),
        }
        if isinstance(ref, dict) and ref.get("tag"):
            item["tag"] = str(ref["tag"]).strip().lstrip("@")
        if isinstance(ref, dict) and ref.get("path"):
            item["path"] = str(ref["path"])
        elif isinstance(ref, str):
            item["path"] = ref
        if item not in refs:
            refs.append(item)

    if shot is not None:
        add("Primary shot reference image", shot.get("startRef"))
        add("Ending shot reference image", shot.get("endRef"))
        for i, ref in enumerate(shot.get("referenceImages") or [], start=1):
            add(f"Additional shot reference image {i}", ref)
    for i, ref in enumerate(style_refs or [], start=1):
        add(f"Project style reference image {i}", ref)
    for ch in cast:
        name = (ch.get("name") or "").strip()
        role = f"Character portrait for {name}" if name else "Character portrait"
        add(role, ch.get("image"))
    return refs


def _materialize_rewrite_images(
    root: Path, refs: list[dict[str, str]]
) -> list[Path]:
    """Resolve the image refs that are actually attached to a rewrite call."""
    files: list[Path] = []
    for ref in refs:
        rel = ref.get("path") or ""
        target = (root / posixpath.normpath("/" + rel).lstrip("/")).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue
        if target.is_file():
            files.append(target)
    return files


def _is_image_ref(ref: Any) -> bool:
    label = _ref_label(ref)
    if not label:
        return False
    ctype, _ = mimetypes.guess_type(label)
    return bool((ctype or "").startswith("image/"))


def _ref_label(ref: Any) -> str:
    if not ref:
        return ""
    if isinstance(ref, str):
        return Path(ref).name
    if not isinstance(ref, dict):
        return ""
    value = ref.get("label") or ref.get("path") or ref.get("resolved") or ""
    return Path(str(value)).name


def _label_summary(label: str) -> str:
    stem = Path(label).stem
    words = re.sub(r"[_-]+", " ", stem).strip()
    return re.sub(r"\s+", " ", words)


def build_server(bind: str, port: int, ctx: Context) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return httpd
