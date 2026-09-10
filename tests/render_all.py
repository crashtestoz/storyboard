#!/usr/bin/env python3
"""What "Render all" picks up, and whether a cut comes out the other end.

Both halves of a real failure. A board of three shots was run from the UI; it
reported success, rendered one shot, and produced no video:

*   Two shots were marked ``done``, so the batch skipped them — even though
    their prompts had been rewritten since. The clips it kept were renders of
    the replaced words, and nothing said so.
*   There was no concat pass at all, so "the final video" existed only as a
    folder of clips.

So: a shot whose inputs have moved is pending again, a shot whose inputs have
not is left alone (a re-render costs half an hour and must not be casual), a
shot chained to one being re-run comes with it, and a board of clips assembles
into one file.

No backend and no models here — ``_pending`` is pure, and the assembly half
runs on two one-second clips made by ffmpeg.

Run:  python3 tests/render_all.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.assemble import (                                  # noqa: E402
    assemble,
    final_stale_reason,
    frame_size,
    shot_clip,
)
from server.orchestrator import Orchestrator                   # noqa: E402
from server.store import (                                     # noqa: E402
    Store,
    default_board,
    default_shot,
    render_fingerprint,
    stale_reason,
)
from server.backends.vpipe_backend import _resolved_prompt      # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'pass' if ok else 'FAIL'}  {name}" + ("" if ok else f"  — {detail}"))
    if not ok:
        failures.append(name)


def section(title: str) -> None:
    print(f"\n-- {title} --")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def a_board(n: int = 3) -> dict:
    """A board of *n* rendered shots, each recorded as a render of itself."""
    board = default_board("Test board")
    board["sceneDescription"] = "An ocean at golden hour."
    board["soundscape"] = "Deep engine roar."
    for i in range(n):
        shot = default_shot(board["defaults"])
        shot["id"] = f"s{i + 1}"
        shot["title"] = f"Shot {i + 1}"
        shot["prompt"] = f"Something happens, take {i + 1}."
        shot["status"] = "done"
        shot["outputs"] = [f"/media/projects/test/shots/{i + 1:02d}/clip.mp4"]
        board["shots"].append(shot)
    # Stamp each one as a render of what the board currently says.
    for shot in board["shots"]:
        shot["renderFingerprint"] = render_fingerprint(shot, board)
    return board


class StubBackend:
    id = "stub"
    label = "Stub"

    def health(self):
        return True, ""


def an_orchestrator(tmp: Path) -> Orchestrator:
    store = Store(workspace=tmp, data_dir=tmp)
    return Orchestrator(StubBackend(), store, workspace=tmp, data_dir=tmp)


def pending_ids(orch: Orchestrator, board: dict) -> list[str]:
    targets, _why = orch._pending(board)
    return [s["id"] for s in targets]


def make_clip(path: Path, seconds: float = 1.0, colour: str = "red",
              with_audio: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-t", f"{seconds}",
        "-i", f"color=c={colour}:s=320x176:r=24",
    ]
    if with_audio:
        argv += [
            "-f", "lavfi", "-t", f"{seconds}",
            "-i", "sine=frequency=440:sample_rate=48000",
            "-c:a", "aac",
        ]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(argv, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# what a whole-board run picks up
# ---------------------------------------------------------------------------

def test_pending(tmp: Path) -> None:
    orch = an_orchestrator(tmp)

    section("a board that is entirely up to date")
    board = a_board()
    check("nothing is pending", pending_ids(orch, board) == [],
          str(pending_ids(orch, board)))

    section("a rewritten prompt")
    board = a_board()
    board["shots"][0]["prompt"] = "Something else entirely happens now."
    ids = pending_ids(orch, board)
    check("the rewritten shot is pending again", ids == ["s1"], str(ids))
    check("the untouched shots are left alone", "s2" not in ids and "s3" not in ids)
    _t, why = orch._pending(board)
    check("and it says why", "changed" in why.get("s1", ""), why.get("s1", ""))

    section("the other inputs that decide a render")
    for field, value in (
        ("model", "ref2va"),
        ("frames", 39),
        ("steps", 16),
        ("seed", 7),
        ("soundNote", "A door slams."),
    ):
        board = a_board()
        board["shots"][1][field] = value
        check(f"changing {field} makes it pending",
              pending_ids(orch, board) == ["s2"])

    section("project-wide changes reach every shot")
    board = a_board()
    board["sceneDescription"] = "A desert at night."
    check("a new scene description makes all three pending",
          pending_ids(orch, board) == ["s1", "s2", "s3"])

    board = a_board()
    board["defaults"]["draft"] = True
    check("switching to draft makes all three pending",
          pending_ids(orch, board) == ["s1", "s2", "s3"])

    section("dialogue drives mouth movement")
    board = a_board()
    board["shots"][0]["dialogue"] = "Say something."
    check("a dialogue line makes the shot pending",
          pending_ids(orch, board) == ["s1"],
          "the line is a visual cue for lips; speech is still muxed afterwards")

    section("things that do not change the picture")
    board = a_board()
    board["shots"][0]["title"] = "Renamed"
    check("renaming a shot does not force a re-render",
          pending_ids(orch, board) == [])

    section("chained shots follow the shot they continue from")
    board = a_board()
    board["shots"][1]["startRef"] = {"kind": "chain", "from": "s1"}
    board["shots"][2]["startRef"] = {"kind": "chain", "from": "s2"}
    for shot in board["shots"]:
        shot["renderFingerprint"] = render_fingerprint(shot, board)
    board["shots"][0]["prompt"] = "A different first shot."
    ids = pending_ids(orch, board)
    check("the whole chain is pending", ids == ["s1", "s2", "s3"], str(ids))
    _t, why = orch._pending(board)
    check("the followers say they are following",
          "continues from" in why.get("s3", ""), why.get("s3", ""))

    section("a resolved chain anchor is not an input change")
    board = a_board()
    board["shots"][1]["startRef"] = {"kind": "chain", "from": "s1"}
    board["shots"][1]["renderFingerprint"] = render_fingerprint(
        board["shots"][1], board
    )
    # What the orchestrator writes into the ref on every run.
    board["shots"][1]["startRef"]["resolved"] = "projects/x/shots/01/frames/frame-0123.png"
    check("re-resolving the anchor does not make it look edited",
          pending_ids(orch, board) == [])

    section("a shot that was never rendered")
    board = a_board()
    board["shots"][2]["status"] = "draft"
    board["shots"][2]["outputs"] = []
    ids = pending_ids(orch, board)
    check("it is pending on its status", ids == ["s3"], str(ids))
    check("and is not also called stale",
          stale_reason(board["shots"][2], board) == "",
          "status already says it has not been rendered")

    section("a render from before change tracking")
    board = a_board()
    board["shots"][0]["renderFingerprint"] = None
    ids = pending_ids(orch, board)
    check("cannot be shown to match, so it is re-rendered", ids == ["s1"], str(ids))


def test_dialogue_prompting() -> None:
    section("dialogue reaches render as a visual cue")
    board = a_board(1)
    board["characters"] = [
        {
            "id": "c1",
            "name": "Kira",
            "description": "Kira: a focused pilot in a worn flight jacket",
        }
    ]
    shot = board["shots"][0]
    shot["characterIds"] = ["c1"]
    shot["dialogue"] = "Hold course."
    prompt = _resolved_prompt(shot, board, with_audio=True)
    check("the spoken words are a mouth-movement cue",
          'Kira speaks the line with natural jaw and lip movement: "Hold course."' in prompt,
          prompt)
    check("a named description is not named twice",
          "Kira: Kira:" not in prompt,
          prompt)
    check("generated speech is explicitly blocked",
          "Generated audio contains ambient sound only" in prompt and
          "no intelligible dialogue" in prompt,
          prompt)


def test_soundscape_modes() -> None:
    section("background sound modes")
    board = a_board(1)
    shot = board["shots"][0]
    shot["soundNote"] = "A hatch slams."

    prompt = _resolved_prompt(shot, board, with_audio=True)
    check("background sound renders into shots by default",
          "Deep engine roar." in prompt,
          prompt)
    check("per-shot sound effects still render",
          "A hatch slams." in prompt,
          prompt)

    board["soundscapeInShots"] = False
    prompt = _resolved_prompt(shot, board, with_audio=True)
    check("background sound can be held out of shot renders",
          "Deep engine roar." not in prompt,
          prompt)
    check("shot effects remain when background sound is held out",
          "A hatch slams." in prompt,
          prompt)


# ---------------------------------------------------------------------------
# the cut
# ---------------------------------------------------------------------------

def test_assemble(tmp: Path) -> None:
    section("joining the clips")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("skip  ffmpeg/ffprobe not on PATH")
        return

    project = tmp / "projects" / "cut"
    make_clip(project / "shots" / "01" / "clip.mp4", 1.0, "red")
    make_clip(project / "shots" / "02" / "clip.mp4", 1.0, "green")
    # A still, or a model with no audio head: no audio stream at all.
    make_clip(project / "shots" / "03" / "clip.mp4", 1.0, "blue", with_audio=False)

    w, h = frame_size("320x176")
    parts = [
        (f"{i:02d} Shot {i}", shot_clip(project / "shots" / f"{i:02d}"))
        for i in (1, 2, 3)
    ]
    out = project / "final.mp4"
    res = assemble(parts, out, w, h)
    check("assembly succeeds", res.ok, res.error)
    check("it wrote the file", out.exists() and out.stat().st_size > 1024)
    check("all three clips are in it", len(res.parts) == 3, str(res.parts))
    check("nothing was reported missing", res.missing == [], str(res.missing))
    check("a soundless clip did not desynchronise the cut",
          2.7 < res.seconds < 3.3, f"{res.seconds}s for three 1s clips")

    section("a board with a gap still assembles, and says so")
    (project / "shots" / "02" / "clip.mp4").unlink()
    parts = [
        (f"{i:02d} Shot {i}", shot_clip(project / "shots" / f"{i:02d}"))
        for i in (1, 2, 3)
    ]
    res = assemble(parts, out, w, h)
    check("it still produces a cut", res.ok, res.error)
    check("it is flagged as partial", res.partial)
    check("and names the shot it left out",
          res.missing == ["02 Shot 2"], str(res.missing))
    check("the shortfall is in the log",
          any("left out" in line for line in res.log), str(res.log))

    section("nothing to join")
    empty = tmp / "projects" / "empty"
    res = assemble([("01 Shot 1", None)], empty / "final.mp4", w, h)
    check("is an error, not a silent no-op", not res.ok and bool(res.error),
          res.error)


def test_clip_choice(tmp: Path) -> None:
    section("which file represents a shot")
    d = tmp / "pick"
    d.mkdir(parents=True)
    clip = d / "clip.mp4"
    dubbed = d / "clip-dubbed.mp4"

    check("no clip at all", shot_clip(d) is None)

    clip.write_bytes(b"x" * 2048)
    check("the clip when that is all there is", shot_clip(d) == clip)

    dubbed.write_bytes(b"x" * 2048)
    import os
    os.utime(dubbed, (clip.stat().st_mtime + 10,) * 2)
    check("the dubbed clip when it is newer", shot_clip(d) == dubbed,
          "the spoken line belongs in the cut")
    shot = {
        "dialogue": "Stay on target.",
        "dialogueStyle": "urgent whisper",
        "dialogueSpokenText": "Stay on target.",
        "dialogueSpokenStyle": "calm",
    }
    check("an old voice direction makes the dub stale",
          shot_clip(d, shot) == clip,
          "clip-dubbed.mp4 has the wrong delivery")
    shot["dialogueSpokenStyle"] = "urgent whisper"
    check("a matching voice direction keeps the dub",
          shot_clip(d, shot) == dubbed,
          "clip-dubbed.mp4 matches the current delivery")

    os.utime(dubbed, (clip.stat().st_mtime - 10,) * 2)
    check("the plain clip when the dub predates a re-render",
          shot_clip(d) == clip,
          "clip-dubbed.mp4 would show the previous take")


def test_final_staleness(tmp: Path) -> None:
    section("whether the cut is still current")
    project = tmp / "projects" / "stale"
    (project / "shots" / "01").mkdir(parents=True)
    (project / "shots" / "01" / "clip.mp4").write_bytes(b"x" * 2048)

    board = default_board("Stale")
    board["shots"] = [default_shot(board["defaults"])]
    check("a clip and no cut says so",
          final_stale_reason(board, project) == "not built yet")

    final = project / "final.mp4"
    final.write_bytes(b"x" * 2048)
    board["finalVideo"] = {"url": "/media/x", "parts": [{"shot": "01", "file": "clip.mp4"}]}
    check("a cut newer than its clips is current",
          final_stale_reason(board, project) == "",
          final_stale_reason(board, project))

    import os
    os.utime(project / "shots" / "01" / "clip.mp4",
             (final.stat().st_mtime + 10,) * 2)
    check("a re-rendered shot makes it stale",
          "re-rendered" in final_stale_reason(board, project),
          final_stale_reason(board, project))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_pending(tmp)
        test_dialogue_prompting()
        test_soundscape_modes()
        test_clip_choice(tmp)
        test_final_staleness(tmp)
        test_assemble(tmp)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
