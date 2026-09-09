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
``POST /api/rewrite``          restyle a shot prompt with a local language model
``GET  /api/boards/<slug>/export``   download the board as JSON
``POST /api/boards/<slug>/refs``     upload a reference image / voice (raw body)
``POST /api/boards/<slug>/refs/adopt``  copy an existing workspace file in
``POST /api/transcribe``        transcribe a reference clip ``{path, engine?}``
``POST /api/boards/<slug>/shots/<id>/dub``  speak the shot's dialogue, mux it
``POST /api/render``            start ``{slug, shotIds?}``
``POST /api/stop``              stop the running batch
``GET  /api/status``            live queue state (polled by the UI)
``GET  /media/<path>``          generated clips / frames / uploads
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .backends.base import Backend
from .dubbing import dub_shot
from .orchestrator import Orchestrator
from .llm import LLMService, rewrite_prompt
from .store import Store, default_shot
from .tts.base import TTSEngine

MAX_UPLOAD = 32 * 1024 * 1024
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
                 backend: Backend, orch: Orchestrator,
                 tts_engines: dict[str, TTSEngine] | None = None,
                 default_tts: str = "none",
                 llm_services: dict[str, LLMService] | None = None,
                 default_llm: str = "none"):
        self.ui_root = ui_root.resolve()
        self.workspace = workspace.resolve()
        self.store = store
        self.backend = backend
        self.orch = orch
        self.tts_engines = tts_engines or {}
        self.default_tts = default_tts
        self.llm_services = llm_services or {}
        self.default_llm = default_llm

    def llm(self, sid: str | None) -> LLMService:
        return self.llm_services.get(sid or self.default_llm) or self.llm_services.get(
            "none"
        )

    def tts(self, kind: str | None) -> TTSEngine:
        return self.tts_engines.get(kind or self.default_tts) or self.tts_engines.get(
            "none"
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "storyboard-to-video/0.2"
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
            return self._send_json({"slug": m.group(1), "board": saved})
        except Exception as exc:  # noqa: BLE001
            return self._err(400, f"{type(exc).__name__}: {exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
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
                    "models": [c.to_json() for c in ctx.backend.capabilities()],
                    "llm": {
                        "default": ctx.default_llm,
                        "services": [s.to_json() for s in ctx.llm_services.values()],
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
                                "voices": [v.to_json() for v in eng.voices()],
                            }
                            for kind, eng in ctx.tts_engines.items()
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
            return self._send_json({"slug": m.group(1), "board": board})

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
            return self._send_json({"images": ctx.store.library()})

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

        if path == "/api/rewrite":
            payload = self._read_json() or {}
            slug = payload.get("slug")
            shot_id = payload.get("shotId")
            if not slug or not shot_id:
                raise ValueError("slug and shotId are required")

            board = ctx.store.load(slug)
            shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
            if shot is None:
                raise FileNotFoundError(f"no shot {shot_id} in {slug}")

            # The text is taken from the request, not from the stored shot: the
            # user may not have saved the words they just typed.
            text = payload.get("text")
            if text is None:
                text = shot.get("prompt") or ""

            service = ctx.llm(payload.get("service") or board.get("defaults", {}).get("llm"))
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
                still=bool(cap and cap.kind == "image"),
            )
            return self._send_json(
                {"text": proposal, "service": service.label, "model": service.model}
            )

        if path == "/api/transcribe":
            return self._transcribe()

        if path == "/api/render":
            payload = self._read_json()
            slug = payload.get("slug")
            if not slug:
                raise ValueError("slug is required")
            return self._send_json(ctx.orch.start(slug, payload.get("shotIds")))

        if path == "/api/stop":
            return self._send_json(ctx.orch.stop())

        return self._err(404, "unknown endpoint")

    def _transcribe(self) -> None:
        """Transcribe a reference clip so the transcript can be reviewed.

        MCC's speech page makes this a visible step — upload, transcribe, then
        correct the text before it conditions the voice — because a
        mis-transcription silently degrades the clone. Same here rather than
        transcribing invisibly at dub time.
        """
        payload = self._read_json()
        rel = payload.get("path")
        engine_id = payload.get("engine")
        if not rel:
            raise ValueError("path is required")

        clip = (self.ctx.workspace / rel).resolve()
        clip.relative_to(self.ctx.workspace.resolve())
        if not clip.is_file():
            raise FileNotFoundError(f"no such clip: {rel}")

        engine = self.ctx.tts(engine_id)
        transcribe = getattr(engine, "transcribe", None)
        if transcribe is None:
            return self._err(
                409,
                f"{engine.label} cannot transcribe — select a voice-cloning "
                "engine, or type the transcript yourself",
            )
        text, err = transcribe(clip)
        if err or not text:
            return self._err(409, err or "transcription returned no text")
        return self._send_json({"text": text, "engine": engine.id})

    def _dub(self, slug: str, shot_id: str) -> None:
        """Speak a shot's dialogue and mux it over the rendered clip."""
        ctx = self.ctx
        board = ctx.store.load(slug)
        shots = board.get("shots") or []
        idx = next((i for i, s in enumerate(shots) if s["id"] == shot_id), None)
        if idx is None:
            raise FileNotFoundError("no such shot")
        shot = shots[idx]

        engine = ctx.tts((board.get("defaults") or {}).get("tts"))
        shot_dir = ctx.workspace / ctx.store.shot_rel_dir(slug, idx + 1)

        # A character's recorded voice is the natural reference for cloning.
        reference = None
        reference_text = ""
        if engine.supports_cloning:
            cast = {c["id"]: c for c in (board.get("characters") or [])}
            for cid in shot.get("characterIds") or []:
                ch = cast.get(cid) or {}
                voice = ch.get("voice")
                if voice and voice.get("path"):
                    candidate = ctx.workspace / voice["path"]
                    if candidate.exists():
                        reference = candidate
                        reference_text = ch.get("voiceText") or ""
                        break

        result = dub_shot(
            engine,
            clip=shot_dir / "clip.mp4",
            shot_dir=shot_dir,
            text=shot.get("dialogue") or "",
            voice=shot.get("dialogueVoice") or None,
            reference=reference,
            reference_text=reference_text,
        )

        if not result.ok:
            return self._send_json(
                {"error": result.error, "log": result.log,
                 "engine": engine.id}, 409
            )

        rel = result.video.relative_to(ctx.workspace)
        shot["dubUrl"] = "/media/" + str(rel).replace("\\", "/")
        ctx.store.save(slug, board)
        return self._send_json(
            {
                "dubUrl": shot["dubUrl"],
                "engine": engine.id,
                "voice": result.speech.voice if result.speech else "",
                "seconds": round(result.speech.seconds, 2) if result.speech else 0,
                "warning": result.warning,
            }
        )

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

        rel = dest.relative_to(self.ctx.workspace)
        return self._send_json(
            {
                # path as vpipe will open it (relative to its workspace)
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
        target = self._resolve_within(self.ctx.workspace, rel)
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


def build_server(bind: str, port: int, ctx: Context) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return httpd
