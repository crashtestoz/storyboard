"""Assistant context/proposal checks; no model or network required."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.storyboard_chat import (  # noqa: E402
    CHAT_SYSTEM_PROMPT, SEARCH_CAPABILITY_PROMPT, chat, compact_board_context,
    validate_actions,
)
from server.backends.vpipe_backend import estimate_render_seconds  # noqa: E402
from server.render_timings import RenderTimings  # noqa: E402
from server.store import (  # noqa: E402
    default_board, default_character, default_shot, render_fingerprint,
)


class FakeLLM:
    label = "Test model"
    model = "test-1"

    def __init__(self, reply):
        # A single reply, or a list to hand back one per call in order — the
        # latter is what a search round trip needs (first-pass query, then
        # the follow-up answer).
        self.replies = [reply] if isinstance(reply, str) else list(reply)
        self.calls: list[tuple[str, str, float]] = []
        self.seen = None

    def health(self):
        return True, ""

    def complete(self, system, user, *, timeout=120.0, max_tokens=None):
        self.seen = (system, user, timeout)
        self.calls.append(self.seen)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class StoryboardChatTests(unittest.TestCase):
    def setUp(self):
        self.board = default_board("Crossing")
        character = default_character("Mara", "Short silver hair, red coat.")
        shot = default_shot(self.board["defaults"])
        shot.update({"title": "At the gate", "prompt": "A wide shot.",
                     "characterIds": [character["id"]],
                     "outputs": ["large-output-that-must-not-enter-context"],
                     "logUrl": "/media/private/run.log"})
        self.board["characters"] = [character]
        self.board["shots"] = [shot]

    def test_context_omits_runtime_data(self):
        context = compact_board_context(self.board, self.board["shots"][0]["id"])
        encoded = json.dumps(context)
        self.assertIn("At the gate", encoded)
        self.assertIn("Mara", encoded)
        self.assertNotIn("large-output", encoded)
        self.assertNotIn("logUrl", encoded)

    def test_actions_are_allow_listed_and_ids_must_exist(self):
        sid = self.board["shots"][0]["id"]
        actions = validate_actions([
            {"tool": "update_shot", "shotId": sid,
             "fields": {"prompt": "Better", "outputs": ["bad"]}},
            {"tool": "update_shot", "shotId": "invented", "fields": {"prompt": "No"}},
            {"tool": "delete_board"},
            {"tool": "set_board_fields", "fields": {"name": "No", "soundscape": "Wind"}},
        ], self.board)
        self.assertEqual(actions, [
            {"tool": "update_shot", "shotId": sid, "fields": {"prompt": "Better"}},
            {"tool": "set_board_fields", "fields": {"soundscape": "Wind"}},
        ])

    def test_chat_sends_compact_context_and_parses_fenced_json(self):
        sid = self.board["shots"][0]["id"]
        model = FakeLLM("```json\n" + json.dumps({
            "message": "Ready to apply.",
            "actions": [{"tool": "update_shot", "shotId": sid,
                         "fields": {"title": "The threshold"}}],
        }) + "\n```")
        result = chat(model, self.board, "Make it stronger", selected_id=sid)
        self.assertEqual(result["message"], "Ready to apply.")
        self.assertEqual(result["actions"][0]["fields"]["title"], "The threshold")
        self.assertNotIn("large-output", model.seen[1])
        self.assertEqual(model.seen[2], 300.0)  # raised in f5be0ce

    def test_plain_text_reply_remains_a_useful_chat_answer(self):
        result = chat(FakeLLM("The pacing works, but shot two needs an eyeline."),
                      self.board, "Review this")
        self.assertEqual(result["actions"], [])
        self.assertIn("pacing works", result["message"])

    def test_duplicated_json_reply_still_parses_cleanly(self):
        # A small local model occasionally repeats its whole JSON reply two
        # or three times back to back instead of stopping. That must not
        # surface the raw, repeated JSON text to the user (see the earlier
        # bug: json.loads on the concatenation raised, and the fallback
        # dumped the whole literal string, escaped quotes and all).
        one = json.dumps({"message": "A LoRA is a small fine-tuned adapter.", "actions": []})
        result = chat(FakeLLM(one + one + one), self.board, "What's a LoRA?")
        self.assertEqual(result["message"], "A LoRA is a small fine-tuned adapter.")
        self.assertNotIn("{", result["message"])

    def test_context_flags_render_need_and_estimates_only_when_needed(self):
        # No render timed on this machine yet: no estimate, never a guess.
        context = compact_board_context(self.board)
        self.assertIsNone(context["shots"][0]["estimatedRenderSeconds"])

        timings = RenderTimings(Path(tempfile.mkdtemp()) / "t.json")
        timings.record(model="ref2va", width=960, height=544, frames=124,
                       steps=8, seconds=300)
        context = compact_board_context(self.board, timings=timings)
        shot_ctx = context["shots"][0]
        # setUp's shot has an output but no matching renderFingerprint, so
        # store.stale_reason calls it stale — it still needs a render.
        self.assertTrue(shot_ctx["needsRender"])
        self.assertIsInstance(shot_ctx["estimatedRenderSeconds"], int)
        self.assertEqual(shot_ctx["dialogueSource"], "recording")

        current = self.board["shots"][0]
        current["renderFingerprint"] = render_fingerprint(current, self.board)
        context = compact_board_context(self.board, timings=timings)
        shot_ctx = context["shots"][0]
        self.assertFalse(shot_ctx["needsRender"])
        self.assertIsNone(shot_ctx["estimatedRenderSeconds"])

    def test_estimate_comes_only_from_measured_renders(self):
        shot = {"id": "s1", "frames": 124, "steps": 8}
        board = {"defaults": {"resolution": "960x544"}, "shots": [shot]}
        timings = RenderTimings(Path(tempfile.mkdtemp()) / "t.json")
        self.assertIsNone(estimate_render_seconds(shot, board, timings))
        timings.record(model="ref2va", width=960, height=544, frames=124, steps=8, seconds=274)
        self.assertEqual(estimate_render_seconds(shot, board, timings), 274)
        # a different length is scaled from that measurement, not a formula
        shorter = {"id": "s2", "frames": 39, "steps": 8}
        self.assertAlmostEqual(estimate_render_seconds(shorter, board, timings),
                               274 * 39 / 124, places=3)
        # another model has no history of its own
        timings.record(model="fl2va", width=960, height=544, frames=124, steps=8, seconds=100)
        self.assertEqual(estimate_render_seconds(shot, board, timings), 274)

    def test_operation_actions_are_allow_listed(self):
        sid = self.board["shots"][0]["id"]
        actions = validate_actions([
            {"tool": "start_render", "shotIds": [sid, "invented"]},
            {"tool": "dub_shot", "shotId": sid},
            {"tool": "dub_shot", "shotId": "invented"},
            {"tool": "stop_render"},
            {"tool": "assemble"},
            {"tool": "start_render"},
        ], self.board)
        self.assertEqual(actions, [
            {"tool": "start_render", "shotIds": [sid]},
            {"tool": "dub_shot", "shotId": sid},
            {"tool": "stop_render"},
            {"tool": "assemble"},
            {"tool": "start_render"},
        ])

    def test_prompt_offers_operations_instead_of_pointing_at_mcp(self):
        for tool in ("start_render", "dub_shot", "stop_render", "assemble"):
            self.assertIn(tool, CHAT_SYSTEM_PROMPT)
        self.assertNotIn("you cannot render", CHAT_SYSTEM_PROMPT.lower())
        self.assertIn("never mention mcp", CHAT_SYSTEM_PROMPT.lower())

    def test_rewrite_proposal_cannot_start_render_in_same_apply(self):
        sid = self.board["shots"][0]["id"]
        model = FakeLLM(json.dumps({
            "message": "I rewrote the shot and will render it.",
            "actions": [
                {"tool": "update_shot", "shotId": sid,
                 "fields": {"prompt": "A stronger shot."}},
                {"tool": "start_render", "shotIds": [sid]},
            ],
        }))
        result = chat(model, self.board, "Rewrite this shot")
        self.assertEqual(result["actions"], [
            {"tool": "update_shot", "shotId": sid,
             "fields": {"prompt": "A stronger shot."}},
        ])
        self.assertIn("ask to render separately", result["message"])

    def test_separate_render_request_can_still_start_render(self):
        sid = self.board["shots"][0]["id"]
        model = FakeLLM(json.dumps({
            "message": "Ready to render.",
            "actions": [{"tool": "start_render", "shotIds": [sid]}],
        }))
        result = chat(model, self.board, "Render shot one now")
        self.assertEqual(result["actions"], [
            {"tool": "start_render", "shotIds": [sid]},
        ])

    def test_search_capability_is_not_offered_when_no_url_is_configured(self):
        model = FakeLLM("A plain answer, no search needed.")
        chat(model, self.board, "What's a dolly zoom?")
        self.assertNotIn(SEARCH_CAPABILITY_PROMPT, model.seen[0])

    def test_search_field_is_ignored_when_no_url_is_configured(self):
        # A model that requests search anyway (e.g. copying the shape from a
        # previous session) must not trigger a search or a second call when
        # this deployment has none configured.
        model = FakeLLM(json.dumps(
            {"message": "Answering anyway.", "actions": [], "search": "dolly zoom"}
        ))
        result = chat(model, self.board, "What's a dolly zoom?")
        self.assertEqual(result["message"], "Answering anyway.")
        self.assertEqual(len(model.calls), 1)

    def test_search_round_trip_when_url_is_configured(self):
        model = FakeLLM([
            json.dumps({"message": "Let me check.", "actions": [],
                       "search": "average shot length feature film"}),
            json.dumps({"message": "About 2.5 seconds per shot on average.",
                       "actions": []}),
        ])
        with patch("server.storyboard_chat.web_search") as fake_search:
            fake_search.return_value = [
                {"title": "ASL explainer", "url": "https://example.com/asl",
                 "snippet": "Average shot length varies by genre."},
            ]
            result = chat(model, self.board, "How long is a typical film shot?",
                          search_url="http://10.0.0.200:31808")
        fake_search.assert_called_once_with(
            "http://10.0.0.200:31808", "average shot length feature film"
        )
        self.assertEqual(len(model.calls), 2)
        self.assertIn(SEARCH_CAPABILITY_PROMPT, model.calls[0][0])
        self.assertIn("SEARCH RESULTS", model.calls[1][1])
        self.assertIn("ASL explainer", model.calls[1][1])
        self.assertEqual(result["message"], "About 2.5 seconds per shot on average.")

    def test_search_is_not_retried_after_the_follow_up(self):
        # Even if the second-pass reply asks to search again, chat() only
        # ever makes one round trip per turn.
        model = FakeLLM([
            json.dumps({"message": "Checking.", "actions": [], "search": "first query"}),
            json.dumps({"message": "Still not sure.", "actions": [], "search": "second query"}),
        ])
        with patch("server.storyboard_chat.web_search", return_value=[]) as fake_search:
            result = chat(model, self.board, "Research this",
                          search_url="http://10.0.0.200:31808")
        fake_search.assert_called_once()
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result["message"], "Still not sure.")


if __name__ == "__main__":
    unittest.main()
