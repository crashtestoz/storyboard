"""Files attached in the Cast editor are filed under the character's name.

A voice clip called "Alexander - Clear, Steady and Refined.mp3" says nothing
the model uses -- only the audio counts -- so the copy in refs/ is named for
who it belongs to (alex-voice.mp3). The original stays where it was.

Run:  python3 tests/cast_file_names.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.store import Store  # noqa: E402


class AdoptUnderName(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = Store(workspace=self.root, data_dir=self.root)
        self.refs = self.store.refs_dir("p")
        self.refs.mkdir(parents=True)
        self.clip = self.refs / "Alexander_-_Clear__Steady_and_Refined.mp3"
        self.clip.write_bytes(b"voice one")

    def adopt(self, rel, name=""):
        return self.store.adopt("p", rel, name)

    def test_named_copy_in_refs_and_original_kept(self):
        ref = self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3", "alex-voice")
        self.assertEqual(ref["path"], "p/refs/alex-voice.mp3")
        self.assertEqual((self.refs / "alex-voice.mp3").read_bytes(), b"voice one")
        self.assertTrue(self.clip.exists())

    def test_same_clip_again_reuses_the_named_file(self):
        self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3", "alex-voice")
        ref = self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3", "alex-voice")
        self.assertEqual(ref["path"], "p/refs/alex-voice.mp3")
        self.assertFalse((self.refs / "alex-voice-2.mp3").exists())

    def test_different_clip_does_not_overwrite(self):
        self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3", "alex-voice")
        other = self.refs / "Rob.mp3"
        other.write_bytes(b"voice two")
        ref = self.adopt("p/refs/Rob.mp3", "alex-voice")
        self.assertEqual(ref["path"], "p/refs/alex-voice-2.mp3")
        self.assertEqual((self.refs / "alex-voice.mp3").read_bytes(), b"voice one")

    def test_from_another_project_is_copied_in_under_the_name(self):
        other = self.root / "q/refs"
        other.mkdir(parents=True)
        (other / "Lauren.MP3").write_bytes(b"voice three")
        ref = self.adopt("q/refs/Lauren.MP3", "nina-voice")
        self.assertEqual(ref["path"], "p/refs/nina-voice.mp3")

    def test_unsafe_name_is_cleaned(self):
        ref = self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3", "../../x y")
        self.assertTrue(ref["path"].startswith("p/refs/"))
        self.assertNotIn("..", ref["path"])

    def test_without_a_name_nothing_changes(self):
        ref = self.adopt("p/refs/Alexander_-_Clear__Steady_and_Refined.mp3")
        self.assertEqual(ref["path"], "p/refs/Alexander_-_Clear__Steady_and_Refined.mp3")



import shutil  # noqa: E402
import subprocess  # noqa: E402

from server.store import default_board, default_shot, render_fingerprint, stale_reason  # noqa: E402


def tone(path: Path, freq: int, fmt_args=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration=1", *fmt_args, str(path)], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class OneVoicePerCharacter(unittest.TestCase):
    """Each cast member's voice lives at refs/<name>-voice.wav, and only there."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = Store(workspace=self.root, data_dir=self.root)
        self.refs = self.store.refs_dir("p")
        tone(self.refs / "Alexander_-_Clear__Steady_and_Refined.mp3", 220)
        board = default_board("p")
        board["characters"] = [{"id": "c1", "name": "Alex", "description": "A man.",
                                "image": None, "voiceText": "",
                                "voice": {"kind": "upload",
                                          "path": "p/refs/Alexander_-_Clear__Steady_and_Refined.mp3",
                                          "url": "/media/p/refs/Alexander_-_Clear__Steady_and_Refined.mp3",
                                          "label": "Alexander_-_Clear__Steady_and_Refined.mp3"}}]
        shot = default_shot(board["defaults"])
        shot.update(id="s1", prompt="Alex shouts.", characterIds=["c1"], speakerId="c1",
                    dialogue="Show me!", dialogueSource="native", status="done",
                    outputs=["/media/p/shots/01/clip.mp4"])
        board["shots"] = [shot]
        shot["renderFingerprint"] = render_fingerprint(shot, board)
        # Written straight to disk: the state an older board is in.
        (self.root / "p").mkdir(exist_ok=True)
        (self.root / "p/storyboard.json").write_text(__import__("json").dumps(board))

    def voice(self, board):
        return board["characters"][0]["voice"]

    def test_existing_clip_is_filed_under_the_name_on_load(self):
        board = self.store.load("p")
        v = self.voice(board)
        self.assertEqual(v["path"], "p/refs/alex-voice.wav")
        self.assertEqual(v["originalName"], "Alexander_-_Clear__Steady_and_Refined.mp3")
        self.assertTrue(v["url"].startswith("/media/p/refs/alex-voice.wav?v="))
        self.assertFalse((self.refs / "Alexander_-_Clear__Steady_and_Refined.mp3").exists())

    def test_rename_alone_does_not_mark_the_scene_changed(self):
        board = self.store.load("p")
        self.assertEqual(stale_reason(board["shots"][0], board), "")

    def test_new_clip_replaces_the_old_one_and_marks_the_scene_changed(self):
        board = self.store.load("p")
        first = (self.refs / "alex-voice.wav").read_bytes()
        tone(self.refs / "Rob.mp3", 880)
        board["characters"][0]["voice"] = {"kind": "upload", "path": "p/refs/Rob.mp3",
                                           "url": "/media/p/refs/Rob.mp3", "label": "Rob.mp3"}
        board = self.store.save("p", board)
        self.assertEqual(self.voice(board)["path"], "p/refs/alex-voice.wav")
        self.assertNotEqual((self.refs / "alex-voice.wav").read_bytes(), first)
        self.assertFalse((self.refs / "Rob.mp3").exists())
        self.assertEqual(sorted(p.name for p in self.refs.iterdir()), ["alex-voice.wav"])
        self.assertTrue(stale_reason(board["shots"][0], board))

    def test_any_format_ends_up_as_the_one_wav(self):
        self.store.load("p")
        tone(self.refs / "take.flac", 440)
        board = self.store.load("p")
        board["characters"][0]["voice"] = {"path": "p/refs/take.flac", "label": "take.flac"}
        board = self.store.save("p", board)
        self.assertEqual(sorted(p.name for p in self.refs.iterdir()), ["alex-voice.wav"])

    def test_old_tab_with_the_pre_rename_path_points_at_the_named_file(self):
        board = self.store.load("p")
        stale_tab = __import__("json").loads((self.root / "p/storyboard.json").read_text())
        stale_tab["characters"][0]["voice"] = {
            "path": "p/refs/Alexander_-_Clear__Steady_and_Refined.mp3",
            "label": "Alexander_-_Clear__Steady_and_Refined.mp3"}
        saved = self.store.save("p", stale_tab)
        self.assertEqual(self.voice(saved)["path"], "p/refs/alex-voice.wav")
        self.assertEqual(stale_reason(saved["shots"][0], saved), "")

    def test_clip_from_another_project_is_left_in_place(self):
        self.store.load("p")
        tone(self.root / "q/refs/Lauren.mp3", 330)
        board = self.store.load("p")
        board["characters"][0]["voice"] = {"path": "q/refs/Lauren.mp3", "label": "Lauren.mp3"}
        self.store.save("p", board)
        self.assertTrue((self.root / "q/refs/Lauren.mp3").exists())

    def test_clip_another_project_links_to_is_kept(self):
        other = default_board("q")
        other["characters"] = [{"id": "x", "name": "Jax", "description": "A pilot.",
                                "voice": {"path": "p/refs/Alexander_-_Clear__Steady_and_Refined.mp3"}}]
        (self.root / "q").mkdir(exist_ok=True)
        (self.root / "q/storyboard.json").write_text(__import__("json").dumps(other))
        self.store.load("p")
        self.assertTrue((self.refs / "alex-voice.wav").exists())
        self.assertTrue((self.refs / "Alexander_-_Clear__Steady_and_Refined.mp3").exists())

    def test_two_characters_with_one_name_never_share_a_file(self):
        board = self.store.load("p")
        tone(self.refs / "other.mp3", 660)
        board["characters"].append({"id": "c2", "name": "Alex", "description": "Another.",
                                    "voice": {"path": "p/refs/other.mp3", "label": "other.mp3"}})
        board = self.store.save("p", board)
        paths = [c["voice"]["path"] for c in board["characters"]]
        self.assertEqual(len(set(paths)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
