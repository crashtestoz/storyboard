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
``GET  /api/boards/<slug>/export``   download the board as JSON
``POST /api/boards/<slug>/refs``     upload a reference image (raw body)
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
from .orchestrator import Orchestrator
from .store import Store, default_shot

MAX_UPLOAD = 32 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class Context:
    """Shared, immutable-ish wiring handed to every request."""

    def __init__(self, ui_root: Path, workspace: Path, store: Store,
                 backend: Backend, orch: Orchestrator):
        self.ui_root = ui_root.resolve()
        self.workspace = workspace.resolve()
        self.store = store
        self.backend = backend
        self.orch = orch


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

        m = re.fullmatch(r"/api/boards/([^/]+)/refs", path)
        if m:
            return self._upload_ref(m.group(1))

        if path == "/api/render":
            payload = self._read_json()
            slug = payload.get("slug")
            if not slug:
                raise ValueError("slug is required")
            return self._send_json(ctx.orch.start(slug, payload.get("shotIds")))

        if path == "/api/stop":
            return self._send_json(ctx.orch.stop())

        return self._err(404, "unknown endpoint")

    def _upload_ref(self, slug: str) -> None:
        """Raw-body image upload; the filename comes from a header.

        Raw body rather than multipart because the stdlib has no multipart
        parser worth using and the client is ours — ``fetch(file)`` sends the
        bytes directly.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = ALLOWED_UPLOAD_TYPES.get(ctype)
        if ext is None:
            raise ValueError(
                f"unsupported image type {ctype!r} "
                f"(allowed: {', '.join(sorted(ALLOWED_UPLOAD_TYPES))})"
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty upload")
        if length > MAX_UPLOAD:
            raise ValueError("image too large (32 MB limit)")

        raw_name = self.headers.get("X-Filename") or f"ref{ext}"
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
