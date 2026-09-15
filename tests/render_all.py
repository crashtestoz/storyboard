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

import json
import shutil
import os
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
    unmixed_dialogue,
)
from server.orchestrator import Orchestrator                   # noqa: E402
from server.store import (                                     # noqa: E402
    Store,
    default_board,
    default_shot,
    last_saved_frame,
    render_fingerprint,
    stale_reason,
)
from server.backends.base import ShotPaths                      # noqa: E402
from server.backends.vpipe_backend import (                     # noqa: E402
    VpipeBackend,
    _ref2va_references,
    _resolved_prompt,
)
from server.app import _rewrite_reference_images                 # noqa: E402
from server.llm import build_user_message                        # noqa: E402

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
        ("startRef", {"path": "projects/test/refs/opening.png"}),
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

    section("a resolved chain anchor is stable, but a corrected one invalidates")
    board = a_board()
    board["shots"][1]["startRef"] = {"kind": "chain", "from": "s1"}
    board["shots"][1]["startRef"]["resolved"] = "projects/x/shots/01/frames/frame-0123.png"
    board["shots"][1]["renderFingerprint"] = render_fingerprint(
        board["shots"][1], board
    )
    check("re-resolving the same anchor does not make it look edited",
          pending_ids(orch, board) == [])
    board["shots"][1]["startRef"]["resolved"] = "projects/x/shots/01/frames/frame-0174.png"
    check("a corrected anchor makes the dependent shot pending",
          pending_ids(orch, board) == ["s2"])

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
            "image": {"kind": "upload", "path": "projects/x/refs/kira.jpg"},
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

    shot["model"] = "ref2va"
    shot["startRef"] = None
    shot["referenceImages"] = [
        {"kind": "upload", "path": "projects/x/refs/corridor.jpg"}
    ]
    shot["prompt"] = "A tracking shot follows Kira from behind."
    prompt = _resolved_prompt(shot, board, with_audio=True, model="ref2va")
    check("a Ref2VA shot reference set is called out as primary visual context",
          "shot reference image set is the primary visual reference" in prompt,
          prompt)
    check("a rear-view shot gets a first-frame camera constraint",
          "keep the visible character facing away" in prompt,
          prompt)
    check("shot-local Ref2VA references retain shared scene text",
          "An ocean at golden hour." in prompt,
          prompt)
    check("shot-local Ref2VA references retain shared soundscape",
          "Deep engine roar." in prompt,
          prompt)
    check("image-backed Ref2VA characters retain Cast details with scene priority",
          "focused pilot in a worn flight jacket" in prompt and
          "use the character portrait and Cast description together" in prompt and
          "Ignore pose and background in the portrait or Cast description" in prompt and
          "any explicit appearance changes" in prompt,
          prompt)


def test_ref2va_reference_priority() -> None:
    section("Ref2VA shot references outrank global style")
    board = a_board(1)
    board["characters"] = [
        {
            "id": "c1",
            "name": "Kira",
            "description": "pilot",
            "image": {"kind": "upload", "path": "projects/x/refs/character.jpg"},
            "voice": {"kind": "upload", "path": "projects/x/refs/voice.wav"},
        }
    ]
    board["styleRefs"] = [
        {"kind": "upload", "path": "projects/x/refs/global-1.jpg"},
        {"kind": "upload", "path": "projects/x/refs/global-2.jpg"},
    ]
    shot = board["shots"][0]
    shot["characterIds"] = ["c1"]
    shot["startRef"] = {"kind": "upload", "path": "projects/x/refs/shot.jpg"}
    shot["endRef"] = {"kind": "upload", "path": "projects/x/refs/shot-end.jpg"}
    shot["referenceImages"] = [
        {"kind": "upload", "path": "projects/x/refs/cockpit-left.jpg"},
        {"kind": "upload", "path": "projects/x/refs/cockpit-right.jpg"},
    ]
    paths = ShotPaths(
        workspace=Path("/tmp/ws"),
        abs_dir=Path("/tmp/ws/projects/x/shots/01"),
        rel_dir="projects/x/shots/01",
        data_dir=Path("/tmp/ws"),
    )

    shot["dialogue"] = "Hold course."
    shot["dialogueSource"] = "native"
    refs = _ref2va_references(shot, board, paths)
    names = [Path(r).name for r in refs]
    check("start/end references remain first",
          names[:2] == ["shot.jpg", "shot-end.jpg"],
          str(names))
    check("Ref2VA receives both labeled frame references",
          "shot.jpg" in names and "shot-end.jpg" in names,
          str(names))
    check("project style refs join shot-local references",
          "global-1.jpg" in names and "global-2.jpg" in names,
          str(names))
    check("shot-local images follow the frame references",
          names[2:4] == ["cockpit-left.jpg", "cockpit-right.jpg"],
          str(names))
    check("voice references come after images", names[-1] == "voice.wav", str(names))
    draft_refs = _ref2va_references(shot, board, paths, include_audio_refs=False)
    draft_names = [Path(r).name for r in draft_refs]
    check("draft Ref2VA skips audio references",
          "voice.wav" not in draft_names,
          str(draft_names))


