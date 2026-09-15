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

import hashlib
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


def last_saved_frame(frames_dir: Path, expected_frames: int = 0) -> Path | None:
    """Return the last frame belonging to the current requested length.

    Re-rendering into ``frame-%04d.png`` does not remove files from a longer
    previous take. Choosing ``frames[-1]`` therefore made a chained shot use
    an old frame whenever the new take was shorter. Prefer the highest frame
    index below the current request; if a short/sketch render has fewer files,
    its actual last file remains the correct fallback.
    """
    frames = sorted(frames_dir.glob("*.png")) if frames_dir.exists() else []
    if not frames:
        return None
    if expected_frames > 0:
        usable = []
        for frame in frames:
            match = re.search(r"-(\d+)$", frame.stem)
            if match and int(match.group(1)) < expected_frames:
                usable.append(frame)
        if usable:
            return usable[-1]
    return frames[-1]


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


def _media_rel(value: str) -> str:
    if value.startswith("/media/"):
        value = value[len("/media/"):]
    if value.startswith("projects/"):
        return value
    return ""


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ref_path(ref: Any) -> str:
    if isinstance(ref, str):
        return _media_rel(ref)
    if not isinstance(ref, dict):
        return ""
    return _media_rel(ref.get("path") or ref.get("url") or ref.get("resolved") or "")


def _usage_label(index: int, shot: dict[str, Any] | None, label: str) -> str:
    if shot:
        title = (shot.get("title") or "").strip()
        head = f"Scene {index}"
        if title:
            head += f" - {title}"
        return f"{head} - {label}"
    return label


def _add_usage(
    usages: dict[str, list[str]],
    ref: Any,
    label: str,
    *,
    index: int | None = None,
    shot: dict[str, Any] | None = None,
) -> None:
    rel = _ref_path(ref)
    if not rel:
        return
    name = _usage_label(index or 0, shot, label) if shot else label
    bucket = usages.setdefault(rel, [])
    if name not in bucket:
        bucket.append(name)


def _collect_media_usages(board: dict[str, Any]) -> dict[str, list[str]]:
    usages: dict[str, list[str]] = {}
    shots = board.get("shots") or []
    characters = {
        c.get("id"): c
        for c in (board.get("characters") or [])
        if c.get("id")
    }

    for i, ref in enumerate(board.get("styleRefs") or [], start=1):
        _add_usage(usages, ref, f"Style reference {i}")

    for idx, shot in enumerate(shots, start=1):
        _add_usage(usages, shot.get("startRef"), "Start", index=idx, shot=shot)
        _add_usage(usages, shot.get("endRef"), "End", index=idx, shot=shot)
        for ref_idx, ref in enumerate(shot.get("referenceImages") or [], start=1):
            _add_usage(usages, ref, f"Shot reference {ref_idx}",
                       index=idx, shot=shot)
        for cid in shot.get("characterIds") or []:
            ch = characters.get(cid)
            if not ch:
                continue
            name = (ch.get("name") or "Character").strip()
            _add_usage(usages, ch.get("image"), f"Character {name}", index=idx, shot=shot)

    used_in_shots = {
        cid
        for shot in shots
        for cid in (shot.get("characterIds") or [])
    }
    for ch in characters.values():
        if ch.get("id") in used_in_shots:
            continue
        name = (ch.get("name") or "Character").strip()
        _add_usage(usages, ch.get("image"), f"Cast - {name}")

    return usages


def new_id(prefix: str = "s") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# What a render was of
# ---------------------------------------------------------------------------

def _ref_key(ref: Any) -> str | None:
    """A reference reduced to what identifies it, and nothing that moves.

    A chain's ``resolved`` frame is included because it is the actual image
    consumed by the render. The resolver keeps it stable when the source take
    is unchanged, but changing from a stale longer-take tail to the current
    tail must invalidate the dependent shot.
    """
    if not ref:
        return None
    if isinstance(ref, str):
        return ref
    if not isinstance(ref, dict):
        return None
    if ref.get("kind") == "chain":
        return f"chain:{ref.get('from') or ''}:{ref.get('resolved') or ''}"
    return ref.get("path") or ref.get("url") or ""


