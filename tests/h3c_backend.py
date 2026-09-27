#!/usr/bin/env python3
"""The h3.c backend, end to end, against a stand-in ``h3`` binary.

The real engine needs ~100 GB of weights, so this checks everything around
it instead: the command line each shot type produces, progress parsing from
h3.c's ``\\r<phase> N/M`` stderr format, PPM-to-PNG frame conversion, and
validation — including a run that fails the way h3.c fails (``h3: reason``,
exit 1).

Run:  python3 tests/h3c_backend.py
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.backends.base import ShotPaths          # noqa: E402
from server.backends.h3c_backend import H3cBackend  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'pass' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


# Prints progress exactly as h3.c's cli_progress does, writes PPM frames to
# --frames-dir and a real MP4 to -o. H3_FAKE_FAIL makes it fail like h3.c.
FAKE_H3 = r'''#!/usr/bin/env python3
import os, subprocess, sys
a = sys.argv[1:]
open(os.path.join(os.path.dirname(__file__), "argv.txt"), "w").write("\n".join(a))
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
w, h, n = int(opt("--width")), int(opt("--height")), int(opt("--frames"))
out, fdir = opt("-o"), opt("--frames-dir")
def prog(phase, total):
    for i in range(total + 1):
        sys.stderr.write("\r%-25s %4d/%-4d" % (phase, i, total))
    sys.stderr.write("\n")
prog("text encoder", 50)
prog("load transformer core", 50)
if os.environ.get("H3_FAKE_FAIL"):
    sys.stderr.write("h3: DiT requires a valid contiguous H3 packed layout\n")
    sys.exit(1)
prog("denoise", int(opt("--steps")))
prog("audio VAE", 7)
prog("video VAE load", 36)
if fdir:
    for i in range(n):
        with open(os.path.join(fdir, "frame-%04d.ppm" % i), "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h) + bytes([i * 5 % 256, 80, 160]) * (w * h))
prog("FFmpeg", n)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                "testsrc=size=%dx%d:rate=24" % (w, h), "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=32000", "-frames:v", str(n),
                "-shortest", "-pix_fmt", "yuv420p", out], check=True)
sys.stderr.write("h3: wrote %s\n" % out)
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h3c-test-"))
    try:
        bindir = tmp / "h3.c"
        bindir.mkdir()
        binary = bindir / "h3"
        binary.write_text(FAKE_H3)
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        (bindir / "h3_shaders.metal").write_text("// stand-in\n")
        models = bindir / "MiniMax-H3"
        (models / "FL2VA/transformer").mkdir(parents=True)
        (models / "FL2VA/transformer/config.json").write_text("{}")

        be = H3cBackend(binary=binary, model_dir=models, workspace=tmp)
        ok, msg = be.health()
        check("health ok with binary, shaders, model dir", ok, msg)
        caps = {c.id: c for c in be.capabilities()}
        check("FL2VA available, Ref2VA not until its checkpoint exists",
              caps["fl2va"].available and not caps["ref2va"].available)
        check("21:9 1792x768 not offered (over 768x1344 px)",
              "1792x768" not in caps["fl2va"].resolutions)
        (models / "Ref2VA/transformer").mkdir(parents=True)
        (models / "Ref2VA/transformer/model.safetensors.index.json").write_text("{}")
        check("Ref2VA available once present",
              {c.id: c for c in be.capabilities()}["ref2va"].available)

        data = tmp / "data"
        refs = data / "refs"
        refs.mkdir(parents=True)
        for name in ("kira.png", "corridor.jpg"):
            (refs / name).write_bytes(b"x")
        (refs / "kira-voice.wav").write_bytes(b"x")
        board = {
            "name": "t",
            "sceneDescription": "A starship corridor.",
            "defaults": {"resolution": "768x432"},
            "characters": [{"id": "c1", "name": "Kira", "description": "a pilot",
                            "image": "refs/kira.png",
                            "voice": {"path": "refs/kira-voice.wav"}}],
            "shots": [],
        }
        shot = {"id": "s1", "prompt": "Kira walks to the door.", "frames": 30,
                "steps": 6, "seed": 7, "characterIds": ["c1"],
                "referenceImages": [{"path": "refs/corridor.jpg"}],
                "dialogue": "Open it.", "dialogueSource": "native"}
        board["shots"].append(shot)
        paths = ShotPaths(workspace=tmp, abs_dir=data / "t/shots/01",
                          rel_dir="t/shots/01", data_dir=data)

        spec = be.prepare(shot, board, paths)
        job = json.loads(Path(spec.payload["spec_path"]).read_text())
        check("spec written as shot.h3c.json, not shot.vpipeline",
              Path(spec.payload["spec_path"]).name == "shot.h3c.json"
              and not (paths.abs_dir / "shot.vpipeline").exists())
        check("frames snapped to 17n+5 (30 -> 39)", job["frames"] == 39, str(job["frames"]))
        check("size snapped to 32 (768x432 -> 768x448)",
              (job["width"], job["height"]) == (768, 448))
        flags = [r["flag"] for r in job["references"]]
        check("references keep order: shot image, portrait, then voice",
              flags == ["--ref-image", "--ref-image", "--ref-audio"], str(flags))
        check("prompt carries <Picture N> bindings",
              "<Picture 1>" in job["prompt"] and "<Picture 2>" in job["prompt"])
        check("timings kept apart from vpipe's", spec.payload["timingModel"] == "h3c:ref2va")

        events = []
        result = be.run(spec, events.append, lambda: False)
        argv = (bindir / "argv.txt").read_text().splitlines()
        check("run exits 0", result.exit_code == 0, result.error or str(result.log[-3:]))
        check("launched with shaders' folder as cwd (argv recorded there)", bool(argv))
        check("low step count forces --reuse 1",
              argv[argv.index("--reuse") + 1] == "1")
        check("progress reaches denoise and decode",
              any(e.phase == "denoise" for e in events)
              and max(e.percent for e in events) > 90)
        pngs = sorted(paths.abs_frames.glob("frame-*.png"))
        check("PPM frames converted to frame-0000.png ...",
              len(pngs) == 39 and pngs[0].name == "frame-0000.png", str(len(pngs)))
        check("temporary PPM folder removed", not (paths.abs_dir / ".h3-frames-ppm").exists())
        spec.started_at = result.started_at
        v = be.validate(spec, result)
        check("validates as done", v.verdict == "done", v.reason)

        # FL2VA with anchors
        shot2 = {"id": "s2", "model": "fl2va", "prompt": "Door opens.", "frames": 22,
                 "startRef": {"path": "refs/corridor.jpg"}}
        # Automatic routing always picks Ref2VA; force FL2VA via the template.
        job2, _ = be._fl2va_spec(shot2, paths, "Door opens.", 512, 512, 22, 20, 1)
        argv2 = be.argv(job2)
        check("FL2VA anchor passed as --first-frame",
              "--first-frame" in argv2 and "--ref-image" not in argv2)
        check("FL2VA at 20 steps uses configured reuse",
              argv2[argv2.index("--reuse") + 1] == "1")

        # a vpipe-only engine is refused with a clear reason
        try:
            be.prepare({"id": "s3", "model": "wan-i2v", "prompt": "x"}, board, paths)
            check("wan-i2v refused on h3c", False)
        except ValueError as exc:
            check("wan-i2v refused on h3c", "vpipe" in str(exc))

        # oversized canvas refused before launching
        try:
            be._fl2va_spec(shot2, paths, "x", 1792, 768, 22, 20, 1)
            check("1792x768 refused", False)
        except ValueError:
            check("1792x768 refused", True)

        # h3.c-style failure
        import os
        os.environ["H3_FAKE_FAIL"] = "1"
        # The previous take's clip is still on disk, as it would be; age it
        # so it reads as the earlier run it is (real renders are minutes
        # apart, this test is not).
        clip = paths.abs_dir / "clip.mp4"
        os.utime(clip, (clip.stat().st_mtime - 600,) * 2)
        spec = be.prepare(shot, board, paths)
        result = be.run(spec, lambda e: None, lambda: False)
        del os.environ["H3_FAKE_FAIL"]
        spec.started_at = result.started_at
        v = be.validate(spec, result)
        check("failure: exit 1 reported, error captured, verdict failed",
              result.exit_code == 1 and v.verdict == "failed"
              and "packed layout" in v.reason, v.reason)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("all passed" if not failures else f"{len(failures)} FAILED: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
