"""Auto-refine: draft -> review -> adjust loop. No model, no GPU.

A fake backend writes a clip per attempt and a fake reviewer scripts the
verdicts. Run from storyboard:  python3 tests/refine.py
"""
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import refine  # noqa: E402
from server.backends.base import JobSpec, RunResult, Validation  # noqa: E402
from render_all import a_board, an_orchestrator, make_clip  # noqa: E402


class DraftBackend:
    id = "draftstub"
    label = "Draft stub"

    def __init__(self):
        self.prepared = []   # (prompt, draft flag the render would see)

    def health(self):
        return True, ""

    def cancel(self):
        pass

    def prepare(self, shot, project, paths):
        self.prepared.append((shot["prompt"], bool(project["defaults"].get("draft"))))
        return JobSpec(shot_id=shot["id"], payload={"draft": project["defaults"].get("draft")},
                       expected_outputs=[paths.abs_dir / "clip.mp4"],
                       frames_dir=paths.abs_frames)

    def run(self, spec, on_event, should_cancel):
        spec.expected_outputs[0].write_bytes(b"clip")
        return RunResult(exit_code=0, started_at=0.0, ended_at=1.0)

    def validate(self, spec, result):
        return Validation(verdict="done")


class FakeReviewer:
    def __init__(self, scores, revisions=None, on_judge=None):
        self.scores = list(scores)
        self.revisions = list(revisions or [])
        self.on_judge = on_judge
        self.revise_calls = []

    def checklist(self, shot, board):
        return ["a dog sits frame left", "it is raining"]

    def judge(self, shot, board, checks, clip, seconds, workdir):
        if self.on_judge:
            self.on_judge()
        score = self.scores.pop(0)
        met = round(len(checks) * score / 100)
        results = [{"check": c, "met": i < met, "evidence": "seen"} for i, c in enumerate(checks)]
        return {"results": results, "overall": "ok", "score": score}

    def revise(self, shot, board, prompt, results, history):
        self.revise_calls.append((prompt, len(history)))
        return self.revisions.pop(0) if self.revisions else prompt


class RefineLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.orch = an_orchestrator(Path(self.temp.name))
        self.backend = self.orch.backend = DraftBackend()
        self.slug = "test"
        self.orch.store.save(self.slug, a_board(2))

    def refine(self, reviewer, **kw):
        self.orch.start_refine(self.slug, "s2", reviewer, **kw)
        self.orch._thread.join(10)
        return self.orch.status()["refine"]

    def test_stops_when_the_checks_pass_and_proposes_the_winning_prompt(self):
        r = self.refine(FakeReviewer([50, 100], ["A dog sits frame left in the rain."]),
                        max_attempts=5)
        self.assertEqual((r["stopReason"], r["best"]), ("passed", 2))
        self.assertEqual([a["score"] for a in r["attempts"]], [50, 100])
        self.assertEqual(self.backend.prepared[1][0], "A dog sits frame left in the rain.")
        self.assertEqual(r["proposal"], {"tool": "update_shot", "shotId": "s2",
                                         "fields": {"prompt": "A dog sits frame left in the rain."}})
        self.assertTrue(all(a["clipUrl"] for a in r["attempts"]))

    def test_the_users_own_checks_replace_the_models(self):
        class NoChecklist(FakeReviewer):
            def checklist(self, shot, board):
                raise AssertionError("the user's checklist should be used as given")
        r = self.refine(NoChecklist([100]), checks=["  real Elias in the foreground ", "", "camera orbits counterclockwise"])
        self.assertEqual(r["checks"], ["real Elias in the foreground", "camera orbits counterclockwise"])
        self.assertEqual([x["check"] for x in r["attempts"][0]["results"]], r["checks"])

    def test_renders_drafts_and_never_touches_the_board(self):
        before = self.orch.store.load(self.slug)
        self.refine(FakeReviewer([100]))
        self.assertEqual(self.backend.prepared, [("Something happens, take 2.", True)])
        after = self.orch.store.load(self.slug)
        self.assertEqual(after["shots"], before["shots"])
        self.assertFalse(after["defaults"].get("draft"))

    def test_draft_can_be_turned_off(self):
        self.refine(FakeReviewer([100]), draft=False)
        self.assertEqual(self.backend.prepared[0][1], False)

    def test_max_attempts_is_the_stop_value(self):
        rev = FakeReviewer([50, 50, 50, 50], ["v2", "v3", "v4"])
        r = self.refine(rev, max_attempts=3)
        self.assertEqual((r["stopReason"], len(r["attempts"])), ("max-attempts", 3))
        self.assertEqual(len(self.backend.prepared), 3)
        self.assertEqual(r["best"], 3)          # ties go to the latest attempt
        self.assertEqual([h for _, h in rev.revise_calls], [0, 1])   # earlier attempts passed on

    def test_pass_percent_lowers_the_bar(self):
        r = self.refine(FakeReviewer([50]), pass_percent=50)
        self.assertEqual(r["stopReason"], "passed")

    def test_an_unchanged_prompt_ends_the_loop(self):
        r = self.refine(FakeReviewer([50, 50]), max_attempts=4)
        self.assertEqual((r["stopReason"], len(r["attempts"])), ("no-change", 1))
        self.assertIsNone(r["proposal"])

    def test_the_best_attempt_wins_even_if_a_later_one_is_worse(self):
        r = self.refine(FakeReviewer([50, 0, 50], ["v2", "v3"]), max_attempts=3)
        self.assertEqual(r["best"], 3)
        r2 = self.refine(FakeReviewer([50, 0, 0], ["v2", "v3"]), max_attempts=3)
        self.assertEqual(r2["best"], 1)
        self.assertIsNone(r2["proposal"])        # the shot's own prompt was best

    def test_stop_ends_the_run_and_keeps_what_was_scored(self):
        rev = FakeReviewer([50, 50], ["v2"], on_judge=lambda: self.orch.stop())
        r = self.refine(rev, max_attempts=4)
        self.assertEqual(r["stopReason"], "stopped")
        self.assertEqual(len(self.backend.prepared), 1)

    def test_a_reviewer_error_is_reported_not_raised(self):
        class Broken(FakeReviewer):
            def judge(self, *a, **k):
                raise RuntimeError("model is not vision-capable")
        r = self.refine(Broken([]))
        self.assertEqual(r["stopReason"], "error")
        self.assertIn("not vision-capable", r["stopDetail"])
        self.assertEqual(r["attempts"][0]["status"], "interrupted")

    def test_the_result_is_kept_for_after_a_reload(self):
        self.refine(FakeReviewer([100]))
        saved = self.orch.refine_result(self.slug, "s2")
        self.assertEqual(saved["stopReason"], "passed")
        self.assertIsNone(self.orch.refine_result(self.slug, "s1"))

    def test_locked_and_promptless_shots_are_refused(self):
        board = self.orch.store.load(self.slug)
        board["shots"][1]["locked"] = True
        self.orch.store.save(self.slug, board)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.orch.start_refine(self.slug, "s2", FakeReviewer([]))
        board["shots"][1]["locked"] = False
        board["shots"][1]["prompt"] = " "
        self.orch.store.save(self.slug, board)
        with self.assertRaisesRegex(RuntimeError, "no prompt"):
            self.orch.start_refine(self.slug, "s2", FakeReviewer([]))

    def test_refuses_while_another_job_runs(self):
        gate = threading.Event()
        rev = FakeReviewer([100], on_judge=lambda: gate.wait(5))
        self.orch.start_refine(self.slug, "s2", rev)
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.orch.start_refine(self.slug, "s1", FakeReviewer([]))
            self.assertTrue(self.orch.touches(self.slug, "s2"))
        finally:
            gate.set()
            self.orch._thread.join(10)