def test_effective_video_model_routing(tmp: Path) -> None:
    section("frame anchors select FL2VA; reference shots select Ref2VA")
    workspace = tmp / "routing"
    for model in ("FL2VA", "Ref2VA"):
        (workspace / "models" / "local" / f"MiniMax-H3-{model}-8bit").mkdir(parents=True)
    backend = VpipeBackend(workspace / "vpipe", workspace)
    paths = ShotPaths(workspace=workspace, abs_dir=workspace / "shots/01",
                      rel_dir="shots/01", data_dir=workspace)
    board = a_board(1)
    shot = board["shots"][0]
    shot["model"] = "ref2va"
    job = backend.prepare(shot, board, paths)
    pipeline = json.loads(Path(job.payload["spec_path"]).read_text())
    ref_encoder = next(s for s in pipeline["stages"] if s["type"] == "video-ref-encoder")
    check("no anchors use the Ref2VA pipeline",
          job.payload["model"] == "ref2va"
          and ref_encoder["config"]["references"] == [])
    for source, ref_port in (("startRef", 5), ("endRef", 6), ("chain", 5)):
        candidate = json.loads(json.dumps(board))
        target = candidate["shots"][0]
        ref = {"kind": "upload", "path": "refs/image.png"}
        target["startRef" if source == "chain" else source] = (
            {"kind": "chain", "resolved": "shots/00/frames/last.png"}
            if source == "chain" else ref
        )
        candidate["styleRefs"] = [{"kind": "upload", "path": "refs/style.png"}]
        before = json.dumps(candidate, sort_keys=True)
        job = backend.prepare(target, candidate, paths)
        pipeline = json.loads(Path(job.payload["spec_path"]).read_text())
        generate = next(s for s in pipeline["stages"] if s["type"] == "generate-video")
        check(f"{source} selects FL2VA",
              job.payload["model"] == "fl2va"
              and not any(s["type"] == "video-ref-encoder" for s in pipeline["stages"])
              and generate["iports"][ref_port]["src"] == f"vae-encode-{'start' if ref_port == 5 else 'end'}")
        check(f"{source} does not mutate the board",
              "FL2VA" in job.summary and json.dumps(candidate, sort_keys=True) == before)
    (workspace / "models/local/MiniMax-H3-FL2VA-8bit").rmdir()
    try:
        candidate = json.loads(json.dumps(board))
        candidate["shots"][0]["startRef"] = {"path": "refs/image.png"}
        backend.prepare(candidate["shots"][0], candidate, paths)
        check("anchored shots require the FL2VA model", False)
    except ValueError:
        check("anchored shots require the FL2VA model", True)