def speech_fingerprint(shot: dict, board: dict) -> str:
    from .dubbing import speaker_for
    speaker = speaker_for(shot, board) or {}
    payload = [shot.get("dialogue", "").strip(), shot.get("dialogueStyle", "").strip(),
               speaker.get("id"), _ref_key(speaker.get("voice")), speaker.get("voiceText"),
               shot.get("dialogueVoice"), (board.get("defaults") or {}).get("tts")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def render_fingerprint(shot: dict[str, Any], board: dict[str, Any]) -> str:
    """Hash of everything that decides what a render of *shot* produces.

    Recorded on a shot when it renders, so "has this been rendered?" can be
    answered as "rendered from *what the board says now*?" — a distinction
    that cost a whole run: every shot was marked done, so a rewritten prompt
    was skipped and the batch cheerfully re-reported clips of the old words.

    Only conditioning and geometry count. ``dialogue`` is included because it
    is passed to the video model as a visual mouth-movement cue; the audio is
    still synthesised outside the render path and muxed over the finished clip.
    """
    defaults = board.get("defaults") or {}
    wanted = set(shot.get("characterIds") or [])
    cast = [
        {
            "name": (c.get("name") or "").strip(),
            "description": (c.get("description") or "").strip(),
            "image": _ref_key(c.get("image")),
        }
        for c in (board.get("characters") or [])
        if c.get("id") in wanted
    ]
    payload = {
        # Ref2VA now receives one ordered set containing start/end, shot,
        # Cast and project references. Bump this whenever the routing changes
        # so old clips are re-rendered instead of being treated as current.
        "layeringVersion": 4,
        "speechInputs": speech_fingerprint(shot, board),
        "recording": shot.get("dialogueAudioUrl") if shot.get("dialogueSource") == "recording" else None,
        "dialogueSource": shot.get("dialogueSource", "auto"),
        "dialogueStyle": shot.get("dialogueStyle", ""),
        "speakerId": shot.get("speakerId", ""),
        "dialogueVoice": shot.get("dialogueVoice", ""),
        "dubMode": shot.get("dubMode", "mix"),
        "voices": [{"id": c.get("id"), "voice": _ref_key(c.get("voice")), "voiceText": c.get("voiceText", "")} for c in board.get("characters", []) if c.get("id") in wanted or c.get("id") == shot.get("speakerId")],
        "referenceMetadata": [{"tag": r.get("tag", ""), "role": r.get("role", "")} for r in ([shot.get("startRef"), shot.get("endRef")] + (shot.get("referenceImages") or []) + (board.get("styleRefs") or []) + [c.get("image") for c in board.get("characters", []) if c.get("id") in wanted]) if isinstance(r, dict)],
        "prompt": (shot.get("prompt") or "").strip(),
        "dialogue": (shot.get("dialogue") or "").strip(),
        "soundNote": (shot.get("soundNote") or "").strip(),
        "scene": (board.get("sceneDescription") or "").strip(),
        "soundscape": (board.get("soundscape") or "").strip()
        if board.get("soundscapeInShots", True) else "",
        "soundscapeInShots": bool(board.get("soundscapeInShots", True)),
        "cast": cast,
        "styleRefs": [_ref_key(r) for r in (board.get("styleRefs") or [])],
        "startRef": _ref_key(shot.get("startRef")),
        "endRef": _ref_key(shot.get("endRef")),
        "referenceImages": [
            _ref_key(r) for r in (shot.get("referenceImages") or [])
        ],
        "model": shot.get("model") or defaults.get("model") or "",
        # Frame size is a project setting with a per-shot fallback, in the same
        # order the backend resolves it.
        "resolution": defaults.get("resolution") or shot.get("resolution") or "",
        "frames": int(shot.get("frames") or 0),
        "steps": int(shot.get("steps") or 0),
        "seed": int(shot.get("seed") or 0),
        "draft": bool(defaults.get("draft")),
        "sketch": bool(defaults.get("draft")) and bool(defaults.get("sketch")),
    }
    if payload["draft"]:
        payload["draftProfile"] = "384-long-edge-4-step-with-audio"
    if payload["sketch"]:
        payload["sketchProfile"] = "min-frames-stretched-silent-pencil-sketch"
    if payload["model"] == "ref2va":
        payload["ref2vaProfile"] = "ordered-reference-set-v3"
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def stale_reason(shot: dict[str, Any], board: dict[str, Any]) -> str:
    """Why this shot's clip is not a render of what the board now says.

    Empty string when the clip is current — or when there is no clip at all,
    since ``status`` already says that and calling it stale as well would
    report the same thing twice.
    """
    if not shot.get("outputs"):
        return ""
    recorded = shot.get("renderFingerprint")
    if not recorded:
        # Rendered by a version that kept no record of its inputs. It may well
        # be current, but nothing here can show that it is, and claiming so is
        # how a stale clip reaches the final cut.
        return "rendered before the board started tracking changes, so it cannot be shown to match"
    if recorded != render_fingerprint(shot, board):
        return "the prompt, references or clip settings changed since this was rendered"
    return ""


def default_shot(defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    d = defaults or {}
    return {
        "id": new_id(),
        "title": "New shot",
        "prompt": "",
        "soundNote": "",
        "characterIds": [],
        "dialogue": "",           # the spoken line, synthesised separately
        "dialogueSource": "recording", # use the approved Dialogue-window take
        "dialogueStyle": "",      # delivery notes for TTS, not spoken text
        "dialogueVoice": "",      # engine voice id, or "" for the default
        "speakerId": "",          # which cast member says it; "" = infer
        "dialogueAudioUrl": None, # the spoken line on its own, for preview
        "dubUrl": None,           # the clip with speech muxed over it
        # "mix": lay the spoken line over the clip's own generated audio, so
        # ambience (engine hum, wind) survives under it. "replace": the
        # dubbed clip carries only the spoken line — for when the video model
        # ignored the "no voice" prompt instruction and generated its own
        # mumbled dialogue, which "mix" would otherwise leave audible under
        # the real line as a second, overlapping voice.
        "dubMode": "mix",
        "startRef": None,
        "endRef": None,
        "referenceImages": [],
        # Storyboard video shots use Ref2VA so the same render can consume
        # start/end references, cast media and other shot references.
        "model": "ref2va",
        "resolution": d.get("resolution", "960x544"),
        "frames": d.get("frames", 124),
        "steps": d.get("steps", 8),
        "seed": 0,
        "status": "draft",
        "reason": "",
        "progress": 0,
        "runtimeSeconds": None,
        "outputs": [],
        "validation": None,
        "thumb": None,
        "logUrl": None,
        "renderedAs": None,   # "draft" | "final"
        # What the last render was of, so a board edit can be told from a
        # board that has simply not been rendered. See render_fingerprint.
        "renderFingerprint": None,
        # The words the stored dialogue.wav actually says. A line edited after
        # it was spoken must not be muxed from the old take.
        "dialogueSpokenText": "",
        "dialogueSpokenStyle": "",
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
            "model": "ref2va",
            "resolution": "960x544",
            "frames": 124,
            "steps": 8,
            "draft": False,
            "sketch": False,
            "stillsSize": "small",
            "tts": "none",
            "llm": "",       # "" means: use the server's default service
        },
        "shots": [],
        "finalVideo": None,
        "outputMuted": False,
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
        usages = self.media_usages()
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
            rel_name = str(rel).replace("\\", "/")
            uses = usages.get(rel_name, [])
            project = path.relative_to(self.root).parts[0]
            out.append(
                {
                    "path": rel_name,
                    "url": "/media/" + rel_name,
                    "label": path.name,
                    "project": project,
                    "kind": kind,
                    "used": bool(uses),
                    "uses": uses,
                    "digest": _file_digest(path) if kind == "image" else "",
                    "bytes": path.stat().st_size,
                    "modifiedAt": path.stat().st_mtime,
                }
            )
        out.sort(key=lambda i: i["modifiedAt"], reverse=True)
        return out

    def used_media_paths(self) -> set[str]:
        """Media paths still referenced by any storyboard."""
        return set(self.media_usages())

    def media_usages(self) -> dict[str, list[str]]:
        """Media paths and human-readable places that still reference them."""
        out: dict[str, list[str]] = {}
        if not self.root.exists():
            return out
        for bp in self.root.glob(f"*/{BOARD_FILE}"):
            try:
                board = json.loads(bp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            project = (board.get("name") or bp.parent.name).strip()
            for rel, labels in _collect_media_usages(board).items():
                bucket = out.setdefault(rel, [])
                for label in labels:
                    full = f"{project} - {label}"
                    if full not in bucket:
                        bucket.append(full)
        return out

    def delete_library_item(self, rel_path: str, *, kind: str = "image") -> dict[str, Any]:
        """Remove an unreferenced media file from the projects tree."""
        exts = self.LIBRARY_EXTS.get(kind) or self.LIBRARY_EXTS["image"]
        data_dir = self.data_dir.resolve()
        root = self.root.resolve()
        path = (data_dir / rel_path).resolve()
        path.relative_to(data_dir)
        path.relative_to(root)
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {rel_path}")
        if path.suffix.lower() not in exts:
            raise ValueError(f"not a {kind} library file: {rel_path}")

        rel_name = str(path.relative_to(data_dir)).replace("\\", "/")
        if rel_name in self.used_media_paths():
            raise ValueError("that file is still used by a storyboard")

        path.unlink()
        return {"ok": True, "path": rel_name}

    # -- read / write ---------------------------------------------------- #

    def load(self, slug: str) -> dict[str, Any]:
        bp = self.board_path(slug)
        if not bp.is_file():
            raise FileNotFoundError(f"no storyboard at {bp}")
        board = json.loads(bp.read_text())
        before_migrate = json.dumps(board, sort_keys=True, separators=(",", ":"))
        board = self.migrate(board)
        migrated = json.dumps(board, sort_keys=True, separators=(",", ":")) != before_migrate
        # Self-heal on the way out: a board saved before rehoming existed,
        # or one hand-copied from another project's folder, can still point
        # at media that lives elsewhere. Every read is a chance to notice
        # and fix that before it's shown to anyone, not just the moment of
        # import. Same idea for a chain ref whose source already has frames
        # but has never had its preview resolved.
        healed = migrated
        healed = self._rehome_media(slug, board) or healed
        healed = self._resolve_chain_previews(slug, board) or healed
        if healed:
            board = self.save(slug, board)
        return board

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

    def _rehome_ref(self, slug: str, ref: Any) -> Any:
        """``adopt`` a single reference into *slug*, in place.

        A ref that carries no real file (a chain reference, an empty slot),
        or that already lives under *slug*'s own ``refs/``, is returned
        completely untouched — same object, so a caller can tell by identity
        whether anything needed fixing at all. Only a path pointing at some
        other project's folder is actually copied in. One whose file no
        longer exists under ``data_dir`` at all — an import from another
        machine — is dropped rather than kept pointing at a path this
        project does not have, which would just move the "points outside
        the project" bug from a resolvable case to an unresolvable one.
        """
        rel = _ref_path(ref)
        if not rel or rel.startswith(f"projects/{slug}/"):
            return ref
        try:
            adopted = self.adopt(slug, rel)
        except FileNotFoundError:
            return None
        return {**ref, **adopted} if isinstance(ref, dict) else adopted

    def _resolve_chain_previews(self, slug: str, board: dict[str, Any]) -> bool:
        """Point every chain ref at its source shot's actual last frame.

        The orchestrator also does this (``Orchestrator._resolve_chain``),
        but only right before *this* shot next renders — so a board whose
        source shot already has frames, rendered before that shot ever ran
        or before this preview existed at all, would otherwise show the
        Frame anchors panel's "⛓ chained" placeholder forever, with no
        picture, despite the frame already sitting on disk. Every load is a
        chance to catch it up, the same way ``_rehome_media`` self-heals
        stale references.
        """
        changed = False
        shots = board.get("shots") or []
        index_by_id = {s.get("id"): i for i, s in enumerate(shots)}
        for shot in shots:
            for key in ("startRef", "endRef"):
                ref = shot.get(key)
                if not isinstance(ref, dict) or ref.get("kind") != "chain":
                    continue
                src_idx = index_by_id.get(ref.get("from"))
                if src_idx is None:
                    continue
                frames_dir = (
                    self.data_dir / self.shot_rel_dir(slug, src_idx + 1) / "frames"
                )
                frame = last_saved_frame(
                    frames_dir, int(shots[src_idx].get("frames") or 0)
                )
                if frame is None:
                    continue
                resolved = str(frame.relative_to(self.data_dir)).replace("\\", "/")
                if ref.get("resolved") != resolved:
                    ref["resolved"] = resolved
                    changed = True
        return changed

    def _rehome_media(self, slug: str, board: dict[str, Any]) -> bool:
        """Copy every reference image a board points at into *slug*'s own refs/, in place.

        A board can carry ``styleRefs``, cast portraits and shot references
        whose ``path``/``url`` point into a *different* project's folder —
        from an import, a hand-copied project directory, or a board edited
        outside the app. Left alone, this project silently depends on that
        other project's folder: deleting or renaming it breaks references
        here, and two projects end up appearing to share what look like each
        other's style references or cast portraits. This gives every
        reference in the board the same treatment ``adopt`` already gives a
        single picked image, and reports whether anything actually moved so
        the caller knows whether the board needs re-saving.
        """
        slug = slugify(slug)
        changed = False

        def rehome_list(refs: list[Any]) -> list[Any]:
            nonlocal changed
            out = []
            for ref in refs:
                new = self._rehome_ref(slug, ref)
                if new is not ref:
                    changed = True
                if new is not None:
                    out.append(new)
            return out

        board["styleRefs"] = rehome_list(board.get("styleRefs") or [])
        for ch in board.get("characters") or []:
            if ch.get("image"):
                new = self._rehome_ref(slug, ch["image"])
                changed = changed or new is not ch["image"]
                ch["image"] = new
        for shot in board.get("shots") or []:
            for key in ("startRef", "endRef"):
                if shot.get(key):
                    new = self._rehome_ref(slug, shot[key])
                    changed = changed or new is not shot[key]
                    shot[key] = new
            if isinstance(shot.get("referenceImages"), list):
                shot["referenceImages"] = rehome_list(shot["referenceImages"])
        return changed

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
        self._rehome_media(slug, board)
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
        board.setdefault("soundscapeInShots", True)
        board.setdefault("characters", [])
        board.setdefault("styleRefs", [])
        board.setdefault("createdAt", time.time())
        board.setdefault("updatedAt", time.time())
        # The assembled cut: {url, builtAt, parts, missing, seconds}. None
        # until the shots have been concatenated at least once.
        board.setdefault("finalVideo", None)
        # Output playback preference. It belongs to the project so switching
        # boards does not unexpectedly turn sound back on (or off).
        board.setdefault("outputMuted", False)

        defaults = board.setdefault("defaults", {})
        # The video model is a Storyboard invariant, not a project preference.
        defaults["model"] = "ref2va"
        defaults.setdefault("resolution", "960x544")
        defaults.setdefault("frames", 124)
        defaults.setdefault("steps", 8)
        defaults.setdefault("draft", False)
        defaults.setdefault("sketch", False)
        defaults.setdefault("stillsSize", "small")
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
            shot.setdefault("dialogueStyle", "")
            shot.setdefault("dialogueVoice", "")
            shot.setdefault("dubUrl", None)
            shot.setdefault("dubMode", "mix")
            shot.setdefault("startRef", None)
            shot.setdefault("endRef", None)
            shot.setdefault("referenceImages", [])
            shot.setdefault("model", defaults["model"])
            # Older boards exposed FL2VA as a per-shot choice. Keep their
            # content and references, but normalize video generation to the
            # single Storyboard model. Create Stills uses a synthetic copy of
            # the shot and remains independent of this persisted value.
            if shot.get("model") != "ref2va":
                shot["model"] = "ref2va"
            shot.setdefault("resolution", defaults["resolution"])
            shot.setdefault("frames", defaults["frames"])
            shot.setdefault("steps", defaults["steps"])
            shot.setdefault("seed", 0)
            shot.setdefault("status", "draft")
            shot.setdefault("reason", "")
            shot.setdefault("progress", 0)
            shot.setdefault("runtimeSeconds", None)
            shot.setdefault("outputs", [])
            shot.setdefault("validation", None)
            shot.setdefault("thumb", None)
            shot.setdefault("logUrl", None)
            shot.setdefault("renderedAs", None)
            shot.setdefault("renderFingerprint", None)
            shot.setdefault("dialogueSpokenText", "")
            shot.setdefault("dialogueSpokenStyle", "")
        return board