class IntentTests(unittest.TestCase):
    board = a_board(4)

    def parse(self, message, selected=None):
        return refine.parse_refine_request(message, self.board, selected)

    def test_natural_phrasings_trigger(self):
        for text in (
            "Create a draft clip of scene 3, review it, then adjust the prompt",
            "render scene 2 as a draft, check the clip and fix the prompt until it matches",
            "keep trying scene 1 until the clip shows the dog on the left, render a draft each time",
            "/refine scene 4 the dog is on the left",
            "auto-refine this shot",
        ):
            self.assertIsNotNone(self.parse(text, "s1"), text)

    def test_ordinary_chat_does_not_trigger(self):
        for text in (
            "Review the board for continuity",
            "Make scene 3's prompt more cinematic",
            "How long will the render take?",
            "Render scene 3",
            "Refine the wording of the scene description",
        ):
            self.assertIsNone(self.parse(text, "s1"), text)

    def test_scene_attempts_and_draft_are_read_from_the_message(self):
        r = self.parse("draft scene 3, review the clip, adjust it, stop after 5 attempts")
        self.assertEqual((r["shotId"], r["maxAttempts"], r["draft"]), ("s3", 5, True))
        r = self.parse("make a full-quality render of scene 2, review it and tweak, max 99 tries")
        self.assertEqual((r["shotId"], r["maxAttempts"], r["draft"]), ("s2", refine.MAX_ATTEMPTS, False))

    def test_default_is_the_selected_shot_and_a_few_attempts(self):
        r = self.parse("draft it, review the clip, adjust until it matches", "s4")
        self.assertEqual((r["shotId"], r["maxAttempts"]), ("s4", refine.DEFAULT_ATTEMPTS))

    def test_no_scene_asks_which(self):
        r = self.parse("draft it, review the clip, adjust until it matches")
        self.assertIn("Which scene", refine.refine_reply(r, self.board)["message"])
        self.assertEqual(refine.refine_reply(r, self.board)["actions"], [])

    def test_the_reply_is_a_start_card_for_that_shot(self):
        reply = refine.refine_reply(self.parse("/refine scene 2 dog on the left, 4 tries max"), self.board)
        self.assertEqual(reply["actions"], [{"tool": "refine_shot", "shotId": "s2",
                                              "requirement": "dog on the left,",
                                              "maxAttempts": 4, "draft": True, "checks": []}])
        self.assertIn("write the checklist when the run starts", reply["message"])

    def test_the_reply_carries_the_checklist_for_editing_before_the_run(self):
        reply = refine.refine_reply(self.parse("/refine scene 2 dog on the left"), self.board,
                                    ["dog at frame left", "rain"])
        self.assertEqual(reply["actions"][0]["checks"], ["dog at frame left", "rain"])
        self.assertIn("edit the checks", reply["message"])

    def test_loop_instructions_are_not_part_of_the_requirement(self):
        r = self.parse("scene 3, up to 5 attempts. The dog sits frame left; draft it, review the clip and adjust.")
        self.assertNotIn("5 attempts", r["requirement"])
        self.assertTrue(r["requirement"].startswith("The dog sits frame left"))
        self.assertEqual((r["shotId"], r["maxAttempts"]), ("s3", 5))

    def test_a_locked_shot_gets_no_card(self):
        board = a_board(2)
        board["shots"][1]["locked"] = True
        r = refine.parse_refine_request("/refine scene 2 x", board, None)
        self.assertEqual(refine.refine_reply(r, board)["actions"], [])

    def test_still_count_is_about_one_a_second_within_limits(self):
        self.assertEqual([refine.still_count(x) for x in (1, 4, 7.3, 30)],
                         [refine.JUDGE_STILLS_MIN, 4, 8, refine.JUDGE_STILLS_MAX])


