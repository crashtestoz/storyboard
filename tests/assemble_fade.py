"""Fade from and to black on the assembled cut, measured on real video.

Run:  python3 tests/assemble_fade.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.assemble import assemble, assembly_fingerprint  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def clip(path: Path, colour: str, seconds: float = 2.0) -> Path:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=24:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={seconds}",
                    "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def luma(video: Path, at: float) -> float:
    """Mean brightness (0-255) of the frame at *at* seconds."""
    out = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{at:.3f}", "-i", str(video),
                          "-frames:v", "1", "-vf", "signalstats,metadata=mode=print:file=-",
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "lavfi.signalstats.YAVG=" in line:
            return float(line.split("=")[1])
    raise AssertionError("no luma reading\n" + out)


def audio_peak_db(video: Path, start: float, seconds: float) -> float:
    """Loudest sample (dBFS) in [start, start + seconds) of the cut's audio."""
    out = subprocess.run([FFMPEG, "-v", "info", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
                          "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    for line in out.splitlines():
        if "max_volume:" in line:
            return float(line.split("max_volume:")[1].split("dB")[0])
    return -91.0   # no samples at all


@unittest.skipUnless(FFMPEG, "ffmpeg not on PATH")
class FadeToBlackTests(unittest.TestCase):
    """The opening fades up from black and the ending fades down to it,
    over the scenes themselves, so the cut keeps its length."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        # the last shot is grey so its held frame can be told from the first
        self.parts = [("01 A", clip(d / "a.mp4", "white")), ("02 B", clip(d / "b.mp4", "gray"))]
        self.out = d / "final.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def test_muted_shot_is_silent_in_the_cut(self):
        muted = str(self.parts[1][1])
        r = assemble(self.parts, self.out, 320, 180, options={"muted": [muted]})
        self.assertTrue(r.ok, r.error or r.log)
        self.assertAlmostEqual(r.seconds, 4.0, delta=0.15)        # picture kept
        self.assertGreater(audio_peak_db(self.out, 0.5, 1.0), -30)  # shot 1 still heard
        self.assertLess(audio_peak_db(self.out, 2.5, 1.0), -80)     # shot 2 silent

    def test_muting_changes_the_fingerprint_only_when_used(self):
        board = {"shots": [{"id": "a"}, {"id": "b"}]}
        before = assembly_fingerprint(board)
        board["shots"][1]["muteAudio"] = False
        self.assertEqual(assembly_fingerprint(board), before)
        board["shots"][1]["muteAudio"] = True
        self.assertNotEqual(assembly_fingerprint(board), before)

    def test_fades_over_the_scenes_without_adding_time(self):
        r = assemble(self.parts, self.out, 320, 180, options={"fadeBlackSeconds": 1.0})
        self.assertTrue(r.ok, r.error or r.log)
        self.assertAlmostEqual(r.seconds, 4.0, delta=0.15)        # no held frames added
        self.assertLess(luma(self.out, 0.0), 30)                  # black at the very start
        self.assertGreater(luma(self.out, 0.5), 60)               # scene 1 fading up
        self.assertLess(luma(self.out, 0.5), 200)
        self.assertGreater(luma(self.out, 1.5), 225)              # full after the fade
        self.assertLess(luma(self.out, r.seconds - 0.05), 30)     # black at the very end
        # sound fades with the picture
        full = audio_peak_db(self.out, 1.2, 0.6)
        self.assertGreater(full, -30)
        self.assertLess(audio_peak_db(self.out, 0.0, 0.1), full - 15)
        self.assertTrue(any("faded up from black" in l for l in r.log))
        self.assertTrue(any("faded down to black" in l for l in r.log))

    def test_fade_longer_than_half_the_cut_is_shortened(self):
        r = assemble(self.parts[:1], self.out, 320, 180, options={"fadeBlackSeconds": 3})
        self.assertTrue(r.ok, r.error)
        self.assertAlmostEqual(r.seconds, 2.0, delta=0.15)
        self.assertGreater(luma(self.out, 1.0), 180)              # peak at the midpoint

    def test_fade_only_the_start(self):
        r = assemble(self.parts, self.out, 320, 180, options={"fadeInBlackSeconds": 1.0})
        self.assertTrue(r.ok, r.error)
        self.assertAlmostEqual(r.seconds, 4.0, delta=0.15)
        self.assertLess(luma(self.out, 0.0), 30)
        self.assertGreater(luma(self.out, r.seconds - 0.05), 100)  # ends on the grey shot

    def test_fade_only_the_end(self):
        r = assemble(self.parts, self.out, 320, 180, options={"fadeOutBlackSeconds": 1.0})
        self.assertTrue(r.ok, r.error)
        self.assertGreater(luma(self.out, 0.0), 200)               # opens on the white shot
        self.assertLess(luma(self.out, r.seconds - 0.05), 30)

    def test_each_end_overrides_the_older_single_setting(self):
        r = assemble(self.parts, self.out, 320, 180,
                     options={"fadeBlackSeconds": 1.0, "fadeOutBlackSeconds": 0})
        self.assertTrue(r.ok, r.error)
        self.assertLess(luma(self.out, 0.0), 30)
        self.assertGreater(luma(self.out, r.seconds - 0.05), 100)

    def test_with_crossfades_between_shots(self):
        r = assemble(self.parts, self.out, 320, 180,
                     options={"fadeBlackSeconds": 0.75, "transitionSeconds": 0.5})
        self.assertTrue(r.ok, r.error)
        self.assertAlmostEqual(r.seconds, 4.0 - 0.5, delta=0.15)
        self.assertLess(luma(self.out, 0.0), 30)
        self.assertLess(luma(self.out, r.seconds - 0.05), 30)

    def test_off_by_default(self):
        r = assemble(self.parts, self.out, 320, 180, options={})
        self.assertTrue(r.ok, r.error)
        self.assertAlmostEqual(r.seconds, 4.0, delta=0.15)
        self.assertGreater(luma(self.out, 0.0), 200)

    def test_out_of_range_is_refused(self):
        r = assemble(self.parts, self.out, 320, 180, options={"fadeBlackSeconds": 5})
        self.assertFalse(r.ok)
        self.assertIn("Fade from/to black", r.error)
        r = assemble(self.parts, self.out, 320, 180, options={"fadeOutBlackSeconds": 5})
        self.assertFalse(r.ok)
        self.assertIn("Fade from/to black", r.error)

    def test_changing_it_changes_the_cut_fingerprint_only_when_set(self):
        self.assertEqual(assembly_fingerprint({"assembly": {}}), assembly_fingerprint({}))
        self.assertNotEqual(assembly_fingerprint({"assembly": {"fadeBlackSeconds": 1}}),
                            assembly_fingerprint({"assembly": {}}))


if __name__ == "__main__":
    unittest.main()
