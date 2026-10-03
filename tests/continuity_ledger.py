"""Continuity ledger checks; a scripted model stands in for the real one."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import continuity_ledger as cl  # noqa: E402
from server.store import default_board, default_shot  # noqa: E402


def state(side=None, pose=None, holding=None, wearing=None, facing=None):
    return {"side": side, "pose": pose, "facing": facing,
            "holding": holding or {}, "wearing": wearing or {}}


def entity(name, start, end=None, kind="character", look=None):
    return {"name": name, "kind": kind, "look": look, "start": start, "end": end or start}


def facts(*entities, location="lab", continues=None, crosses=False):
    return cl._clean_facts({"location": location, "continues": continues,
                            "cameraCrossesLine": crosses, "entities": list(entities)})


class ScriptedLLM:
    """Answers extraction from a shot-number script and cut checks from
    another; counts calls and how many ran at once."""

    label, model = "Scripted", "test"

    def __init__(self, extracts=None, cuts=None, delay=0.0):
        self.extracts, self.cuts, self.delay = extracts or {}, cuts or {}, delay
        self.calls = []
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def complete(self, system, user, *, timeout=300.0, max_tokens=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
            self.calls.append(user)
        if system is cl.EXTRACT_SYSTEM:
            n = int(user.rsplit("\nSHOT ", 1)[1].split(":", 1)[0])
            return json.dumps(self.extracts.get(n, {"entities": []}))
        n = int(user.split("SHOT B (shot ", 1)[1].split(")", 1)[0])
        return json.dumps(self.cuts.get(n, {"issues": []}))


class CarryTests(unittest.TestCase):
    def test_a_side_flip_without_crossing_the_line_is_flagged(self):
        carried = cl.carry([
            facts(entity("Alex", state("left"))),
            facts(entity("Alex", state("right")), continues=True),
        ])
        self.assertEqual(len(carried["issues"]), 1)
        self.assertIn("Alex is frame left at the cut (shot 1)", carried["issues"][0]["problem"])

    def test_a_flip_after_the_camera_crosses_the_line_is_not(self):
        carried = cl.carry([
            facts(entity("Alex", state("left"))),
            facts(entity("Alex", state("right")), continues=True, crosses=True),
        ])
        self.assertEqual(carried["issues"], [])

    def test_moving_across_frame_within_a_shot_carries_its_end_side(self):
        carried = cl.carry([
            facts(entity("Maya", state("right"), state("left"))),
            facts(entity("Maya", state("left")), continues=True),
        ])
        self.assertEqual(carried["issues"], [])

    def test_a_new_scene_starts_positions_over(self):
        carried = cl.carry([
            facts(entity("Alex", state("left"))),
            facts(entity("Alex", state("right")), location="boardroom", continues=False),
        ])
        self.assertEqual(carried["issues"], [])
        self.assertFalse(carried["shots"][1]["sameScene"])

    def test_unstated_values_carry_across_shots_that_omit_them(self):
        carried = cl.carry([
            facts(entity("Alex", state("left", "seated"))),
            facts(entity("Nina", state("right")), continues=True),
            facts(entity("Alex", state("right", "standing")), continues=True),
        ])
        problems = [i["problem"] for i in carried["issues"]]
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("(shot 1)" in p and "shot 3" in p for p in problems))

    def test_unclear_continuation_falls_back_to_same_location(self):
        carried = cl.carry([
            facts(entity("Alex", state("left"))),
            facts(entity("Alex", state("right"))),
        ])
        self.assertTrue(carried["shots"][1]["sameScene"])
        self.assertEqual(len(carried["issues"]), 1)

    def test_props_and_set_pieces_get_no_frame_side_rule(self):
        carried = cl.carry([
            facts(entity("headset", state("right"), kind="prop")),
            facts(entity("headset", state("left"), kind="prop"), continues=True),
        ])
        self.assertEqual(carried["issues"], [])

    def test_props_changing_hands_and_worn_items_reappearing(self):
        carried = cl.carry([
            facts(entity("Alex", state(holding={"tweezers": "right"}),
                         state(holding={"tweezers": "right"}, wearing={"headset": False}))),
            facts(entity("Alex", state(holding={"the tweezers": "left"},
                                        wearing={"his headset": True})), continues=True),
        ])
        problems = " | ".join(i["problem"] for i in carried["issues"])
        self.assertIn("tweezers in the right hand", problems)
        self.assertIn("has the headset off at the cut", problems)

    def test_things_that_reappear_after_a_gap_bring_their_earlier_look(self):
        table = lambda look: entity("steel table", state(), kind="set", look=look)
        carried = cl.carry([
            facts(table("dented steel table"), location="interrogation room"),
            facts(entity("Nina", state("left")), location="corridor", continues=False),
            facts(table("polished wooden table"), location="interrogation room", continues=False),
        ])
        earlier = carried["shots"][2]["earlier"]
        self.assertEqual(earlier["steel table"], {"look": "dented steel table", "shot": 1})
        self.assertEqual(earlier["location: interrogation room"]["shot"], 1)

    def test_an_unread_shot_breaks_the_chain_rather_than_guessing(self):
        carried = cl.carry([facts(entity("Alex", state("left"))), None,
                            facts(entity("Alex", state("right")), continues=True)])
        self.assertTrue(carried["shots"][1]["unread"])
        self.assertEqual(carried["issues"], [])

    def test_state_before_a_shot(self):
        f = [facts(entity("Alex", state("left", "seated"))),
             facts(entity("Alex", state(), state(pose="standing")), continues=True)]
        before = cl.state_before(f, 2)
        self.assertEqual(before["stateAtStart"]["Alex".lower()]["pose"][0], "seated")

    def test_extraction_is_allow_listed(self):
        f = cl._clean_facts({"location": "The Lab", "continues": "yes", "entities": [
            {"name": "His Headset", "kind": "gizmo", "start": {"side": "Center",
             "pose": "flying", "holding": {"cup": "left"}, "wearing": {"hat": "maybe"}}},
            {"kind": "prop"}, "junk"]})
        self.assertEqual(f["location"], "lab")
        self.assertIsNone(f["continues"])
        self.assertEqual(len(f["entities"]), 1)
        e = f["entities"][0]
        self.assertEqual((e["name"], e["kind"]), ("headset", "prop"))
        self.assertEqual(e["start"]["side"], "centre")
        self.assertIsNone(e["start"]["pose"])
        self.assertEqual(e["start"]["wearing"], {})
        self.assertEqual(e["end"], e["start"])


class BoardPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.board = default_board("Sample")
        self.board["shots"] = [default_shot(self.board["defaults"]) for _ in range(3)]
        prompts = ["Alex stands at frame left of the bench.",
                   "Alex stands at frame right, frowning.",
                   "Alex leaves."]
        for shot, prompt in zip(self.board["shots"], prompts):
            shot["prompt"] = prompt
        el = lambda side: {"name": "Alex", "kind": "character",
                           "start": {"side": side, "pose": "standing"}}
        self.extracts = {1: {"location": "lab", "entities": [el("left")]},
                         2: {"location": "lab", "continues": True, "entities": [el("right")]},
                         3: {"location": "lab", "continues": True, "entities": []}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rule_issue_gets_a_checked_fix_and_reads_are_cached(self):
        llm = ScriptedLLM(self.extracts, {2: {"rules": [
            {"rule": 1, "real": True, "reason": "Flip.", "shot": "B",
             "find": "at frame right", "replace": "at frame left"}]}})
        report = cl.check_board(self.board, self.dir, [llm])
        self.assertEqual(report["readFresh"], 3)
        [issue] = report["issues"]
        self.assertEqual(issue["source"], "rule")
        self.assertEqual(issue["fix"], {
            "tool": "replace_text", "find": "at frame right", "replace": "at frame left",
            "scope": ["shots"], "shotIds": [self.board["shots"][1]["id"]], "shotNumber": 2})
        # Second pass: nothing re-read; edit one shot and only it (and the
        # shot after, whose input includes it as PREVIOUS SHOT) is re-read.
        self.assertEqual(cl.check_board(self.board, self.dir, [llm])["readFresh"], 0)
        self.board["shots"][1]["prompt"] = "Alex stands at frame left, frowning."
        self.assertEqual(cl.check_board(self.board, self.dir, [llm])["readFresh"], 2)

    def test_a_fix_whose_find_is_not_in_the_prompt_is_dropped(self):
        llm = ScriptedLLM(self.extracts, {2: {"rules": [
            {"rule": 1, "real": True, "shot": "B", "find": "invented words", "replace": "x"}]}})
        [issue] = cl.check_board(self.board, self.dir, [llm])["issues"]
        self.assertNotIn("fix", issue)

    def test_a_dismissed_rule_issue_stays_visible_as_dismissed(self):
        llm = ScriptedLLM(self.extracts, {2: {"rules": [
            {"rule": 1, "real": False, "reason": "He walks round the bench between shots."}]}})
        [issue] = cl.check_board(self.board, self.dir, [llm])["issues"]
        self.assertEqual(issue["dismissed"], "He walks round the bench between shots.")

    def test_only_an_explicit_false_dismisses_a_rule_issue(self):
        llm = ScriptedLLM(self.extracts, {2: {"rules": [
            {"rule": 1, "reason": "This is a real break."}]}})
        [issue] = cl.check_board(self.board, self.dir, [llm])["issues"]
        self.assertNotIn("dismissed", issue)
        self.assertEqual(issue["reason"], "This is a real break.")

    def test_cut_checks_see_the_scene_and_cast_defaults(self):
        self.board["sceneDescription"] = "Alex's headset is a thin black band."
        llm = ScriptedLLM(self.extracts)
        cl.check_board(self.board, self.dir, [llm])
        cut_prompts = [c for c in llm.calls if "SHOT B (shot" in c]
        self.assertTrue(all("thin black band" in c for c in cut_prompts))

    def test_the_review_adds_its_own_findings(self):
        llm = ScriptedLLM(self.extracts, {3: {"issues": [
            {"shot": "B", "quote": "leaves", "problem": "No exit named.",
             "find": "Alex leaves.", "replace": "Alex walks out through the door."}]}})
        issues = cl.check_board(self.board, self.dir, [llm])["issues"]
        review = [i for i in issues if i["source"] == "review"]
        self.assertEqual(review[0]["cut"], [2, 3])
        self.assertEqual(review[0]["fix"]["find"], "Alex leaves.")

    def test_a_failed_read_is_reported_and_not_cached(self):
        class Flaky(ScriptedLLM):
            def complete(self, system, user, **kw):
                if system is cl.EXTRACT_SYSTEM and "\nSHOT 2:" in user:
                    return "not json"
                return super().complete(system, user, **kw)
        report = cl.check_board(self.board, self.dir, [Flaky(self.extracts)])
        self.assertEqual(report["unread"], [2])
        self.assertNotIn(self.board["shots"][1]["id"], cl.load_ledger(self.dir)["shots"])

    def test_calls_run_in_parallel_across_services(self):
        a, b = ScriptedLLM(self.extracts, delay=0.05), ScriptedLLM(self.extracts, delay=0.05)
        cl.check_board(self.board, self.dir, [a, b], per_service=2)
        self.assertTrue(a.calls and b.calls)
        self.assertGreater(a.peak + b.peak, 2)


if __name__ == "__main__":
    unittest.main()
