"""Length check: dialogue timing, beat parsing and snapping; no model needed.

Run:  python3 tests/length_estimate.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.backends.vpipe_backend import H3_FRAME_RULE  # noqa: E402
from server.length_estimate import (  # noqa: E402
    action_beats, dialogue_seconds, estimate, verdict,
)


class FakeLLM:
    label = "Fake"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, *, timeout=120.0, max_tokens=None):
        return self.reply


SCENE_5 = json.dumps({"beats": [
    {"action": "Doc climbs in under the gull-wing door and settles", "min": 2.0, "comfortable": 3.0},
    {"action": "glances at Marty with a manic grin", "min": 1.5, "comfortable": 2.0},
    {"action": "turns forward", "min": 0.8, "comfortable": 1.2},
    {"action": "grips the wheel", "min": 0.8, "comfortable": 1.0},
    {"action": "pulls the door down", "min": 1.5, "comfortable": 2.0},
], "note": ""})


class DialogueTests(unittest.TestCase):
    def test_no_line_is_zero(self):
        self.assertEqual(dialogue_seconds({"dialogue": "  "}, None), (0.0, ""))

    def test_words_at_speaking_rate_plus_pauses(self):
        secs, basis = dialogue_seconds({"dialogue": "Great Scott... we did it!"}, None)
        self.assertAlmostEqual(secs, 5 / 2.5 + 0.4, places=2)
        self.assertIn("5 words", basis)

    def test_delivery_note_changes_the_rate(self):
        line = {"dialogue": "one two three four five"}
        normal = dialogue_seconds(line, None)[0]
        slow = dialogue_seconds({**line, "dialogueStyle": "slow, hesitant"}, None)[0]
        fast = dialogue_seconds({**line, "dialogueStyle": "rapid and excited"}, None)[0]
        self.assertLess(fast, normal)
        self.assertGreater(slow, normal)

    def test_a_matching_recorded_take_is_measured(self):
        with tempfile.TemporaryDirectory() as d:
            with wave.open(str(Path(d) / "dialogue.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\0\0" * 16000 * 3)   # 3.0 s
            shot = {"dialogue": "Hi.", "dialogueSpokenText": "Hi."}
            self.assertEqual(dialogue_seconds(shot, Path(d)), (3.0, "recorded take"))
            # a take of a different line is not this line's length
            shot["dialogueSpokenText"] = "Hello there."
            self.assertNotEqual(dialogue_seconds(shot, Path(d))[1], "recorded take")


class BeatTests(unittest.TestCase):
    def test_reply_in_fences_after_thinking_still_parses(self):
        reply = "<think>hmm {not json}</think>\n```json\n" + SCENE_5 + "\n```"
        beats, _ = action_beats(FakeLLM(reply), "prompt")
        self.assertEqual(len(beats), 5)

    def test_comfortable_is_never_below_min_and_junk_is_dropped(self):
        reply = json.dumps({"beats": [
            {"action": "a", "min": 2, "comfortable": 1},
            {"action": "", "min": 1, "comfortable": 1},
            {"action": "c", "min": "x"},
        ]})
        beats, _ = action_beats(FakeLLM(reply), "p")
        self.assertEqual(beats, [{"action": "a", "min": 2.0, "comfortable": 2.0}])

    def test_no_beats_is_an_error(self):
        with self.assertRaises(ValueError):
            action_beats(FakeLLM("I can't time that."), "p")


class EstimateTests(unittest.TestCase):
    def test_scene_5_is_too_short_at_124_frames(self):
        e = estimate(FakeLLM(SCENE_5), {"prompt": "Doc gets in.", "frames": 124},
                     frame_rule=H3_FRAME_RULE)
        self.assertEqual((e["minSeconds"], e["comfortableSeconds"]), (6.6, 9.2))
        # snapped up to 17n+5
        for f in (e["minFrames"], e["comfortableFrames"]):
            self.assertEqual(f % 17, 5)
            self.assertGreaterEqual(f / 24, 6.6)
        self.assertEqual(verdict(e, 124), "short")
        self.assertEqual(verdict(e, e["minFrames"]), "tight")
        self.assertEqual(verdict(e, e["comfortableFrames"]), "ok")
        self.assertEqual(e["basis"]["prompt"], "Doc gets in.")

    def test_long_dialogue_sets_the_minimum(self):
        beats = json.dumps({"beats": [{"action": "stands", "min": 1, "comfortable": 1.5}]})
        line = " ".join(["word"] * 25)                       # 10 s at 2.5 words/s
        e = estimate(FakeLLM(beats), {"prompt": "p", "dialogue": line}, frame_rule=H3_FRAME_RULE)
        self.assertEqual(e["limitedBy"], "dialogue")
        self.assertGreaterEqual(e["minSeconds"], 11.0)       # plus a beat each side

    def test_over_fifteen_seconds_suggests_a_split(self):
        beats = json.dumps({"beats": [{"action": "a long journey", "min": 20, "comfortable": 25}]})
        e = estimate(FakeLLM(beats), {"prompt": "p"}, frame_rule=H3_FRAME_RULE)
        self.assertTrue(e["tooLongForOneShot"])
        self.assertLessEqual(e["comfortableFrames"], 362)

    def test_empty_prompt_is_refused(self):
        with self.assertRaises(ValueError):
            estimate(FakeLLM(SCENE_5), {"prompt": ""})


if __name__ == "__main__":
    unittest.main()
