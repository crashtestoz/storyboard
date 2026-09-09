"""Storyboard persistence.

A storyboard is one JSON file, ``storyboard.json``, living in the same folder
as the shots it produced::

    <workspace>/projects/<slug>/
        storyboard.json          <- the whole board: scene, refs, shots
        refs/                    <- uploaded reference images
        shots/01/shot.vpipeline  <- generated per render
        shots/01/clip.mp4
        shots/01/frames/*.png

Keeping the board next to its output means a project folder is self-contained
— copy or zip the folder and you have the board *and* everything it made.
That also makes save/load/export the same operation on the same file rather
than three formats.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BOARD_FILE = "storyboard.json"
SCHEMA = 1


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:60] or "untitled"


def new_id(prefix: str = "s") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def default_shot(defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    d = defaults or {}
    return {
        "id": new_id(),
        "title": "New shot",
        "prompt": "",
        "soundNote": "",
        "characterIds": [],
        "dialogue": "",           # the spoken line, synthesised separately
        "dialogueVoice": "",      # engine voice id, or "" for the default
        "dubUrl": None,           # the clip with speech muxed over it
        "startRef": None,
        "endRef": None,
        "model": d.get("model", "fl2va"),
        "resolution": d.get("resolution", "960x544"),
        "frames": d.get("frames", 124),
        "steps": d.get("steps", 8),
        "seed": 0,
        "status": "draft",
        "progress": 0,
        "runtimeSeconds": None,
        "outputs": [],
        "validation": None,
        "thumb": None,
        "logUrl": None,
        "renderedAs": None,   # "draft" | "final"
    }


def default_character(name: str = "", description: str = "") -> dict[str, Any]:
    """One member of the cast.

    ``name`` and ``description`` are required — the name is how a shot prompt
    refers to them, and the description is what actually conditions the model.
    An image and a voice clip are optional: they become Ref2VA references
    (a picture, and a soundtrack carrying that voice) when a shot using this
    character runs on a model that takes reference lists.
    """
    return {
        "id": new_id("c"),
        "name": name,
        "description": description,
        "image": None,   # {path, url, label}
        "voice": None,   # {path, url, label}
        # What the reference clip says. Qwen3-TTS's voice cloning conditions on
        # the transcript as well as the audio, so a clip alone is not enough;
        # the service can derive this itself via /transcribe.
        "voiceText": "",
    }


def default_board(name: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": name or "Untitled storyboard",
        "sceneDescription": "",
        "soundscape": "",
        "characters": [],
        "styleRefs": [],
        "defaults": {
            "model": "fl2va",
            "resolution": "960x544",
            "frames": 124,
            "steps": 8,
            "draft": False,
            "tts": "none",
        },
        "shots": [],
        "createdAt": time.time(),
        "updatedAt": time.time(),
    }


@dataclass
class Store:
    """Project folders under ``<workspace>/projects``."""

    workspace: Path

    @property
    def root(self) -> Path:
        return self.workspace / "projects"

    # -- locations ------------------------------------------------------- #

    def project_dir(self, slug: str) -> Path:
        safe = slugify(slug)
        return self.root / safe

    def board_path(self, slug: str) -> Path:
        return self.project_dir(slug) / BOARD_FILE

    def refs_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "refs"

    def shot_rel_dir(self, slug: str, index: int) -> str:
        return f"projects/{slugify(slug)}/shots/{index:02d}"

    # -- listing --------------------------------------------------------- #

    def list_boards(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            bp = d / BOARD_FILE
            if not bp.is_file():
                continue
            try:
                board = json.loads(bp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "slug": d.name,
                    "name": board.get("name", d.name),
                    "shots": len(board.get("shots") or []),
                    "updatedAt": board.get("updatedAt") or bp.stat().st_mtime,
                }
            )
        out.sort(key=lambda b: b["updatedAt"], reverse=True)
        return out

    def library(self, limit: int = 300) -> list[dict[str, Any]]:
        """Images already in the workspace, for picking without re-uploading.

        Covers uploaded refs, loose images dropped into a project folder by
        hand, and rendered stills. Per-frame directories are skipped
        deliberately: a handful of clips is thousands of PNGs, and chaining to
        a previous shot's last frame is already a dedicated control rather
        than something you hunt for in a grid.
        """
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.rglob("*")):
            if len(out) >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            # Any per-frame directory, however it is named — "frames",
            # "frames-h3", "frames-ref" all exist in practice, and each holds
            # a whole clip's worth of PNGs that would bury everything else.
            parts = path.relative_to(self.root).parts
            if any(part.startswith("frames") for part in parts[:-1]):
                continue
            rel = path.relative_to(self.workspace)
            project = path.relative_to(self.root).parts[0]
            out.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "url": "/media/" + str(rel).replace("\\", "/"),
                    "label": path.name,
                    "project": project,
                    "bytes": path.stat().st_size,
                    "modifiedAt": path.stat().st_mtime,
                }
            )
        out.sort(key=lambda i: i["modifiedAt"], reverse=True)
        return out

    # -- read / write ---------------------------------------------------- #

    def load(self, slug: str) -> dict[str, Any]:
        bp = self.board_path(slug)
        if not bp.is_file():
            raise FileNotFoundError(f"no storyboard at {bp}")
        board = json.loads(bp.read_text())
        return self.migrate(board)

    def save(self, slug: str, board: dict[str, Any]) -> dict[str, Any]:
        board = self.migrate(board)
        board["updatedAt"] = time.time()
        d = self.project_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "refs").mkdir(exist_ok=True)
        bp = d / BOARD_FILE
        # write via a temp file so an interrupted save cannot truncate the board
        tmp = bp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(board, indent=2) + "\n")
        tmp.replace(bp)
        return board

    def create(self, name: str) -> tuple[str, dict[str, Any]]:
        slug = slugify(name)
        base, n = slug, 2
        while self.board_path(slug).exists():
            slug = f"{base}-{n}"
            n += 1
        board = default_board(name)
        self.save(slug, board)
        return slug, board

    def delete(self, slug: str, *, keep_outputs: bool = True) -> None:
        d = self.project_dir(slug)
        if not d.exists():
            return
        if keep_outputs:
            # Only remove the board; generated media can be expensive to
            # recreate, so deleting a board never throws away renders.
            self.board_path(slug).unlink(missing_ok=True)
        else:
            shutil.rmtree(d)

    def adopt(self, slug: str, rel_path: str) -> dict[str, Any]:
        """Copy an existing workspace file into this project's ``refs/``.

        Picking an image from another project would otherwise leave this board
        pointing into that project's folder: deleting the other project breaks
        this one, and an exported board refers to a file its own folder does
        not contain. Copying keeps the promise the layout is built on — a
        project folder holds everything that project needs.
        """
        src = (self.workspace / rel_path).resolve()
        # never let a crafted path reach outside the workspace
        src.relative_to(self.workspace.resolve())
        if not src.is_file():
            raise FileNotFoundError(f"no such file: {rel_path}")

        refs = self.refs_dir(slug)
        refs.mkdir(parents=True, exist_ok=True)
        dest = refs / src.name
        if dest.resolve() == src:
            # already this project's own ref — nothing to copy
            pass
        else:
            n = 2
            while dest.exists() and dest.stat().st_size != src.stat().st_size:
                dest = refs / f"{src.stem}-{n}{src.suffix}"
                n += 1
            if not dest.exists():
                shutil.copy2(src, dest)

        rel = dest.relative_to(self.workspace)
        return {
            "path": str(rel).replace("\\", "/"),
            "url": "/media/" + str(rel).replace("\\", "/"),
            "label": dest.name,
        }

    # -- import / export -------------------------------------------------- #

    def export(self, slug: str) -> str:
        """The board as a JSON string, suitable for downloading."""
        board = self.load(slug)
        board = dict(board)
        board.pop("_slug", None)
        return json.dumps(board, indent=2) + "\n"

    def import_board(self, raw: str | dict, *, name: str | None = None) -> tuple[str, dict]:
        board = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if not isinstance(board.get("shots"), list):
            raise ValueError("not a storyboard: no 'shots' array")
        board = self.migrate(board)
        board["name"] = name or board.get("name") or "Imported storyboard"
        # An imported board's shots have not been rendered *here*, and their
        # recorded outputs point at another machine's folders. Reset run state
        # rather than show a green board with nothing behind it.
        for shot in board["shots"]:
            shot.update(
                status="draft", progress=0, runtimeSeconds=None,
                outputs=[], validation=None, thumb=None, logUrl=None,
                dubUrl=None,
            )
        slug = slugify(board["name"])
        base, n = slug, 2
        while self.board_path(slug).exists():
            slug = f"{base}-{n}"
            n += 1
        self.save(slug, board)
        return slug, board

    # -- schema ----------------------------------------------------------- #

    def migrate(self, board: dict[str, Any]) -> dict[str, Any]:
        """Fill in anything a board is missing.

        Boards are hand-editable files that outlive the code that wrote them,
        so read defensively: never KeyError on a board written by an older
        version or edited by hand.
        """
        board.setdefault("schema", SCHEMA)
        board.setdefault("name", "Untitled storyboard")
        board.setdefault("sceneDescription", "")
        board.setdefault("soundscape", "")
        board.setdefault("characters", [])
        board.setdefault("styleRefs", [])
        board.setdefault("createdAt", time.time())
        board.setdefault("updatedAt", time.time())

        defaults = board.setdefault("defaults", {})
        defaults.setdefault("model", "fl2va")
        defaults.setdefault("resolution", "960x544")
        defaults.setdefault("frames", 124)
        defaults.setdefault("steps", 8)
        defaults.setdefault("draft", False)
        defaults.setdefault("tts", "none")

        for ch in board.get("characters") or []:
            ch.setdefault("id", new_id("c"))
            ch.setdefault("name", "")
            ch.setdefault("description", "")
            ch.setdefault("image", None)
            ch.setdefault("voice", None)
            ch.setdefault("voiceText", "")

        shots = board.setdefault("shots", [])
        seen: set[str] = set()
        for shot in shots:
            shot.setdefault("id", new_id())
            while shot["id"] in seen:
                shot["id"] = new_id()
            seen.add(shot["id"])
            shot.setdefault("title", "Untitled shot")
            shot.setdefault("prompt", "")
            shot.setdefault("soundNote", "")
            shot.setdefault("characterIds", [])
            shot.setdefault("dialogue", "")
            shot.setdefault("dialogueVoice", "")
            shot.setdefault("dubUrl", None)
            shot.setdefault("startRef", None)
            shot.setdefault("endRef", None)
            shot.setdefault("model", defaults["model"])
            shot.setdefault("resolution", defaults["resolution"])
            shot.setdefault("frames", defaults["frames"])
            shot.setdefault("steps", defaults["steps"])
            shot.setdefault("seed", 0)
            shot.setdefault("status", "draft")
            shot.setdefault("progress", 0)
            shot.setdefault("runtimeSeconds", None)
            shot.setdefault("outputs", [])
            shot.setdefault("validation", None)
            shot.setdefault("thumb", None)
            shot.setdefault("logUrl", None)
            shot.setdefault("renderedAs", None)
        return board
