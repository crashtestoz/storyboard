#!/usr/bin/env python3
"""The soundtrack: when it can be made, when it is reused, and the ducking.

No music model here — a fake engine writes a sine tone through ffmpeg, which
is all the cache and the mix need to be checked. The ducking test assembles a
real cut and measures the music's level under a line against its level
between lines.

Run:  python3 tests/soundtrack.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import assemble as assembly                      # noqa: E402
from server import soundtrack as soundtrack_pkg              # noqa: E402
from server.soundtrack import SoundtrackEngine, SoundtrackResult  # noqa: E402
from server.soundtrack import board as music                 # noqa: E402
from server.store import Store, default_shot, render_fingerprint  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def run(*argv: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *argv], check=True)


def make_clip(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run("-f", "lavfi", "-t", str(seconds), "-i", "color=c=blue:s=64x64:r=24",
        "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=48000:cl=stereo",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))


def tone(path: Path, seconds: float, freq: int = 220, delay: float = 0.0) -> None:
    """*seconds* of sine; with *delay*, that much silence first."""
    src = f"sine=f={freq}:r=44100:d={seconds}"
    if delay:
        run("-f", "lavfi", "-i", src, "-af", f"adelay={int(delay * 1000)}:all=1,apad=pad_dur=1",
            "-ac", "1", str(path))
    else:
        run("-f", "lavfi", "-i", src, "-ac", "2", str(path))


class FakeEngine(SoundtrackEngine):
    id = "fake"
    label = "Fake engine"
    kind = "fake"

    def __init__(self):
        self.calls = []

    def health(self):
        return True, "ready"

    def generate(self, prompt, out, *, seconds, model, seed, init_audio=None, init_noise_level=1.0):
        self.calls.append(dict(prompt=prompt, seconds=seconds, model=model, seed=seed,
                               init_audio=init_audio, init_noise_level=init_noise_level))
        tone(out, seconds)
        return SoundtrackResult(ok=True, path=out, seconds=seconds)


def rms_db(path: Path, start: float, length: float) -> float:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-vn", "-af", "astats=metadata=0", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    return float(re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", proc.stderr)[-1])


class DuckExpression(unittest.TestCase):
    def value(self, expr, t):
        return eval(expr, {"max": max, "min": min, "t": t})     # noqa: S307 - our own string

    def test_nothing_to_duck(self):
        self.assertEqual(assembly.duck_expression([], 12, 0.1, 0.5), "1")
        self.assertEqual(assembly.duck_expression([(1, 2)], 0, 0.1, 0.5), "1")

    def test_dips_ahead_of_the_line_and_recovers_after(self):
        expr = assembly.duck_expression([(2.0, 3.0)], 12, 0.2, 0.5)
        dipped = 10 ** (-12 / 20)
        self.assertAlmostEqual(self.value(expr, 0.5), 1.0)
        self.assertAlmostEqual(self.value(expr, 1.9), 1 - (1 - dipped) * 0.5, places=3)
        self.assertAlmostEqual(self.value(expr, 2.5), dipped, places=3)
        self.assertAlmostEqual(self.value(expr, 3.25), 1 - (1 - dipped) * 0.5, places=3)
        self.assertAlmostEqual(self.value(expr, 4.0), 1.0)

    def test_close_lines_merge_rather_than_pump(self):
        expr = assembly.duck_expression([(1.0, 2.0), (2.3, 3.0), (8.0, 9.0)], 12, 0.1, 0.4)
        self.assertEqual(expr.count("max(0"), 2)
        self.assertLess(self.value(expr, 2.15), 0.3)             # stays down in the gap

    def test_offsets_follow_crossfades(self):
        self.assertEqual(assembly.clip_offsets([(0, 4), (0, 4), (0, 4)], 1.0), [0.0, 3.0, 6.0])
        self.assertEqual(assembly.clip_offsets([(0, 4), (0, 4)], 0.0), [0.0, 4.0])


class Settings(unittest.TestCase):
    def test_a_board_with_a_background_file_keeps_it_as_its_soundtrack(self):
        s = music.settings({"assembly": {"backgroundAudio": {"path": "x/refs/a.wav"}}})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["source"], "upload")
        self.assertFalse(music.settings({})["enabled"])

    def test_values_are_bounded(self):
        s = music.settings({"soundtrack": {"duckDb": 99, "referenceStrength": -1, "model": "nope"}})
        self.assertEqual((s["duckDb"], s["referenceStrength"], s["model"]), (30, 0, "sm-music"))

    def test_boards_before_the_tab_keep_their_cut_fingerprint(self):
        board = {"assembly": {"transitionSeconds": 0.5}, "shots": [{"id": "a", "trimIn": 1}]}
        legacy = hashlib.sha256(json.dumps(
            [board["assembly"], [["a", 1, 0]]], sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(assembly.assembly_fingerprint(board), legacy)
        board["soundtrack"] = {"enabled": True}
        self.assertNotEqual(assembly.assembly_fingerprint(board), legacy)

    def test_no_music_note_only_moves_shots_when_it_is_on(self):
        shot = dict(default_shot(), id="s1", prompt="A ship.")
        base = {"shots": [shot], "defaults": {}, "characters": []}
        before = render_fingerprint(shot, base)
        off = dict(base, soundtrack={"enabled": True, "noMusicInShots": False})
        on = dict(base, soundtrack={"enabled": True, "noMusicInShots": True})
        self.assertEqual(render_fingerprint(shot, off), before)
        self.assertNotEqual(render_fingerprint(shot, on), before)

    def test_an_empty_render_style_keeps_existing_fingerprints(self):
        shot = dict(default_shot(), id="s1", prompt="A ship.")
        base = {"shots": [shot], "defaults": {}, "characters": []}
        before = render_fingerprint(shot, base)
        self.assertEqual(render_fingerprint(shot, dict(base, renderStyle="")), before)
        self.assertNotEqual(render_fingerprint(shot, dict(base, renderStyle="Pencil sketch.")), before)


@unittest.skipUnless(FFMPEG and shutil.which("ffprobe"), "needs ffmpeg")
class Generation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(workspace=self.tmp, data_dir=self.tmp)
        self.slug, board = self.store.create("Music test")
        board["shots"] = [dict(default_shot(), id="s1", title="one"),
                          dict(default_shot(), id="s2", title="two")]
        board["assembly"] = {"transitionSeconds": 0.5}
        board["soundtrack"] = {"enabled": True, "source": "generate", "prompt": "warm strings",
                               "seed": 7, "duck": False}
        self.store.save(self.slug, board)
        self.project = self.store.project_dir(self.slug)
        self.engine = FakeEngine()
        self.ctx = SimpleNamespace(store=self.store, data_dir=self.tmp,
                                   soundtrack_engines=lambda: {"fake": self.engine})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        return music.status(self.store.load(self.slug), self.project, self.tmp,
                            {"fake": self.engine}, self.slug)

    def test_waits_for_every_scene_then_generates_once(self):
        make_clip(self.project / "shots" / "01" / "clip.mp4", 2)
        st = self.status()
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(music.prepare(self.ctx, self.slug)["state"], "skipped")
        self.assertEqual(self.engine.calls, [])

        make_clip(self.project / "shots" / "02" / "clip.mp4", 2)
        st = self.status()
        self.assertEqual(st["state"], "ready")
        self.assertAlmostEqual(st["seconds"], 3.5, delta=0.1)   # 2 + 2 − one 0.5 s crossfade

        self.assertEqual(music.prepare(self.ctx, self.slug)["state"], "generated")
        self.assertEqual(len(self.engine.calls), 1)
        self.assertAlmostEqual(self.engine.calls[0]["seconds"], 3.5, delta=0.1)
        self.assertEqual(self.status()["state"], "current")

        # Assembling again reuses the file…
        self.assertEqual(music.prepare(self.ctx, self.slug)["state"], "current")
        self.assertEqual(len(self.engine.calls), 1)
        board = self.store.load(self.slug)
        options = assembly.board_options(board, self.project, self.tmp)
        self.assertTrue(options["backgroundPath"].endswith("soundtrack.wav"))

        # …until something that shapes the music changes.
        board["soundtrack"]["prompt"] = "brass fanfare"
        self.store.save(self.slug, board)
        self.assertEqual(self.status()["state"], "stale")
        self.assertNotIn("backgroundPath", assembly.board_options(board, self.project, self.tmp))
        self.assertEqual(music.prepare(self.ctx, self.slug)["state"], "generated")
        self.assertEqual(self.engine.calls[-1]["prompt"], "brass fanfare")

    def test_a_trim_changes_the_length_and_so_the_music(self):
        make_clip(self.project / "shots" / "01" / "clip.mp4", 2)
        make_clip(self.project / "shots" / "02" / "clip.mp4", 2)
        music.prepare(self.ctx, self.slug)
        board = self.store.load(self.slug)
        board["shots"][1]["trimOut"] = 0.5
        self.store.save(self.slug, board)
        self.assertEqual(self.status()["state"], "stale")
        music.prepare(self.ctx, self.slug)
        self.assertAlmostEqual(self.engine.calls[-1]["seconds"], 3.0, delta=0.1)

    def test_a_cut_longer_than_the_model_loops(self):
        make_clip(self.project / "shots" / "01" / "clip.mp4", 2)
        make_clip(self.project / "shots" / "02" / "clip.mp4", 2)
        saved = copy.deepcopy(soundtrack_pkg.MODELS)
        try:
            soundtrack_pkg.MODELS["sm-music"]["maxSeconds"] = 2
            st = self.status()
            self.assertTrue(st["loops"])
            self.assertEqual(st["targetSeconds"], 2)
        finally:
            soundtrack_pkg.MODELS.clear()
            soundtrack_pkg.MODELS.update(saved)

    def test_reference_is_looped_and_strength_sets_noise(self):
        make_clip(self.project / "shots" / "01" / "clip.mp4", 2)
        make_clip(self.project / "shots" / "02" / "clip.mp4", 2)
        ref = self.project / "refs" / "tone.wav"
        tone(ref, 1.0, 330)
        board = self.store.load(self.slug)
        board["soundtrack"]["reference"] = {"path": str(ref.relative_to(self.tmp))}
        board["soundtrack"]["referenceStrength"] = 1.0
        self.store.save(self.slug, board)
        music.prepare(self.ctx, self.slug)
        call = self.engine.calls[-1]
        self.assertAlmostEqual(call["init_noise_level"], 0.4)
        self.assertAlmostEqual(assembly._duration(call["init_audio"]), 3.5, delta=0.1)

    def test_music_ducks_under_a_recorded_line(self):
        shot_dir = self.project / "shots" / "01"
        make_clip(shot_dir / "clip.mp4", 3)
        # A dubbed clip whose line is in dialogue.wav from 1.0 s to 2.0 s. The
        # clip's own audio is silent, so the cut's sound is the music alone.
        shutil.copy(shot_dir / "clip.mp4", shot_dir / "clip-dubbed.mp4")
        tone(shot_dir / "dialogue.wav", 1.0, 880, delay=1.0)
        board = self.store.load(self.slug)
        board["shots"] = board["shots"][:1]
        board["shots"][0]["dialogue"] = "Hello there."
        board["assembly"] = {"backgroundVolume": 1.0}
        board["soundtrack"].update(duck=True, duckDb=12, duckAttack=0.1, duckRelease=0.3)
        self.store.save(self.slug, board)
        self.assertEqual(music.prepare(self.ctx, self.slug)["state"], "generated")

        board = self.store.load(self.slug)
        options = assembly.board_options(board, self.project, self.tmp)
        self.assertIn(str(shot_dir / "clip-dubbed.mp4"), options["duck"]["keys"])
        out = self.project / "final.mp4"
        res = assembly.assemble(assembly.board_parts(board, self.project), out, 64, 64, options=options)
        self.assertTrue(res.ok, res.error)
        between, under = rms_db(out, 0.3, 0.4), rms_db(out, 1.35, 0.3)
        self.assertAlmostEqual(between - under, 12, delta=2,
                               msg=f"music {between:.1f} dB between lines, {under:.1f} dB under one")
        self.assertAlmostEqual(rms_db(out, 2.55, 0.3), between, delta=1.5)   # and it comes back


if __name__ == "__main__":
    unittest.main(verbosity=1)