def test_rewrite_reference_context() -> None:
    section("rewrite context includes reference images")
    board = a_board(1)
    board["characters"] = [
        {
            "id": "c1",
            "name": "Kira",
            "description": "pilot",
            "image": {"kind": "upload", "path": "projects/x/refs/kira-portrait.jpg"},
            "voice": {"kind": "upload", "path": "projects/x/refs/kira.wav"},
        }
    ]
    shot = board["shots"][0]
    shot["characterIds"] = ["c1"]
    shot["startRef"] = {
        "kind": "upload",
        "path": "projects/x/refs/bright-white-corridor.jpeg",
    }
    shot["endRef"] = {
        "kind": "upload",
        "path": "projects/x/refs/final-frame.png",
    }
    shot["referenceImages"] = [
        {"kind": "upload", "path": "projects/x/refs/black-ribbed-doorway.webp"},
        {"kind": "upload", "path": "projects/x/refs/not-a-picture.mp3"},
    ]
    cast = [board["characters"][0]]

    refs = _rewrite_reference_images(shot, cast)
    labels = [r["label"] for r in refs]
    check("rewrite references include shot and character images",
          labels == [
              "bright-white-corridor.jpeg",
              "final-frame.png",
              "black-ribbed-doorway.webp",
              "kira-portrait.jpg",
          ],
          str(labels))
    check("rewrite references skip voice clips",
          "not-a-picture.mp3" not in labels and "kira.wav" not in labels,
          str(labels))

    msg = build_user_message(
        "Kira walks forward.",
        scene=board["sceneDescription"],
        characters=cast,
        reference_images=refs,
    )
    check("rewrite prompt names reference image context",
          "Reference images attached to this shot" in msg,
          msg)
    check("rewrite prompt includes readable reference summaries",
          "bright white corridor" in msg and "black ribbed doorway" in msg,
          msg)

    sound_msg = build_user_message(
        "Birds are audible nearby.",
        scene="A quiet woodland clearing.",
        soundscape="Steady wind and distant river noise.",
        context="A close shot of the clearing.",
        context_label="This shot's local context:",
    )
    check("sound accent rewrite receives the general background sound as context",
          "Steady wind and distant river noise." in sound_msg,
          sound_msg)
    check("sound accent context labels the general background as an exclusion",
          "Do not repeat, paraphrase, or replace" in sound_msg,
          sound_msg)


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

    section("a chain ignores stale frames from a longer previous take")
    frames_dir = tmp / "stale-frames"
    frames_dir.mkdir()
    for number in (0, 174, 242):
        (frames_dir / f"frame-{number:04d}.png").write_bytes(b"x")
    check("the current requested tail wins over an old longer tail",
          last_saved_frame(frames_dir, 175).name == "frame-0174.png")

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
    shot["dialogueSpokenText"] = ""
    shot["dialogueSpokenStyle"] = ""
    check("legacy dubbed clips without spoken metadata are trusted",
          shot_clip(d, shot) == dubbed,
          "older boards did not record dialogueSpokenText")

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

    dubbed = project / "shots" / "01" / "clip-dubbed.mp4"
    dubbed.write_bytes(b"x" * 2048)
    os.utime(project / "shots" / "01" / "clip.mp4",
             (final.stat().st_mtime - 20,) * 2)
    os.utime(dubbed, (final.stat().st_mtime - 10,) * 2)
    board["shots"][0]["dialogue"] = "Stay on target."
    check("a better dubbed clip makes the cut stale by filename",
          "clip-dubbed.mp4" in final_stale_reason(board, project),
          final_stale_reason(board, project))
    board["shots"][0]["dialogue"] = ""
    dubbed.unlink()

    os.utime(project / "shots" / "01" / "clip.mp4",
             (final.stat().st_mtime + 10,) * 2)
    check("a re-rendered shot makes it stale",
          "re-rendered" in final_stale_reason(board, project),
          final_stale_reason(board, project))


def test_unmixed_dialogue(tmp: Path) -> None:
    section("dialogue warnings")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("skip  ffmpeg/ffprobe not on PATH")
        return

    project = tmp / "projects" / "dialogue"
    board = default_board("Dialogue")
    shot = default_shot(board["defaults"])
    shot["id"] = "s1"
    shot["dialogue"] = "Stay on target."
    shot["dialogueSpokenText"] = "Stay on target."
    board["shots"] = [shot]
    (project / "shots" / "01").mkdir(parents=True)
    (project / "shots" / "01" / "dialogue.wav").write_bytes(b"x" * 2048)

    make_clip(project / "shots" / "01" / "clip.mp4", 1.0, "red", with_audio=True)
    check("a clip that already has audio is not called silent",
          unmixed_dialogue(board, project) == {},
          str(unmixed_dialogue(board, project)))

    (project / "shots" / "01" / "clip.mp4").unlink()
    make_clip(project / "shots" / "01" / "clip.mp4", 1.0, "red", with_audio=False)
    check("a soundless clip with a separate spoken line is warned",
          "s1" in unmixed_dialogue(board, project),
          str(unmixed_dialogue(board, project)))


