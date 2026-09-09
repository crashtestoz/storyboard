#!/usr/bin/env python3
"""Path handling in the store: renaming a project, and an independent data dir.

Both of these move files around under a board that has already recorded paths
to them, which is exactly where a silent bug costs someone a thumbnail, a cast
portrait, or a render — with nothing on screen to explain why.

Run:  python3 tests/store_paths.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from backends.base import ShotPaths          # noqa: E402
from store import Store, default_shot        # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'pass' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def seeded_board(st: Store, slug_name: str) -> str:
    """A board with the path-bearing fields a real one accumulates."""
    slug, board = st.create(slug_name)
    shot = default_shot(board["defaults"])
    rel = st.shot_rel_dir(slug, 1)
    shot.update(
        title="One",
        thumb=f"/media/{rel}/frames/frame-0001.png",
        logUrl=f"/media/{rel}/run.log",
        outputs=[f"/media/{rel}/clip.mp4"],
    )
    board["shots"] = [shot]
    board["characters"] = [{
        "id": "c1", "name": "Kira", "description": "a wiry pilot",
        "image": {"path": f"projects/{slug}/refs/kira.png",
                  "url": f"/media/projects/{slug}/refs/kira.png",
                  "label": "kira.png"},
        "voice": None, "voiceText": "",
    }]
    st.save(slug, board)

    # the files those paths name, so "does it still resolve" is a real question
    d = st.project_dir(slug)
    (d / "shots/01/frames").mkdir(parents=True, exist_ok=True)
    (d / "shots/01/frames/frame-0001.png").write_bytes(b"x")
    (d / "shots/01/run.log").write_text("log\n")
    (d / "shots/01/clip.mp4").write_bytes(b"x")
    (d / "refs").mkdir(parents=True, exist_ok=True)
    (d / "refs/kira.png").write_bytes(b"x")
    return slug


def broken_paths(st: Store, board: dict) -> list[str]:
    """Every path the board claims, that does not exist on disk."""
    missing: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str) and "projects/" in node:
            rel = node[len("/media/"):] if node.startswith("/media/") else node
            if not (st.data_dir / rel).exists():
                missing.append(node)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(board)
    return missing


def test_rename(tmp: Path) -> None:
    print("\n-- rename --")
    st = Store(workspace=tmp)
    slug = seeded_board(st, "Ocean Flyby")

    new_slug, board = st.rename(slug, "Canyon Chase")
    check("slug follows the new name", new_slug == "canyon-chase", new_slug)
    check("display name is set", board["name"] == "Canyon Chase")
    check("folder moved", (st.data_dir / "projects/canyon-chase").is_dir())
    check("old folder gone", not (st.data_dir / f"projects/{slug}").exists())
    blob = json.dumps(board)
    check("no stale references to the old slug", f"projects/{slug}/" not in blob)
    check("every recorded path still resolves", not broken_paths(st, board),
          str(broken_paths(st, board)))
    check("cast portrait repointed",
          board["characters"][0]["image"]["url"]
          == "/media/projects/canyon-chase/refs/kira.png")

    # a name that slugifies to the same thing must not move anything
    same, board2 = st.rename("canyon-chase", "Canyon  Chase!!")
    check("same-slug rename does not move the folder", same == "canyon-chase")
    check("display name still updates", board2["name"] == "Canyon  Chase!!")

    # renaming onto a taken name must not clobber the other project
    other = seeded_board(st, "Second")
    taken, _ = st.rename(other, "Canyon Chase")
    check("collision gets a suffix", taken == "canyon-chase-2", taken)
    check("the original survives a collision",
          (st.data_dir / "projects/canyon-chase/storyboard.json").is_file())

    try:
        st.rename("canyon-chase", "   ")
        check("empty name is refused", False, "no error raised")
    except ValueError:
        check("empty name is refused", True)

    try:
        st.rename("no-such-project", "Whatever")
        check("missing board is refused", False, "no error raised")
    except FileNotFoundError:
        check("missing board is refused", True)

    # a slug that is a prefix of another's must not be caught by the rewrite
    a = seeded_board(st, "test")
    b = seeded_board(st, "test video")
    board_a = st.load(a)
    board_a["shots"][0]["thumb"] = f"/media/projects/{b}/shots/01/other.png"
    st.save(a, board_a)
    moved, board_a = st.rename(a, "Renamed")
    check("another project's path is left alone",
          board_a["shots"][0]["thumb"] == f"/media/projects/{b}/shots/01/other.png",
          board_a["shots"][0]["thumb"])
    check("this project's own paths still move",
          board_a["shots"][0]["outputs"][0] == f"/media/projects/{moved}/shots/01/clip.mp4",
          board_a["shots"][0]["outputs"][0])


def test_independent_data_dir(tmp: Path) -> None:
    print("\n-- data dir separate from the vpipe workspace --")
    ws = tmp / "vpipe-workspace"
    data = tmp / "my-storyboards"
    (ws / "models").mkdir(parents=True)
    st = Store(workspace=ws, data_dir=data)

    slug = seeded_board(st, "Elsewhere")
    check("projects live under the data dir",
          (data / "projects" / slug / "storyboard.json").is_file())
    check("nothing is written into the workspace",
          not (ws / "projects").exists())

    board = st.load(slug)
    check("stored paths stay relative, so the folder is portable",
          all(not p.startswith("/") or p.startswith("/media/")
              for p in [board["characters"][0]["image"]["path"]]))
    check("recorded paths resolve against the data dir",
          not broken_paths(st, board), str(broken_paths(st, board)))

    # what goes inside a generated pipeline has to be absolute, because the
    # data dir is not under the cwd vpipe is launched from
    paths = ShotPaths(
        workspace=ws,
        abs_dir=data / st.shot_rel_dir(slug, 1),
        rel_dir=st.shot_rel_dir(slug, 1),
        data_dir=data,
    )
    check("pipeline output dir is absolute", Path(paths.pipe_dir).is_absolute())
    check("pipeline output dir is in the data dir",
          str(data) in paths.pipe_dir, paths.pipe_dir)
    check("pipeline frames dir is in the data dir",
          str(data) in paths.pipe_frames, paths.pipe_frames)
    ref = paths.pipe_path(board["characters"][0]["image"]["path"])
    check("a stored reference resolves to a real absolute file",
          Path(ref).is_absolute() and Path(ref).is_file(), ref)

    # defaulting: no data_dir means the workspace, so an existing install is
    # unaffected by any of this
    st2 = Store(workspace=ws)
    check("data dir defaults to the workspace", st2.data_dir == ws)
    check("default layout is the historical one",
          st2.root == ws / "projects")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sbv-store-test-"))
    try:
        test_rename(tmp / "a")
        test_independent_data_dir(tmp / "b")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