class FakeLLM:
    label, model = "Fake", "fake-1"

    def __init__(self, replies):
        self.replies, self.media = list(replies), []

    def complete(self, system, user, **kw):
        return self.replies.pop(0)

    def complete_with_media(self, system, user, images=None, **kw):
        self.media.append(len(images or []))
        return self.replies.pop(0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed to take stills")
class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_clip(self.root / "clip.mp4", seconds=2.0)
        self.board = a_board(1)
        self.shot = self.board["shots"][0]

    def judge(self, llm, checks=("a", "b", "c")):
        return refine.Reviewer(llm, self.root).judge(
            self.shot, self.board, list(checks), self.root / "clip.mp4", 2.0, self.root)

    def test_score_is_computed_from_the_answers(self):
        llm = FakeLLM(['{"results":[{"check":"a","met":true,"evidence":"f1"},'
                       '{"check":"b","met":false,"evidence":"f2"},'
                       '{"check":"c","met":true,"evidence":"f3"}],"overall":"close"}'])
        v = self.judge(llm)
        self.assertEqual(v["score"], 67)
        self.assertEqual(llm.media, [refine.still_count(2.0)])

    def test_a_skipped_check_counts_as_unmet_and_stays_in_the_list(self):
        v = self.judge(FakeLLM(['{"results":[{"check":"a","met":true}]}']))
        self.assertEqual([r["met"] for r in v["results"]], [True, False, False])
        self.assertEqual([r["check"] for r in v["results"]], ["a", "b", "c"])

    def test_only_true_counts_as_met(self):
        v = self.judge(FakeLLM(['{"results":[{"check":"a","met":"yes"}]}']), checks=("a",))
        self.assertEqual(v["score"], 0)

    def test_an_unreadable_review_retries_once_then_says_why(self):
        llm = FakeLLM(["I think it looks great!", "Still prose."])
        with self.assertRaisesRegex(RuntimeError, "vision-capable"):
            self.judge(llm)

    def test_checklist_is_cleaned_and_capped(self):
        many = ", ".join(f'"check {i}"' for i in range(20))
        llm = FakeLLM([f'{{"checks": [{many}, "check 1"]}}'])
        checks = refine.Reviewer(llm, self.root).checklist(self.shot, self.board)
        self.assertEqual(len(checks), refine.MAX_CHECKS)

    def test_the_request_is_put_first_and_is_the_source_of_truth(self):
        llm = FakeLLM(['{"checks": ["a"]}'])
        calls = []
        orig = llm.complete
        llm.complete = lambda system, user, **kw: (calls.append((system, user)), orig(system, user, **kw))[1]
        refine.Reviewer(llm, self.root, "Real Elias in the foreground.").checklist(self.shot, self.board)
        system, user = calls[0]
        self.assertIn("source of truth", system)
        self.assertIn("one check per concrete statement", system)
        self.assertLess(user.index("USER REQUEST"), user.index("SHOT PROMPT"))

    def test_draft_checklist_never_fails_the_chat_turn(self):
        class Down(FakeLLM):
            def complete(self, *a, **k):
                raise RuntimeError("ollama is down")
        self.assertEqual(refine.draft_checklist(Down([]), self.root, self.shot, self.board, "x"), [])

    def test_draft_checklist_lets_a_departing_browser_stop_the_work(self):
        from server.llm import ClientGone

        class Gone(FakeLLM):
            def complete(self, *a, **k):
                raise ClientGone()
        with self.assertRaises(ClientGone):
            refine.draft_checklist(Gone([]), self.root, self.shot, self.board, "x")

    def test_revise_returns_the_new_prompt(self):
        llm = FakeLLM(['{"prompt": " Better prompt. "}'])
        out = refine.Reviewer(llm, self.root).revise(
            self.shot, self.board, "old",
            [{"check": "a", "met": False, "evidence": "no"}], [])
        self.assertEqual(out, "Better prompt.")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed")
class StillsTests(unittest.TestCase):
    """A real render's audio runs a hair past its last video frame (7.30s vs
    7.29s), so a still asked for at the container's duration found no frame and
    the judge was silently shown a clip with its ending missing."""

    def setUp(self):
        import subprocess
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clip = self.root / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-t", "2.0", "-i", "color=c=red:s=320x176:r=24",
             "-f", "lavfi", "-t", "2.1", "-i", "sine=frequency=440:sample_rate=48000",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.clip)],
            check=True)

    def test_every_requested_still_is_written_including_the_last(self):
        from server.storyboard_chat import clip_stills
        seconds = refine.clip_seconds(self.clip, 99.0)
        stills = clip_stills(self.clip, seconds, self.root, "s", count=4)
        self.assertEqual(len(stills), 4)
        self.assertGreater(stills[-1][1], 1.8)             # the ending, not 2/3 of the way

    def test_seconds_is_the_video_length_not_the_audios(self):
        self.assertAlmostEqual(refine.clip_seconds(self.clip, 99.0), 2.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