def test_load_heals_foreign_refs(tmp: Path) -> None:
    section("load heals foreign refs")
    store = Store(workspace=tmp, data_dir=tmp)

    other_slug, _ = store.create("Other Project")
    other_refs = store.refs_dir(other_slug)
    other_refs.mkdir(parents=True, exist_ok=True)
    (other_refs / "hero.png").write_bytes(b"y" * 32)

    # A board hand-edited (or left over from before rehoming existed) to
    # point a character portrait and a style ref at another project's refs/.
    slug, board = store.create("My Project")
    board["characters"] = [{
        "id": "c1", "name": "Hero",
        "image": {
            "path": f"projects/{other_slug}/refs/hero.png",
            "url": f"/media/projects/{other_slug}/refs/hero.png",
            "label": "hero.png",
        },
    }]
    board["styleRefs"] = [{
        "path": f"projects/{other_slug}/refs/hero.png",
        "url": f"/media/projects/{other_slug}/refs/hero.png",
        "label": "hero.png",
    }]
    store.save(slug, board)

    reloaded = store.load(slug)
    char_image = reloaded["characters"][0]["image"]
    style = reloaded["styleRefs"][0]
    check("a foreign character portrait is rehomed on load",
          char_image["path"].startswith(f"projects/{slug}/"), char_image["path"])
    check("a foreign style ref is rehomed on load",
          style["path"].startswith(f"projects/{slug}/"), style["path"])
    check("the fix is saved, not just returned in memory",
          json.loads(store.board_path(slug).read_text())["styleRefs"][0]["path"]
          == style["path"])
    check("the other project's own file is untouched", (other_refs / "hero.png").is_file())

    # Loading an already-healed board is a no-op: no further copies appear.
    before = list((store.refs_dir(slug)).iterdir())
    store.load(slug)
    check("re-loading a clean board makes no further copies",
          list((store.refs_dir(slug)).iterdir()) == before)


def test_import_rehomes_media(tmp: Path) -> None:
    section("import rehomes media")
    store = Store(workspace=tmp, data_dir=tmp)

    src_slug, src_board = store.create("Source Project")
    refs_dir = store.refs_dir(src_slug)
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "mood.png").write_bytes(b"x" * 32)
    src_board["styleRefs"] = [{
        "path": f"projects/{src_slug}/refs/mood.png",
        "url": f"/media/projects/{src_slug}/refs/mood.png",
        "label": "mood.png",
    }]
    store.save(src_slug, src_board)

    new_slug, new_board = store.import_board(
        store.export(src_slug), name="Copy of Source"
    )
    check("import gives the copy its own slug", new_slug != src_slug, new_slug)

    style = (new_board.get("styleRefs") or [None])[0] or {}
    check("the imported style ref points into the new project",
          style.get("path", "").startswith(f"projects/{new_slug}/"),
          style.get("path"))
    check("the style ref image was copied into the new project",
          (tmp / "projects" / new_slug / "refs" / "mood.png").is_file())
    check("the source project's own file is untouched",
          (refs_dir / "mood.png").is_file())

    # If the copy still pointed at the source's folder, deleting the source
    # would take the copy's style reference down with it.
    store.delete(src_slug, keep_outputs=False)
    reloaded = store.load(new_slug)
    style = (reloaded.get("styleRefs") or [None])[0] or {}
    check("the copy's style ref survives the source project being deleted",
          (tmp / "projects" / new_slug / "refs" / "mood.png").is_file()
          and style.get("path", "").startswith(f"projects/{new_slug}/"),
          style.get("path"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_pending(tmp)
        test_dialogue_prompting()
        test_ref2va_reference_priority()
        test_effective_video_model_routing(tmp)
        test_rewrite_reference_context()
        test_soundscape_modes()
        test_clip_choice(tmp)
        test_final_staleness(tmp)
        test_unmixed_dialogue(tmp)
        test_load_heals_foreign_refs(tmp)
        test_import_rehomes_media(tmp)
        test_assemble(tmp)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
