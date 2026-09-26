#!/usr/bin/env python3
"""MCP server for Storyboard — every board feature the UI exposes, as tools.

Standard library only, on the same "linked, not installed" footing as the
rest of this app: this process is a thin JSON-RPC-over-stdio front end to the
storyboard HTTP API (``server/app.py``). It does not reimplement any
storyboard logic — every tool below is a direct call to the same endpoint the
web UI itself uses, so a board edited through an MCP client and one edited
through the browser are edited exactly the same way, sometimes seconds apart.

Run by an MCP client (Claude Desktop, Claude Code, etc.), not by hand:

    {
      "mcpServers": {
        "storyboard": {
          "command": "python3",
          "args": ["/absolute/path/to/storyboard/mcp/server.py"]
        }
      }
    }

On the first request it makes sure the storyboard HTTP server is actually
running at ``SBV_MCP_URL`` (default ``http://127.0.0.1:9877``); if nothing
answers, it launches ``start.sh`` itself and waits for it to come up. That is
the sense in which the MCP server "starts together with" the app: adding this
entry to an MCP client's config is enough on its own — no separate
``./start.sh`` first.

See ``../docs/REFERENCE.md`` → "MCP: driving Storyboard from an AI assistant" for the
full tool reference.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

STORYBOARD_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "storyboard"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def _base_url() -> str:
    explicit = os.environ.get("SBV_MCP_URL")
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("SBV_MCP_HOST", "127.0.0.1")
    port = os.environ.get("SBV_PORT", "9877")
    return f"http://{host}:{port}"


BASE_URL = _base_url()


# -- HTTP bridge to server/app.py ---------------------------------------- #


def api(
    method: str,
    path: str,
    *,
    body: Any = None,
    query: dict[str, str] | None = None,
    raw: bytes | None = None,
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 180,
) -> Any:
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data: bytes | None = None
    hdrs = dict(headers or {})
    if raw is not None:
        data = raw
        hdrs["Content-Type"] = content_type or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return json.loads(payload or b"{}")
            return {"contentType": ctype, "bytes": len(payload)}
    except urllib.error.HTTPError as exc:
        raw_err = exc.read()
        try:
            msg = json.loads(raw_err).get("error") or raw_err.decode("utf-8", "replace")
        except Exception:
            msg = raw_err.decode("utf-8", "replace") or exc.reason
        raise RuntimeError(f"storyboard server: HTTP {exc.code} — {msg}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"cannot reach the storyboard server at {BASE_URL} ({exc.reason}). "
            "It may still be starting — try again in a moment."
        ) from None


def _healthy(timeout: float = 1.5) -> bool:
    try:
        api("GET", "/api/info", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_server_running() -> None:
    """Launch ``start.sh`` if nothing is answering at BASE_URL yet."""
    if _healthy():
        return
    start_script = STORYBOARD_ROOT / "start.sh"
    if not start_script.is_file():
        return
    log_path = STORYBOARD_ROOT / "server.log"
    try:
        with open(log_path, "a") as log:
            subprocess.Popen(
                ["/bin/bash", str(start_script)],
                cwd=str(STORYBOARD_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError:
        return  # a tool call will surface a clear connection error instead

    deadline = time.time() + 30
    while time.time() < deadline:
        if _healthy():
            return
        time.sleep(0.5)


def require(args: dict[str, Any], key: str) -> Any:
    val = args.get(key)
    if val in (None, ""):
        raise RuntimeError(f"'{key}' is required")
    return val


def q(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


BOARD_ARG = {
    "type": "object",
    "description": (
        "The full board document — same shape returned by sbv_get_board and "
        "saved by the UI on every edit: sceneDescription, defaults, "
        "characters, shots, etc."
    ),
}


# -- tools ----------------------------------------------------------------- #
# One tool per storyboard HTTP endpoint. Nothing here is app logic — each
# handler only shapes arguments into the request server/app.py already
# expects, so this list stays a mirror of that file's routing table.


def t_info(args: dict[str, Any]) -> Any:
    return api("GET", "/api/info")


def t_list_boards(args: dict[str, Any]) -> Any:
    return api("GET", "/api/boards")


def t_get_board(args: dict[str, Any]) -> Any:
    return api("GET", f"/api/boards/{q(require(args, 'slug'))}")


def t_create_board(args: dict[str, Any]) -> Any:
    return api("POST", "/api/boards", body={"name": args.get("name") or "Untitled storyboard"})


def t_import_board(args: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"board": require(args, "board")}
    if args.get("name"):
        body["name"] = args["name"]
    return api("POST", "/api/boards", body=body)


def t_save_board(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    board = require(args, "board")
    return api("PUT", f"/api/boards/{q(slug)}", body=board)


def t_delete_board(args: dict[str, Any]) -> Any:
    return api("DELETE", f"/api/boards/{q(require(args, 'slug'))}")


def t_rename_board(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    name = require(args, "name")
    return api("POST", f"/api/boards/{q(slug)}/rename", body={"name": name})


def t_export_board(args: dict[str, Any]) -> Any:
    return api("GET", f"/api/boards/{q(require(args, 'slug'))}/export")


def t_add_shot(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    return api("POST", f"/api/boards/{q(slug)}/shots", body=args.get("shot") or {})


def t_upload_ref(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    file_path = require(args, "file_path")
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise RuntimeError(f"no such local file: {file_path}")
    ctype, _ = mimetypes.guess_type(str(path))
    return api(
        "POST",
        f"/api/boards/{q(slug)}/refs",
        raw=path.read_bytes(),
        content_type=ctype or "application/octet-stream",
        headers={"X-Filename": path.name},
    )


def t_adopt_ref(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    rel = require(args, "path")
    return api("POST", f"/api/boards/{q(slug)}/refs/adopt", body={"path": rel})


def t_list_library(args: dict[str, Any]) -> Any:
    return api("GET", "/api/library", query={"kind": args.get("kind") or "image"})


def t_delete_library_item(args: dict[str, Any]) -> Any:
    rel = require(args, "path")
    return api("DELETE", "/api/library", query={"path": rel, "kind": args.get("kind") or "image"})


def t_transcribe(args: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"path": require(args, "path")}
    if args.get("engine"):
        body["engine"] = args["engine"]
    return api("POST", "/api/transcribe", body=body)


def t_dub_shot(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    shot_id = require(args, "shot_id")
    body: dict[str, Any] = {}
    if args.get("text") is not None:
        body["text"] = args["text"]
    if args.get("style") is not None:
        body["style"] = args["style"]
    if args.get("dub_mode") is not None:
        body["dubMode"] = args["dub_mode"]
    return api("POST", f"/api/boards/{q(slug)}/shots/{q(shot_id)}/dub", body=body)


def t_accept_take(args: dict[str, Any]) -> Any:
    slug = require(args, "slug")
    shot_id = require(args, "shot_id")
    return api("POST", f"/api/boards/{q(slug)}/shots/{q(shot_id)}/accept", body={})


def t_rewrite_prompt(args: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"slug": require(args, "slug"), "shotId": require(args, "shot_id")}
    if args.get("text") is not None:
        body["text"] = args["text"]
    if args.get("service"):
        body["service"] = args["service"]
    return api("POST", "/api/rewrite", body=body)


def t_describe_character(args: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"image": require(args, "image")}
    for key in ("name", "description", "service"):
        if args.get(key):
            body[key] = args[key]
    return api("POST", "/api/describe-character", body=body)


def t_start_render(args: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"slug": require(args, "slug")}
    if args.get("shot_ids") is not None:
        body["shotIds"] = args["shot_ids"]
    if args.get("board") is not None:
        body["board"] = args["board"]
    return api("POST", "/api/render", body=body)


def t_stop_render(args: dict[str, Any]) -> Any:
    return api("POST", "/api/stop", body={})


def t_status(args: dict[str, Any]) -> Any:
    return api("GET", "/api/status")


def t_assemble(args: dict[str, Any]) -> Any:
    return api("POST", "/api/assemble", body={"slug": require(args, "slug")})


def t_set_data_dir(args: dict[str, Any]) -> Any:
    return api("POST", "/api/server-settings", body={"dataDir": require(args, "data_dir")})


def t_restart_server(args: dict[str, Any]) -> Any:
    return api("POST", "/api/server-settings/restart", body={})


TOOLS: list[dict[str, Any]] = [
    {
        "name": "sbv_info",
        "description": "Backend/model health, and the configured speech and rewrite services.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_info,
    },
    {
        "name": "sbv_list_boards",
        "description": "List every storyboard on disk (name, slug, shot count, rendered count).",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_list_boards,
    },
    {
        "name": "sbv_get_board",
        "description": "Load one storyboard's full JSON (scene, cast, shots, defaults, staleness).",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string", "description": "Board slug/folder name."}},
            "required": ["slug"],
        },
        "fn": t_get_board,
    },
    {
        "name": "sbv_create_board",
        "description": "Create a new, empty storyboard.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Display name (default: 'Untitled storyboard')."}},
        },
        "fn": t_create_board,
    },
    {
        "name": "sbv_import_board",
        "description": "Create a new storyboard from an exported board JSON (a copy, not a link to the original).",
        "inputSchema": {
            "type": "object",
            "properties": {"board": BOARD_ARG, "name": {"type": "string"}},
            "required": ["board"],
        },
        "fn": t_import_board,
    },
    {
        "name": "sbv_save_board",
        "description": (
            "Save (overwrite) a storyboard's full JSON — the same call the UI makes on every "
            "edit. Use sbv_get_board first, edit the object, then save it back; this is how "
            "every board field (scene description, defaults, characters, shots, sound, etc.) "
            "is changed, not through separate per-field endpoints."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "board": BOARD_ARG},
            "required": ["slug", "board"],
        },
        "fn": t_save_board,
    },
    {
        "name": "sbv_delete_board",
        "description": "Delete a storyboard's project file (keeps already-rendered media on disk).",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        "fn": t_delete_board,
    },
    {
        "name": "sbv_rename_board",
        "description": "Rename a storyboard; moves its project folder and rewrites every path it recorded.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "name": {"type": "string"}},
            "required": ["slug", "name"],
        },
        "fn": t_rename_board,
    },
    {
        "name": "sbv_export_board",
        "description": "Get a storyboard as portable JSON, exactly as the UI's download does.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        "fn": t_export_board,
    },
    {
        "name": "sbv_add_shot",
        "description": "Append a new shot to a storyboard, with optional starting field values.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "shot": {"type": "object", "description": "Optional partial shot fields (prompt, dialogue, startRef, endRef, referenceImages, etc.). Video shots always render with Ref2VA; model is ignored."},
            },
            "required": ["slug"],
        },
        "fn": t_add_shot,
    },
    {
        "name": "sbv_upload_ref",
        "description": (
            "Upload a local image or voice clip file into a board's refs/ folder "
            "(style reference, start/end frame, character portrait or voice clip)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "file_path": {"type": "string", "description": "Absolute path to a local file (image or audio, 32MB max)."},
            },
            "required": ["slug", "file_path"],
        },
        "fn": t_upload_ref,
    },
    {
        "name": "sbv_adopt_ref",
        "description": "Copy a file already inside another project (found via sbv_list_library) into this board's refs/.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "path": {"type": "string", "description": "Path relative to the data directory."}},
            "required": ["slug", "path"],
        },
        "fn": t_adopt_ref,
    },
    {
        "name": "sbv_list_library",
        "description": "List reusable images or voice clips across all projects (the image/voice picker's contents).",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["image", "voice"], "description": "Default 'image'."}},
        },
        "fn": t_list_library,
    },
    {
        "name": "sbv_delete_library_item",
        "description": "Delete an unused library file. Refuses items still referenced by a board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the data directory, from sbv_list_library."},
                "kind": {"type": "string", "enum": ["image", "voice"]},
            },
            "required": ["path"],
        },
        "fn": t_delete_library_item,
    },
    {
        "name": "sbv_transcribe",
        "description": "Transcribe a reference voice clip to text, for review before it conditions a voice clone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Clip path relative to the data directory."},
                "engine": {"type": "string", "description": "Speech engine id; default: whichever configured engine can transcribe."},
            },
            "required": ["path"],
        },
        "fn": t_transcribe,
    },
    {
        "name": "sbv_dub_shot",
        "description": (
            "Synthesise a shot's spoken dialogue in its speaking character's cloned voice, and mux it "
            "over the clip if one has been rendered. Works before the shot is rendered — call it "
            "before sbv_start_render to hear the line and check it fits: the response's 'warning' "
            "field names it when the spoken line runs longer than the shot's planned frames (or, "
            "once rendered, longer than the actual clip), so a shot can be lengthened or the line "
            "shortened before spending render time on it. Only for a shot whose dialogueSource is "
            "'recording' (a separate TTS take) or unset. Refuses on 'native' dialogueSource: H3 "
            "generates that shot's speech itself, lip-synced, during rendering — there is nothing to "
            "synthesise beforehand, so the only way to get its audio is sbv_start_render."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "shot_id": {"type": "string"},
                "text": {"type": "string", "description": "Line to speak; default: the shot's saved dialogue."},
                "style": {"type": "string", "description": "Voice direction, e.g. 'tired, quiet, breathy'."},
                "dub_mode": {"type": "string", "enum": ["mix", "replace"], "description": "Keep the clip's own audio under the line, or replace it."},
            },
            "required": ["slug", "shot_id"],
        },
        "fn": t_dub_shot,
    },
    {
        "name": "sbv_accept_take",
        "description": "Mark a shot's existing render as current for the board as it now stands, without re-rendering.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "shot_id": {"type": "string"}},
            "required": ["slug", "shot_id"],
        },
        "fn": t_accept_take,
    },
    {
        "name": "sbv_rewrite_prompt",
        "description": "Ask the configured local language model to restyle a shot's prompt. Returns a proposal only — save it with sbv_save_board yourself if you like it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "shot_id": {"type": "string"},
                "text": {"type": "string", "description": "Text to rewrite; default: the shot's saved prompt."},
                "service": {"type": "string", "description": "LLM service id from sbv_info; default: the board's."},
            },
            "required": ["slug", "shot_id"],
        },
        "fn": t_rewrite_prompt,
    },
    {
        "name": "sbv_describe_character",
        "description": "Ask the configured vision-capable language model to draft a character description from a reference portrait.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Reference image path relative to the data directory."},
                "name": {"type": "string"},
                "description": {"type": "string", "description": "Existing description to refine, if any."},
                "service": {"type": "string"},
            },
            "required": ["image"],
        },
        "fn": t_describe_character,
    },
    {
        "name": "sbv_start_render",
        "description": "Start rendering a storyboard's shots (all of them, or a chosen subset) through the video backend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "shot_ids": {"type": "array", "items": {"type": "string"}, "description": "Omit to render every shot that needs it."},
                "board": BOARD_ARG | {"description": "Optional: save this board before rendering it."},
            },
            "required": ["slug"],
        },
        "fn": t_start_render,
    },
    {
        "name": "sbv_stop_render",
        "description": "Stop the render currently in progress.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_stop_render,
    },
    {
        "name": "sbv_status",
        "description": "Current render queue state — same data the UI polls once a second while a render runs.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_status,
    },
    {
        "name": "sbv_assemble",
        "description": "Join a board's rendered shot clips, in order, into final.mp4 without re-rendering anything.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        "fn": t_assemble,
    },
    {
        "name": "sbv_set_data_dir",
        "description": "Change the projects folder (same field as ⚙ Settings → Storyboard data folder). Project directories live directly inside it; takes effect after sbv_restart_server.",
        "inputSchema": {
            "type": "object",
            "properties": {"data_dir": {"type": "string", "description": "Absolute path."}},
            "required": ["data_dir"],
        },
        "fn": t_set_data_dir,
    },
    {
        "name": "sbv_restart_server",
        "description": "Restart the storyboard server process (needed after sbv_set_data_dir). Refuses while a render is running.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_restart_server,
    },
]

TOOL_MAP: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOLS}


# -- MCP: JSON-RPC 2.0 over stdio ------------------------------------------ #
# Deliberately hand-rolled rather than depending on the `mcp` package: the
# subset of the protocol a tools-only server needs (initialize, tools/list,
# tools/call, ping) is a few dozen lines, and this keeps the storyboard repo's
# "nothing to install" property for its own MCP integration too.


def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle_message(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    if not method:
        return
    msg_id = msg.get("id")
    is_request = "id" in msg
    params = msg.get("params") or {}

    def reply(result: dict[str, Any]) -> None:
        if is_request:
            send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def fail(code: int, message: str) -> None:
        if is_request:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    if method == "initialize":
        reply(
            {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )
        return

    if method in ("notifications/initialized", "notifications/cancelled"):
        return

    if method == "ping":
        reply({})
        return

    if method == "tools/list":
        reply(
            {
                "tools": [
                    {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                    for t in TOOLS
                ]
            }
        )
        return

    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_MAP.get(name)
        if tool is None:
            fail(-32602, f"unknown tool {name!r}")
            return
        ensure_server_running()
        try:
            result = tool["fn"](params.get("arguments") or {})
            reply({"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}], "isError": False})
        except Exception as exc:  # noqa: BLE001 — reported to the calling model, not raised
            reply({"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
        return

    fail(-32601, f"method not found: {method}")


def main() -> int:
    ensure_server_running()
    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle_message(msg)
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
