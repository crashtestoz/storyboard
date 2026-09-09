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


def _rewrite_slug(node: Any, old: str, new: str) -> Any:
    """Repoint every stored path from one project slug to another.

    Two forms occur: browser URLs (``/media/projects/<slug>/...``) and
    workspace-relative paths (``projects/<slug>/...``). Both are anchored on
    ``projects/`` and end at a slash, so a project whose slug is a prefix of
    another's cannot be caught by accident.
    """
    if isinstance(node, str):
        return node.replace(f"projects/{old}/", f"projects/{new}/")
    if isinstance(node, list):
        return [_rewrite_slug(v, old, new) for v in node]
    if isinstance(node, dict):
        return {k: _rewrite_slug(v, old, new) for k, v in node.items()}
    return node


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
        "speakerId": "",          # which cast member says it; "" = infer
        "dialogueAudioUrl": None, # the spoken line on its own, for preview
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
            "llm": "",       # "" means: use the server's default service
        },
        "shots": [],
        "createdAt": time.time(),
        "updatedAt": time.time(),
    }


@dataclass
class Store:
    """Project folders under ``<data_dir>/projects``.

    Two roots, deliberately separate:

    ``workspace``
        Where vpipe is launched from. Not ours — vpipe resolves ``models/``
        and its LMDB model registry relative to it, so it is dictated by
        where the models were prepared. Read only, for offering existing
        renders to pick from.

    ``data_dir``
        Where the storyboards and their uploads live. Ours entirely, and by
        default the workspace so nothing moves for an existing install — but
        it can be anywhere, which is the point: a storyboard and its
        references are the user's documents and should not have to live inside
        another tool's runtime directory to be usable.
    """

    workspace: Path
    data_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.data_dir is None:
            self.data_dir = self.workspace

    @property
    def root(self) -> Path:
        return self.data_dir / "projects"

    # -- locations ------------------------------------------------------- #

    def project_dir(self, slug: str) -> Path:
        safe = slugify(slug)
        return self.root / safe

    def board_path(self, slug: str) -> Path:
        return self.project_dir(slug) / BOARD_FILE

    def refs_dir(self, slug: str) -> Path:
        return self.project_dir(slug) / "refs"

    def shot_rel_dir(self, slug: str, index: int) -> str:
        """A shot's directory, relative to ``data_dir``.

        This is the form stored in a board and served under ``/media/``. It
        stays relative so a board keeps working when the data directory moves,
        which is what makes a project folder portable.
        """
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
            shots = board.get("shots") or []
            out.append(
                {
                    "slug": d.name,
                    "name": board.get("name", d.name),
                    "shots": len(shots),
                    # What the folder actually holds, so two boards with the
                    # same name are distinguishable: an imported copy carries
                    # the shot list but none of the renders, and picking the
                    # wrong one is how you lose track of your work.
                    "rendered": sum(1 for sh in shots if sh.get("outputs")),
                    "configPath": str(bp),
                    "updatedAt": board.get("updatedAt") or bp.stat().st_mtime,
                }
            )
        out.sort(key=lambda b: b["updatedAt"], reverse=True)
        return out

    #: What the picker can offer, by kind. Audio is here because a voice clip
    #: already uploaded to a project was otherwise unreachable: the only way to
    #: attach one was to upload it again from the filesystem, so a clip sitting
    #: in a project's refs/ could not be linked to a character at all.
    LIBRARY_EXTS = {
        "image": {".png", ".jpg", ".jpeg", ".webp"},
        "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
    }

    def library(self, limit: int = 300, kind: str = "image") -> list[dict[str, Any]]:
        """Files already in the projects tree, for picking without re-uploading.

        Covers uploaded refs, loose files dropped into a project folder by
        hand, and rendered stills. Per-frame directories are skipped
        deliberately: a handful of clips is thousands of PNGs, and chaining to
        a previous shot's last frame is already a dedicated control rather
        than something you hunt for in a grid.
        """
        exts = self.LIBRARY_EXTS.get(kind) or self.LIBRARY_EXTS["image"]
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
            rel = path.relative_to(self.data_dir)
            project = path.relative_to(self.root).parts[0]
            out.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "url": "/media/" + str(rel).replace("\\", "/"),
                    "label": path.name,
                    "project": project,
                    "kind": kind,
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

    def rename(self, slug: str, new_name: str) -> tuple[str, dict[str, Any]]:
        """Rename a project: its display name **and** its folder.

        The folder is the slug, and the slug is baked into every path the
        board has recorded — thumbnails, run logs, outputs, character
        portraits, style references. So renaming the display name alone would
        leave the folder on disk saying something else, and moving the folder
        alone would break every one of those paths. This does both, and
        rewrites the paths to match.

        The rewrite walks the whole board rather than naming the fields it
        knows about, because the fields holding paths have grown over time and
        a rename that silently missed one would cost the user a thumbnail or a
        cast portrait with nothing to explain why.

        Returns the new slug, which the caller must use from here on.
        """
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("a project needs a name")

        old_slug = slugify(slug)
        board = self.load(old_slug)          # raises if there is no such board

        new_slug = slugify(new_name)
        if new_slug != old_slug:
            base, n = new_slug, 2
            while self.project_dir(new_slug).exists():
                new_slug = f"{base}-{n}"
                n += 1

            # Move first: if this fails, nothing has been changed at all.
            self.project_dir(old_slug).rename(self.project_dir(new_slug))
            board = _rewrite_slug(board, old_slug, new_slug)

        board["name"] = new_name
        board = self.save(new_slug, board)
        return new_slug, board

    def delete(self, slug: str, *, keep_outputs: bool = True) -> None:
        d = self.project_dir(slug)
        if not d.exists():
            return
        if keep_outputs:
            # Only remove the board; generated media can be expensive to
            # recreate, so deleting a board never throws away renders.
            self.board_path(slug).unlink(missing_ok=True)
            # If nothing was left worth keeping, do not leave an empty shell
            # of a folder behind either. "Nothing" means literally no files
            # anywhere under it — anything at all, and the folder stays.
            try:
                if not any(p.is_file() for p in d.rglob("*")):
                    shutil.rmtree(d)
            except OSError:
                pass
        else:
            shutil.rmtree(d)

    def adopt(self, slug: str, rel_path: str) -> dict[str, Any]:
        """Copy an existing file from the projects tree into this project's ``refs/``.

        Picking an image from another project would otherwise leave this board
        pointing into that project's folder: deleting the other project breaks
        this one, and an exported board refers to a file its own folder does
        not contain. Copying keeps the promise the layout is built on — a
        project folder holds everything that project needs.
        """
        src = (self.data_dir / rel_path).resolve()
        # never let a crafted path reach outside the data directory
        src.relative_to(self.data_dir.resolve())
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

        rel = dest.relative_to(self.data_dir)
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

    def reconcile_startup(self) -> list[str]:
        """Demote run states that cannot still be true.

        A shot left "running" or "queued" when the process exited has no one
        watching it any more, so it would sit there forever claiming to be in
        progress. Called once at startup, before any render can be active —
        doing it in migrate() would corrupt the state of a live run, since the
        orchestrator re-reads the board between shots.
        """
        touched: list[str] = []
        for entry in self.list_boards():
            slug = entry["slug"]
            try:
                board = self.load(slug)
            except (OSError, ValueError):
                continue
            changed = False
            for shot in board.get("shots") or []:
                if shot.get("status") == "running":
                    shot["status"] = "interrupted"
                    changed = True
                    touched.append(f"{slug}/{shot.get('title') or shot['id']}")
                elif shot.get("status") == "queued":
                    shot["status"] = "draft"
                    changed = True
            if changed:
                self.save(slug, board)
        return touched

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
        defaults.setdefault("llm", "")

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
            shot.setdefault("speakerId", "")
            shot.setdefault("dialogueAudioUrl", None)
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
